//! status 看板：仓位 + 资金费 + 动态（PHASE2_PLAN §4）。
//!
//! - 行情和账户来自 Hyperliquid info；动态另查公开来源。只读、无 daemon、不存文件。
//! - 资金费区分市场费率（metaAndAssetCtxs）与账户真实收付（userFunding）。
//! - 单块失败标注“不可用”并保留其他块；不补零、不编造。
//! - 解锁锚点来自人工配置（含来源与核实日期），7 天内的锚点才提醒。

mod announcements;

pub(crate) use announcements::{collect_dashboard, dashboard_sources};

use chrono::{DateTime, Utc};
use hypersdk::Address;
use rust_decimal::Decimal;

use crate::config::{Config, NETWORK};
use crate::hyperliquid::{AccountView, Adapter, AssetCtxView, FundingPaymentView, now_epoch_ms};
use crate::pair::{LONG_COIN, PairStore, SHORT_COIN};

/// 简单年化系数：小时费率 × 24 × 365（文档 §4）。
const HOURS_PER_YEAR: Decimal = Decimal::from_parts(8760, 0, 0, false, 0);

/// 渲染三块看板。任何一块失败都降级为“不可用（原因）”，不影响其他块。
pub async fn render(adapter: &Adapter, cfg: &Config) -> Vec<String> {
    let mut lines = vec![
        format!("network: {NETWORK}"),
        format!("account: {}", cfg.account_address),
    ];
    let user: Result<Address, String> = cfg
        .account_address()
        .map_err(|e| format!("账户地址非法: {e}"));
    let account_request = async {
        match &user {
            Ok(u) => adapter.account(*u).await.map_err(|e| e.to_string()),
            Err(e) => Err(e.clone()),
        }
    };
    let funding_request = async {
        match &user {
            Ok(u) => {
                let since = now_epoch_ms()
                    .map_err(|e| e.to_string())?
                    .saturating_sub(48 * 3_600_000);
                adapter
                    .user_funding_since(*u, since)
                    .await
                    .map_err(|e| e.to_string())
            }
            Err(e) => Err(e.clone()),
        }
    };
    let (account, hype_ctx, btc_ctx, funding_hist, dynamics) = tokio::join!(
        account_request,
        adapter.asset_ctx(LONG_COIN),
        adapter.asset_ctx(SHORT_COIN),
        funding_request,
        block_dynamics(adapter, cfg),
    );
    let hype_ctx = hype_ctx.map_err(|e| e.to_string());
    let btc_ctx = btc_ctx.map_err(|e| e.to_string());

    lines.extend(block_positions(
        cfg,
        account.as_ref(),
        hype_ctx.as_ref(),
        btc_ctx.as_ref(),
    ));
    lines.extend(block_funding(
        account.as_ref(),
        hype_ctx.as_ref(),
        btc_ctx.as_ref(),
        funding_hist.as_ref(),
    ));
    lines.extend(dynamics);
    lines.push(format!(
        "数据时间: {}（单块标注不可用时，其余块不受影响）",
        now_utc()
    ));
    lines
}

fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

fn fmt_pct(x: Decimal, dp: u32) -> String {
    (x * Decimal::ONE_HUNDRED).round_dp(dp).to_string()
}

// ---------- 块 1：仓位 ----------

