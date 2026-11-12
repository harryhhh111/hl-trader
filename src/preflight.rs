//! Read-only order policy. No signer, state files or writes are used here.
use hypersdk::Address;
use hypersdk::hypercore::PerpMarket;
use rust_decimal::Decimal;
use serde::Serialize;

use crate::adjust::{AdjustError, AdjustRequest, AdjustmentLeg, now_utc};
use crate::dashboard::config::DashboardPair;
use crate::hyperliquid::{Adapter, find_market};

pub const MAX_LEVERAGE: u32 = 3;

#[derive(Debug, Serialize)]
pub struct LegSettings {
    pub coin: String,
    pub margin_mode: String,
    pub leverage: u32,
    pub market_max_leverage: u64,
    pub cross_available: bool,
    pub ready_for_increase: bool,
    pub diagnostics: Vec<String>,
}

#[derive(Serialize)]
pub struct PreflightReport {
    pub schema_version: u8,
    pub pair_id: String,
    pub network: String,
    pub config_revision: String,
    pub account_address: String,
    pub queried_at: String,
    pub policy_max_leverage: u32,
    pub ready_for_increase: bool,
    pub legs: Vec<LegSettings>,
}

pub fn cross_available(market: &PerpMarket) -> bool {
    !market.isolated_margin && market.margin_mode.is_none()
}

pub async fn read_leg(
    adapter: &Adapter,
    account: Address,
    market: &PerpMarket,
) -> Result<LegSettings, AdjustError> {
    let value = adapter.leverage_for(account, &market.name).await?;
    if !["cross", "isolated"].contains(&value.leverage_type.as_str())
        || value.value != value.value.trunc()
        || value.value < Decimal::ONE
    {
        return Err(AdjustError::Refused(format!(
            "{} 的保证金或杠杆数据非法，禁止增仓",
            market.name
        )));
    }
    let leverage: u32 = value
        .value
        .normalize()
        .to_string()
        .parse()
        .map_err(|_| AdjustError::Refused("当前杠杆超出支持范围".into()))?;
    let cross_available = cross_available(market);
    let mut diagnostics = Vec::new();
    let ready_for_increase = !market.delisted
        && leverage <= MAX_LEVERAGE
        && u64::from(leverage) <= market.max_leverage
        && value.leverage_type == if cross_available { "cross" } else { "isolated" };
    if leverage > MAX_LEVERAGE || u64::from(leverage) > market.max_leverage {
        diagnostics.push(format!(
            "{} 当前 {}x，请手动调整至不超过 {}x",
            market.name,
            leverage,
            u64::from(MAX_LEVERAGE).min(market.max_leverage)
        ));
    }
    if cross_available && value.leverage_type != "cross" {
        diagnostics.push(format!(
            "{} 支持 cross，请手动切换为全仓后再增仓",
            market.name
        ));
    }
    if !cross_available {
        diagnostics.push(format!(
            "{} 仅支持逐仓（isolated），作为 cross 规则的例外；杠杆仍不得超过 {}x",
            market.name, MAX_LEVERAGE
        ));
        if value.leverage_type != "isolated" {
            diagnostics.push("当前模式与市场限制不一致，请手动调整后重新检查".into());
        }
    }
    if market.delisted {
        diagnostics.push("市场已退市，禁止增仓".into());
    }
    Ok(LegSettings {
        coin: market.name.clone(),
        margin_mode: value.leverage_type,
        leverage,
        market_max_leverage: market.max_leverage,
        cross_available,
        ready_for_increase,
        diagnostics,
    })
}

pub async fn collect(
    adapter: &Adapter,
    pair: &DashboardPair,
    account: Address,
    config_revision: &str,
) -> Result<PreflightReport, AdjustError> {
    let descriptor = &pair.descriptor;
    let dex = (!descriptor.dex.is_empty()).then_some(descriptor.dex.as_str());
    let markets = adapter.perp_markets_for(dex).await?;
    let mut legs = Vec::with_capacity(2);
    for coin in &descriptor.coins {
        legs.push(read_leg(adapter, account, find_market(&markets, coin)?).await?);
    }
    Ok(PreflightReport {
        schema_version: 1,
        pair_id: descriptor.id.clone(),
        network: descriptor.network.label().into(),
        config_revision: config_revision.into(),
        account_address: format!("{account}").to_lowercase(),
        queried_at: now_utc(),
        policy_max_leverage: MAX_LEVERAGE,
        ready_for_increase: legs.iter().all(|leg| leg.ready_for_increase),
        legs,
    })
}

pub async fn guard_increase(
    adapter: &Adapter,
    request: &AdjustRequest<'_>,
    legs: &[AdjustmentLeg],
) -> Result<(), AdjustError> {
    if !legs
        .iter()
        .any(|leg| !leg.reduce_only && leg.order_qty > Decimal::ZERO)
    {
        return Ok(());
    }
    let report = collect(
        adapter,
        request.pair,
        request.account,
        request.config_revision,
    )
    .await?;
    if !report.ready_for_increase {
        let errors: Vec<_> = report
            .legs
            .iter()
            .filter(|leg| !leg.ready_for_increase)
            .flat_map(|leg| leg.diagnostics.iter().cloned())
            .collect();
        return Err(AdjustError::Refused(format!(
            "下单前检查未通过：{}；只减仓和平仓仍可操作",
            errors.join("；")
        )));
    }
    Ok(())
}
