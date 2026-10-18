//! v2 多 pair 快照采集与 DTO（多 pair 方案 §5.3 契约、§6 采集口径）。
//!
//! - 顶层 `schema_version: 2`；数值一律 DecimalString（String/Option<String>），
//!   null 绝不补 0；分源时间如实记录，未尝试的来源 queried_at=null。
//! - 同次快照共享已取得的源数据（clearinghouseState 同时服务 account/positions/funding；
//!   market 同时服务 positions/funding/ratio），块间并发、单源 8s 期限。
//! - 全程只读：只发 /info 类请求，不装载签名器、不触碰 v1 PairStore。

use std::collections::{BTreeMap, HashSet};
use std::time::Duration;

use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use hypersdk::Address;
use hypersdk::hypercore::types::ClearinghouseState;
use rust_decimal::Decimal;
use serde::Serialize;
use tokio::time::timeout;

use super::client::InfoClient;
use super::config::{DashboardPair, Network, PairDescriptor, UnlockAnchor};

pub const SCHEMA_VERSION_V2: u32 = 2;
const SOURCE_TIMEOUT: Duration = Duration::from_secs(8);
const CANDLE_LOOKBACK_DAYS: u32 = 121;
const RATIO_DISPLAY_DAYS: i64 = 90;
const MA_WINDOW: i64 = 30;
const FUNDING_MAX_PAGES: usize = 1000;
const HOURS_PER_DAY: Decimal = Decimal::from_parts(24, 0, 0, false, 0);
const HOURS_PER_YEAR: Decimal = Decimal::from_parts(8760, 0, 0, false, 0);
const HOUR_MS: u64 = 3_600_000;
const UNLOCKS_NOTE: &str = "日历预告不等于实际申领已发生";
const EPOCH_RFC3339: &str = "1970-01-01T00:00:00Z";

// ---------- DTO ----------

#[derive(Debug, Serialize)]
pub struct PairSnapshot {
    pub schema_version: u32,
    pub config_revision: String,
    pub queried_at: String,
    pub pair: PairDescriptor,
    pub blocks: Blocks,
}

