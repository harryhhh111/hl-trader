use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use alloy::dyn_abi::TypedData;
use hypersdk::Address;
use hypersdk::hypercore::api::{Action, ActionRequest};
use hypersdk::hypercore::{Chain, Cloid, PerpMarket, PrivateKeySigner, Signature};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::dashboard::config::{self, DashboardPair};
use crate::hyperliquid::{
    AccountView, Adapter, AdapterError, BboView, PlaceOutcome, cloid_hex, find_market, parse_cloid,
};
use crate::pair::{self, LegResult, LegSend, send_leg};
use crate::write_lock::WriteLock;

#[derive(Debug, thiserror::Error)]
pub enum AdjustError {
    #[error(transparent)]
    Adapter(#[from] AdapterError),
    #[error("操作被拒绝: {0}")]
    Refused(String),
    #[error("调仓结果待核对: {0}")]
    Unresolved(String),
    #[error("本地调仓状态错误: {0}")]
    State(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdjustmentStatus {
    Preview,
    Prepared,
    Executing,
    Succeeded,
    Incomplete,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdjustmentLegStatus {
    NotRequired,
    Prepared,
    Pending,
    Filled,
    PartiallyFilled,
    Rejected,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SigningRequest {
    pub leg_index: usize,
    pub nonce: u64,
    pub typed_data: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdjustmentLeg {
    pub coin: String,
    #[serde(default)]
    pub asset: usize,
    #[serde(default)]
    pub nonce: Option<u64>,
    pub position_side: String,
    pub current_size: Decimal,
    pub current_notional: Decimal,
    pub mid_px: Decimal,
    pub target_size: Decimal,
    pub estimated_target_notional: Decimal,
    pub action: String,
    pub order_qty: Decimal,
    pub protect_px: Decimal,
    pub reduce_only: bool,
    pub cloid: Option<String>,
    pub status: AdjustmentLegStatus,
    pub confirmed_qty: Decimal,
    pub avg_px: Option<Decimal>,
    pub oid: Option<u64>,
    pub result: Option<String>,
    pub final_size: Option<Decimal>,
    pub final_notional: Option<Decimal>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdjustmentReport {
    pub schema_version: u8,
    pub status: AdjustmentStatus,
    pub action_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    pub pair_id: String,
    pub network: String,
    pub config_revision: String,
    pub account_address: String,
    pub target_notional: Decimal,
    pub created_at: String,
    pub legs: Vec<AdjustmentLeg>,
    pub diagnostics: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_signer: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub signing_requests: Vec<SigningRequest>,
}

pub struct AdjustRequest<'a> {
    pub pair: &'a DashboardPair,
    pub config_path: &'a Path,
    pub config_revision: &'a str,
    pub account: Address,
    pub target_notional: Decimal,
    pub max_notional: Decimal,
    pub state_dir: &'a Path,
    pub request_id: Option<&'a str>,
}

struct AdjustStore {
    dir: PathBuf,
}

impl AdjustStore {
    fn new(base: &Path, pair_id: &str, account: Address) -> Self {
        Self {
            dir: base.join(pair_id).join(format!("{account}").to_lowercase()),
        }
    }

    fn state_path(&self) -> PathBuf {
        self.dir.join("adjust-state.json")
    }

    fn acquire(&self) -> Result<WriteLock, AdjustError> {
        fs::create_dir_all(&self.dir).map_err(|e| AdjustError::State(e.to_string()))?;
        let mut permissions = fs::metadata(&self.dir)
            .map_err(|e| AdjustError::State(e.to_string()))?
            .permissions();
        permissions.set_mode(0o700);
        fs::set_permissions(&self.dir, permissions)
            .map_err(|e| AdjustError::State(e.to_string()))?;
        WriteLock::at(&self.dir.join("adjust.lock")).map_err(AdjustError::State)
    }

    fn load(&self) -> Result<Option<AdjustmentReport>, AdjustError> {
        let path = self.state_path();
        if !path.exists() {
            return Ok(None);
        }
        let text = fs::read_to_string(path).map_err(|e| AdjustError::State(e.to_string()))?;
        serde_json::from_str(&text)
            .map(Some)
            .map_err(|e| AdjustError::State(format!("解析调仓回执失败: {e}")))
    }

    fn save(&self, report: &AdjustmentReport) -> Result<(), AdjustError> {
        fs::create_dir_all(&self.dir).map_err(|e| AdjustError::State(e.to_string()))?;
        let json =
            serde_json::to_string_pretty(report).map_err(|e| AdjustError::State(e.to_string()))?;
        let temporary = self
            .dir
            .join(format!("adjust-state.tmp.{}", std::process::id()));
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(|e| AdjustError::State(format!("写入调仓回执失败: {e}")))?;
        use std::io::Write as _;
        file.write_all(json.as_bytes())
            .and_then(|_| file.sync_all())
            .map_err(|e| AdjustError::State(format!("保存调仓回执失败: {e}")))?;
        fs::rename(&temporary, self.state_path()).map_err(|e| AdjustError::State(e.to_string()))?;
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|e| AdjustError::State(format!("同步调仓回执目录失败: {e}")))
    }

    fn archive(&self) -> Result<(), AdjustError> {
        let Some(report) = self.load()? else {
            return Ok(());
        };
        let action_id = report
            .action_id
            .ok_or_else(|| AdjustError::State("旧调仓回执缺少 action_id".into()))?;
        let source = self.state_path();
        let target = self
            .dir
            .join(format!("adjust-{action_id}-{:?}.json", report.status).to_lowercase());
        let bytes = fs::read(&source).map_err(|e| AdjustError::State(e.to_string()))?;
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&target)
            .map_err(|e| AdjustError::State(format!("归档旧调仓回执失败: {e}")))?;
        use std::io::Write as _;
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|e| AdjustError::State(format!("保存归档回执失败: {e}")))?;
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|e| AdjustError::State(e.to_string()))
    }
}

fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

fn new_cloid() -> Result<Cloid, AdjustError> {
    pair::gen_cloid().map_err(|e| AdjustError::State(e.to_string()))
}

fn signed_position(account: &AccountView, coin: &str) -> Decimal {
    account
        .positions
        .iter()
        .find(|position| position.coin == coin)
        .map(|position| position.size)
        .unwrap_or(Decimal::ZERO)
}

fn mid(book: &BboView, coin: &str) -> Result<Decimal, AdjustError> {
    match (book.bid, book.ask) {
        (Some(bid), Some(ask)) if bid > Decimal::ZERO && ask >= bid => {
            Ok((bid + ask) / Decimal::TWO)
        }
        _ => Err(AdjustError::Refused(format!("{coin} 盘口不完整或价格非法"))),
    }
}

fn check_prior(store: &AdjustStore, request: &AdjustRequest<'_>) -> Result<(), AdjustError> {
    let Some(prior) = store.load()? else {
        return Ok(());
    };
    if prior.pair_id != request.pair.descriptor.id
        || prior.account_address != format!("{}", request.account).to_lowercase()
    {
        return Err(AdjustError::Unresolved(
            "已有回执的 pair 或账户身份与当前请求不一致".into(),
        ));
    }
    let awaiting_browser_signatures = prior.status == AdjustmentStatus::Prepared
        && prior.expected_signer.is_some()
        && prior.legs.iter().all(|leg| {
            matches!(
                leg.status,
                AdjustmentLegStatus::Prepared | AdjustmentLegStatus::NotRequired
            )
        });
    let unresolved = !awaiting_browser_signatures
        && (matches!(
            prior.status,
            AdjustmentStatus::Prepared | AdjustmentStatus::Executing | AdjustmentStatus::Unknown
        ) || prior.legs.iter().any(|leg| {
            matches!(
                leg.status,
                AdjustmentLegStatus::Pending | AdjustmentLegStatus::Unknown
            )
        }));
    if unresolved && prior.config_revision != request.config_revision {
        return Err(AdjustError::Unresolved(
            "未核清动作的配置版本已变化，禁止根据新配置继续写入".into(),
        ));
    }
    if unresolved {
        return Err(AdjustError::Unresolved(format!(
            "动作 {} 的结果尚未核清，禁止再次提交；请先按回执中的 cloid 和交易所持仓人工核对",
            prior.action_id.unwrap_or_else(|| "unknown".into())
        )));
    }
    Ok(())
}

struct Plan {
    report: AdjustmentReport,
    markets: [PerpMarket; 2],
    start_positions: [Decimal; 2],
}

async fn build_plan(
    adapter: &Adapter,
    request: &AdjustRequest<'_>,
    action_id: Option<String>,
) -> Result<Plan, AdjustError> {
    if request.max_notional <= Decimal::ZERO {
        return Err(AdjustError::Refused("每腿名义上限必须为正数".into()));
    }
    if request.target_notional < Decimal::ZERO
        || (request.target_notional > Decimal::ZERO && request.target_notional < Decimal::TEN)
        || request.target_notional > request.max_notional
    {
        return Err(AdjustError::Refused(format!(
            "每腿目标名义必须为 0 或不小于 10 USD，且不能超过上限 {} USD",
            request.max_notional
        )));
    }
    let descriptor = &request.pair.descriptor;
    let dex = (!descriptor.dex.is_empty()).then_some(descriptor.dex.as_str());
    let account = adapter.account_for(request.account, dex).await?;
    let coins = &descriptor.coins;
    if account
        .open_orders
        .iter()
        .any(|order| coins.contains(&order.coin))
    {
        return Err(AdjustError::Refused(
            "当前账户在目标两腿仍有挂单，请先处理挂单再调仓".into(),
        ));
    }
    let start_positions = [
        signed_position(&account, &coins[0]),
        signed_position(&account, &coins[1]),
    ];
    if start_positions[0] < Decimal::ZERO || start_positions[1] > Decimal::ZERO {
        return Err(AdjustError::Refused(format!(
            "现有方向不符合 {} 多 / {} 空，拒绝反向或改向交易",
            coins[0], coins[1]
        )));
    }
    let markets = adapter.perp_markets_for(dex).await?;
    let first_market = find_market(&markets, &coins[0])?.clone();
    let second_market = find_market(&markets, &coins[1])?.clone();
    let markets = [first_market, second_market];
    if markets.iter().any(|market| market.delisted) {
        return Err(AdjustError::Refused("目标市场已退市，拒绝调仓".into()));
    }

    let mut legs = Vec::with_capacity(2);
    for index in 0..2 {
        let market = &markets[index];
        let book = adapter.bbo(&market.name).await?;
        let mid = mid(&book, &market.name)?;
        let target_size = pair::floor_qty(request.target_notional / mid, market.sz_decimals);
        if request.target_notional > Decimal::ZERO
            && (target_size <= Decimal::ZERO || target_size * mid < Decimal::TEN)
        {
            return Err(AdjustError::Refused(format!(
                "{} 按市场精度换算后低于最小名义，无法达到目标",
                market.name
            )));
        }
        let current_signed = start_positions[index];
        let current_size = current_signed.abs();
        let delta = target_size - current_size;
        let (action, order_qty, reduce_only) = if delta > Decimal::ZERO {
            (if index == 0 { "buy" } else { "sell" }, delta, false)
        } else if delta < Decimal::ZERO {
            (if index == 0 { "sell" } else { "buy" }, -delta, true)
        } else {
            ("none", Decimal::ZERO, false)
        };
        let protect = if order_qty > Decimal::ZERO {
            pair::protect_price(market, action == "buy", &book)
                .map_err(|error| AdjustError::Refused(error.to_string()))?
        } else {
            Decimal::ZERO
        };
        if order_qty > Decimal::ZERO && order_qty * protect < Decimal::TEN {
            return Err(AdjustError::Refused(format!(
                "{} 的调仓差额低于最小下单名义 10 USD，未生成订单",
                market.name
            )));
        }
        let cloid = if action_id.is_some() && order_qty > Decimal::ZERO {
            Some(cloid_hex(new_cloid()?))
        } else {
            None
        };
        legs.push(AdjustmentLeg {
            coin: market.name.clone(),
            asset: market.index,
            nonce: None,
            position_side: if index == 0 { "long" } else { "short" }.into(),
            current_size,
            current_notional: current_size * mid,
            mid_px: mid,
            target_size,
            estimated_target_notional: target_size * mid,
            action: action.into(),
            order_qty,
            protect_px: protect,
            reduce_only,
            cloid,
            status: if order_qty > Decimal::ZERO {
                AdjustmentLegStatus::Prepared
            } else {
                AdjustmentLegStatus::NotRequired
            },
            confirmed_qty: Decimal::ZERO,
            avg_px: None,
            oid: None,
            result: None,
            final_size: None,
            final_notional: None,
        });
    }
    let status = if action_id.is_some() {
        AdjustmentStatus::Prepared
    } else {
        AdjustmentStatus::Preview
    };
    Ok(Plan {
        report: AdjustmentReport {
            schema_version: 1,
            status,
            action_id,
            request_id: request.request_id.map(str::to_owned),
            pair_id: descriptor.id.clone(),
            network: descriptor.network.label().into(),
            config_revision: request.config_revision.into(),
            account_address: format!("{}", request.account).to_lowercase(),
            target_notional: request.target_notional,
            created_at: now_utc(),
            legs,
            diagnostics: vec![
                "目标为每腿绝对名义；数量按市场精度向下取整，实际成交名义可能略有差异".into(),
                "IOC 未全部成交、拒单或结果未知时停止后续写操作，不自动追单或反向补偿".into(),
            ],
            expected_signer: None,
            signing_requests: Vec::new(),
        },
        markets,
        start_positions,
    })
}

fn ensure_config_unchanged(request: &AdjustRequest<'_>) -> Result<(), AdjustError> {
    let current =
        config::revision_at(request.config_path).map_err(|e| AdjustError::State(e.to_string()))?;
    if current != request.config_revision {
        return Err(AdjustError::Refused(
            "pair 配置已变化，请重新预览后再确认".into(),
        ));
    }
    Ok(())
}

fn finish_known(report: &mut AdjustmentReport, detail: String) {
    report.status = AdjustmentStatus::Incomplete;
    report.diagnostics.push(detail);
    report
        .diagnostics
        .push("本次为已知未完成状态；重新预览会按最新实际仓位计算差额".into());
}

fn finish_unknown(report: &mut AdjustmentReport, detail: String) {
    report.status = AdjustmentStatus::Unknown;
    report.diagnostics.push(detail);
    report
        .diagnostics
        .push("结果未知，禁止直接重试；请先按 cloid、成交记录和实际持仓人工核对".into());
}

fn save_after_write(store: &AdjustStore, report: &AdjustmentReport) -> Result<(), AdjustError> {
    store.save(report).map_err(|error| {
        AdjustError::Unresolved(format!(
            "订单已发送，但调仓回执保存失败；禁止继续写入或重试: {error}"
        ))
    })
}

pub async fn run_adjust(
    adapter: &Adapter,
    signer: Option<&PrivateKeySigner>,
    request: &AdjustRequest<'_>,
    execute: bool,
) -> Result<AdjustmentReport, AdjustError> {
    if execute != signer.is_some() {
        return Err(AdjustError::State(
            "execute 与 signer 必须同时提供或省略".into(),
        ));
    }
    ensure_config_unchanged(request)?;
    let store = AdjustStore::new(
        request.state_dir,
        &request.pair.descriptor.id,
        request.account,
    );
    let _guard = if execute {
        Some(store.acquire()?)
    } else {
        None
    };
    check_prior(&store, request)?;
    let action_id = if execute {
        Some(format!("adjust-{}", cloid_hex(new_cloid()?)))
    } else {
        None
    };
    let plan = build_plan(adapter, request, action_id).await?;
    let mut report = plan.report;
    if report.legs.iter().all(|leg| leg.order_qty == Decimal::ZERO) {
        report.status = if execute {
            AdjustmentStatus::Succeeded
        } else {
            AdjustmentStatus::Preview
        };
        for leg in &mut report.legs {
            leg.final_size = Some(leg.current_size);
            leg.final_notional = Some(leg.current_notional);
        }
        report
            .diagnostics
            .push("两腿已处于精度允许的目标数量，无需下单".into());
        return Ok(report);
    }
    if !execute {
        return Ok(report);
    }
    let signer = signer.expect("checked above");
    store.archive()?;
    store.save(&report)?;

    let mut order_indices: Vec<usize> = report
        .legs
        .iter()
        .enumerate()
        .filter(|(_, leg)| leg.reduce_only && leg.order_qty > Decimal::ZERO)
        .map(|(index, _)| index)
        .collect();
    order_indices.extend(
        report
            .legs
            .iter()
            .enumerate()
            .filter(|(_, leg)| !leg.reduce_only && leg.order_qty > Decimal::ZERO)
            .map(|(index, _)| index),
    );
    for index in order_indices {
        if let Err(error) = ensure_config_unchanged(request) {
            finish_known(&mut report, error.to_string());
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        report.status = AdjustmentStatus::Executing;
        report.legs[index].status = AdjustmentLegStatus::Pending;
        store.save(&report)?;
        let cloid = parse_cloid(
            report.legs[index]
                .cloid
                .as_deref()
                .ok_or_else(|| AdjustError::State("待执行腿缺少 cloid".into()))?,
        )
        .map_err(AdjustError::State)?;
        let result = send_leg(
            adapter,
            signer,
            &LegSend {
                market: &plan.markets[index],
                buy: report.legs[index].action == "buy",
                qty: report.legs[index].order_qty,
                protect_px: report.legs[index].protect_px,
                reduce_only: report.legs[index].reduce_only,
                cloid,
            },
        )
        .await;
        let (stop, unknown, coin, detail) = {
            let leg = &mut report.legs[index];
            let stop = match result {
                LegResult::Filled { oid, qty, avg_px } => {
                    leg.status = AdjustmentLegStatus::Filled;
                    leg.confirmed_qty = qty;
                    leg.avg_px = Some(avg_px);
                    leg.oid = Some(oid);
                    leg.result = Some("全部成交".into());
                    false
                }
                LegResult::PartFilled { oid, qty, avg_px } => {
                    leg.status = AdjustmentLegStatus::PartiallyFilled;
                    leg.confirmed_qty = qty;
                    leg.avg_px = Some(avg_px);
                    leg.oid = Some(oid);
                    leg.result = Some("部分成交，剩余 IOC 数量已终结".into());
                    true
                }
                LegResult::Rejected(message) => {
                    leg.status = AdjustmentLegStatus::Rejected;
                    leg.result = Some(message);
                    true
                }
                LegResult::Unknown(message) => {
                    leg.status = AdjustmentLegStatus::Unknown;
                    leg.result = Some(message);
                    true
                }
            };
            (
                stop,
                leg.status == AdjustmentLegStatus::Unknown,
                leg.coin.clone(),
                leg.result.clone().unwrap_or_else(|| "未全部成交".into()),
            )
        };
        if unknown {
            finish_unknown(&mut report, format!("{coin} 的结果未知: {detail}"));
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        if stop {
            finish_known(&mut report, format!("{coin}: {detail}；已停止后续写操作"));
        }
        save_after_write(&store, &report)?;
        if stop {
            return Ok(report);
        }
    }

    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let actual = match adapter.account_for(request.account, dex).await {
        Ok(account) => account,
        Err(error) => {
            finish_unknown(&mut report, format!("成交后无法核对实际持仓: {error}"));
            save_after_write(&store, &report)?;
            return Ok(report);
        }
    };
    for index in 0..2 {
        let leg = &mut report.legs[index];
        let signed_fill = if leg.action == "buy" {
            leg.confirmed_qty
        } else if leg.action == "sell" {
            -leg.confirmed_qty
        } else {
            Decimal::ZERO
        };
        let expected = plan.start_positions[index] + signed_fill;
        let actual_size = signed_position(&actual, &leg.coin);
        if actual_size != expected {
            let coin = leg.coin.clone();
            finish_unknown(
                &mut report,
                format!("{coin} 实际持仓 {actual_size} 与已确认动作推导值 {expected} 不一致"),
            );
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        leg.final_size = Some(actual_size.abs());
        leg.final_notional = Some(actual_size.abs() * leg.mid_px);
    }
    report.status = AdjustmentStatus::Succeeded;
    report
        .diagnostics
        .push("两腿订单均已全部成交，并已核对交易所实际持仓".into());
    save_after_write(&store, &report)?;
    Ok(report)
}

alloy::sol! {
    #[derive(serde::Serialize)]
    struct Agent {
        string source;
        bytes32 connectionId;
    }
}

fn action_for_leg(leg: &AdjustmentLeg) -> Result<Action, AdjustError> {
    use hypersdk::hypercore::types::{
        BatchOrder, OrderGrouping, OrderRequest, OrderTypePlacement, TimeInForce,
    };

    let cloid = parse_cloid(
        leg.cloid
            .as_deref()
            .ok_or_else(|| AdjustError::State("待签名腿缺少 cloid".into()))?,
    )
    .map_err(AdjustError::State)?;
    Ok(Action::Order(BatchOrder {
        orders: vec![OrderRequest {
            asset: leg.asset,
            is_buy: leg.action == "buy",
            limit_px: leg.protect_px,
            sz: leg.order_qty,
            reduce_only: leg.reduce_only,
            order_type: OrderTypePlacement::Limit {
                tif: TimeInForce::Ioc,
            },
            cloid,
        }],
        grouping: OrderGrouping::Na,
        builder: None,
    }))
}

fn chain_for(request: &AdjustRequest<'_>) -> Chain {
    match request.pair.descriptor.network {
        config::Network::Mainnet => Chain::Mainnet,
        config::Network::Testnet => Chain::Testnet,
    }
}

fn typed_data_for(
    action: &Action,
    nonce: u64,
    chain: Chain,
) -> Result<serde_json::Value, AdjustError> {
    let connection_id = action
        .hash(nonce, None, None)
        .map_err(|error| AdjustError::State(format!("计算订单签名哈希失败: {error}")))?;
    let agent = Agent {
        source: if chain.is_mainnet() { "a" } else { "b" }.into(),
        connectionId: connection_id,
    };
    let domain = alloy::sol_types::eip712_domain! {
        name: "Exchange",
        version: "1",
        chain_id: 1337,
        verifying_contract: Address::ZERO,
    };
    serde_json::to_value(TypedData::from_struct(&agent, Some(domain)))
        .map_err(|error| AdjustError::State(format!("生成钱包签名数据失败: {error}")))
}

fn deserialize_hex_signature<'de, D>(deserializer: D) -> Result<Signature, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let encoded = String::deserialize(deserializer)?;
    encoded.parse().map_err(serde::de::Error::custom)
}

#[derive(Deserialize)]
pub struct BrowserSignature {
    pub leg_index: usize,
    #[serde(deserialize_with = "deserialize_hex_signature")]
    pub signature: Signature,
}

pub async fn prepare_browser_adjustment(
    adapter: &Adapter,
    request: &AdjustRequest<'_>,
    expected_signer: Address,
) -> Result<AdjustmentReport, AdjustError> {
    if expected_signer == Address::ZERO || expected_signer == request.account {
        return Err(AdjustError::Refused(
            "必须使用与原交易账户不同的 API Wallet 地址".into(),
        ));
    }
    ensure_config_unchanged(request)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let store = AdjustStore::new(
        request.state_dir,
        &request.pair.descriptor.id,
        request.account,
    );
    let _guard = store.acquire()?;
    check_prior(&store, request)?;
    let action_id = Some(format!("adjust-{}", cloid_hex(new_cloid()?)));
    let plan = build_plan(adapter, request, action_id).await?;
    let mut report = plan.report;
    report.expected_signer = Some(format!("{expected_signer}").to_lowercase());
    if report.legs.iter().all(|leg| leg.order_qty == Decimal::ZERO) {
        report.status = AdjustmentStatus::Succeeded;
        for leg in &mut report.legs {
            leg.final_size = Some(leg.current_size);
            leg.final_notional = Some(leg.current_notional);
        }
        report
            .diagnostics
            .push("两腿已处于精度允许的目标数量，无需下单".into());
        return Ok(report);
    }
    store.archive()?;
    for index in 0..report.legs.len() {
        if report.legs[index].order_qty == Decimal::ZERO {
            continue;
        }
        let nonce = adapter.next_nonce()?;
        let action = action_for_leg(&report.legs[index])?;
        report.legs[index].nonce = Some(nonce);
        report.signing_requests.push(SigningRequest {
            leg_index: index,
            nonce,
            typed_data: typed_data_for(&action, nonce, chain_for(request))?,
        });
    }
    report
        .diagnostics
        .push("等待浏览器中的 API Wallet 签名；签名收集完成前不会发送订单".into());
    store.save(&report)?;
    Ok(report)
}

fn classify_place_result(result: Result<PlaceOutcome, AdapterError>, qty: Decimal) -> LegResult {
    match result {
        Ok(PlaceOutcome::Filled {
            oid,
            total_sz,
            avg_px,
        }) if total_sz == qty && avg_px > Decimal::ZERO => LegResult::Filled {
            oid,
            qty: total_sz,
            avg_px,
        },
        Ok(PlaceOutcome::Filled {
            total_sz, avg_px, ..
        }) if total_sz.is_zero() && avg_px.is_zero() => {
            LegResult::Rejected("IOC 零成交且订单已终结".into())
        }
        Ok(PlaceOutcome::Filled {
            oid,
            total_sz,
            avg_px,
        }) if total_sz >= Decimal::ZERO && total_sz < qty && avg_px > Decimal::ZERO => {
            LegResult::PartFilled {
                oid,
                qty: total_sz,
                avg_px,
            }
        }
        Ok(PlaceOutcome::Filled { .. }) => LegResult::Unknown("成交数量或价格不合法".into()),
        Ok(PlaceOutcome::Rejected(message)) => LegResult::Rejected(message),
        Ok(PlaceOutcome::Resting { oid }) => {
            LegResult::Unknown(format!("IOC 单意外挂单 oid={oid}，结果未知，请核对"))
        }
        Err(error @ (AdapterError::Unknown(_) | AdapterError::BadResponse(_))) => {
            LegResult::Unknown(error.to_string())
        }
        Err(AdapterError::Permission(message)) => {
            LegResult::Rejected(format!("权限拒绝: {message}"))
        }
        Err(error) => LegResult::Unknown(error.to_string()),
    }
}

fn normalize_signature(mut signature: Signature) -> Result<Signature, AdjustError> {
    signature.v = match signature.v {
        0 => 27,
        1 => 28,
        27 | 28 => signature.v,
        other => {
            return Err(AdjustError::Refused(format!(
                "签名恢复标识 {other} 不受支持"
            )));
        }
    };
    Ok(signature)
}

pub async fn submit_browser_adjustment(
    adapter: &Adapter,
    request: &AdjustRequest<'_>,
    expected_signer: Address,
    action_id: &str,
    signatures: Vec<BrowserSignature>,
) -> Result<AdjustmentReport, AdjustError> {
    if expected_signer == Address::ZERO || expected_signer == request.account {
        return Err(AdjustError::Refused(
            "必须使用与原交易账户不同的 API Wallet 地址".into(),
        ));
    }
    ensure_config_unchanged(request)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let store = AdjustStore::new(
        request.state_dir,
        &request.pair.descriptor.id,
        request.account,
    );
    let _guard = store.acquire()?;
    let mut report = store
        .load()?
        .ok_or_else(|| AdjustError::Refused("找不到待签名调仓动作，请重新预览".into()))?;
    let expected_signer_text = format!("{expected_signer}").to_lowercase();
    if report.action_id.as_deref() != Some(action_id)
        || report.status != AdjustmentStatus::Prepared
        || report.expected_signer.as_deref() != Some(expected_signer_text.as_str())
        || report.pair_id != request.pair.descriptor.id
        || report.account_address != format!("{}", request.account).to_lowercase()
        || report.config_revision != request.config_revision
    {
        return Err(AdjustError::Refused(
            "调仓动作身份或状态已变化，请重新预览并签名".into(),
        ));
    }
    let provided: std::collections::BTreeMap<usize, Signature> = signatures
        .into_iter()
        .map(|item| Ok((item.leg_index, normalize_signature(item.signature)?)))
        .collect::<Result<_, AdjustError>>()?;
    let order_indices: Vec<usize> = report
        .legs
        .iter()
        .enumerate()
        .filter(|(_, leg)| leg.order_qty > Decimal::ZERO)
        .map(|(index, _)| index)
        .collect();
    if provided.len() != order_indices.len()
        || order_indices
            .iter()
            .any(|index| !provided.contains_key(index))
    {
        return Err(AdjustError::Refused(
            "每条待执行腿必须恰好提供一份签名".into(),
        ));
    }
    let chain = chain_for(request);
    let mut signed_requests = std::collections::BTreeMap::new();
    for index in &order_indices {
        let leg = &report.legs[*index];
        let signature = provided
            .get(index)
            .ok_or_else(|| AdjustError::Refused(format!("{} 缺少 API Wallet 签名", leg.coin)))?;
        let nonce = leg
            .nonce
            .ok_or_else(|| AdjustError::State("待签名腿缺少 nonce".into()))?;
        let signed = ActionRequest {
            action: action_for_leg(leg)?,
            nonce,
            signature: *signature,
            vault_address: None,
            expires_after: None,
        };
        let recovered = signed
            .recover(chain)
            .map_err(|error| AdjustError::Refused(format!("签名无法验证: {error}")))?;
        if recovered != expected_signer {
            return Err(AdjustError::Refused(format!(
                "{} 的签名地址不是所选 API Wallet，已拒绝提交",
                leg.coin
            )));
        }
        signed_requests.insert(*index, signed);
    }

    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let account = adapter.account_for(request.account, dex).await?;
    if account
        .open_orders
        .iter()
        .any(|order| request.pair.descriptor.coins.contains(&order.coin))
    {
        return Err(AdjustError::Refused(
            "签名后目标两腿出现挂单，请处理后重新预览".into(),
        ));
    }
    let start_positions = [
        signed_position(&account, &report.legs[0].coin),
        signed_position(&account, &report.legs[1].coin),
    ];
    for (index, start) in start_positions.iter().enumerate() {
        let leg = &report.legs[index];
        let expected = if leg.position_side == "long" {
            leg.current_size
        } else {
            -leg.current_size
        };
        if *start != expected {
            return Err(AdjustError::Refused(format!(
                "{} 的仓位在签名前后发生变化，请重新预览并签名",
                leg.coin
            )));
        }
    }
    let markets = adapter.perp_markets_for(dex).await?;
    let markets = [
        find_market(&markets, &report.legs[0].coin)?.clone(),
        find_market(&markets, &report.legs[1].coin)?.clone(),
    ];
    if markets
        .iter()
        .enumerate()
        .any(|(index, market)| market.delisted || market.index != report.legs[index].asset)
    {
        return Err(AdjustError::Refused(
            "市场资产 ID 或上市状态已变化，请重新预览并签名".into(),
        ));
    }
    report.signing_requests.clear();

    let mut execution_order: Vec<usize> = order_indices
        .iter()
        .copied()
        .filter(|index| report.legs[*index].reduce_only)
        .collect();
    execution_order.extend(
        order_indices
            .iter()
            .copied()
            .filter(|index| !report.legs[*index].reduce_only),
    );
    for index in execution_order {
        if let Err(error) = ensure_config_unchanged(request) {
            finish_known(&mut report, error.to_string());
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        report.status = AdjustmentStatus::Executing;
        report.legs[index].status = AdjustmentLegStatus::Pending;
        store.save(&report)?;
        let signed = signed_requests
            .remove(&index)
            .ok_or_else(|| AdjustError::State("已验证签名缺失".into()))?;
        let result = classify_place_result(
            adapter.place_signed_order(signed).await,
            report.legs[index].order_qty,
        );
        let (stop, unknown, coin, detail) = {
            let leg = &mut report.legs[index];
            let stop = match result {
                LegResult::Filled { oid, qty, avg_px } => {
                    leg.status = AdjustmentLegStatus::Filled;
                    leg.confirmed_qty = qty;
                    leg.avg_px = Some(avg_px);
                    leg.oid = Some(oid);
                    leg.result = Some("全部成交".into());
                    false
                }
                LegResult::PartFilled { oid, qty, avg_px } => {
                    leg.status = AdjustmentLegStatus::PartiallyFilled;
                    leg.confirmed_qty = qty;
                    leg.avg_px = Some(avg_px);
                    leg.oid = Some(oid);
                    leg.result = Some("部分成交，剩余 IOC 数量已终结".into());
                    true
                }
                LegResult::Rejected(message) => {
                    leg.status = AdjustmentLegStatus::Rejected;
                    leg.result = Some(message);
                    true
                }
                LegResult::Unknown(message) => {
                    leg.status = AdjustmentLegStatus::Unknown;
                    leg.result = Some(message);
                    true
                }
            };
            (
                stop,
                leg.status == AdjustmentLegStatus::Unknown,
                leg.coin.clone(),
                leg.result.clone().unwrap_or_else(|| "未全部成交".into()),
            )
        };
        if unknown {
            finish_unknown(&mut report, format!("{coin} 的结果未知: {detail}"));
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        if stop {
            finish_known(&mut report, format!("{coin}: {detail}；已停止后续写操作"));
        }
        save_after_write(&store, &report)?;
        if stop {
            return Ok(report);
        }
    }

    let actual = match adapter.account_for(request.account, dex).await {
        Ok(account) => account,
        Err(error) => {
            finish_unknown(&mut report, format!("成交后无法核对实际持仓: {error}"));
            save_after_write(&store, &report)?;
            return Ok(report);
        }
    };
    for (index, start) in start_positions.iter().enumerate() {
        let leg = &mut report.legs[index];
        let signed_fill = if leg.action == "buy" {
            leg.confirmed_qty
        } else if leg.action == "sell" {
            -leg.confirmed_qty
        } else {
            Decimal::ZERO
        };
        let expected = *start + signed_fill;
        let actual_size = signed_position(&actual, &leg.coin);
        if actual_size != expected {
            let coin = leg.coin.clone();
            finish_unknown(
                &mut report,
                format!("{coin} 实际持仓 {actual_size} 与已确认动作推导值 {expected} 不一致"),
            );
            save_after_write(&store, &report)?;
            return Ok(report);
        }
        leg.final_size = Some(actual_size.abs());
        leg.final_notional = Some(actual_size.abs() * leg.mid_px);
    }
    report.status = AdjustmentStatus::Succeeded;
    report
        .diagnostics
        .push("两腿订单均已全部成交，并已核对交易所实际持仓".into());
    save_after_write(&store, &report)?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use httpmock::MockServer;
    use std::str::FromStr;
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

    static DIR_SEQ: AtomicU64 = AtomicU64::new(0);
    const USER: &str = "0x7f2120f612b78d579197762196b8a202ab654096";

    struct Fixture {
        config: config::DashboardConfig,
        pair: DashboardPair,
        path: PathBuf,
        state_dir: PathBuf,
    }

    impl Fixture {
        fn request(&self) -> AdjustRequest<'_> {
            AdjustRequest {
                pair: &self.pair,
                config_path: &self.path,
                config_revision: &self.config.revision,
                account: USER.parse().unwrap(),
                target_notional: Decimal::new(2000, 0),
                max_notional: Decimal::new(5000, 0),
                state_dir: &self.state_dir,
                request_id: None,
            }
        }
    }

    fn signer() -> PrivateKeySigner {
        PrivateKeySigner::from_str(
            "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        .unwrap()
    }

    fn account(nbis: &str, crwv: &str) -> String {
        let position = |coin: &str, size: &str| {
            serde_json::json!({"type": "oneWay", "position": {
                "coin": coin, "szi": size,
                "leverage": {"type": "cross", "value": 3}, "entryPx": "10",
                "positionValue": "1000", "unrealizedPnl": "0", "returnOnEquity": "0",
                "liquidationPx": null, "marginUsed": "100", "maxLeverage": 5,
                "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}
            }})
        };
        serde_json::json!({
            "marginSummary": {"accountValue": "10000", "totalNtlPos": "0", "totalRawUsd": "10000", "totalMarginUsed": "0"},
            "crossMarginSummary": {"accountValue": "10000", "totalNtlPos": "0", "totalRawUsd": "10000", "totalMarginUsed": "0"},
            "crossMaintenanceMarginUsed": "0", "withdrawable": "9000", "time": 1760000000000u64,
            "assetPositions": [position("xyz:NBIS", nbis), position("xyz:CRWV", crwv)]
        })
        .to_string()
    }

    fn mock_accounts(server: &MockServer, accounts: Vec<String>) {
        let next = AtomicUsize::new(0);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState")
                .body_includes(r#""dex":"xyz""#);
            then.respond_with(move |_| {
                let index = next.fetch_add(1, Ordering::SeqCst).min(accounts.len() - 1);
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(accounts[index].clone())
                    .build()
            });
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders")
                .body_includes(r#""dex":"xyz""#);
            then.status(200).body("[]");
        });
    }

    fn mock_markets(server: &MockServer) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"perpDexs""#);
            then.status(200).body(r#"[null,{"name":"xyz"}]"#);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"spotMeta""#);
            then.status(200).body(
                r#"{
                "universe": [],
                "tokens": [{"name":"USDC","index":0,"tokenId":"0x00000000000000000000000000000000",
                    "szDecimals":8,"weiDecimals":8,"evmContract":null}]
            }"#,
            );
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"meta""#)
                .body_includes(r#""dex":"xyz""#);
            then.status(200).body(
                r#"{
                "universe": [
                    {"szDecimals":2,"name":"xyz:NBIS","maxLeverage":5,"onlyIsolated":false},
                    {"szDecimals":2,"name":"xyz:CRWV","maxLeverage":5,"onlyIsolated":false}
                ],
                "collateralToken":0
            }"#,
            );
        });
        for (coin, bid, ask) in [("xyz:NBIS", "9.9", "10.1"), ("xyz:CRWV", "49.75", "50.25")] {
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes(r#""type":"l2Book""#)
                    .body_includes(coin);
                then.status(200).body(format!(
                    r#"{{"coin":"{coin}","time":1760000000000,"snapshot":true,
                        "levels":[[{{"px":"{bid}","sz":"1000","n":1}}],[{{"px":"{ask}","sz":"1000","n":1}}]]}}"#
                ));
            });
        }
    }