fn block_positions(
    cfg: &Config,
    account: Result<&AccountView, &String>,
    hype_ctx: Result<&AssetCtxView, &String>,
    btc_ctx: Result<&AssetCtxView, &String>,
) -> Vec<String> {
    let ctx_of = |coin: &str| -> Result<&AssetCtxView, String> {
        match coin {
            LONG_COIN => hype_ctx.map_err(|e| e.clone()),
            _ => btc_ctx.map_err(|e| e.clone()),
        }
    };
    let view = match account {
        Ok(v) => v,
        Err(e) => {
            return vec![
                "\n== 仓位 ==".to_string(),
                format!("不可用（账户查询失败: {e}）"),
            ];
        }
    };
    let mut out = vec![format!("== 仓位 ==  权益 {} USDC", view.account_value)];

    let mut notional = Vec::new();
    let mut prices_missing = false;
    for coin in [LONG_COIN, SHORT_COIN] {
        match view.positions.iter().find(|p| p.coin == coin) {
            Some(p) => {
                let mark = match ctx_of(coin) {
                    Ok(c) if c.mark_px > Decimal::ZERO => c.mark_px,
                    result => {
                        let e = result.err().unwrap_or_else(|| "标记价格非正数".into());
                        prices_missing = true;
                        notional.push(Decimal::ZERO);
                        out.push(format!(
                            "  {coin} 持仓 {}，当前名义和名义/权益不可用（{e}）",
                            p.size
                        ));
                        continue;
                    }
                };
                let ntl = (p.size.abs() * mark).round_dp(2);
                let lev = if view.account_value <= Decimal::ZERO {
                    "不可用（权益非正数）".into()
                } else {
                    (ntl / view.account_value).round_dp(3).to_string()
                };
                notional.push(ntl);
                let mark_note = if mark.is_zero() {
                    "（markPx 不可用）"
                } else {
                    ""
                };
                out.push(format!(
                    "  {} {}  {} (当前名义 {} USD{mark_note}, entry {}, 名义/权益 {}, uPNL {})",
                    coin,
                    if p.size > Decimal::ZERO { "多" } else { "空" },
                    p.size,
                    ntl,
                    p.entry_px
                        .map(|x| x.to_string())
                        .unwrap_or_else(|| "n/a".into()),
                    lev,
                    p.unrealized_pnl
                ));
            }
            None => {
                notional.push(Decimal::ZERO);
                out.push(format!("  {coin}  无持仓"));
            }
        }
    }
    let (nl, ns) = (notional[0], notional[1]);
    let pair_leg = if prices_missing {
        "名义比及漂移不可用（行情缺失，实际持仓见上）".to_string()
    } else {
        match (nl > Decimal::ZERO, ns > Decimal::ZERO) {
            (true, true) => {
                let drift = ((nl - ns).abs() / ((nl + ns) / Decimal::TWO) * Decimal::ONE_HUNDRED)
                    .round_dp(2);
                format!("名义比 {} (漂移 {drift}%)", (nl / ns).round_dp(4))
            }
            (false, false) => "无 pair 仓位".to_string(),
            _ => "仅单腿敞口（另一腿为零）".to_string(),
        }
    };
    let action = match PairStore::new(cfg.receipt_dir.clone()).load() {
        Ok(Some(st))
            if st.account.parse::<Address>().ok() != cfg.account_address().ok()
                || st.network != NETWORK =>
        {
            "状态文件属于其他账户或网络".into()
        }
        Ok(Some(st))
            if matches!(
                st.phase,
                crate::pair::Phase::Closed | crate::pair::Phase::Failed
            ) =>
        {
            format!("无（上次动作 {}）", st.phase)
        }
        Ok(Some(st)) => format!(
            "未完成动作 {}（phase={}, action={}）",
            st.action_id, st.phase, st.action
        ),
        Ok(None) => "无".to_string(),
        Err(e) => format!("状态文件不可读（{e}）"),
    };
    let orders = view
        .open_orders
        .iter()
        .filter(|o| o.coin == LONG_COIN || o.coin == SHORT_COIN)
        .count();
    out.push(format!(
        "  {pair_leg}; 未完成动作 {action}; pair 挂单数 {orders}"
    ));
    out
}

// ---------- 块 2：资金费 ----------