#[derive(Debug, Serialize)]
pub struct Blocks {
    pub account: AccountBlock,
    pub positions: PositionsBlock,
    pub funding: FundingBlock,
    pub ratio: RatioBlock,
    /// 仅 unlocks_enabled=false 时为 null；其余情况块必须存在。
    pub unlocks: Option<UnlocksBlock>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockStatus {
    Ok,
    Partial,
    Unavailable,
}

#[derive(Debug, Clone, Serialize)]
pub struct SourceQuery {
    status: &'static str,
    /// null 仅表示未尝试（例如账户未配置）。
    queried_at: Option<String>,
}

impl SourceQuery {
    fn ok(queried_at: String) -> Self {
        Self {
            status: "ok",
            queried_at: Some(queried_at),
        }
    }
    fn unavailable(queried_at: Option<String>) -> Self {
        Self {
            status: "unavailable",
            queried_at,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EquityScope {
    PerpDex,
    /// 账户级权益口径（本轮不自行计算；保留枚举以匹配契约类型）。
    #[allow(dead_code)]
    Account,
    Unavailable,
}

#[derive(Debug, Serialize)]
pub struct AccountSources {
    mode: SourceQuery,
    balances: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct AccountBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: AccountSources,
    /// 官方 userAbstraction 原始枚举；未知值原样保留并附诊断。
    pub account_mode: Option<String>,
    pub equity: Option<String>,
    pub equity_scope: EquityScope,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct PositionsSources {
    positions: SourceQuery,
    orders: SourceQuery,
    market: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct PositionItem {
    pub coin: String,
    pub side: &'static str,
    pub size: String,
    pub entry_px: Option<String>,
    pub mark_px: Option<String>,
    pub notional: Option<String>,
    pub unrealized_pnl: Option<String>,
    pub margin_mode: Option<&'static str>,
    pub margin_used: Option<String>,
    pub liquidation_px: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct PositionsBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: PositionsSources,
    pub items: Option<Vec<PositionItem>>,
    pub position_state: &'static str,
    /// 指定两腿实际持仓行的未实现盈亏合计；单腿取该行，确认空仓为 0。
    pub unrealized_pnl: Option<String>,
    pub notional_ratio: Option<String>,
    pub ratio_drift_pct: Option<String>,
    pub open_orders: Option<u64>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct FundingSources {
    positions: SourceQuery,
    market: SourceQuery,
    user_funding: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct DailyEstimate {
    pub direction: &'static str,
    pub amount: String,
}

#[derive(Debug, Serialize)]
pub struct PaymentItem {
    pub time: String,
    pub coin: String,
    pub usdc: String,
    pub rate: String,
}

#[derive(Debug, Serialize)]
pub struct FundingBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: FundingSources,
    /// key 恰好是配置完整 coins。
    pub hourly_rates: BTreeMap<String, Option<String>>,
    pub annualized_pct: BTreeMap<String, Option<String>>,
    pub daily_estimate: Option<DailyEstimate>,
    pub window_start: String,
    pub window_end: String,
    pub window_complete: bool,
    pub actual_48h_net: Option<String>,
    pub recent_payments: Option<Vec<PaymentItem>>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct RatioSources {
    market: SourceQuery,
    candles_first: SourceQuery,
    candles_second: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct RatioPoint {
    pub date: String,
    pub first_close: Option<String>,
    pub second_close: Option<String>,
    pub ratio: Option<String>,
    pub ma30: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct RatioBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: RatioSources,
    /// 两腿同次市场响应的正 mark 相除。
    pub live: Option<String>,
    pub latest_close: Option<String>,
    pub as_of_date: Option<String>,
    pub ma30: Option<String>,
    pub vs_ma30_pct: Option<String>,
    /// 最近 90 个日历日；缺日保留 date、价格/比价/MA 为 null（断线）。全部失败为 null。
    pub points: Option<Vec<RatioPoint>>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct UnlocksSources {
    config: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct AnchorItem {
    pub date: String,
    pub days_ahead: i64,
    pub label: String,
    pub source_url: String,
    pub verified_utc: String,
}

#[derive(Debug, Serialize)]
pub struct UnlocksBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: UnlocksSources,
    pub anchors: Vec<AnchorItem>,
    pub note: &'static str,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct AnnouncementsSnapshot {
    pub schema_version: u32,
    pub queried_at: String,
    pub block: crate::snapshot::AnnouncementsBlock,
}

// ---------- 工具 ----------

struct Timed<T> {
    value: Option<T>,
    reason: Option<String>,
    at: Option<String>,
}

impl<T> Timed<T> {
    fn not_attempted() -> Self {
        Self {
            value: None,
            reason: None,
            at: None,
        }
    }
    fn query(&self) -> SourceQuery {
        match (&self.value, &self.at) {
            (Some(_), Some(at)) => SourceQuery::ok(at.clone()),
            _ => SourceQuery::unavailable(self.at.clone()),
        }
    }
}

async fn timed<T, E>(
    fut: impl std::future::Future<Output = Result<T, E>>,
    describe: impl Fn(&E) -> String,
) -> Timed<T> {
    match timeout(SOURCE_TIMEOUT, fut).await {
        Ok(Ok(v)) => Timed {
            value: Some(v),
            reason: None,
            at: Some(now_rfc3339()),
        },
        Ok(Err(e)) => Timed {
            value: None,
            reason: Some(describe(&e)),
            at: Some(now_rfc3339()),
        },
        Err(_) => Timed {
            value: None,
            reason: Some("查询超时".to_string()),
            at: Some(now_rfc3339()),
        },
    }
}

fn now_rfc3339() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn rfc3339_of(ms: u64) -> String {
    DateTime::<Utc>::from_timestamp_millis(ms as i64)
        .map(|d| d.to_rfc3339_opts(SecondsFormat::Secs, true))
        .unwrap_or_else(|| EPOCH_RFC3339.to_string())
}

fn block_status(sources_ok: &[bool], diagnostics: &[String]) -> BlockStatus {
    if !sources_ok.iter().any(|ok| *ok) {
        BlockStatus::Unavailable
    } else if sources_ok.iter().all(|ok| *ok) && diagnostics.is_empty() {
        BlockStatus::Ok
    } else {
        BlockStatus::Partial
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MarketListing {
    Missing,
    Active,
    Delisted,
}

#[derive(Debug, Clone, Copy)]
struct AssetCtx {
    funding: Option<Decimal>,
    mark_px: Option<Decimal>,
    oracle_px: Option<Decimal>,
    listing: MarketListing,
}

impl AssetCtx {
    fn is_active(&self) -> bool {
        self.listing == MarketListing::Active
    }
}

/// 从 metaAndAssetCtxs 原始响应解析指定完整 coin 的 ctx，保留字段缺失与下架状态。
fn parse_asset_ctx(value: &serde_json::Value, coin: &str) -> AssetCtx {
    let empty = AssetCtx {
        funding: None,
        mark_px: None,
        oracle_px: None,
        listing: MarketListing::Missing,
    };
    let Some(arr) = value.as_array().filter(|a| a.len() == 2) else {
        return empty;
    };
    let Some(universe) = arr[0].get("universe").and_then(|u| u.as_array()) else {
        return empty;
    };
    let Some(ctxs) = arr[1].as_array() else {
        return empty;
    };
    let Some(idx) = universe
        .iter()
        .position(|m| m.get("name").and_then(|n| n.as_str()) == Some(coin))
    else {
        return empty;
    };
    let listing = if universe[idx]
        .get("isDelisted")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        MarketListing::Delisted
    } else {
        MarketListing::Active
    };
    let Some(ctx) = ctxs.get(idx) else {
        return AssetCtx { listing, ..empty };
    };
    let dec = |key: &str| -> Option<Decimal> {
        ctx.get(key)
            .and_then(|x| x.as_str())
            .and_then(|s| s.parse::<Decimal>().ok())
    };
    AssetCtx {
        funding: dec("funding"),
        mark_px: dec("markPx"),
        oracle_px: dec("oraclePx"),
        listing,
    }
}

// ---------- 资金费分页（原始毫秒 + 含网络/账户/coin/时间/源标识的去重键） ----------

struct FundingHistory {
    records: Vec<FundingRecord>,
    complete: bool,
    error: Option<String>,
}

struct FundingRecord {
    coin: String,
    time_ms: u64,
    usdc: Decimal,
    rate: Decimal,
}

/// 分页获取 [start, end] 内 userFunding；中断/截断保留已取得部分并标 complete=false。
async fn paginate_funding(
    client: &InfoClient,
    user: Address,
    account: &str,
    network: Network,
    start_ms: u64,
    end_ms: u64,
) -> FundingHistory {
    let mut cursor = start_ms;
    let mut seen: HashSet<String> = HashSet::new();
    let mut records = Vec::new();
    let network_label = network.label();
    for _ in 0..FUNDING_MAX_PAGES {
        let page = timeout(
            SOURCE_TIMEOUT,
            client.user_funding_page(user, cursor, end_ms),
        )
        .await;
        let entries = match page {
            Ok(Ok(entries)) => entries,
            Ok(Err(e)) => {
                return FundingHistory {
                    records,
                    complete: false,
                    error: Some(e),
                };
            }
            Err(_) => {
                return FundingHistory {
                    records,
                    complete: false,
                    error: Some("查询超时".to_string()),
                };
            }
        };
        let count = entries.len();
        let mut next: Option<u64> = None;
        for entry in entries {
            if entry.time < cursor || entry.time > end_ms {
                return FundingHistory {
                    records,
                    complete: false,
                    error: Some("资金费记录超出请求区间".to_string()),
                };
            }
            next = Some(next.map_or(entry.time, |n: u64| n.max(entry.time)));
            // 去重键含网络、账户、coin、时间及源标识（hash 可能跨币种重复，不能只用它）。
            let key = format!(
                "{network_label}|{account}|{}|{}|{}",
                entry.delta.coin, entry.time, entry.hash
            );
            if seen.insert(key) {
                records.push(FundingRecord {
                    coin: entry.delta.coin,
                    time_ms: entry.time,
                    usdc: entry.delta.usdc,
                    rate: entry.delta.funding_rate,
                });
            }
        }
        if count < 500 {
            return FundingHistory {
                records,
                complete: true,
                error: None,
            };
        }
        match next {
            Some(next) if next > cursor => cursor = next,
            _ => {
                return FundingHistory {
                    records,
                    complete: false,
                    error: Some("资金费分页无法推进，历史不完整".to_string()),
                };
            }
        }
    }
    FundingHistory {
        records,
        complete: false,
        error: Some("资金费历史超过本次分页上限".to_string()),
    }
}

// ---------- 采集 ----------

pub fn pair_snapshot_json(snapshot: &PairSnapshot) -> Result<String, serde_json::Error> {
    serde_json::to_string(snapshot)
}

pub fn announcements_json(snapshot: &AnnouncementsSnapshot) -> Result<String, serde_json::Error> {
    serde_json::to_string(snapshot)
}

/// 公告模式（`--announcements --json`）：复用 v1 结构化采集，包装为 v2 顶层。
pub async fn collect_announcements() -> AnnouncementsSnapshot {
    AnnouncementsSnapshot {
        schema_version: SCHEMA_VERSION_V2,
        queried_at: now_rfc3339(),
        block: crate::snapshot::collect_announcements("https://t.me/s").await,
    }
}

/// 单个 pair 的 v2 快照。`now_ms` 由调用方固定（采集开始时刻）。
pub async fn collect_pair(
    client: &InfoClient,
    pair: &DashboardPair,
    config_revision: &str,
    now_ms: u64,
) -> PairSnapshot {
    let descriptor = &pair.descriptor;
    let generated_at = rfc3339_of(now_ms);
    let today = DateTime::<Utc>::from_timestamp_millis(now_ms as i64)
        .map(|d| d.date_naive())
        .unwrap_or_else(|| Utc::now().date_naive());
    let window_end_ms = now_ms;
    let window_start_ms = now_ms.saturating_sub(48 * HOUR_MS);
    let dex: Option<&str> = if descriptor.dex.is_empty() {
        None
    } else {
        Some(descriptor.dex.as_str())
    };
    let account: Option<Address> = descriptor
        .account_address
        .as_deref()
        .and_then(|s| s.parse().ok());

    // 共享源并发拉取；账户未配置的来源不尝试（queried_at=null）。
    let (mode, balances, orders, market, funding_history, candles_first, candles_second) = tokio::join!(
        async {
            match account {
                Some(user) => {
                    timed(client.abstraction_mode(user, dex), |e: &String| e.clone()).await
                }
                None => Timed::not_attempted(),
            }
        },
        async {
            match account {
                Some(user) => {
                    timed(client.clearinghouse_state(user, dex), |e: &String| {
                        e.clone()
                    })
                    .await
                }
                None => Timed::not_attempted(),
            }
        },
        async {
            match account {
                Some(user) => timed(client.open_orders(user, dex), |e: &String| e.clone()).await,
                None => Timed::not_attempted(),
            }
        },
        timed(client.meta_and_asset_ctxs(dex), |e: &String| e.clone()),
        async {
            match account {
                Some(user) => {
                    let history = paginate_funding(
                        client,
                        user,
                        descriptor.account_address.as_deref().unwrap_or(""),
                        descriptor.network,
                        window_start_ms,
                        window_end_ms,
                    )
                    .await;
                    let at = now_rfc3339();
                    let complete = history.complete;
                    Timed {
                        value: Some(history),
                        reason: history_error(&complete),
                        at: Some(at),
                    }
                }
                None => Timed::not_attempted(),
            }
        },
        timed(
            client.daily_closes(&descriptor.coins[0], CANDLE_LOOKBACK_DAYS),
            |e: &String| e.clone(),
        ),
        timed(
            client.daily_closes(&descriptor.coins[1], CANDLE_LOOKBACK_DAYS),
            |e: &String| e.clone(),
        ),
    );

    let ctx = market.value.as_ref().map(|v| {
        [
            parse_asset_ctx(v, &descriptor.coins[0]),
            parse_asset_ctx(v, &descriptor.coins[1]),
        ]
    });

    let account_block = build_account_block(&mode, &balances);
    let positions_block = build_positions_block(descriptor, &balances, &orders, &market);
    let funding_block = build_funding_block(
        descriptor,
        &balances,
        &market,
        &funding_history,
        window_start_ms,
        window_end_ms,
    );
    let ratio_block = build_ratio_block(
        descriptor,
        &market,
        &ctx,
        &candles_first,
        &candles_second,
        now_ms,
        today,
    );
    let unlocks_block = pair
        .unlock_anchors
        .as_ref()
        .map(|anchors| build_unlocks_block(anchors, today, generated_at.clone()));

    PairSnapshot {
        schema_version: SCHEMA_VERSION_V2,
        config_revision: config_revision.to_string(),
        queried_at: generated_at,
        pair: descriptor.clone(),
        blocks: Blocks {
            account: account_block,
            positions: positions_block,
            funding: funding_block,
            ratio: ratio_block,
            unlocks: unlocks_block,
        },
    }
}

fn history_error(complete: &bool) -> Option<String> {
    if *complete {
        None
    } else {
        Some("资金费历史获取不完整".to_string())
    }
}

fn build_account_block(mode: &Timed<String>, balances: &Timed<ClearinghouseState>) -> AccountBlock {
    let mut diagnostics = Vec::new();
    let mode_ok = mode.value.is_some();
    let balances_ok = balances.value.is_some();

    if mode.at.is_none() && balances.at.is_none() {
        diagnostics.push("账户未配置，账户数据不可用".to_string());
    } else {
        if let Some(reason) = &mode.reason {
            diagnostics.push(format!("账户模式查询失败（{reason}）"));
        }
        if let Some(reason) = &balances.reason {
            diagnostics.push(format!("账户余额查询失败（{reason}）"));
        }
    }

    let account_mode = mode.value.clone();
    let (equity, equity_scope) = match account_mode.as_deref() {
        None => (None, EquityScope::Unavailable),
        Some("default") | Some("disabled") | Some("dexAbstraction") => {
            // 标准口径：该 DEX marginSummary.accountValue 可信。
            match &balances.value {
                Some(state) => (
                    Some(state.margin_summary.account_value.to_string()),
                    EquityScope::PerpDex,
                ),
                None => {
                    diagnostics
                        .push("权益不可用（余额查询失败），持仓等独立数据不受影响".to_string());
                    (None, EquityScope::Unavailable)
                }
            }
        }
        Some(other) => {
            diagnostics.push(format!(
                "账户模式 \"{other}\" 的权益口径不适用单 DEX 余额（仅展示经验证的持仓）"
            ));
            (None, EquityScope::Unavailable)
        }
    };

    let status = block_status(&[mode_ok, balances_ok], &diagnostics);
    AccountBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: AccountSources {
            mode: mode.query(),
            balances: balances.query(),
        },
        account_mode,
        equity,
        equity_scope,
        diagnostics,
    }
}

fn build_positions_block(
    descriptor: &PairDescriptor,
    balances: &Timed<ClearinghouseState>,
    orders: &Timed<Vec<hypersdk::hypercore::types::BasicOrder>>,
    market: &Timed<serde_json::Value>,
) -> PositionsBlock {
    let mut diagnostics = Vec::new();
    let positions_ok = balances.value.is_some();
    let orders_ok = orders.value.is_some();
    let market_ok = market.value.is_some();
    let ctx = market.value.as_ref().map(|v| {
        [
            parse_asset_ctx(v, &descriptor.coins[0]),
            parse_asset_ctx(v, &descriptor.coins[1]),
        ]
    });

    if let Some(reason) = &balances.reason {
        diagnostics.push(format!("仓位查询失败（{reason}）"));
    }
    if let Some(reason) = &orders.reason {
        diagnostics.push(format!("挂单查询失败（{reason}）"));
    }

    for (i, coin) in descriptor.coins.iter().enumerate() {
        if let Some(contexts) = ctx {
            match contexts[i].listing {
                MarketListing::Missing => {
                    diagnostics.push(format!("{coin} 不在市场元数据中（可能已下架或未上市）"))
                }
                MarketListing::Delisted => {
                    diagnostics.push(format!("{coin} 已下架，实时行情与估算不可用"))
                }
                MarketListing::Active => {}
            }
        }
    }

    let mut items: Option<Vec<PositionItem>> = None;
    let mut position_state: &'static str = "unknown";
    let mut unrealized_total: Option<String> = None;
    let mut notional_ratio: Option<String> = None;
    let mut drift: Option<String> = None;
    let account_configured = balances.at.is_some();

    if !account_configured {
        diagnostics.push("账户未配置，仓位数据不可用".to_string());
    }
    if let Some(state) = &balances.value {
        let mut legs: Vec<Option<&hypersdk::hypercore::types::PositionData>> = Vec::new();
        for coin in &descriptor.coins {
            legs.push(
                state
                    .asset_positions
                    .iter()
                    .map(|a| &a.position)
                    .find(|p| p.coin == *coin && !p.szi.is_zero()),
            );
        }
        let (first, second) = (legs[0], legs[1]);
        let mut rows = Vec::new();
        let mut notionals: Vec<Option<Decimal>> = Vec::new();
        for (i, leg) in legs.iter().enumerate() {
            match leg {
                Some(p) => {
                    let coin = &descriptor.coins[i];
                    let mark = ctx
                        .filter(|c| c[i].is_active())
                        .and_then(|c| c[i].mark_px)
                        .filter(|m| *m > Decimal::ZERO);
                    if mark.is_none() {
                        diagnostics
                            .push(format!("{coin} 标记价格缺失、非正数或已下架，名义不可用"));
                    }
                    let size = p.szi.abs();
                    notionals.push(mark.map(|m| size * m));
                    rows.push(PositionItem {
                        coin: coin.clone(),
                        side: p.side(),
                        size: size.to_string(),
                        entry_px: p.entry_px.map(|x| x.to_string()),
                        mark_px: mark.map(|m| m.to_string()),
                        notional: mark.map(|m| (size * m).round_dp(2).to_string()),
                        unrealized_pnl: Some(p.unrealized_pnl.to_string()),
                        margin_mode: Some(match p.leverage.leverage_type {
                            hypersdk::hypercore::types::LeverageType::Cross => "cross",
                            hypersdk::hypercore::types::LeverageType::Isolated => "isolated",
                        }),
                        margin_used: Some(p.margin_used.to_string()),
                        liquidation_px: p.liquidation_px.map(|x| x.to_string()),
                    });
                }
                None => notionals.push(None),
            }
        }
        unrealized_total = Some(
            legs.iter()
                .flatten()
                .map(|p| p.unrealized_pnl)
                .sum::<Decimal>()
                .to_string(),
        );
        position_state = match (first, second) {
            (None, None) => "flat",
            (Some(_), None) | (None, Some(_)) => "single_leg",
            (Some(a), Some(b)) => {
                if (a.szi > Decimal::ZERO) == (b.szi > Decimal::ZERO) {
                    diagnostics.push("两腿同向持仓，不宣称对冲正常".to_string());
                    "same_direction"
                } else {
                    "opposite"
                }
            }
        };
        match (notionals[0], notionals[1]) {
            (Some(n1), Some(n2)) if n1 > Decimal::ZERO && n2 > Decimal::ZERO => {
                notional_ratio = Some((n1 / n2).round_dp(4).to_string());
                drift = Some(
                    ((n1 - n2).abs() / ((n1 + n2) / Decimal::TWO) * Decimal::ONE_HUNDRED)
                        .round_dp(2)
                        .to_string(),
                );
            }
            _ => {
                if position_state != "flat" {
                    diagnostics.push("两腿名义都非正时无法计算名义比与差异".to_string());
                }
            }
        }
        items = Some(rows);
    }

    let open_orders = orders.value.as_ref().map(|list| {
        list.iter()
            .filter(|o| o.coin == descriptor.coins[0] || o.coin == descriptor.coins[1])
            .count() as u64
    });

    let status = if !account_configured {
        // 账户未配置：该块不适用（即使行情来源可用），整体 unavailable。
        BlockStatus::Unavailable
    } else {
        block_status(&[positions_ok, orders_ok, market_ok], &diagnostics)
    };
    PositionsBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: PositionsSources {
            positions: balances.query(),
            orders: orders.query(),
            market: market.query(),
        },
        items,
        position_state,
        unrealized_pnl: unrealized_total,
        notional_ratio,
        ratio_drift_pct: drift,
        open_orders,
        diagnostics,
    }
}

fn build_funding_block(
    descriptor: &PairDescriptor,
    balances: &Timed<ClearinghouseState>,
    market: &Timed<serde_json::Value>,
    funding_history: &Timed<FundingHistory>,
    window_start_ms: u64,
    window_end_ms: u64,
) -> FundingBlock {
    let mut diagnostics = Vec::new();
    let positions_ok = balances.value.is_some();
    let market_ok = market.value.is_some();
    let funding_ok = funding_history.value.as_ref().is_some_and(|h| h.complete);

    if balances.at.is_none() {
        diagnostics.push("账户未配置，资金费收付数据不可用".to_string());
    } else if let Some(reason) = &balances.reason {
        diagnostics.push(format!("持仓查询失败（{reason}），日收付估算不可用"));
    }
    if funding_history.at.is_none() {
        diagnostics.push("账户未配置，实际资金费收付不可用".to_string());
    }

    let ctx = market.value.as_ref().map(|v| {
        [
            parse_asset_ctx(v, &descriptor.coins[0]),
            parse_asset_ctx(v, &descriptor.coins[1]),
        ]
    });

    if let Some(reason) = &market.reason {
        diagnostics.push(format!("市场行情查询失败（{reason}）"));
    }

    let mut hourly_rates = BTreeMap::new();
    let mut annualized_pct = BTreeMap::new();
    for (i, coin) in descriptor.coins.iter().enumerate() {
        let rate = ctx.filter(|c| c[i].is_active()).and_then(|c| c[i].funding);
        if let Some(contexts) = ctx {
            match contexts[i].listing {
                MarketListing::Delisted => {
                    diagnostics.push(format!("{coin} 已下架，当前费率与日收付估算不可用"));
                }
                MarketListing::Missing => {
                    diagnostics.push(format!("{coin} 不在市场元数据中，当前费率不可用"));
                }
                MarketListing::Active if rate.is_none() => {
                    diagnostics.push(format!("{coin} 资金费率缺失或非法，当前费率不可用"));
                }
                MarketListing::Active => {}
            }
        }
        hourly_rates.insert(coin.clone(), rate.map(|r| r.to_string()));
        annualized_pct.insert(
            coin.clone(),
            rate.map(|r| {
                (r * HOURS_PER_YEAR * Decimal::ONE_HUNDRED)
                    .round_dp(2)
                    .to_string()
            }),
        );
    }

    // 日估算：net = −Σ(signed_size × oracle_px × hourly_rate × 24)。
    let daily_estimate = match &balances.value {
        Some(state) => {
            let signed = |coin: &str| -> Decimal {
                state
                    .asset_positions
                    .iter()
                    .map(|a| &a.position)
                    .find(|p| p.coin == coin && !p.szi.is_zero())
                    .map(|p| p.szi)
                    .unwrap_or(Decimal::ZERO)
            };
            let s0 = signed(&descriptor.coins[0]);
            let s1 = signed(&descriptor.coins[1]);
            if s0.is_zero() && s1.is_zero() {
                None // 空仓：估算不适用（无诊断）
            } else {
                let mut net = Decimal::ZERO;
                let mut computable = true;
                for (i, s) in [(0usize, s0), (1usize, s1)] {
                    if s.is_zero() {
                        continue; // 未持仓币种的费率缺失不影响另一腿
                    }
                    let inputs = ctx.filter(|c| c[i].is_active()).and_then(|c| {
                        match (c[i].oracle_px, c[i].funding) {
                            (Some(oracle), Some(rate)) if oracle > Decimal::ZERO => {
                                Some((oracle, rate))
                            }
                            _ => None,
                        }
                    });
                    match inputs {
                        Some((oracle, rate)) => net -= s * oracle * rate,
                        None => {
                            computable = false;
                            diagnostics.push(format!(
                                "{} 资金费率、预言机价格或上市状态不可用，日收付估算无法计算",
                                descriptor.coins[i]
                            ));
                        }
                    }
                }
                if !computable {
                    None
                } else {
                    let net = net * HOURS_PER_DAY;
                    Some(DailyEstimate {
                        direction: if net > Decimal::ZERO {
                            "receive"
                        } else if net < Decimal::ZERO {
                            "pay"
                        } else {
                            "flat"
                        },
                        amount: net.abs().round_dp(4).to_string(),
                    })
                }
            }
        }
        None => None,
    };

    let (window_complete, actual_48h_net, recent_payments) = match &funding_history.value {
        Some(history) => {
            let pair: Vec<&FundingRecord> = history
                .records
                .iter()
                .filter(|r| r.time_ms >= window_start_ms && r.time_ms < window_end_ms)
                .filter(|r| r.coin == descriptor.coins[0] || r.coin == descriptor.coins[1])
                .collect();
            if let Some(err) = &history.error {
                let detail = if pair.is_empty() {
                    "没有已取得的本 pair 有效记录"
                } else {
                    "仅展示已取得的部分净收付"
                };
                diagnostics.push(format!("资金费历史获取不完整（{err}），{detail}"));
            }
            if history.complete || !pair.is_empty() {
                let net: Decimal = pair.iter().map(|r| r.usdc).sum();
                let mut sorted = pair;
                sorted.sort_by_key(|r| std::cmp::Reverse(r.time_ms));
                let mut recent = Vec::new();
                let mut dropped = 0usize;
                for r in sorted.iter().take(3) {
                    match DateTime::<Utc>::from_timestamp_millis(r.time_ms as i64) {
                        Some(t) => recent.push(PaymentItem {
                            time: t.to_rfc3339_opts(SecondsFormat::Secs, true),
                            coin: r.coin.clone(),
                            usdc: r.usdc.to_string(),
                            rate: r.rate.to_string(),
                        }),
                        None => dropped += 1,
                    }
                }
                if dropped > 0 {
                    diagnostics.push(format!("{dropped} 条资金费记录时间无法解析，未展示"));
                }
                (history.complete, Some(net.to_string()), Some(recent))
            } else {
                (false, None, None)
            }
        }
        None => {
            if let Some(reason) = &funding_history.reason {
                diagnostics.push(format!("资金费历史查询失败（{reason}）"));
            }
            (false, None, None)
        }
    };

    let status = block_status(&[positions_ok, market_ok, funding_ok], &diagnostics);
    FundingBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: FundingSources {
            positions: balances.query(),
            market: market.query(),
            // 历史不完整（分页中断/截断）时来源不算 ok，但可以展示部分合计。
            user_funding: SourceQuery {
                status: if funding_ok { "ok" } else { "unavailable" },
                queried_at: funding_history.at.clone(),
            },
        },
        hourly_rates,
        annualized_pct,
        daily_estimate,
        window_start: rfc3339_of(window_start_ms),
        window_end: rfc3339_of(window_end_ms),
        window_complete,
        actual_48h_net,
        recent_payments,
        diagnostics,
    }
}

fn build_ratio_block(
    descriptor: &PairDescriptor,
    market: &Timed<serde_json::Value>,
    ctx: &Option<[AssetCtx; 2]>,
    candles_first: &Timed<Vec<(u64, Decimal)>>,
    candles_second: &Timed<Vec<(u64, Decimal)>>,
    now_ms: u64,
    today: NaiveDate,
) -> RatioBlock {
    let mut diagnostics = Vec::new();
    let market_ok = market.value.is_some();
    let c1_ok = candles_first.value.is_some();
    let c2_ok = candles_second.value.is_some();

    if let Some(reason) = &candles_first.reason {
        diagnostics.push(format!("{} 日线查询失败（{reason}）", descriptor.coins[0]));
    }
    if let Some(reason) = &candles_second.reason {
        diagnostics.push(format!("{} 日线查询失败（{reason}）", descriptor.coins[1]));
    }

    if let Some(reason) = &market.reason {
        diagnostics.push(format!("实时行情查询失败（{reason}）"));
    }
    let live = ctx.and_then(|c| {
        if !c[0].is_active() || !c[1].is_active() {
            return None;
        }
        match (c[0].mark_px, c[1].mark_px) {
            (Some(first), Some(second)) if first > Decimal::ZERO && second > Decimal::ZERO => {
                Some((first / second).to_string())
            }
            _ => None,
        }
    });
    if ctx.is_some() && live.is_none() {
        diagnostics.push("两腿标记价格非正、缺失、未上市或已下架，实时比价不可用".to_string());
    }

    let (latest_close, as_of_date, ma30, vs_ma30_pct, points) =
        match (&candles_first.value, &candles_second.value) {
            (Some(first), Some(second)) => {
                let closes_to_map = |v: &[(u64, Decimal)]| -> BTreeMap<String, Decimal> {
                    v.iter()
                        .filter(|(t, _)| *t <= now_ms)
                        .filter(|(_, c)| *c > Decimal::ZERO)
                        .filter_map(|(t, c)| {
                            DateTime::<Utc>::from_timestamp_millis(*t as i64)
                                .map(|d| (d.format("%Y-%m-%d").to_string(), *c))
                        })
                        .collect()
                };
                let first_map = closes_to_map(first);
                let second_map = closes_to_map(second);
                // 先由全部已收盘共同日期构建比价，预热完成后再裁剪展示窗口。
                let ratios: BTreeMap<String, Decimal> = first_map
                    .iter()
                    .filter_map(|(date, first)| {
                        second_map
                            .get(date)
                            .map(|second| (date.clone(), *first / *second))
                    })
                    .collect();
                let anchor = ratios.keys().max().cloned();
                match anchor {
                    None => {
                        diagnostics.push("没有共同收盘日，比价序列不可用".to_string());
                        (None, None, None, None, Some(Vec::new()))
                    }
                    Some(anchor_date) => {
                        let anchor_day =
                            NaiveDate::parse_from_str(&anchor_date, "%Y-%m-%d").unwrap();
                        let latest = ratios[&anchor_date];
                        // 90 个日历日 points（锚 = 最近共同收盘日）；缺日保留 null 断线。
                        let mut points = Vec::new();
                        for d in 0..RATIO_DISPLAY_DAYS {
                            let day =
                                anchor_day - chrono::Duration::days(RATIO_DISPLAY_DAYS - 1 - d);
                            let key = day.to_string();
                            let f = first_map.get(&key).copied();
                            let s = second_map.get(&key).copied();
                            let ratio = ratios.get(&key).copied();
                            // 连续 30 个日历日都有 ratio 才有当日 MA（29 天预热）。
                            let ma = if ratio.is_some() {
                                let mut window = Decimal::ZERO;
                                let mut valid = true;
                                for back in 0..MA_WINDOW {
                                    let wd = day - chrono::Duration::days(MA_WINDOW - 1 - back);
                                    match ratios.get(&wd.to_string()) {
                                        Some(r) => window += *r,
                                        None => {
                                            valid = false;
                                            break;
                                        }
                                    }
                                }
                                valid.then(|| window / Decimal::from(MA_WINDOW))
                            } else {
                                None
                            };
                            points.push(RatioPoint {
                                date: key,
                                first_close: f.map(|x| x.to_string()),
                                second_close: s.map(|x| x.to_string()),
                                ratio: ratio.map(|r| r.to_string()),
                                ma30: ma.map(|m| m.to_string()),
                            });
                        }
                        let ma_anchor: Option<Decimal> =
                            points.last().and_then(|p: &RatioPoint| {
                                p.ma30.as_deref().and_then(|s| s.parse::<Decimal>().ok())
                            });
                        let expected = (today - chrono::Duration::days(1)).to_string();
                        let fresh = anchor_date == expected;
                        if !fresh {
                            diagnostics.push(format!(
                                "日线未更新，最近共同收盘日 {anchor_date}，需要 {expected} UTC"
                            ));
                        }
                        let vs = if fresh {
                            match ma_anchor {
                                Some(ma) if ma > Decimal::ZERO => Some(
                                    ((latest / ma - Decimal::ONE) * Decimal::ONE_HUNDRED)
                                        .round_dp(2)
                                        .to_string(),
                                ),
                                _ => {
                                    diagnostics.push(
                                        "连续有效共同收盘日不足 30 个，30MA 不可用".to_string(),
                                    );
                                    None
                                }
                            }
                        } else {
                            None
                        };
                        (
                            Some(latest.to_string()),
                            Some(anchor_date.clone()),
                            ma_anchor.map(|m| m.to_string()),
                            vs,
                            Some(points),
                        )
                    }
                }
            }
            _ => (None, None, None, None, None),
        };

    let status = block_status(&[market_ok, c1_ok, c2_ok], &diagnostics);
    RatioBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: RatioSources {
            market: market.query(),
            candles_first: candles_first.query(),
            candles_second: candles_second.query(),
        },
        live,
        latest_close,
        as_of_date,
        ma30,
        vs_ma30_pct,
        points,
        diagnostics,
    }
}

fn build_unlocks_block(
    anchors: &[UnlockAnchor],
    today: NaiveDate,
    generated_at: String,
) -> UnlocksBlock {
    let mut diagnostics = Vec::new();
    let mut items: Vec<AnchorItem> = Vec::new();
    let mut valid_count = 0usize;
    let mut seen = std::collections::BTreeSet::new();
    for a in anchors {
        let Ok(date) = NaiveDate::parse_from_str(&a.date_utc, "%Y-%m-%d") else {
            continue;
        };
        let url_ok = reqwest::Url::parse(&a.source_url)
            .map(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some())
            .unwrap_or(false);
        let verified_ok = NaiveDate::parse_from_str(&a.verified_utc, "%Y-%m-%d")
            .map(|v| v <= today)
            .unwrap_or(false);
        if !url_ok || !verified_ok {
            continue;
        }
        valid_count += 1;
        let days_ahead = (date - today).num_days();
        if !(0..=7).contains(&days_ahead) {
            continue;
        }
        let key = (
            date,
            a.label.clone(),
            a.source_url.clone(),
            a.verified_utc.clone(),
        );
        if !seen.insert(key) {
            continue;
        }
        items.push(AnchorItem {
            date: date.to_string(),
            days_ahead,
            label: a.label.clone(),
            source_url: a.source_url.clone(),
            verified_utc: a.verified_utc.clone(),
        });
    }
    items.sort_by(|x, y| x.date.cmp(&y.date).then_with(|| x.label.cmp(&y.label)));

    if anchors.is_empty() {
        diagnostics.push("未配置解锁锚点".to_string());
    } else if valid_count == 0 {
        diagnostics.push(
            "解锁锚点全部无效：日期无法解析、来源链接非 HTTP(S) 或核实日期晚于当天".to_string(),
        );
    }
    let status = if diagnostics.is_empty() {
        BlockStatus::Ok
    } else {
        BlockStatus::Partial
    };
    UnlocksBlock {
        status,
        queried_at: generated_at.clone(),
        source_queries: UnlocksSources {
            config: SourceQuery::ok(generated_at),
        },
        anchors: items,
        note: UNLOCKS_NOTE,
        diagnostics,
    }
}

#[cfg(test)]
mod tests {
    use super::super::client::InfoClient;
    use super::super::config::{Network, PairDescriptor, UnlockAnchor};
    use super::*;
    use httpmock::MockServer;
    use serde_json::{Value, json};

    const USER: &str = "0x627f37fb0d7bc5552c404f00622f651737d7ce92";
    const DAY_MS: u64 = 86_400_000;

    fn pair(
        coins: [&str; 2],
        dex: &str,
        account: Option<&str>,
    ) -> super::super::config::DashboardPair {
        super::super::config::DashboardPair {
            descriptor: PairDescriptor {
                id: "test-pair".to_string(),
                label: "TEST".to_string(),
                network: Network::Mainnet,
                dex: dex.to_string(),
                coins: [coins[0].to_string(), coins[1].to_string()],
                account_address: account.map(|a| a.to_string()),
                unlocks_enabled: false,
            },
            enabled: true,
            unlock_anchors: None,
        }
    }

    // xyz:NBIS funding 0.0001 mark 40 oracle 39.9；xyz:CRWV 0.0002 mark 8 oracle 8.01。
    const META_XYZ: &str = r#"[
        {"universe": [
            {"szDecimals": 2, "name": "xyz:NBIS", "maxLeverage": 5, "onlyIsolated": false},
            {"szDecimals": 3, "name": "xyz:CRWV", "maxLeverage": 10, "onlyIsolated": false}
        ]},
        [
            {"funding": "0.0001", "markPx": "40", "oraclePx": "39.9"},
            {"funding": "0.0002", "markPx": "8", "oraclePx": "8.01"}
        ]
    ]"#;

    const META_XYZ_CRWV_MARK_ZERO: &str = r#"[
        {"universe": [
            {"szDecimals": 2, "name": "xyz:NBIS", "maxLeverage": 5, "onlyIsolated": false},
            {"szDecimals": 3, "name": "xyz:CRWV", "maxLeverage": 10, "onlyIsolated": false}
        ]},
        [
            {"funding": "0.0001", "markPx": "40", "oraclePx": "39.9"},
            {"funding": "0.0002", "markPx": "0", "oraclePx": "8.01"}
        ]
    ]"#;

    // NBIS 多 25（uPNL 37.5）；CRWV 空 120（uPNL −24）。
    const ACCOUNT_TWO_LEGS: &str = r#"{
        "marginSummary": {"accountValue": "1000", "totalNtlPos": "1960", "totalRawUsd": "1000", "totalMarginUsed": "820"},
        "crossMarginSummary": {"accountValue": "1000", "totalNtlPos": "1960", "totalRawUsd": "1000", "totalMarginUsed": "820"},
        "crossMaintenanceMarginUsed": "100", "withdrawable": "180", "time": 1760000000000,
        "assetPositions": [
            {"type": "oneWay", "position": {"coin": "xyz:NBIS", "szi": "25",
                "leverage": {"type": "isolated", "value": 2}, "entryPx": "38.5", "positionValue": "1000",
                "unrealizedPnl": "37.5", "returnOnEquity": "0", "liquidationPx": "10.5", "marginUsed": "500",
                "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}},
            {"type": "oneWay", "position": {"coin": "xyz:CRWV", "szi": "-120",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "8.2", "positionValue": "960",
                "unrealizedPnl": "-24", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "320",
                "maxLeverage": 10, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
        ]
    }"#;

    const ACCOUNT_EMPTY: &str = r#"{
        "marginSummary": {"accountValue": "1000", "totalNtlPos": "0", "totalRawUsd": "1000", "totalMarginUsed": "0"},
        "crossMarginSummary": {"accountValue": "1000", "totalNtlPos": "0", "totalRawUsd": "1000", "totalMarginUsed": "0"},
        "crossMaintenanceMarginUsed": "0", "withdrawable": "1000", "time": 1760000000000,
        "assetPositions": []
    }"#;

    // 只有他币仓位：两腿过滤后应为 flat。
    const ACCOUNT_OTHER_ONLY: &str = r#"{
        "marginSummary": {"accountValue": "1000", "totalNtlPos": "50", "totalRawUsd": "1000", "totalMarginUsed": "10"},
        "crossMarginSummary": {"accountValue": "1000", "totalNtlPos": "50", "totalRawUsd": "1000", "totalMarginUsed": "10"},
        "crossMaintenanceMarginUsed": "5", "withdrawable": "990", "time": 1760000000000,
        "assetPositions": [
            {"type": "oneWay", "position": {"coin": "xyz:OTHER", "szi": "5",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "10", "positionValue": "50",
                "unrealizedPnl": "2", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "10",
                "maxLeverage": 10, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
        ]
    }"#;

    // 两腿挂单 + 一个他币挂单。
    const ORDERS: &str = r#"[
        {"timestamp": 1760000000000, "coin": "xyz:NBIS", "side": "B", "limitPx": "39", "sz": "1",
         "oid": 1, "origSz": "1", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false},
        {"timestamp": 1760000000000, "coin": "xyz:CRWV", "side": "A", "limitPx": "8.1", "sz": "2",
         "oid": 2, "origSz": "2", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false},
        {"timestamp": 1760000000000, "coin": "xyz:OTHER", "side": "B", "limitPx": "1", "sz": "1",
         "oid": 3, "origSz": "1", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false}
    ]"#;

    fn funding_entry(coin: &str, usdc: &str, rate: &str, time_ms: u64, hash: &str) -> Value {
        json!({
            "delta": {"type": "funding", "coin": coin, "usdc": usdc, "szi": "1",
                      "fundingRate": rate, "nSamples": null},
            "hash": hash,
            "time": time_ms
        })
    }

    fn candle(close_time: u64, coin: &str, close: &str) -> Value {
        json!({
            "t": close_time - DAY_MS, "T": close_time, "s": coin, "i": "1d",
            "o": close, "h": close, "l": close, "c": close, "v": "1", "n": 1
        })
    }

    /// days 根日线，日期 today−days .. today−1；最后一天可用不同收盘价。
    fn candles_json(
        now_ms: u64,
        coin: &str,
        close: &str,
        last_close: Option<&str>,
        days: usize,
    ) -> String {
        let today0 = now_ms - (now_ms % DAY_MS);
        let closes: Vec<Value> = (0..days)
            .rev()
            .map(|i| {
                let t = today0 - i as u64 * DAY_MS - 1;
                let c = if i == 0 {
                    last_close.unwrap_or(close)
                } else {
                    close
                };
                candle(t, coin, c)
            })
            .collect();
        json!(closes).to_string()
    }

    fn mock_essentials(server: &MockServer, meta: &'static str, account: &'static str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("metaAndAssetCtxs");
            then.status(200).body(meta);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(account);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body(ORDERS);
        });
    }

    fn mock_mode(server: &MockServer, mode: &'static str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#);
            then.status(200).body(mode);
        });
    }

    fn mock_funding(server: &MockServer, body: String) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body(body);
        });
    }