    async fn setup(accounts: Vec<String>) -> (MockServer, Fixture) {
        let server = MockServer::start_async().await;
        mock_markets(&server);
        mock_accounts(&server, accounts);
        let dir = std::env::temp_dir().join(format!(
            "hltrader-adjust-{}-{}",
            std::process::id(),
            DIR_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("pairs.json");
        fs::write(
            &path,
            format!(
                r#"{{"config_version":1,"pairs":[{{"id":"nbis-crwv","label":"NBIS / CRWV",
                    "enabled":true,"network":"mainnet","dex":"xyz","coins":["xyz:NBIS","xyz:CRWV"],
                    "account_address":"{USER}","unlock_anchors":null}}]}}"#
            ),
        )
        .unwrap();
        let config = config::load(&path).unwrap();
        let pair = config.enabled_pair("nbis-crwv").unwrap().clone();
        (
            server,
            Fixture {
                config,
                pair,
                path: path.clone(),
                state_dir: dir.join("state"),
            },
        )
    }

    fn filled(oid: u64, qty: &str, price: &str) -> String {
        format!(
            r#"{{"status":"ok","response":{{"type":"order","data":{{"statuses":[
                {{"filled":{{"totalSz":"{qty}","avgPx":"{price}","oid":{oid}}}}}]}}}}}}"#
        )
    }

    fn rejected() -> &'static str {
        r#"{"status":"ok","response":{"type":"order","data":{"statuses":[{"error":"rejected"}]}}}"#
    }

    #[tokio::test]
    async fn preview_computes_hip3_target_delta_without_writing() {
        let (server, fixture) = setup(vec![account("0", "0")]).await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let report = run_adjust(
            &Adapter::with_base_url(&server.base_url()),
            None,
            &fixture.request(),
            false,
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Preview);
        assert_eq!(report.legs[0].target_size, Decimal::new(200, 0));
        assert_eq!(report.legs[1].target_size, Decimal::new(40, 0));
        assert_eq!(report.legs[0].action, "buy");
        assert_eq!(report.legs[1].action, "sell");
        assert!(report.legs.iter().all(|leg| leg.cloid.is_none()));
        assert_eq!(exchange.calls(), 0);
        assert!(!fixture.state_dir.exists());
    }

    #[tokio::test]
    async fn execute_reduces_before_increasing_and_checks_actual_positions() {
        let (server, fixture) = setup(vec![account("250", "-30"), account("200", "-40")]).await;
        let reduce = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110000"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#)
                .body_includes(r#""s":"50""#);
            then.status(200).body(filled(11, "50", "10"));
        });
        let increase = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110001"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":false"#)
                .body_includes(r#""s":"10""#);
            then.status(200).body(filled(12, "10", "50"));
        });
        let report = run_adjust(
            &Adapter::with_base_url(&server.base_url()),
            Some(&signer()),
            &fixture.request(),
            true,
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Succeeded);
        assert_eq!(reduce.calls(), 1);
        assert_eq!(increase.calls(), 1);
        assert_eq!(report.legs[0].final_size, Some(Decimal::new(200, 0)));
        assert_eq!(report.legs[1].final_size, Some(Decimal::new(40, 0)));
        assert!(fixture.state_dir.exists());
    }

    #[tokio::test]
    async fn rejection_stops_second_leg_and_allows_fresh_preview() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        let first = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110000"#);
            then.status(200).body(rejected());
        });
        let second = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110001"#);
            then.status(200).body(filled(12, "10", "50"));
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_adjust(&adapter, Some(&signer()), &fixture.request(), true)
            .await
            .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Incomplete);
        assert_eq!(first.calls(), 1);
        assert_eq!(second.calls(), 0);
        let preview = run_adjust(&adapter, None, &fixture.request(), false)
            .await
            .unwrap();
        assert_eq!(preview.status, AdjustmentStatus::Preview);
    }

    #[tokio::test]
    async fn partial_fill_stops_second_leg() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110000"#);
            then.status(200).body(filled(11, "25", "10"));
        });
        let second = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110001"#);
            then.status(200).body(filled(12, "10", "50"));
        });
        let report = run_adjust(
            &Adapter::with_base_url(&server.base_url()),
            Some(&signer()),
            &fixture.request(),
            true,
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Incomplete);
        assert_eq!(report.legs[0].status, AdjustmentLegStatus::PartiallyFilled);
        assert_eq!(second.calls(), 0);
    }

    #[tokio::test]
    async fn unknown_result_blocks_another_submission() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110000"#);
            then.status(500).body("timeout");
        });
        let second = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110001"#);
            then.status(200).body(filled(12, "10", "50"));
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_adjust(&adapter, Some(&signer()), &fixture.request(), true)
            .await
            .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Unknown);
        assert_eq!(second.calls(), 0);
        let error = run_adjust(&adapter, None, &fixture.request(), false)
            .await
            .unwrap_err();
        assert!(matches!(error, AdjustError::Unresolved(_)));
        assert_eq!(second.calls(), 0);
    }

    #[tokio::test]
    async fn no_op_execute_does_not_create_order_or_state() {
        let (server, fixture) = setup(vec![account("200", "-40")]).await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let mut request = fixture.request();
        request.request_id = Some("123e4567-e89b-42d3-a456-426614174000");
        let report = run_adjust(
            &Adapter::with_base_url(&server.base_url()),
            Some(&signer()),
            &request,
            true,
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Succeeded);
        assert_eq!(request.request_id, report.request_id.as_deref());
        assert!(
            report
                .diagnostics
                .iter()
                .any(|line| line.contains("无需下单"))
        );
        assert_eq!(exchange.calls(), 0);
        assert!(
            !fixture
                .state_dir
                .join("nbis-crwv")
                .join(USER)
                .join("adjust-state.json")
                .exists()
        );
    }

    static BROWSER_WALLET_SEQ: AtomicU64 = AtomicU64::new(0);

    fn test_wallet() -> PrivateKeySigner {
        let number = BROWSER_WALLET_SEQ.fetch_add(1, Ordering::Relaxed) + 1;
        PrivateKeySigner::from_str(&format!("{number:064x}")).unwrap()
    }

    fn sign_browser_requests(
        report: &AdjustmentReport,
        signer: &PrivateKeySigner,
    ) -> Vec<BrowserSignature> {
        report
            .signing_requests
            .iter()
            .map(|request| {
                let action = action_for_leg(&report.legs[request.leg_index]).unwrap();
                let signed = action
                    .sign_sync(signer, request.nonce, None, None, Chain::Mainnet)
                    .unwrap();
                BrowserSignature {
                    leg_index: request.leg_index,
                    signature: signed.signature,
                }
            })
            .collect()
    }

    #[test]
    fn browser_signature_accepts_hex_json() {
        let encoded = format!("0x{}{}1b", "11".repeat(32), "22".repeat(32));
        let json = format!(r#"[{{"leg_index":0,"signature":"{encoded}"}}]"#);
        let parsed: Vec<BrowserSignature> = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].leg_index, 0);
        let expected = encoded.parse::<Signature>().unwrap();
        assert_eq!(parsed[0].signature.r, expected.r);
        assert_eq!(parsed[0].signature.s, expected.s);
        assert_eq!(parsed[0].signature.v, expected.v);
        assert_eq!(normalize_signature(parsed[0].signature).unwrap().v, 27);
    }

    #[tokio::test]
    async fn browser_typed_data_matches_hyperliquid_signing_hash_without_exchange() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let api_wallet = test_wallet();
        let report = prepare_browser_adjustment(
            &Adapter::with_base_url(&server.base_url()),
            &fixture.request(),
            api_wallet.address(),
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Prepared);
        assert_eq!(report.signing_requests.len(), 2);
        for request in &report.signing_requests {
            let action = action_for_leg(&report.legs[request.leg_index]).unwrap();
            let typed: TypedData = serde_json::from_value(request.typed_data.clone()).unwrap();
            assert_eq!(
                typed.eip712_signing_hash().unwrap(),
                action
                    .prehash(request.nonce, None, None, Chain::Mainnet)
                    .unwrap()
            );
        }
        assert_eq!(exchange.calls(), 0);
    }

    #[tokio::test]
    async fn browser_signatures_are_recovered_and_submitted_only_once() {
        let (server, fixture) = setup(vec![
            account("250", "-30"),
            account("250", "-30"),
            account("200", "-40"),
        ])
        .await;
        let reduce = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110000"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled(11, "50", "10"));
        });
        let increase = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":110001"#)
                .body_includes(r#""r":false"#);
            then.status(200).body(filled(12, "10", "50"));
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let api_wallet = test_wallet();
        let prepared =
            prepare_browser_adjustment(&adapter, &fixture.request(), api_wallet.address())
                .await
                .unwrap();
        let mut signatures = sign_browser_requests(&prepared, &api_wallet);
        signatures[0].signature.v -= 27;
        let report = submit_browser_adjustment(
            &adapter,
            &fixture.request(),
            api_wallet.address(),
            prepared.action_id.as_deref().unwrap(),
            signatures,
        )
        .await
        .unwrap();
        assert_eq!(report.status, AdjustmentStatus::Succeeded);
        assert_eq!(reduce.calls(), 1);
        assert_eq!(increase.calls(), 1);
        let replay = submit_browser_adjustment(
            &adapter,
            &fixture.request(),
            api_wallet.address(),
            prepared.action_id.as_deref().unwrap(),
            sign_browser_requests(&prepared, &api_wallet),
        )
        .await;
        assert!(matches!(replay, Err(AdjustError::Refused(_))));
        assert_eq!(reduce.calls(), 1);
        assert_eq!(increase.calls(), 1);
    }

    #[tokio::test]
    async fn wrong_wallet_signature_is_rejected_before_exchange() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let api_wallet = test_wallet();
        let wrong_wallet = PrivateKeySigner::from_str(
            "0x1111111111111111111111111111111111111111111111111111111111111111",
        )
        .unwrap();
        let prepared =
            prepare_browser_adjustment(&adapter, &fixture.request(), api_wallet.address())
                .await
                .unwrap();
        let error = submit_browser_adjustment(
            &adapter,
            &fixture.request(),
            api_wallet.address(),
            prepared.action_id.as_deref().unwrap(),
            sign_browser_requests(&prepared, &wrong_wallet),
        )
        .await
        .unwrap_err();
        assert!(matches!(error, AdjustError::Refused(_)));
        assert_eq!(exchange.calls(), 0);
    }

    #[tokio::test]
    async fn original_account_cannot_be_used_as_browser_signer() {
        let (server, fixture) = setup(vec![account("250", "-30")]).await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let error = prepare_browser_adjustment(
            &Adapter::with_base_url(&server.base_url()),
            &fixture.request(),
            USER.parse().unwrap(),
        )
        .await
        .unwrap_err();
        assert!(matches!(error, AdjustError::Refused(_)));
        assert_eq!(exchange.calls(), 0);
    }
}