fn block_funding(
    account: Result<&AccountView, &String>,
    hype_ctx: Result<&AssetCtxView, &String>,
    btc_ctx: Result<&AssetCtxView, &String>,
    funding_hist: Result<&Vec<FundingPaymentView>, &String>,
) -> Vec<String> {
    let mut out = block_current_funding(account, hype_ctx, btc_ctx);
    // 近 3 次账户实际收付（userFunding，不是市场费率历史）。
    match funding_hist {
        Ok(entries) => {
            let net: Decimal = entries
                .iter()
                .filter(|e| e.coin == LONG_COIN || e.coin == SHORT_COIN)
                .map(|e| e.usdc)
                .sum();
            out.push(format!(
                "  过去 48 小时 pair 实际资金费净收付: {net} USDC（正为收，负为付）"
            ));
            let recent: Vec<_> = entries
                .iter()
                .filter(|e| e.coin == LONG_COIN || e.coin == SHORT_COIN)
                .rev()
                .take(3)
                .collect();
            if recent.is_empty() {
                out.push("  近 3 次账户实际收付: 无数据（过去 48 小时无资金费收付记录）".into());
            } else {
                for e in recent {
                    let t = DateTime::<Utc>::from_timestamp_millis(e.time_ms as i64)
                        .map(|d| d.format("%Y-%m-%d %H:%M UTC").to_string())
                        .unwrap_or_else(|| format!("{}ms", e.time_ms));
                    out.push(format!(
                        "  收付: {t} {} {} USDC (费率 {}%/h)",
                        e.coin,
                        e.usdc,
                        fmt_pct(e.funding_rate, 5)
                    ));
                }
            }
        }
        Err(e) => out.push(format!("  近 3 次账户实际收付: 不可用（{e}）")),
    }
    out
}
fn block_current_funding(
    account: Result<&AccountView, &String>,
    hype_ctx: Result<&AssetCtxView, &String>,
    btc_ctx: Result<&AssetCtxView, &String>,
) -> Vec<String> {
    let mut out = vec!["\n== 资金费 ==  (每小时结算，正费率多方付空方)".to_string()];
    for (coin, ctx) in [(LONG_COIN, hype_ctx), (SHORT_COIN, btc_ctx)] {
        match ctx {
            Ok(ctx) => out.push(format!(
                "  {coin} 当前 {}%/h（简单年化 {}%）",
                fmt_pct(ctx.funding, 5),
                fmt_pct(ctx.funding * HOURS_PER_YEAR, 2)
            )),
            Err(e) => out.push(format!("  {coin} 当前费率: 不可用（{e}）")),
        }
    }
    let (Ok(hype), Ok(btc)) = (hype_ctx, btc_ctx) else {
        out.push("  日净收付预估: 不可用（两腿费率/预言机价格未齐全）".into());
        return out;
    };

    // 按当前持仓与费率不变的日净收付预估：net = −Σ(szi × oraclePx × r) × 24（文档 §4）。
    match account {
        Ok(view) => {
            let szi_of = |coin: &str| -> Decimal {
                view.positions
                    .iter()
                    .find(|p| p.coin == coin)
                    .map(|p| p.size)
                    .unwrap_or(Decimal::ZERO)
            };
            let h = szi_of(LONG_COIN);
            let b = szi_of(SHORT_COIN);
            if h.is_zero() && b.is_zero() {
                out.push("  按当前费率和持仓不变预估: 无持仓，不适用".into());
            } else if hype.oracle_px <= Decimal::ZERO || btc.oracle_px <= Decimal::ZERO {
                out.push("  日净收付预估: 不可用（预言机价格非正数）".into());
            } else {
                let hourly = -(h * hype.oracle_px * hype.funding + b * btc.oracle_px * btc.funding);
                let daily = (hourly * Decimal::from(24)).round_dp(4);
                let direction = if daily >= Decimal::ZERO { "收" } else { "付" };
                out.push(format!(
                    "  按当前费率和持仓不变预估: 日净{direction} {} USDC（当前费率不变的估算，不代表未来实际收付）",
                    daily.abs()
                ));
            }
        }
        Err(_) => out.push("  按当前费率和持仓不变预估: 不可用（账户查询失败）".into()),
    }

    out
}

// ---------- 块 3：动态 ----------

async fn block_dynamics(adapter: &Adapter, cfg: &Config) -> Vec<String> {
    let mut out = vec!["\n== 动态 ==".to_string()];
    out.push(unlock_line(cfg));
    let (ratio, updates) = tokio::join!(ratio_line(adapter), announcements::render());
    out.push(ratio);
    out.extend(updates);
    out
}

fn unlock_line(cfg: &Config) -> String {
    unlock_line_at(cfg, Utc::now().date_naive())
}

