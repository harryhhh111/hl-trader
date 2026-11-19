//! Quantity-aware IOC limits. A plan's reference mid never moves during sends.
use hypersdk::hypercore::{PerpMarket, types::Side};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::hyperliquid::OrderBookView;

pub const DEFAULT_SLIPPAGE_BPS: u32 = 5;
pub const MAX_SLIPPAGE_BPS: u32 = 50;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DepthCheck {
    pub book_time_ms: u64,
    pub available_qty: Decimal,
    pub required_qty: Decimal,
    pub estimated_avg_px: Decimal,
    pub worst_fill_px: Decimal,
}

pub fn limit_price(
    market: &PerpMarket,
    buy: bool,
    reference: Decimal,
    bps: u32,
) -> Result<Decimal, String> {
    if reference <= Decimal::ZERO || !(1..=MAX_SLIPPAGE_BPS).contains(&bps) {
        return Err("最大滑点必须为 1–50 个基点，参考中间价必须为正".into());
    }
    let fraction = Decimal::from(bps) / Decimal::from(10000);
    let raw = reference
        * if buy {
            Decimal::ONE + fraction
        } else {
            Decimal::ONE - fraction
        };
    // Conservative rounding keeps the signed price INSIDE the user's bound.
    market
        .round_by_side(if buy { Side::Bid } else { Side::Ask }, raw, true)
        .filter(|p| *p > Decimal::ZERO && if buy { *p <= raw } else { *p >= raw })
        .ok_or_else(|| "无法在最大滑点范围内得到合法保护价".into())
}