    fn mock_candles(server: &MockServer, now_ms: u64, days: usize) {
        for (coin, close) in [("xyz:NBIS", "80"), ("xyz:CRWV", "10")] {
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes("candleSnapshot")
                    .body_includes(coin);
                then.status(200)
                    .body(candles_json(now_ms, coin, close, None, days));
            });
        }
    }

    fn realtime_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    async fn collect(
        server: &MockServer,
        p: &super::super::config::DashboardPair,
        now_ms: u64,
    ) -> PairSnapshot {
        collect_pair(
            &InfoClient::with_base_url(&server.base_url(), Network::Mainnet),
            p,
            "rev-test",
            now_ms,
        )
        .await
    }

    fn to_json(snapshot: &PairSnapshot) -> Value {
        serde_json::from_str(&pair_snapshot_json(snapshot).unwrap()).unwrap()
    }

    #[tokio::test]
    async fn happy_path_full_contract() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_TWO_LEGS);
        mock_mode(&server, r#""default""#);
        mock_funding(
            &server,
            json!([
                funding_entry("xyz:NBIS", "1.5", "0.0001", now_ms - 2 * HOUR_MS, "0xaaa1"),
                funding_entry("xyz:CRWV", "-0.5", "0.0002", now_ms - HOUR_MS, "0xaaa2"),
                funding_entry("xyz:OTHER", "99", "0.001", now_ms - HOUR_MS, "0xaaa3"),
            ])
            .to_string(),
        );
        mock_candles(&server, now_ms, 90);
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body("{}");
        });

        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let snap = collect(&server, &p, now_ms).await;
        let v = to_json(&snap);

        assert_eq!(v["schema_version"], 2);
        assert_eq!(v["config_revision"], "rev-test");
        assert_eq!(v["pair"]["id"], "test-pair");
        assert_eq!(v["pair"]["network"], "mainnet");
        assert_eq!(v["pair"]["dex"], "xyz");
        assert_eq!(v["pair"]["coins"][0], "xyz:NBIS");
        assert_eq!(v["pair"]["account_address"], USER);
        assert_eq!(v["pair"]["unlocks_enabled"], false);
        assert!(
            v["blocks"].get("unlocks").is_some(),
            "unlocks_enabled=false → null"
        );
        assert!(v["blocks"]["unlocks"].is_null());

        let a = &v["blocks"]["account"];
        assert_eq!(a["status"], "ok");
        assert_eq!(a["source_queries"]["mode"]["status"], "ok");
        assert_eq!(a["source_queries"]["balances"]["status"], "ok");
        assert_eq!(a["account_mode"], "default");
        assert_eq!(a["equity"], "1000");
        assert_eq!(a["equity_scope"], "perp_dex");
        assert!(a["diagnostics"].as_array().unwrap().is_empty());

        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["status"], "ok");
        for key in ["positions", "orders", "market"] {
            assert_eq!(pos["source_queries"][key]["status"], "ok", "来源 {key}");
        }
        assert_eq!(pos["position_state"], "opposite");
        assert_eq!(pos["unrealized_pnl"], "13.5", "37.5 − 24");
        let items = pos["items"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["coin"], "xyz:NBIS");
        assert_eq!(items[0]["side"], "long");
        assert_eq!(items[0]["size"], "25");
        assert_eq!(items[0]["entry_px"], "38.5");
        assert_eq!(items[0]["mark_px"], "40");
        assert_eq!(items[0]["notional"], "1000");
        assert_eq!(items[0]["unrealized_pnl"], "37.5");
        assert_eq!(items[0]["margin_mode"], "isolated");
        assert_eq!(items[0]["margin_used"], "500");
        assert_eq!(items[0]["liquidation_px"], "10.5");
        assert_eq!(items[1]["coin"], "xyz:CRWV");
        assert_eq!(items[1]["side"], "short");
        assert_eq!(items[1]["size"], "120");
        assert_eq!(items[1]["margin_mode"], "cross");
        assert!(items[1]["liquidation_px"].is_null(), "交易所 null 原样保留");
        assert_eq!(pos["notional_ratio"], "1.0417");
        assert_eq!(pos["ratio_drift_pct"], "4.08");
        assert_eq!(pos["open_orders"], 2, "只计两腿挂单");

        let f = &v["blocks"]["funding"];
        assert_eq!(f["status"], "ok");
        let rates = &f["hourly_rates"];
        assert_eq!(rates["xyz:NBIS"], "0.0001", "key 恰好是完整 coin");
        assert_eq!(rates["xyz:CRWV"], "0.0002");
        assert_eq!(f["annualized_pct"]["xyz:NBIS"], "87.60");
        assert_eq!(f["annualized_pct"]["xyz:CRWV"], "175.20");
        let est = &f["daily_estimate"];
        assert_eq!(est["direction"], "receive");
        assert_eq!(
            est["amount"], "2.2198",
            "net=−(25×39.9×0.0001−120×8.01×0.0002)×24"
        );
        assert_eq!(f["window_start"], rfc3339_of(now_ms - 48 * HOUR_MS));
        assert_eq!(f["window_end"], rfc3339_of(now_ms));
        assert_eq!(f["window_complete"], true);
        assert_eq!(f["actual_48h_net"], "1.0", "1.5 − 0.5，OTHER 币不计");
        let recent = f["recent_payments"].as_array().unwrap();
        assert_eq!(recent.len(), 2);
        assert_eq!(recent[0]["coin"], "xyz:CRWV", "倒序");
        assert_eq!(recent[0]["usdc"], "-0.5");
        assert_eq!(recent[0]["rate"], "0.0002");
        assert!(recent[0]["time"].as_str().unwrap().ends_with('Z'));

        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "ok");
        for key in ["market", "candles_first", "candles_second"] {
            assert_eq!(r["source_queries"][key]["status"], "ok");
        }
        assert_eq!(r["live"], "5", "mark 40/8");
        assert_eq!(r["latest_close"], "8", "收盘 80/10");
        assert_eq!(r["as_of_date"], yesterday_str(now_ms));
        assert!(r["ma30"].is_string());
        assert_eq!(r["vs_ma30_pct"], "0", "比值恒 8 → 与 MA 一致");
        let points = r["points"].as_array().unwrap();
        assert_eq!(points.len(), 90);
        assert!(points.iter().all(|p| p["ratio"] == "8"));

        assert_eq!(exchange.calls(), 0, "只读回归：绝不请求 /exchange");
    }

    fn yesterday_str(now_ms: u64) -> String {
        DateTime::<Utc>::from_timestamp_millis(now_ms as i64)
            .unwrap()
            .date_naive()
            .pred_opt()
            .unwrap()
            .to_string()
    }

    #[tokio::test]
    async fn unified_and_portfolio_margin_null_equity_but_positions_survive() {
        for mode in [r#""unifiedAccount""#, r#""portfolioMargin""#] {
            let now_ms = realtime_ms();
            let server = MockServer::start_async().await;
            mock_essentials(&server, META_XYZ, ACCOUNT_TWO_LEGS);
            mock_mode(&server, mode);
            mock_funding(&server, "[]".to_string());
            mock_candles(&server, now_ms, 32);
            let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
            let v = to_json(&collect(&server, &p, now_ms).await);
            let a = &v["blocks"]["account"];
            assert_eq!(a["account_mode"], mode.trim_matches('"'));
            assert!(a["equity"].is_null(), "{mode} 权益必须 null");
            assert_eq!(a["equity_scope"], "unavailable");
            assert_eq!(a["status"], "partial");
            assert!(
                a["diagnostics"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|d| d.as_str().unwrap().contains("口径不适用"))
            );
            // 独立数据照常。
            assert_eq!(
                v["blocks"]["positions"]["items"].as_array().unwrap().len(),
                2
            );
            assert_eq!(v["blocks"]["positions"]["position_state"], "opposite");
            assert_eq!(v["blocks"]["funding"]["hourly_rates"]["xyz:NBIS"], "0.0001");
        }
    }

    #[tokio::test]
    async fn unknown_mode_preserved_with_diagnostic() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_TWO_LEGS);
        mock_mode(&server, r#""weirdFutureMode""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let a = &v["blocks"]["account"];
        assert_eq!(a["account_mode"], "weirdFutureMode", "未知值原样保留");
        assert!(a["equity"].is_null());
        assert_eq!(a["equity_scope"], "unavailable");
        assert_eq!(a["status"], "partial");
    }

    #[tokio::test]
    async fn mode_query_failure_keeps_other_blocks() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_TWO_LEGS);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#);
            then.status(500).body("boom");
        });
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let a = &v["blocks"]["account"];
        assert!(a["account_mode"].is_null());
        assert!(a["equity"].is_null());
        assert_eq!(a["equity_scope"], "unavailable");
        assert_eq!(a["source_queries"]["mode"]["status"], "unavailable");
        assert_eq!(v["blocks"]["positions"]["position_state"], "opposite");
        assert_eq!(v["blocks"]["funding"]["status"], "ok");
    }

    #[tokio::test]
    async fn position_states_flat_single_same_and_filtering() {
        // flat：空仓。
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["position_state"], "flat");
        assert_eq!(pos["items"].as_array().unwrap().len(), 0);
        assert_eq!(pos["unrealized_pnl"], "0");
        assert!(pos["notional_ratio"].is_null());
        assert_eq!(pos["status"], "ok");

        // 只有他币仓位 → 精确过滤后为 flat。
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_OTHER_ONLY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let v2 = to_json(&collect(&server, &p, now_ms).await);
        let pos = &v2["blocks"]["positions"];
        assert_eq!(pos["position_state"], "flat", "他币仓位不进 pair");
        assert_eq!(pos["items"].as_array().unwrap().len(), 0);

        // 单腿。
        let account_single = ACCOUNT_TWO_LEGS.replace(
            r#""szi": "-120",
                "leverage": {"type": "cross", "value": 3}"#,
            r#""szi": "0",
                "leverage": {"type": "cross", "value": 3}"#,
        );
        let account_single: &'static str = Box::leak(account_single.into_boxed_str());
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, account_single);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let v = to_json(&collect(&server, &p, now_ms).await);
        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["position_state"], "single_leg");
        assert_eq!(pos["items"].as_array().unwrap().len(), 1);
        assert_eq!(pos["unrealized_pnl"], "37.5", "单腿保留已知盈亏合计");

        // 同向。
        let account_same = ACCOUNT_TWO_LEGS.replace(r#""szi": "-120""#, r#""szi": "120""#);
        let account_same: &'static str = Box::leak(account_same.into_boxed_str());
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, account_same);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let v = to_json(&collect(&server, &p, now_ms).await);
        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["position_state"], "same_direction");
        assert_eq!(pos["items"].as_array().unwrap().len(), 2);
        assert!(
            pos["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("同向"))
        );
        assert_eq!(pos["status"], "partial");
    }

    #[tokio::test]
    async fn missing_mark_nulls_notional_only() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ_CRWV_MARK_ZERO, ACCOUNT_TWO_LEGS);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["status"], "partial");
        let items = pos["items"].as_array().unwrap();
        assert_eq!(items[0]["notional"], "1000", "NBIS 正常");
        assert!(items[1]["mark_px"].is_null());
        assert!(items[1]["notional"].is_null(), "mark 非正 → null");
        assert_eq!(items[1]["size"], "120", "数量与交易所盈亏保留");
        assert_eq!(items[1]["unrealized_pnl"], "-24");
        assert!(pos["notional_ratio"].is_null());
        // 费率仍可用（market 来源 ok）。
        assert_eq!(v["blocks"]["funding"]["hourly_rates"]["xyz:CRWV"], "0.0002");
    }

    #[tokio::test]
    async fn funding_window_boundaries_and_same_ms_dedup() {
        let now_ms = realtime_ms();
        let start = now_ms - 48 * HOUR_MS;
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(
            &server,
            json!([
                funding_entry("xyz:NBIS", "1", "0.0001", start, "0xh1"),
                funding_entry("xyz:NBIS", "2", "0.0001", start, "0xh2"),
                funding_entry("xyz:NBIS", "4", "0.0001", now_ms - 1, "0xh3"),
                funding_entry("xyz:NBIS", "8", "0.0001", now_ms, "0xh4"),
            ])
            .to_string(),
        );
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let f = &v["blocks"]["funding"];
        assert_eq!(f["window_complete"], true);
        assert_eq!(
            f["actual_48h_net"], "7",
            "start 含、end 不含、同毫秒不同 hash 不互相去重"
        );
    }

    #[tokio::test]
    async fn funding_pagination_stall_marks_incomplete_with_partial_sum() {
        let now_ms = realtime_ms();
        let base_time = now_ms - 10 * HOUR_MS;
        let page: Vec<Value> = (0..500)
            .map(|i| {
                funding_entry(
                    "xyz:NBIS",
                    "0.001",
                    "0.0001",
                    base_time + i,
                    &format!("0x{i:064x}"),
                )
            })
            .collect();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        // 第一页 startTime=window_start；第二页 cursor 不推进（同 time 重复）。
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding")
                .body_excludes(format!(r#""startTime":{base_time}""#));
            then.status(200).json_body_obj(&page);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(format!(r#""startTime":{}""#, base_time + 499));
            then.status(200).json_body_obj(&page);
        });
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let f = &v["blocks"]["funding"];
        assert_eq!(f["window_complete"], false);
        assert_eq!(f["source_queries"]["user_funding"]["status"], "unavailable");
        assert_ne!(f["status"], "ok");
        assert_eq!(f["actual_48h_net"], "0.500", "500×0.001 部分合计");
        assert!(
            f["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("不完整"))
        );
    }

    #[tokio::test]
    async fn flat_account_keeps_funding_history() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(
            &server,
            json!([funding_entry(
                "xyz:NBIS",
                "0.7",
                "0.0001",
                now_ms - HOUR_MS,
                "0xhh"
            )])
            .to_string(),
        );
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let f = &v["blocks"]["funding"];
        assert!(f["daily_estimate"].is_null(), "空仓估算不适用");
        assert_eq!(f["actual_48h_net"], "0.7", "平仓后历史收付仍计入");
        assert_eq!(f["recent_payments"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn ratio_ma_needs_30_consecutive_days() {
        let now_ms = realtime_ms();
        // 29 天：MA 不可用。
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 29);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "partial");
        assert!(r["ma30"].is_null());
        assert!(r["vs_ma30_pct"].is_null());
        assert!(
            r["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("不足 30"))
        );
        assert_eq!(r["points"].as_array().unwrap().len(), 90);
        assert!(
            r["points"]
                .as_array()
                .unwrap()
                .iter()
                .all(|pt| pt["ma30"].is_null())
        );

        // 30 天：第 30 个有效日出 MA。
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 30);
        let v2 = to_json(&collect(&server, &p, now_ms).await);
        let r = &v2["blocks"]["ratio"];
        assert_eq!(r["status"], "ok");
        assert_eq!(r["ma30"], "8");
        assert_eq!(r["vs_ma30_pct"], "0");
        let points = r["points"].as_array().unwrap();
        assert_eq!(points.len(), 90);
        let with_ma = points.iter().filter(|pt| !pt["ma30"].is_null()).count();
        assert_eq!(with_ma, 1, "只有锚日满 30 天连续窗口");
    }

    #[tokio::test]
    async fn ratio_gap_breaks_ma_line() {
        let now_ms = realtime_ms();
        let today0 = now_ms - (now_ms % DAY_MS);
        // 32 根但中间挖掉 2 天（锚日前第 10、11 天）。
        // 90 天窗口内提供 90 根，挖掉锚日前第 10、11 天 → 恰好 2 个缺日。
        let candles_gap = |coin: &str, close: &str| -> String {
            let closes: Vec<Value> = (0..90)
                .rev()
                .filter(|i| *i != 10 && *i != 11)
                .map(|i| candle(today0 - i as u64 * DAY_MS - 1, coin, close))
                .collect();
            json!(closes).to_string()
        };
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        for coin in ["xyz:NBIS", "xyz:CRWV"] {
            let body = candles_gap(coin, if coin == "xyz:NBIS" { "80" } else { "10" });
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes("candleSnapshot")
                    .body_includes(coin);
                then.status(200).body(body);
            });
        }
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let points = v["blocks"]["ratio"]["points"].as_array().unwrap();
        // 缺日 point 保留 date 且 ratio/ma null（断线）。
        let gap_days: Vec<&str> = points
            .iter()
            .filter(|pt| pt["ratio"].is_null())
            .map(|pt| pt["date"].as_str().unwrap())
            .collect();
        assert_eq!(gap_days.len(), 2, "恰好两个缺日断线");
        // 缺日（锚日前 10、11 天）当日及之后无 MA，更早的日期有 MA。
        let anchor = yesterday_str(now_ms);
        let anchor_day = NaiveDate::parse_from_str(&anchor, "%Y-%m-%d").unwrap();
        for pt in points {
            let d = NaiveDate::parse_from_str(pt["date"].as_str().unwrap(), "%Y-%m-%d").unwrap();
            let distance = (anchor_day - d).num_days();
            if distance <= 11 {
                // 缺日（10、11 天前）本身及断线窗口内无 MA。
                assert!(pt["ma30"].is_null(), "锚日前 {distance} 天 MA 应断线");
            } else if distance <= 60 {
                // 窗口 [d−29, d] 完整落在数据内。
                assert!(!pt["ma30"].is_null(), "锚日前 {distance} 天 MA 应存在");
            } else {
                // 序列开头 29 天为 MA 预热期。
                assert!(pt["ma30"].is_null(), "锚日前 {distance} 天 MA 应预热为空");
            }
        }
    }

    #[tokio::test]
    async fn ratio_stale_marks_partial_but_keeps_real_as_of() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        // 日线止于 3 天前（模拟过期）。
        let stale = |coin: &str, close: &str| -> String {
            let today0 = now_ms - (now_ms % DAY_MS);
            let closes: Vec<Value> = (3..35)
                .rev()
                .map(|i| candle(today0 - i as u64 * DAY_MS - 1, coin, close))
                .collect();
            json!(closes).to_string()
        };
        for coin in ["xyz:NBIS", "xyz:CRWV"] {
            let body = stale(coin, if coin == "xyz:NBIS" { "80" } else { "10" });
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes("candleSnapshot")
                    .body_includes(coin);
                then.status(200).body(body);
            });
        }
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "partial");
        let today0 = now_ms - (now_ms % DAY_MS);
        // i=3 → t = today0 − 3·DAY − 1ms → 日期为 today−4（跨到前一日 23:59）。
        let expected_asof =
            DateTime::<Utc>::from_timestamp_millis((today0 - 3 * DAY_MS - 1) as i64)
                .unwrap()
                .date_naive()
                .to_string();
        assert_eq!(
            r["as_of_date"], expected_asof,
            "真实 as_of_date（i=3 → 4 天前的日期）"
        );
        assert!(r["vs_ma30_pct"].is_null(), "过期时 vs 为 null");
        assert!(!r["latest_close"].is_null(), "旧收盘仍可展示");
        assert!(
            r["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("日线未更新"))
        );
    }

    #[tokio::test]
    async fn ratio_nonpositive_close_excluded() {
        let now_ms = realtime_ms();
        let today0 = now_ms - (now_ms % DAY_MS);
        // NBIS 昨天收盘为 "0" → 该日无 ratio，锚变成前天；91 根覆盖前移后的窗口。
        let nbis: Vec<Value> = (0..91)
            .rev()
            .map(|i| {
                let c = if i == 0 { "0" } else { "80" };
                candle(today0 - i as u64 * DAY_MS - 1, "xyz:NBIS", c)
            })
            .collect();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot")
                .body_includes("xyz:NBIS");
            then.status(200).json_body_obj(&nbis);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot")
                .body_includes("xyz:CRWV");
            then.status(200)
                .body(candles_json(now_ms, "xyz:CRWV", "10", None, 91));
        });
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "partial", "最新有效日是前天 → 过期");
        let day_before = (NaiveDate::parse_from_str(&yesterday_str(now_ms), "%Y-%m-%d").unwrap()
            - chrono::Duration::days(1))
        .to_string();
        assert_eq!(r["as_of_date"], day_before, "非正收盘剔除后锚前移");
        let points = r["points"].as_array().unwrap();
        assert!(
            points.iter().all(|pt| pt["ratio"] == "8"),
            "序列内的点两腿价格齐全"
        );
    }

    #[tokio::test]
    async fn null_account_market_only() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        // 账户相关请求应零调用。
        let ch = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(ACCOUNT_EMPTY);
        });
        let mode_m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#);
            then.status(200).body(r#""default""#);
        });
        let orders_m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body("[]");
        });
        let funding_m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body("[]");
        });

        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", None);
        let v = to_json(&collect(&server, &p, now_ms).await);
        assert_eq!(v["pair"]["account_address"], Value::Null);

        let a = &v["blocks"]["account"];
        assert_eq!(a["status"], "unavailable");
        assert_eq!(a["source_queries"]["mode"]["status"], "unavailable");
        assert!(
            a["source_queries"]["mode"]["queried_at"].is_null(),
            "未尝试"
        );
        assert_eq!(a["source_queries"]["balances"]["queried_at"], Value::Null);
        assert!(a["account_mode"].is_null());
        assert!(a["equity"].is_null());

        let pos = &v["blocks"]["positions"];
        assert_eq!(pos["status"], "unavailable");
        assert!(pos["items"].is_null(), "不是空数组");
        assert_eq!(pos["position_state"], "unknown");
        assert_eq!(
            pos["source_queries"]["positions"]["queried_at"],
            Value::Null
        );

        let f = &v["blocks"]["funding"];
        assert_eq!(f["status"], "partial", "行情部分仍有效");
        assert_eq!(f["hourly_rates"]["xyz:NBIS"], "0.0001");
        assert!(f["daily_estimate"].is_null());
        assert_eq!(f["window_complete"], false);
        assert!(f["actual_48h_net"].is_null());
        assert!(f["recent_payments"].is_null());

        assert_eq!(v["blocks"]["ratio"]["status"], "ok", "行情块照常");

        assert_eq!(ch.calls(), 0, "不调用零地址/不尝试账户查询");
        assert_eq!(mode_m.calls(), 0);
        assert_eq!(orders_m.calls(), 0);
        assert_eq!(funding_m.calls(), 0);
    }

    #[tokio::test]
    async fn unlocks_null_vs_empty_vs_valid() {
        let now_ms = realtime_ms();
        let today = DateTime::<Utc>::from_timestamp_millis(now_ms as i64)
            .unwrap()
            .date_naive();

        // unlock_anchors = null → blocks.unlocks = null。
        let server = MockServer::start_async().await;
        mock_essentials(&server, META_XYZ, ACCOUNT_EMPTY);
        mock_mode(&server, r#""disabled""#);
        mock_funding(&server, "[]".to_string());
        mock_candles(&server, now_ms, 32);
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        assert!(v["blocks"]["unlocks"].is_null());

        // unlock_anchors = [] → 块存在、partial + 诊断。
        let mut p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        p.descriptor.unlocks_enabled = true;
        p.unlock_anchors = Some(vec![]);
        let v2 = to_json(&collect(&server, &p, now_ms).await);
        let u = &v2["blocks"]["unlocks"];
        assert_eq!(u["status"], "partial");
        assert_eq!(u["anchors"].as_array().unwrap().len(), 0);
        assert_eq!(u["note"], "日历预告不等于实际申领已发生");
        assert_eq!(u["source_queries"]["config"]["status"], "ok");

        // 有效锚点 → ok。
        p.unlock_anchors = Some(vec![
            UnlockAnchor {
                date_utc: (today + chrono::Duration::days(5)).to_string(),
                label: "团队解锁".to_string(),
                source_url: "https://example.com/u".to_string(),
                verified_utc: "2026-10-01".to_string(),
            },
            UnlockAnchor {
                date_utc: "bad-date".to_string(),
                label: "无效".to_string(),
                source_url: "https://example.com/u".to_string(),
                verified_utc: "2026-10-01".to_string(),
            },
        ]);
        let v = to_json(&collect(&server, &p, now_ms).await);
        let u = &v["blocks"]["unlocks"];
        assert_eq!(u["status"], "ok", "有有效锚点即 ok（无效条静默剔除）");
        let anchors = u["anchors"].as_array().unwrap();
        assert_eq!(anchors.len(), 1);
        assert_eq!(anchors[0]["days_ahead"], 5);
        assert_eq!(
            anchors[0]["date"],
            (today + chrono::Duration::days(5)).to_string()
        );
    }

    #[tokio::test]
    async fn announcements_mode_wraps_v1_block() {
        let server = MockServer::start_async().await;
        let html = r#"<div class="tgme_widget_message" data-post="hyperliquid_announcements/9"><div class="tgme_widget_message_text">Hello</div><a class="tgme_widget_message_date" href="https://t.me/hyperliquid_announcements/9"><time datetime="2026-10-17T10:00:00Z"></time></a></div>"#;
        server.mock(|when, then| {
            when.method(httpmock::Method::GET)
                .path("/hyperliquid_announcements");
            then.status(200)
                .header("content-type", "text/html; charset=utf-8")
                .body(html);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::GET).path("/hyperliquid_api");
            then.status(302).header("location", "https://t.me/");
        });
        let snap = collect_announcements_for_test(&server.base_url()).await;
        let line = announcements_json(&snap).unwrap();
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["schema_version"], 2);
        assert!(v["queried_at"].as_str().unwrap().ends_with('Z'));
        let block = &v["block"];
        assert_eq!(block["status"], "partial", "api 频道 302");
        assert_eq!(block["source_queries"]["general"]["status"], "ok");
        assert_eq!(block["source_queries"]["api"]["status"], "unavailable");
        assert_eq!(block["items"].as_array().unwrap().len(), 1);
        assert_eq!(block["items"][0]["kind"], "general");
        assert_eq!(block["items"][0]["text"], "Hello");
        assert!(
            block["items"][0]["published_at"]
                .as_str()
                .unwrap()
                .ends_with('Z')
        );
    }

    async fn collect_announcements_for_test(base: &str) -> AnnouncementsSnapshot {
        AnnouncementsSnapshot {
            schema_version: SCHEMA_VERSION_V2,
            queried_at: now_rfc3339(),
            block: crate::snapshot::collect_announcements(base).await,
        }
    }

    #[tokio::test]
    async fn happy_path_native_dex_without_prefix() {
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("metaAndAssetCtxs")
                .body_excludes(r#""dex""#);
            then.status(200).body(
                r#"[{"universe": [
                        {"szDecimals": 2, "name": "HYPE", "maxLeverage": 5, "onlyIsolated": false},
                        {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false}
                    ]},
                    [{"funding": "0.0001", "markPx": "30", "oraclePx": "30"},
                     {"funding": "0.00001", "markPx": "60000", "oraclePx": "60000"}]]"#,
            );
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState")
                .body_excludes(r#""dex""#);
            then.status(200).body(ACCOUNT_EMPTY);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body("[]");
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#);
            then.status(200).body(r#""default""#);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body("[]");
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot")
                .body_includes(r#""coin":"HYPE""#);
            then.status(200)
                .body(candles_json(now_ms, "HYPE", "30", None, 32));
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot")
                .body_includes(r#""coin":"BTC""#);
            then.status(200)
                .body(candles_json(now_ms, "BTC", "60000", None, 32));
        });
        let p = pair(["HYPE", "BTC"], "", Some(USER));
        let v = to_json(&collect(&server, &p, now_ms).await);
        assert_eq!(v["pair"]["dex"], "");
        assert_eq!(v["blocks"]["account"]["status"], "ok");
        assert_eq!(v["blocks"]["positions"]["position_state"], "flat");
        assert_eq!(v["blocks"]["funding"]["hourly_rates"]["HYPE"], "0.0001");
        assert_eq!(v["blocks"]["ratio"]["status"], "ok");
    }

    fn ready<T>(value: T) -> Timed<T> {
        Timed {
            value: Some(value),
            reason: None,
            at: Some("2026-10-19T08:00:00Z".to_string()),
        }
    }

    fn candles_vec(now_ms: u64, coin: &str, close: &str, days: usize) -> Vec<(u64, Decimal)> {
        let value: Vec<Value> =
            serde_json::from_str(&candles_json(now_ms, coin, close, None, days)).unwrap();
        value
            .iter()
            .map(|v| {
                (
                    v["T"].as_u64().unwrap(),
                    v["c"].as_str().unwrap().parse().unwrap(),
                )
            })
            .collect()
    }

    #[test]
    fn acceptance_missing_rate_must_not_be_zero() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let mut market: Value = serde_json::from_str(META_XYZ).unwrap();
        market[1][0].as_object_mut().unwrap().remove("funding");
        let block = build_funding_block(
            &p.descriptor,
            &ready(serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap()),
            &ready(market),
            &Timed::not_attempted(),
            0,
            48 * HOUR_MS,
        );
        assert_eq!(block.hourly_rates["xyz:NBIS"], None);
        assert_eq!(block.annualized_pct["xyz:NBIS"], None);
        assert!(block.daily_estimate.is_none());
        assert!(block.diagnostics.iter().any(|d| d.contains("缺失或非法")));
    }

    #[test]
    fn market_fields_preserve_invalid_zero_negative_and_delisted_states() {
        let mut market: Value = serde_json::from_str(META_XYZ).unwrap();
        for invalid in [Value::Null, json!("not-a-number")] {
            market[1][0]["funding"] = invalid;
            assert_eq!(parse_asset_ctx(&market, "xyz:NBIS").funding, None);
        }
        market[1][0].as_object_mut().unwrap().remove("funding");
        assert_eq!(parse_asset_ctx(&market, "xyz:NBIS").funding, None);
        market[1][0]["funding"] = json!("0");
        assert_eq!(
            parse_asset_ctx(&market, "xyz:NBIS").funding,
            Some(Decimal::ZERO)
        );
        market[1][0]["funding"] = json!("-0.0001");
        assert!(parse_asset_ctx(&market, "xyz:NBIS").funding.unwrap() < Decimal::ZERO);
        market[1][0]["markPx"] = Value::Null;
        market[1][0]["oraclePx"] = json!("bad");
        let ctx = parse_asset_ctx(&market, "xyz:NBIS");
        assert_eq!(ctx.mark_px, None);
        assert_eq!(ctx.oracle_px, None);
        assert_eq!(ctx.listing, MarketListing::Active);
        assert_eq!(
            parse_asset_ctx(&market, "xyz:MISSING").listing,
            MarketListing::Missing
        );
    }

    #[test]
    fn missing_rate_on_unpositioned_leg_does_not_block_daily_estimate() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let mut account: Value = serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap();
        account["assetPositions"]
            .as_array_mut()
            .unwrap()
            .truncate(1);
        let mut market: Value = serde_json::from_str(META_XYZ).unwrap();
        market[1][1].as_object_mut().unwrap().remove("funding");
        let block = build_funding_block(
            &p.descriptor,
            &ready(serde_json::from_value(account).unwrap()),
            &ready(market),
            &Timed::not_attempted(),
            0,
            48 * HOUR_MS,
        );
        assert_eq!(block.hourly_rates["xyz:CRWV"], None);
        assert!(block.daily_estimate.is_some());
    }

    #[test]
    fn acceptance_failed_history_must_not_be_zero() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let history = ready(FundingHistory {
            records: vec![],
            complete: false,
            error: Some("HTTP 500".to_string()),
        });
        let block = build_funding_block(
            &p.descriptor,
            &Timed::not_attempted(),
            &Timed::not_attempted(),
            &history,
            0,
            48 * HOUR_MS,
        );
        assert_eq!(block.actual_48h_net, None);
        assert!(block.recent_payments.is_none());
        assert!(!block.window_complete);
    }

    #[test]
    fn funding_history_distinguishes_empty_partial_other_and_zero_sum() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let record = |coin: &str, usdc: &str| FundingRecord {
            coin: coin.to_string(),
            time_ms: HOUR_MS,
            usdc: usdc.parse().unwrap(),
            rate: Decimal::ZERO,
        };
        let build = |records, complete| {
            build_funding_block(
                &p.descriptor,
                &Timed::not_attempted(),
                &Timed::not_attempted(),
                &ready(FundingHistory {
                    records,
                    complete,
                    error: (!complete).then(|| "page failed".to_string()),
                }),
                0,
                48 * HOUR_MS,
            )
        };

        let complete_empty = build(vec![], true);
        assert_eq!(complete_empty.actual_48h_net.as_deref(), Some("0"));
        assert_eq!(complete_empty.recent_payments.unwrap().len(), 0);
        assert!(complete_empty.window_complete);

        let other_only = build(
            vec![FundingRecord {
                coin: "xyz:OTHER".to_string(),
                time_ms: HOUR_MS,
                usdc: Decimal::ONE,
                rate: Decimal::ZERO,
            }],
            false,
        );
        assert_eq!(other_only.actual_48h_net, None);
        assert!(other_only.recent_payments.is_none());

        let zero_sum = build(
            vec![record("xyz:NBIS", "1.5"), record("xyz:CRWV", "-1.5")],
            false,
        );
        assert_eq!(
            zero_sum.actual_48h_net.unwrap().parse::<Decimal>().unwrap(),
            Decimal::ZERO
        );
        assert_eq!(zero_sum.recent_payments.unwrap().len(), 2);
        assert!(!zero_sum.window_complete);
    }

    #[test]
    fn acceptance_single_leg_pnl_must_be_preserved() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let mut account: Value = serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap();
        account["assetPositions"]
            .as_array_mut()
            .unwrap()
            .truncate(1);
        let block = build_positions_block(
            &p.descriptor,
            &ready(serde_json::from_value(account).unwrap()),
            &ready(vec![]),
            &ready(serde_json::from_str(META_XYZ).unwrap()),
        );
        assert_eq!(block.unrealized_pnl.as_deref(), Some("37.5"));
        assert_eq!(
            block.items.unwrap()[0].unrealized_pnl.as_deref(),
            Some("37.5")
        );
    }

    #[test]
    fn second_single_leg_negative_pnl_must_be_preserved() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let mut account: Value = serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap();
        account["assetPositions"].as_array_mut().unwrap().remove(0);
        let block = build_positions_block(
            &p.descriptor,
            &ready(serde_json::from_value(account).unwrap()),
            &ready(vec![]),
            &ready(serde_json::from_str(META_XYZ).unwrap()),
        );
        assert_eq!(block.position_state, "single_leg");
        assert_eq!(block.unrealized_pnl.as_deref(), Some("-24"));
        assert_eq!(block.items.unwrap()[0].side, "short");
    }

    #[test]
    fn acceptance_delisted_market_must_not_be_listed() {
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", Some(USER));
        let mut market: Value = serde_json::from_str(META_XYZ).unwrap();
        market[0]["universe"][0]["isDelisted"] = json!(true);
        assert_eq!(
            parse_asset_ctx(&market, "xyz:NBIS").listing,
            MarketListing::Delisted
        );
        let positions = build_positions_block(
            &p.descriptor,
            &ready(serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap()),
            &ready(vec![]),
            &ready(market.clone()),
        );
        assert_eq!(positions.items.as_ref().unwrap().len(), 2);
        assert_eq!(positions.items.as_ref().unwrap()[0].mark_px, None);
        assert_eq!(positions.unrealized_pnl.as_deref(), Some("13.5"));
        assert!(positions.diagnostics.iter().any(|d| d.contains("已下架")));

        let funding = build_funding_block(
            &p.descriptor,
            &ready(serde_json::from_str(ACCOUNT_TWO_LEGS).unwrap()),
            &ready(market),
            &Timed::not_attempted(),
            0,
            48 * HOUR_MS,
        );
        assert_eq!(funding.hourly_rates["xyz:NBIS"], None);
        assert!(funding.daily_estimate.is_none());
        assert_ne!(funding.status, BlockStatus::Ok);
    }

    #[test]
    fn acceptance_ma_warmup_must_cover_first_display_day() {
        let now = realtime_ms();
        let today = DateTime::<Utc>::from_timestamp_millis(now as i64)
            .unwrap()
            .date_naive();
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", None);
        let block = build_ratio_block(
            &p.descriptor,
            &Timed::not_attempted(),
            &None,
            &ready(candles_vec(now, "xyz:NBIS", "80", 121)),
            &ready(candles_vec(now, "xyz:CRWV", "10", 121)),
            now,
            today,
        );
        let points = block.points.unwrap();
        assert_eq!(points.len(), 90);
        assert_eq!(points[0].ma30.as_deref(), Some("8"));
        assert_eq!(points[89].ma30.as_deref(), Some("8"));
    }

    #[test]
    fn ma_boundaries_and_warmup_gap_are_preserved() {
        let now = realtime_ms();
        let today = DateTime::<Utc>::from_timestamp_millis(now as i64)
            .unwrap()
            .date_naive();
        let p = pair(["xyz:NBIS", "xyz:CRWV"], "xyz", None);
        let ratio = |first, second| {
            build_ratio_block(
                &p.descriptor,
                &Timed::not_attempted(),
                &None,
                &ready(first),
                &ready(second),
                now,
                today,
            )
        };
        let short = ratio(
            candles_vec(now, "xyz:NBIS", "80", 29),
            candles_vec(now, "xyz:CRWV", "10", 29),
        );
        assert_eq!(short.points.unwrap().last().unwrap().ma30, None);
        let enough = ratio(
            candles_vec(now, "xyz:NBIS", "80", 30),
            candles_vec(now, "xyz:CRWV", "10", 30),
        );
        assert_eq!(
            enough.points.unwrap().last().unwrap().ma30.as_deref(),
            Some("8")
        );

        let mut first = candles_vec(now, "xyz:NBIS", "80", 121);
        first.remove(10);
        let gapped = ratio(first, candles_vec(now, "xyz:CRWV", "10", 121));
        assert_eq!(gapped.points.unwrap()[0].ma30, None);
    }
}