fn unlock_line_at(cfg: &Config, today: chrono::NaiveDate) -> String {
    use chrono::NaiveDate;
    if cfg.unlock_anchors.is_empty() {
        return "  解锁锚点: 未配置（在配置文件中添加 [[unlock_anchors]]，附来源链接与核实日期）"
            .into();
    }
    let mut parsed: Vec<(NaiveDate, &str, &str, &str)> = cfg
        .unlock_anchors
        .iter()
        .filter_map(|a| {
            NaiveDate::parse_from_str(&a.date_utc, "%Y-%m-%d")
                .ok()
                .filter(|_| {
                    NaiveDate::parse_from_str(&a.verified_utc, "%Y-%m-%d")
                        .is_ok_and(|verified| verified <= today)
                        && reqwest::Url::parse(&a.source_url).is_ok_and(|url| {
                            matches!(url.scheme(), "http" | "https") && url.host_str().is_some()
                        })
                        && !a.label.trim().is_empty()
                })
                .map(|d| {
                    (
                        d,
                        a.label.as_str(),
                        a.source_url.as_str(),
                        a.verified_utc.as_str(),
                    )
                })
        })
        .collect();
    if parsed.is_empty() {
        return "  解锁锚点: 日期或核实信息无法解析/不完整（需有效日期、来源链接及非未来核实日期）；实际申领未核实".into();
    }
    let omitted = cfg.unlock_anchors.len() - parsed.len();
    parsed.sort_by_key(|(d, _, _, _)| *d);
    parsed.dedup();
    let upcoming: Vec<_> = parsed
        .iter()
        .filter(|(date, _, _, _)| {
            let days = (*date - today).num_days();
            (0..=7).contains(&days)
        })
        .collect();
    let summary = if !upcoming.is_empty() {
        let mut lines = vec![
            "  解锁锚点: 本周需核对 announced claim（UTC 日历；日历不能证明实际申领发生）".into(),
        ];
        for (d, label, src, verified) in upcoming {
            lines.push(format!(
                "    {d}（{} 天后）{label}（来源 {src}，{verified} 人工标记核实）",
                (*d - today).num_days()
            ));
        }
        lines.join("\n")
    } else {
        match parsed.iter().find(|(d, _, _, _)| *d >= today) {
            Some((d, label, src, verified)) => format!(
                "  解锁锚点: 下次 {d}（{} 天后）{label}（来源 {src}，{verified} 人工标记核实）",
                (*d - today).num_days()
            ),
            None => "  解锁锚点: 配置中的日期均已过，请核实并更新来源".into(),
        }
    };
    format!(
        "{summary}；实际申领未核实{}",
        if omitted > 0 {
            format!("；另有 {omitted} 条配置无效，未用于提醒")
        } else {
            String::new()
        }
    )
}

/// HYPE/BTC 比价与 30MA（已收盘日线，按 UTC 日期对齐；缺数据显示不可用，不补零）。
async fn ratio_line(adapter: &Adapter) -> String {
    let need = 32; // 对齐后保证有 ≥30 个共同收盘日
    let (h, b) = match tokio::join!(
        adapter.daily_closes(LONG_COIN, need),
        adapter.daily_closes(SHORT_COIN, need),
    ) {
        (Ok(h), Ok(b)) => (h, b),
        (h, b) => {
            let why = match (h, b) {
                (Err(e), _) => format!("{LONG_COIN} 日线查询失败: {e}"),
                (_, Err(e)) => format!("{SHORT_COIN} 日线查询失败: {e}"),
                _ => unreachable!(),
            };
            return format!("  HYPE/BTC: 不可用（{why}）");
        }
    };
    let now_ms = match now_epoch_ms() {
        Ok(t) => t,
        Err(e) => return format!("  HYPE/BTC: 不可用（{e}）"),
    };
    ratio_from_closes(h, b, now_ms)
}

fn ratio_from_closes(h: Vec<(u64, Decimal)>, b: Vec<(u64, Decimal)>, now_ms: u64) -> String {
    match ratio_metrics(h, b, now_ms) {
        Ok(m) => {
            let pos = if m.vs_ma30_pct >= Decimal::ZERO {
                "上方"
            } else {
                "下方"
            };
            format!(
                "  HYPE/BTC: {}（截至 {} UTC 收盘，30MA {pos} {}%）",
                m.current,
                m.as_of_date,
                m.vs_ma30_pct.abs()
            )
        }
        Err(why) => format!("  HYPE/BTC: 不可用（{why}）"),
    }
}

/// HYPE/BTC 比价的结构化指标（status --json 看板契约 §4.1 ratio 块）。
pub(crate) struct RatioMetrics {
    /// 当前共同收盘日比值（未舍入）。
    pub current: Decimal,
    /// 30MA（未舍入）。
    pub ma30: Decimal,
    /// 当前相对 30MA 的带符号百分数数值（2 位小数；2.06 = 上方 2.06%）。
    pub vs_ma30_pct: Decimal,
    /// 比价使用的 UTC 收盘日（YYYY-MM-DD）。
    pub as_of_date: String,
}