pub fn check_depth(
    book: &OrderBookView,
    buy: bool,
    qty: Decimal,
    limit: Decimal,
    now_ms: u64,
) -> Result<DepthCheck, String> {
    if qty <= Decimal::ZERO || limit <= Decimal::ZERO {
        return Err("深度检查的数量与保护价必须为正".into());
    }
    if book.time_ms == 0
        || now_ms.saturating_sub(book.time_ms) > 5000
        || book.time_ms > now_ms.saturating_add(1000)
    {
        return Err("盘口时间已过期或异常，请重新预览".into());
    }
    for (levels, descending) in [(&book.bids, true), (&book.asks, false)] {
        if levels.is_empty()
            || levels
                .iter()
                .any(|l| l.px <= Decimal::ZERO || l.sz <= Decimal::ZERO)
            || levels.windows(2).any(|w| {
                if descending {
                    w[0].px <= w[1].px
                } else {
                    w[0].px >= w[1].px
                }
            })
        {
            return Err("盘口价格、数量或档位顺序无效".into());
        }
    }
    if book.bids[0].px > book.asks[0].px {
        return Err("盘口买卖价交叉".into());
    }
    let levels = if buy { &book.asks } else { &book.bids };
    let mut available = Decimal::ZERO;
    let mut remaining = qty;
    let mut notional = Decimal::ZERO;
    let mut worst = Decimal::ZERO;
    for level in levels {
        if if buy {
            level.px > limit
        } else {
            level.px < limit
        } {
            break;
        }
        available += level.sz;
        let take = remaining.min(level.sz);
        if take > Decimal::ZERO {
            notional += take * level.px;
            remaining -= take;
            worst = level.px;
        }
    }
    if remaining > Decimal::ZERO {
        return Err(format!(
            "最大滑点范围内深度不足：需要 {qty}，可成交 {available}，保护价 {limit}；请重新预览"
        ));
    }
    Ok(DepthCheck {
        book_time_ms: book.time_ms,
        available_qty: available,
        required_qty: qty,
        estimated_avg_px: notional / qty,
        worst_fill_px: worst,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use hypersdk::hypercore::{Cloid, PriceTick, SpotToken, types::BookLevel};
    use std::str::FromStr;

    fn d(value: &str) -> Decimal {
        Decimal::from_str(value).unwrap()
    }
    fn market() -> PerpMarket {
        PerpMarket {
            name: "HYPE".into(),
            index: 159,
            sz_decimals: 2,
            collateral: SpotToken {
                name: "USDC".into(),
                index: 0,
                token_id: Cloid::ZERO,
                evm_contract: None,
                cross_chain_address: None,
                sz_decimals: 8,
                wei_decimals: 8,
                evm_extra_decimals: 0,
            },
            max_leverage: 5,
            isolated_margin: false,
            margin_mode: None,
            deployer_fee_scale: None,
            growth_mode: false,
            aligned_quote_token: false,
            delisted: false,
            table: PriceTick::for_perp(2),
        }
    }
    fn levels(values: &[(&str, &str)]) -> Vec<BookLevel> {
        values
            .iter()
            .map(|(p, q)| BookLevel {
                px: d(p),
                sz: d(q),
                n: 1,
            })
            .collect()
    }
    fn book() -> OrderBookView {
        OrderBookView {
            bids: levels(&[("99.99", "0.3"), ("99.98", "0.7"), ("99.90", "100")]),
            asks: levels(&[("100.01", "0.2"), ("100.02", "0.8"), ("100.10", "100")]),
            time_ms: 10000,
        }
    }
    #[test]
    fn buy_and_sell_walk_multiple_levels_and_report_vwap() {
        let b = book();
        let buy = check_depth(&b, true, d("0.5"), d("100.05"), 10000).unwrap();
        assert_eq!(buy.available_qty, d("1"));
        assert_eq!(buy.estimated_avg_px, d("100.016"));
        assert_eq!(buy.worst_fill_px, d("100.02"));
        let sell = check_depth(&b, false, d("0.5"), d("99.95"), 10000).unwrap();
        assert_eq!(sell.available_qty, d("1"));
        assert_eq!(sell.estimated_avg_px, d("99.986"));
        assert_eq!(sell.worst_fill_px, d("99.98"));
    }
    #[test]
    fn liquidity_outside_cap_cannot_cover_missing_quantity() {
        for (buy, cap) in [(true, "100.05"), (false, "99.95")] {
            let error = check_depth(&book(), buy, d("1.01"), d(cap), 10000).unwrap_err();
            assert!(error.contains("需要 1.01，可成交 1"), "{error}");
        }
    }
    #[test]
    fn quantity_and_price_boundaries_are_inclusive() {
        assert!(check_depth(&book(), true, d("1"), d("100.02"), 10000).is_ok());
        assert!(check_depth(&book(), false, d("1"), d("99.98"), 10000).is_ok());
        assert!(check_depth(&book(), true, d("0.2"), d("100.009"), 10000).is_err());
    }
    #[test]
    fn stale_future_and_invalid_book_data_are_rejected() {
        let mut b = book();
        assert!(check_depth(&b, true, d("0.1"), d("100.05"), 15001).is_err());
        assert!(check_depth(&b, true, d("0.1"), d("100.05"), 8999).is_err());
        b.asks[0].sz = Decimal::ZERO;
        assert!(check_depth(&b, true, d("0.1"), d("100.05"), 10000).is_err());
        b = book();
        b.asks.reverse();
        assert!(check_depth(&b, true, d("0.1"), d("100.05"), 10000).is_err());
        b = book();
        b.bids[0].px = d("101");
        assert!(check_depth(&b, true, d("0.1"), d("100.05"), 10000).is_err());
    }
    #[test]
    fn tick_rounding_never_exceeds_slippage_cap() {
        for reference in ["0.99999", "9.9999", "68.6695", "999.999", "64030.5"] {
            for bps in [1, 5, 50] {
                let mid = d(reference);
                let fraction = Decimal::from(bps) / Decimal::from(10000);
                let buy = limit_price(&market(), true, mid, bps).unwrap();
                let sell = limit_price(&market(), false, mid, bps).unwrap();
                assert!(buy <= mid * (Decimal::ONE + fraction));
                assert!(sell >= mid * (Decimal::ONE - fraction));
                assert_eq!(market().round_price(buy), Some(buy));
                assert_eq!(market().round_price(sell), Some(sell));
            }
        }
        for bps in [0, 51, u32::MAX] {
            assert!(limit_price(&market(), true, d("100"), bps).is_err());
        }
    }
}