/// 日线对齐后的结构化比价指标；数据不足/过期/不连续时返回中文原因（与文本看板同口径）。
pub(crate) fn ratio_metrics(
    h: Vec<(u64, Decimal)>,
    b: Vec<(u64, Decimal)>,
    now_ms: u64,
) -> Result<RatioMetrics, String> {
    let Some(now) = i64::try_from(now_ms)
        .ok()
        .and_then(DateTime::<Utc>::from_timestamp_millis)
    else {
        return Err("当前时间非法".into());
    };
    let closes_to_map = |v: Vec<(u64, Decimal)>| -> std::collections::BTreeMap<String, Decimal> {
        v.into_iter()
            .filter(|(t, _)| *t <= now_ms) // 仅已收盘日
            .filter_map(|(t, c)| {
                DateTime::<Utc>::from_timestamp_millis(t as i64)
                    .map(|d| (d.format("%Y-%m-%d").to_string(), c))
            })
            .collect()
    };
    let hm = closes_to_map(h);
    let bm = closes_to_map(b);
    let mut ratios: Vec<(String, Decimal)> = hm
        .iter()
        .filter_map(|(d, hc)| {
            bm.get(d)
                .filter(|bc| **bc > Decimal::ZERO && *hc > Decimal::ZERO)
                .map(|bc| (d.clone(), *hc / *bc))
        })
        .collect();
    ratios.sort();
    let today = now.date_naive();
    let expected_date = (today - chrono::Duration::days(1)).to_string();
    if ratios.last().map(|(d, _)| d.as_str()) != Some(expected_date.as_str()) {
        let latest = ratios.last().map(|(d, _)| d.as_str()).unwrap_or("无");
        return Err(format!(
            "日线未更新，最近共同收盘日 {latest}，需要 {expected_date} UTC"
        ));
    }
    let (cur, ma) = ratio_and_ma(&ratios)?;
    let pct = ((cur - ma) / ma * Decimal::ONE_HUNDRED).round_dp(2);
    Ok(RatioMetrics {
        current: cur,
        ma30: ma,
        vs_ma30_pct: pct,
        as_of_date: expected_date,
    })
}

/// 比价序列的当前值与 30MA（已收盘日线、按 UTC 日期对齐后的共同比值）。
/// 不足 30 根不补零，直接报错（PHASE2_PLAN §4）。
fn ratio_and_ma(ratios: &[(String, Decimal)]) -> Result<(Decimal, Decimal), String> {
    if ratios.len() < 30 {
        return Err(format!("已收盘共同日线只有 {} 根，不足 30", ratios.len()));
    }
    let recent = &ratios[ratios.len() - 30..];
    let mut previous = None;
    for (date, ratio) in recent {
        let day = chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d")
            .map_err(|_| format!("日线日期非法: {date}"))?;
        if previous.is_some_and(|p| day - p != chrono::Duration::days(1)) {
            return Err("最近 30 个共同收盘日不连续，无法计算 30 日均线".into());
        }
        if *ratio <= Decimal::ZERO {
            return Err(format!("{date} 收盘比价非正数"));
        }
        previous = Some(day);
    }
    let tail: Vec<Decimal> = ratios.iter().rev().take(30).map(|(_, r)| *r).collect();
    let ma = tail.iter().sum::<Decimal>() / Decimal::from(30);
    let cur = ratios.last().map(|(_, r)| *r).unwrap_or(Decimal::ZERO);
    if ma.is_zero() {
        return Err("30MA 为零".into());
    }
    Ok((cur, ma))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::UnlockAnchor;
    use rust_decimal::Decimal;
    use std::path::PathBuf;

    fn cfg_with_anchors(anchors: Vec<UnlockAnchor>) -> Config {
        Config {
            account_address: "0x7F2120F612B78D579197762196B8A202AB654096".into(),
            secret_file: PathBuf::from("/tmp/agent.key"),
            max_notional_usd: Decimal::new(100, 0),
            leg_fraction: Decimal::new(7, 1),
            unlock_anchors: anchors,
            receipt_dir: PathBuf::from("/tmp"),
        }
    }

    fn anchor(days_from_today: i64, label: &str) -> UnlockAnchor {
        let d = chrono::Utc::now().date_naive() + chrono::Duration::days(days_from_today);
        UnlockAnchor {
            date_utc: d.format("%Y-%m-%d").to_string(),
            label: label.into(),
            source_url: "https://example.com/src".into(),
            verified_utc: "2026-10-01".into(),
        }
    }

    #[test]
    fn unlock_line_states() {
        // 未配置
        let s = unlock_line(&cfg_with_anchors(vec![]));
        assert!(s.contains("未配置"), "{s}");
        // 7 天内 → 提醒核对
        let s = unlock_line(&cfg_with_anchors(vec![anchor(3, "团队解锁")]));
        assert!(s.contains("本周需核对"), "{s}");
        assert!(s.contains("example.com/src"), "{s}");
        assert!(s.contains("2026-10-01"), "应显示核实日期: {s}");
        // 7 天外 → 普通预告
        let s = unlock_line(&cfg_with_anchors(vec![anchor(30, "投资者解锁")]));
        assert!(s.contains("30 天后"), "{s}");
        // 日期全非法
        let bad = UnlockAnchor {
            date_utc: "not-a-date".into(),
            label: "x".into(),
            source_url: "u".into(),
            verified_utc: "v".into(),
        };
        let s = unlock_line(&cfg_with_anchors(vec![bad]));
        assert!(s.contains("无法解析"), "{s}");
    }

    #[test]
    fn ratio_and_ma_rules() {
        // 32 根，比值恒为 0.0005；最后一根抬高 → 当前值在 30MA 上方
        let mut ratios: Vec<(String, Decimal)> = (0..32)
            .map(|i| {
                let d = chrono::NaiveDate::from_ymd_opt(2026, 9, 1).unwrap()
                    + chrono::Duration::days(i);
                (d.to_string(), Decimal::new(5, 4))
            })
            .collect();
        *ratios.last_mut().unwrap() = ("2026-10-02".into(), Decimal::new(6, 4));
        let (cur, ma) = ratio_and_ma(&ratios).unwrap();
        assert_eq!(cur, Decimal::new(6, 4));
        // 29 根 0.0005 + 1 根 0.0006 → MA = (29*5+6)/30 ×1e-4
        let expect =
            (Decimal::new(29, 0) * Decimal::new(5, 4) + Decimal::new(6, 4)) / Decimal::from(30);
        assert_eq!(ma, expect);
        assert!(cur > ma);

        // 不足 30 根 → 报错，不补零
        let short: Vec<(String, Decimal)> = ratios.iter().take(10).cloned().collect();
        assert!(ratio_and_ma(&short).is_err());
    }

    #[test]
    fn funding_annualized_is_hourly_based() {
        // 文档口径：小时费率 × 24 × 365。0.01%/h ≈ 87.6%/年
        let r = Decimal::new(1, 4);
        let annual = fmt_pct(r * HOURS_PER_YEAR, 2);
        assert_eq!(annual, "87.60");
    }

    #[test]
    fn funding_history_survives_market_failure() {
        let err = "行情超时".to_string();
        let entries = vec![FundingPaymentView {
            coin: LONG_COIN.into(),
            time_ms: 1_760_000_000_000,
            usdc: Decimal::new(-23, 2),
            funding_rate: Decimal::new(1, 4),
        }];
        let text = block_funding(Err(&err), Err(&err), Err(&err), Ok(&entries)).join("\n");
        assert!(text.contains("行情超时"));
        assert!(text.contains("HYPE -0.23 USDC"));
        assert!(!text.contains("无数据"));
        assert!(text.contains("实际资金费净收付: -0.23 USDC"));
        let btc = AssetCtxView {
            funding: Decimal::new(1, 4),
            mark_px: Decimal::from(80000),
            oracle_px: Decimal::from(80000),
        };
        let text = block_funding(Err(&err), Err(&err), Ok(&btc), Ok(&entries)).join("\n");
        assert!(text.contains("BTC 当前 0.0100%/h"), "{text}");
        assert!(text.contains("HYPE -0.23 USDC"));
        assert!(text.contains("日净收付预估: 不可用"));
    }

    #[test]
    fn unlock_lists_all_upcoming_events_once() {
        let today = chrono::NaiveDate::from_ymd_opt(2026, 10, 16).unwrap();
        let event = |date: &str, label: &str| UnlockAnchor {
            date_utc: date.into(),
            label: label.into(),
            source_url: "https://example.com/announcement".into(),
            verified_utc: "2026-10-15".into(),
        };
        let text = unlock_line_at(
            &cfg_with_anchors(vec![
                event("2026-10-23", "七天边界"),
                event("2026-10-16", "当天事件"),
                event("2026-10-18", "第二事件"),
                event("2026-10-18", "第二事件"),
                event("2026-10-24", "更晚事件"),
            ]),
            today,
        );
        assert!(text.contains("当天事件"));
        assert!(text.contains("七天边界"));
        assert_eq!(text.matches("第二事件").count(), 1);
        assert!(!text.contains("更晚事件"));
        assert!(text.find("当天事件").unwrap() < text.find("第二事件").unwrap());
        assert!(text.contains("实际申领未核实"));
    }

    #[test]
    fn ratio_requires_fresh_contiguous_closed_days() {
        let now = chrono::NaiveDate::from_ymd_opt(2026, 10, 16)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap()
            .and_utc()
            .timestamp_millis() as u64;
        let today_start = now - 12 * 3_600_000;
        let day_ms = 86_400_000;
        let closes: Vec<_> = (0..32)
            .rev()
            .map(|i| (today_start - i * day_ms - 1, Decimal::ONE))
            .collect();
        let good = ratio_from_closes(closes.clone(), closes.clone(), now);
        assert!(good.contains("截至 2026-10-15 UTC 收盘"), "{good}");
        assert!(!good.contains("不可用"));
        let mut gap = closes.clone();
        gap.remove(20); // Still 31 common bars, but not 30 consecutive days.
        assert!(ratio_from_closes(gap.clone(), gap, now).contains("不连续"));
        let mut stale = closes.clone();
        stale.pop(); // 31 consecutive bars ending a day too early.
        assert!(ratio_from_closes(stale.clone(), stale, now).contains("日线未更新"));
        let mut future = closes.clone();
        future.push((today_start + day_ms - 1, Decimal::from(100)));
        assert_eq!(ratio_from_closes(future.clone(), future, now), good);
        let mut invalid = closes.clone();
        invalid[20].1 = Decimal::ZERO;
        assert!(ratio_from_closes(invalid, closes, now).contains("不连续"));
    }

    #[test]
    fn unlock_invalid_verification_is_visible() {
        let mut bad = anchor(1, "未核实事件");
        bad.verified_utc = "2999-01-01".into();
        let text = unlock_line(&cfg_with_anchors(vec![bad.clone(), anchor(3, "有效配置")]));
        assert!(text.contains("有效配置"));
        assert!(text.contains("1 条配置无效"));
        assert!(text.contains("实际申领未核实"));
        bad.verified_utc = "2026-10-01".into();
        bad.source_url = "".into();
        assert!(unlock_line(&cfg_with_anchors(vec![bad])).contains("不完整"));
    }
    #[test]
    fn missing_prices_never_erase_real_positions() {
        let cfg = cfg_with_anchors(Vec::new());
        let account = AccountView {
            account_value: Decimal::new(100, 0),
            available_margin: Decimal::new(80, 0),
            query_time_utc: String::new(),
            open_orders: Vec::new(),
            positions: vec![crate::hyperliquid::PositionView {
                coin: LONG_COIN.into(),
                side: "long",
                size: Decimal::ONE,
                entry_px: Some(Decimal::new(40, 0)),
                unrealized_pnl: Decimal::ZERO,
            }],
        };
        let error = "行情请求失败".to_string();
        let text = block_positions(&cfg, Ok(&account), Err(&error), Err(&error)).join("\n");
        assert!(text.contains("HYPE 持仓 1"));
        assert!(text.contains("名义比及漂移不可用"));
        assert!(!text.contains("无 pair 仓位"));
        assert!(!text.contains("当前名义 0"));
        let invalid = AssetCtxView {
            funding: Decimal::new(1, 4),
            mark_px: Decimal::ZERO,
            oracle_px: Decimal::ZERO,
        };
        let text = block_positions(&cfg, Ok(&account), Ok(&invalid), Ok(&invalid)).join("\n");
        assert!(text.contains("名义比及漂移不可用"));
        assert!(!text.contains("无 pair 仓位"));
        let text = block_current_funding(Ok(&account), Ok(&invalid), Ok(&invalid)).join("\n");
        assert!(text.contains("预言机价格非正数"));
    }
}
