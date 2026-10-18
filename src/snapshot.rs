//! `status --json` 结构化快照（stock_data 看板契约 §4.1，schema_version 1）。
//!
//! - stdout 只含一个紧凑 JSON 对象；打印只发生在 main。本模块不做任何输出。
//! - 五个区块并发（tokio::join!），块内来源串行；每个外部来源查询带 8s 期限。
//! - 单源失败只降级依赖它的字段/区块；diagnostics 为通用中文描述，不含本机路径。
//! - 金额/价格/数量/费率一律十进制字符串；未知为 null，绝不补 0 或空串。
//! - 全程只读：绝不装载私钥、绝不触发写操作。

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use hypersdk::Address;
use rust_decimal::Decimal;
use serde::Serialize;
use tokio::time::timeout;

use crate::config::{DashboardConfig, NETWORK};
use crate::hyperliquid::{Adapter, AdapterError, FundingPaymentView};
use crate::intel::{collect_dashboard, dashboard_sources, ratio_metrics};
use crate::pair::{LONG_COIN, PairStore, Phase, SHORT_COIN};

pub const SCHEMA_VERSION: u32 = 1;
/// 契约固定网络值；内部常量 NETWORK（"hyperliquid-testnet"）到合同值的映射在此完成。
const NETWORK_JSON: &str = "testnet";
/// 单源查询期限（契约：每类外部查询 6–8s）。
const SOURCE_TIMEOUT: Duration = Duration::from_secs(8);
/// 日线拉取根数（对齐后保证 ≥30 个共同收盘日，与文本看板同口径）。
const CANDLE_DAYS: u32 = 32;
const HOURS_PER_DAY: Decimal = Decimal::from_parts(24, 0, 0, false, 0);
const HOURS_PER_YEAR: Decimal = Decimal::from_parts(8760, 0, 0, false, 0);
const HOUR_MS: u64 = 3_600_000;
const MAX_ANNOUNCEMENTS: usize = 10;
const UNLOCKS_NOTE: &str = "日历预告不等于实际申领已发生";
const EPOCH_RFC3339: &str = "1970-01-01T00:00:00Z";

// ---------- DTO（字段名与类型即契约；数值一律 String/Option<String>） ----------

#[derive(Debug, Serialize)]
pub struct Snapshot {
    pub schema_version: u32,
    pub network: &'static str,
    pub account_address: String,
    pub queried_at: String,
    pub blocks: Blocks,
}

#[derive(Debug, Serialize)]
pub struct Blocks {
    pub positions: PositionsBlock,
    pub funding: FundingBlock,
    pub ratio: RatioBlock,
    pub unlocks: UnlocksBlock,
    pub announcements: AnnouncementsBlock,
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
    queried_at: String,
}

impl SourceQuery {
    fn new(ok: bool, queried_at: String) -> Self {
        Self {
            status: if ok { "ok" } else { "unavailable" },
            queried_at,
        }
    }
    fn unavailable(queried_at: String) -> Self {
        Self::new(false, queried_at)
    }
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

#[derive(Debug, Serialize)]
pub struct PositionsBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: PositionsSources,
    pub equity: Option<String>,
    pub positions: Option<Vec<PositionItem>>,
    pub notional_ratio: Option<String>,
    pub ratio_drift: Option<String>,
    pub action_state: &'static str,
    pub open_orders: Option<u64>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct PositionsSources {
    account: SourceQuery,
    market: SourceQuery,
    pair_state: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct PositionItem {
    coin: String,
    side: String,
    size: String,
    mark_px: Option<String>,
    unrealized_pnl: String,
    notional: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct FundingBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: FundingSources,
    pub hourly_rates: FundingRates,
    pub annualized: FundingRates,
    pub daily_estimate: Option<DailyEstimate>,
    pub window_start: String,
    pub window_end: String,
    pub window_complete: bool,
    pub actual_48h_net: Option<String>,
    pub recent_payments: Option<Vec<PaymentItem>>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct FundingSources {
    account: SourceQuery,
    market: SourceQuery,
    user_funding: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct FundingRates {
    #[serde(rename = "HYPE")]
    hype: Option<String>,
    #[serde(rename = "BTC")]
    btc: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct DailyEstimate {
    direction: &'static str,
    amount: String,
}

#[derive(Debug, Serialize)]
pub struct PaymentItem {
    time: String,
    coin: String,
    usdc: String,
    rate: String,
}

#[derive(Debug, Serialize)]
pub struct RatioBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: RatioSources,
    pub current: Option<String>,
    pub ma30: Option<String>,
    pub vs_ma30_pct: Option<String>,
    pub as_of_date: Option<String>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct RatioSources {
    candles: SourceQuery,
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
pub struct UnlocksSources {
    config: SourceQuery,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AnchorItem {
    date: String,
    days_ahead: i64,
    label: String,
    source_url: String,
    verified_utc: String,
}

#[derive(Debug, Serialize)]
pub struct AnnouncementsBlock {
    pub status: BlockStatus,
    pub queried_at: String,
    pub source_queries: AnnouncementSources,
    pub items: Vec<AnnouncementItem>,
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct AnnouncementSources {
    general: SourceQuery,
    api: SourceQuery,
}

#[derive(Debug, Serialize)]
pub struct AnnouncementItem {
    kind: &'static str,
    text: String,
    published_at: Option<String>,
    url: String,
    source: &'static str,
}

// ---------- 入口 ----------

/// 采集一次完整快照（生产入口：真实时钟 + 官方 Telegram 预览地址）。
pub async fn collect(adapter: &Adapter, cfg: &DashboardConfig) -> Snapshot {
    collect_inner(adapter, cfg, realtime_ms(), "https://t.me/s").await
}

/// 序列化为单行紧凑 JSON（stdout 唯一内容的来源；打印只发生在 main）。
pub fn to_json_line(snapshot: &Snapshot) -> Result<String, serde_json::Error> {
    serde_json::to_string(snapshot)
}

async fn collect_inner(
    adapter: &Adapter,
    cfg: &DashboardConfig,
    now_ms: u64,
    announcements_base: &str,
) -> Snapshot {
    let generated_at = rfc3339_of(now_ms);
    let today = DateTime::<Utc>::from_timestamp_millis(now_ms as i64)
        .map(|d| d.date_naive())
        .unwrap_or_else(|| Utc::now().date_naive());
    // 资金费窗口在查询开始时固定：end = now，start = end − 48h，分页过程不滚动。
    let window_end_ms = now_ms;
    let window_start_ms = now_ms.saturating_sub(48 * HOUR_MS);

    let Some(user) = cfg.account_address().ok() else {
        return all_unavailable(
            cfg,
            &generated_at,
            window_start_ms,
            window_end_ms,
            "账户地址非法，无法生成快照",
        );
    };

    let (positions, funding, ratio, unlocks, announcements) = tokio::join!(
        collect_positions(adapter, cfg, user),
        collect_funding(adapter, user, window_start_ms, window_end_ms),
        collect_ratio(adapter, now_ms),
        async { collect_unlocks(cfg, today, generated_at.clone()) },
        collect_announcements(announcements_base),
    );

    Snapshot {
        schema_version: SCHEMA_VERSION,
        network: NETWORK_JSON,
        account_address: format!("{user}").to_lowercase(),
        queried_at: generated_at,
        blocks: Blocks {
            positions,
            funding,
            ratio,
            unlocks,
            announcements,
        },
    }
}

fn all_unavailable(
    cfg: &DashboardConfig,
    generated_at: &str,
    window_start_ms: u64,
    window_end_ms: u64,
    reason: &str,
) -> Snapshot {
    let diagnostics = vec![reason.to_string()];
    let q = || SourceQuery::unavailable(generated_at.to_string());
    Snapshot {
        schema_version: SCHEMA_VERSION,
        network: NETWORK_JSON,
        account_address: cfg.account_address.clone(),
        queried_at: generated_at.to_string(),
        blocks: Blocks {
            positions: PositionsBlock {
                status: BlockStatus::Unavailable,
                queried_at: generated_at.to_string(),
                source_queries: PositionsSources {
                    account: q(),
                    market: q(),
                    pair_state: q(),
                },
                equity: None,
                positions: None,
                notional_ratio: None,
                ratio_drift: None,
                action_state: "unknown",
                open_orders: None,
                diagnostics: diagnostics.clone(),
            },
            funding: FundingBlock {
                status: BlockStatus::Unavailable,
                queried_at: generated_at.to_string(),
                source_queries: FundingSources {
                    account: q(),
                    market: q(),
                    user_funding: q(),
                },
                hourly_rates: FundingRates {
                    hype: None,
                    btc: None,
                },
                annualized: FundingRates {
                    hype: None,
                    btc: None,
                },
                daily_estimate: None,
                window_start: rfc3339_of(window_start_ms),
                window_end: rfc3339_of(window_end_ms),
                window_complete: false,
                actual_48h_net: None,
                recent_payments: None,
                diagnostics: diagnostics.clone(),
            },
            ratio: RatioBlock {
                status: BlockStatus::Unavailable,
                queried_at: generated_at.to_string(),
                source_queries: RatioSources { candles: q() },
                current: None,
                ma30: None,
                vs_ma30_pct: None,
                as_of_date: None,
                diagnostics: diagnostics.clone(),
            },
            unlocks: UnlocksBlock {
                status: BlockStatus::Unavailable,
                queried_at: generated_at.to_string(),
                source_queries: UnlocksSources { config: q() },
                anchors: Vec::new(),
                note: UNLOCKS_NOTE,
                diagnostics: diagnostics.clone(),
            },
            announcements: AnnouncementsBlock {
                status: BlockStatus::Unavailable,
                queried_at: generated_at.to_string(),
                source_queries: AnnouncementSources {
                    general: q(),
                    api: q(),
                },
                items: Vec::new(),
                diagnostics,
            },
        },
    }
}

// ---------- 通用工具 ----------

fn realtime_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn rfc3339_of(ms: u64) -> String {
    DateTime::<Utc>::from_timestamp_millis(ms as i64)
        .map(|d| d.to_rfc3339_opts(SecondsFormat::Secs, true))
        .unwrap_or_else(|| EPOCH_RFC3339.to_string())
}

fn now_rfc3339() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn short_reason(e: &AdapterError) -> &'static str {
    match e {
        AdapterError::Validation(_) => "参数校验失败",
        AdapterError::Unknown(_) => "网络错误或超时",
        AdapterError::Permission(_) => "无权限",
        AdapterError::BadResponse(_) => "响应格式无法识别",
        AdapterError::UnknownCoin(_) => "交易对不存在",
        AdapterError::Clock(_) => "系统时间异常",
    }
}

/// 单源期限包装：Ok(Some(v)) 成功；Ok(None) 附带通用失败原因。
async fn timed<T, E>(
    fut: impl std::future::Future<Output = Result<T, E>>,
    describe: impl Fn(&E) -> String,
) -> (Option<T>, Option<String>) {
    match timeout(SOURCE_TIMEOUT, fut).await {
        Ok(Ok(v)) => (Some(v), None),
        Ok(Err(e)) => (None, Some(describe(&e))),
        Err(_) => (None, Some("查询超时".to_string())),
    }
}

async fn timed_adapter<T>(
    fut: impl std::future::Future<Output = Result<T, AdapterError>>,
) -> (Option<T>, Option<String>) {
    timed(fut, |e| short_reason(e).to_string()).await
}

async fn timed_text<T>(
    fut: impl std::future::Future<Output = Result<T, String>>,
) -> (Option<T>, Option<String>) {
    timed(fut, |e| e.clone()).await
}

fn phase_json(phase: Phase) -> &'static str {
    match phase {
        Phase::Prepared => "pair_prepared",
        Phase::Opening => "pair_opening",
        Phase::Open => "pair_open",
        Phase::Closing => "pair_closing",
        Phase::Compensating => "pair_compensating",
        Phase::Incomplete => "pair_incomplete",
        Phase::Unknown => "pair_unknown",
        Phase::Closed => "pair_closed",
        Phase::Failed => "pair_failed",
    }
}

// ---------- 块 1：仓位 ----------

async fn collect_positions(
    adapter: &Adapter,
    cfg: &DashboardConfig,
    user: Address,
) -> PositionsBlock {
    let mut diagnostics = Vec::new();

    let (account, account_reason) = timed_adapter(adapter.account(user)).await;
    let account_at = now_rfc3339();
    let account_ok = account_reason.is_none();
    if let Some(reason) = &account_reason {
        diagnostics.push(format!(
            "账户查询失败（{reason}），权益、持仓与挂单不可用，未按空仓处理"
        ));
    }

    let (ctx, market_reason) = timed_adapter(async {
        let hype = adapter.asset_ctx(LONG_COIN).await?;
        let btc = adapter.asset_ctx(SHORT_COIN).await?;
        Ok::<_, AdapterError>((hype, btc))
    })
    .await;
    let market_at = now_rfc3339();
    let market_ok = market_reason.is_none();
    if let Some(reason) = &market_reason {
        diagnostics.push(format!(
            "行情查询失败（{reason}），标记价格与名义金额不可用"
        ));
    }

    let (action_state, pair_ok, pair_diag) = action_state_of(cfg);
    let pair_at = now_rfc3339();
    if let Some(d) = pair_diag {
        diagnostics.push(d);
    }

    let equity = account.as_ref().map(|a| a.account_value.to_string());
    let open_orders = account.as_ref().map(|a| {
        a.open_orders
            .iter()
            .filter(|o| o.coin == LONG_COIN || o.coin == SHORT_COIN)
            .count() as u64
    });

    let mut positions: Option<Vec<PositionItem>> = None;
    let mut notional_long: Option<Decimal> = None;
    let mut notional_short: Option<Decimal> = None;
    if let Some(view) = &account {
        let mut items = Vec::new();
        for coin in [LONG_COIN, SHORT_COIN] {
            let Some(p) = view
                .positions
                .iter()
                .find(|p| p.coin == coin && !p.size.is_zero())
            else {
                continue;
            };
            // mark 缺失或非正：mark_px 与 notional 均为 null 并诊断（契约硬要求）。
            let mark = ctx
                .as_ref()
                .map(|(h, b)| {
                    if coin == LONG_COIN {
                        h.mark_px
                    } else {
                        b.mark_px
                    }
                })
                .filter(|m| *m > Decimal::ZERO);
            if mark.is_none() && market_ok {
                diagnostics.push(format!("{coin} 标记价格缺失或非正数，当前名义不可用"));
            }
            let size = p.size.abs();
            let notional_raw = mark.map(|m| size * m);
            if coin == LONG_COIN {
                notional_long = notional_raw;
            } else {
                notional_short = notional_raw;
            }
            items.push(PositionItem {
                coin: coin.to_string(),
                side: p.side.to_string(),
                size: size.to_string(),
                mark_px: mark.map(|m| m.to_string()),
                unrealized_pnl: p.unrealized_pnl.to_string(),
                notional: notional_raw.map(|n| n.round_dp(2).to_string()),
            });
        }
        let hype_pos = view
            .positions
            .iter()
            .find(|p| p.coin == LONG_COIN && !p.size.is_zero());
        let btc_pos = view
            .positions
            .iter()
            .find(|p| p.coin == SHORT_COIN && !p.size.is_zero());
        if let (Some(h), Some(b)) = (hype_pos, btc_pos)
            && h.side == b.side
        {
            diagnostics.push(format!(
                "两腿同方向持仓（均为{}），不符合 HYPE 多/BTC 空组合，不能视为对冲正常",
                if h.side == "long" { "多头" } else { "空头" }
            ));
        }
        positions = Some(items);
    }

    // 名义比与漂移：用未舍入名义计算，再分别保留 4 位 / 2 位小数。
    let (notional_ratio, ratio_drift) = match (notional_long, notional_short) {
        (Some(nl), Some(ns)) if nl > Decimal::ZERO && ns > Decimal::ZERO => (
            Some((nl / ns).round_dp(4).to_string()),
            Some(
                ((nl - ns).abs() / ((nl + ns) / Decimal::TWO) * Decimal::ONE_HUNDRED)
                    .round_dp(2)
                    .to_string(),
            ),
        ),
        _ => {
            if account.is_some() {
                let has_long = notional_long.is_some();
                let has_short = notional_short.is_some();
                let why = match (has_long, has_short) {
                    (false, false) => "无 HYPE/BTC 持仓，名义比与漂移不适用",
                    (true, true) => "任一腿名义非正，名义比与漂移不可用",
                    _ => "仅单腿持仓，另一腿名义为零或缺失，名义比与漂移不可用",
                };
                diagnostics.push(why.to_string());
            }
            (None, None)
        }
    };

    let status = block_status(&[account_ok, market_ok, pair_ok], &diagnostics);
    PositionsBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: PositionsSources {
            account: SourceQuery::new(account_ok, account_at),
            market: SourceQuery::new(market_ok, market_at),
            pair_state: SourceQuery::new(pair_ok, pair_at),
        },
        equity,
        positions,
        notional_ratio,
        ratio_drift,
        action_state,
        open_orders,
        diagnostics,
    }
}

/// 读取本地 pair 动作状态（只读，绝不获取写锁或修改权限）。
/// 返回 (action_state, 来源是否可用, 诊断)。账户/网络不匹配或状态不可用一律 "unknown"。
fn action_state_of(cfg: &DashboardConfig) -> (&'static str, bool, Option<String>) {
    let store = PairStore::new(cfg.receipt_dir.clone());
    match store.load() {
        Ok(Some(state)) => {
            let identity_ok = state.network == NETWORK
                && state.account.parse::<Address>().ok() == cfg.account_address().ok();
            if !identity_ok {
                return (
                    "unknown",
                    false,
                    Some("本地动作状态属于其他账户或网络，未采用".to_string()),
                );
            }
            match state.phase {
                Phase::Unknown => ("pair_unknown", true, Some("本地动作结果待核对".to_string())),
                phase => (phase_json(phase), true, None),
            }
        }
        Ok(None) => (
            "unknown",
            false,
            Some("本地动作状态不存在，无法核对".to_string()),
        ),
        Err(_) => (
            "unknown",
            false,
            Some("本地动作状态不可读或解析失败".to_string()),
        ),
    }
}

// ---------- 块 2：资金费 ----------

async fn collect_funding(
    adapter: &Adapter,
    user: Address,
    window_start_ms: u64,
    window_end_ms: u64,
) -> FundingBlock {
    let mut diagnostics = Vec::new();

    let (account, account_reason) = timed_adapter(adapter.account(user)).await;
    let account_at = now_rfc3339();
    let account_ok = account_reason.is_none();
    if let Some(reason) = &account_reason {
        diagnostics.push(format!("账户查询失败（{reason}），日净收付估算不可用"));
    }

    let (ctx, market_reason) = timed_adapter(async {
        let hype = adapter.asset_ctx(LONG_COIN).await?;
        let btc = adapter.asset_ctx(SHORT_COIN).await?;
        Ok::<_, AdapterError>((hype, btc))
    })
    .await;
    let market_at = now_rfc3339();
    let market_ok = market_reason.is_none();
    if let Some(reason) = &market_reason {
        diagnostics.push(format!("行情查询失败（{reason}），资金费率不可用"));
    }

    let rate_of = |coin: &str| -> Option<Decimal> {
        ctx.as_ref().map(|(h, b)| {
            if coin == LONG_COIN {
                h.funding
            } else {
                b.funding
            }
        })
    };
    let hourly_rates = FundingRates {
        hype: rate_of(LONG_COIN).map(|r| r.to_string()),
        btc: rate_of(SHORT_COIN).map(|r| r.to_string()),
    };
    let annualized = FundingRates {
        hype: rate_of(LONG_COIN).map(|r| {
            (r * HOURS_PER_YEAR * Decimal::ONE_HUNDRED)
                .round_dp(2)
                .to_string()
        }),
        btc: rate_of(SHORT_COIN).map(|r| {
            (r * HOURS_PER_YEAR * Decimal::ONE_HUNDRED)
                .round_dp(2)
                .to_string()
        }),
    };

    // 日净收付估算：net = −Σ(szi × oraclePx × rate) × 24（与文本看板同口径）。
    let daily_estimate = match (&account, &ctx) {
        (Some(view), Some((hype, btc))) => {
            let szi_of = |coin: &str| -> Decimal {
                view.positions
                    .iter()
                    .find(|p| p.coin == coin)
                    .map(|p| p.size)
                    .unwrap_or(Decimal::ZERO)
            };
            let (h, b) = (szi_of(LONG_COIN), szi_of(SHORT_COIN));
            if h.is_zero() && b.is_zero() {
                None
            } else if hype.oracle_px <= Decimal::ZERO || btc.oracle_px <= Decimal::ZERO {
                diagnostics.push("预言机价格非正数，日净收付估算不可用".to_string());
                None
            } else {
                let net = -(h * hype.oracle_px * hype.funding + b * btc.oracle_px * btc.funding)
                    * HOURS_PER_DAY;
                Some(DailyEstimate {
                    direction: if net >= Decimal::ZERO {
                        "receive"
                    } else {
                        "pay"
                    },
                    amount: net.abs().round_dp(4).to_string(),
                })
            }
        }
        _ => None,
    };

    // userFunding：窗口在采集开始时已固定，按 [start, end) 过滤、按 HYPE/BTC 过滤。
    let (entries, funding_reason) =
        timed_adapter(adapter.user_funding_since(user, window_start_ms)).await;
    let funding_at = now_rfc3339();
    let funding_ok = funding_reason.is_none();
    let (window_complete, actual_48h_net, recent_payments) = match entries {
        Some(entries) => {
            let pair: Vec<&FundingPaymentView> = entries
                .iter()
                .filter(|e| e.time_ms >= window_start_ms && e.time_ms < window_end_ms)
                .filter(|e| e.coin == LONG_COIN || e.coin == SHORT_COIN)
                .collect();
            let net: Decimal = pair.iter().map(|e| e.usdc).sum();
            let mut by_time = pair;
            by_time.sort_by_key(|e| std::cmp::Reverse(e.time_ms));
            let mut recent = Vec::new();
            let mut dropped = 0usize;
            for e in by_time.iter().take(3) {
                match DateTime::<Utc>::from_timestamp_millis(e.time_ms as i64) {
                    Some(t) => recent.push(PaymentItem {
                        time: t.to_rfc3339_opts(SecondsFormat::Secs, true),
                        coin: e.coin.clone(),
                        usdc: e.usdc.to_string(),
                        rate: e.funding_rate.to_string(),
                    }),
                    None => dropped += 1,
                }
            }
            if dropped > 0 {
                diagnostics.push(format!("{dropped} 条资金费收付记录时间无法解析，未展示"));
            }
            (true, Some(net.to_string()), Some(recent))
        }
        None => {
            diagnostics.push(format!(
                "资金费收付历史查询失败（{}），实际 48 小时净收付不可用",
                funding_reason.unwrap_or_else(|| "未知原因".to_string())
            ));
            (false, None, None)
        }
    };

    let status = block_status(&[account_ok, market_ok, funding_ok], &diagnostics);
    FundingBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: FundingSources {
            account: SourceQuery::new(account_ok, account_at),
            market: SourceQuery::new(market_ok, market_at),
            user_funding: SourceQuery::new(funding_ok, funding_at),
        },
        hourly_rates,
        annualized,
        daily_estimate,
        window_start: rfc3339_of(window_start_ms),
        window_end: rfc3339_of(window_end_ms),
        window_complete,
        actual_48h_net,
        recent_payments,
        diagnostics,
    }
}

// ---------- 块 3：比价与 30MA ----------

async fn collect_ratio(adapter: &Adapter, now_ms: u64) -> RatioBlock {
    let (closes, reason) = timed_adapter(async {
        let hype = adapter.daily_closes(LONG_COIN, CANDLE_DAYS).await?;
        let btc = adapter.daily_closes(SHORT_COIN, CANDLE_DAYS).await?;
        Ok::<_, AdapterError>((hype, btc))
    })
    .await;
    let at = now_rfc3339();

    let (candles_ok, metrics, diagnostics) = match closes {
        Some((h, b)) => match ratio_metrics(h, b, now_ms) {
            Ok(m) => (true, Some(m), Vec::new()),
            Err(why) => (false, None, vec![format!("日线数据不可用（{why}）")]),
        },
        None => (
            false,
            None,
            vec![format!(
                "日线查询失败（{}）",
                reason.unwrap_or_else(|| "未知原因".to_string())
            )],
        ),
    };

    let status = block_status(&[candles_ok], &diagnostics);
    RatioBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: RatioSources {
            candles: SourceQuery::new(candles_ok, at),
        },
        current: metrics.as_ref().map(|m| m.current.to_string()),
        ma30: metrics.as_ref().map(|m| m.ma30.to_string()),
        vs_ma30_pct: metrics.as_ref().map(|m| m.vs_ma30_pct.to_string()),
        as_of_date: metrics.as_ref().map(|m| m.as_of_date.clone()),
        diagnostics,
    }
}

// ---------- 块 4：解锁锚点（纯配置数据，queried_at = 快照生成时间） ----------

fn collect_unlocks(cfg: &DashboardConfig, today: NaiveDate, generated_at: String) -> UnlocksBlock {
    let mut diagnostics = Vec::new();
    let mut anchors: Vec<AnchorItem> = Vec::new();
    let mut valid_count = 0usize;
    let mut seen = std::collections::BTreeSet::new();

    for a in &cfg.unlock_anchors {
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
        anchors.push(AnchorItem {
            date: date.to_string(),
            days_ahead,
            label: a.label.clone(),
            source_url: a.source_url.clone(),
            verified_utc: a.verified_utc.clone(),
        });
    }
    anchors.sort_by(|x, y| x.date.cmp(&y.date).then_with(|| x.label.cmp(&y.label)));

    if cfg.unlock_anchors.is_empty() {
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
            config: SourceQuery::new(true, generated_at),
        },
        anchors,
        note: UNLOCKS_NOTE,
        diagnostics,
    }
}

// ---------- 块 5：官方动态 ----------

/// v1/v2 共用的公告块采集；v2（`dashboard --announcements --json`）直接复用。
pub(crate) async fn collect_announcements(base: &str) -> AnnouncementsBlock {
    let mut diagnostics = Vec::new();
    let mut items: Vec<AnnouncementItem> = Vec::new();
    let mut sources_ok = Vec::new();
    let mut general_query = SourceQuery::unavailable(now_rfc3339());
    let mut api_query = SourceQuery::unavailable(now_rfc3339());

    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(6))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .ok();

    match &client {
        Some(client) => {
            for source in &dashboard_sources() {
                let (outcome, reason) = timed_text(collect_dashboard(client, base, source)).await;
                let at = now_rfc3339();
                let query = SourceQuery::new(outcome.is_some(), at);
                match source.kind {
                    "general" => general_query = query,
                    _ => api_query = query,
                }
                match outcome {
                    Some(outcome) => {
                        sources_ok.push(true);
                        if outcome.missing_time > 0 {
                            diagnostics.push(format!(
                                "{} 频道 {} 条公告缺少发布时间，published_at 为 null",
                                source.kind, outcome.missing_time
                            ));
                        }
                        if outcome.invalid > 0 {
                            diagnostics.push(format!(
                                "{} 频道 {} 条公告无法验证来源或时间，未收录",
                                source.kind, outcome.invalid
                            ));
                        }
                        for entry in outcome.entries {
                            items.push(AnnouncementItem {
                                kind: source.kind,
                                text: entry.text,
                                published_at: entry
                                    .published
                                    .map(|d| d.to_rfc3339_opts(SecondsFormat::Secs, true)),
                                url: entry.url,
                                source: "telegram",
                            });
                        }
                    }
                    None => {
                        sources_ok.push(false);
                        diagnostics.push(format!(
                            "{} 频道公告查询失败（{}）",
                            source.kind,
                            reason.unwrap_or_else(|| "未知原因".to_string())
                        ));
                    }
                }
            }
        }
        None => {
            sources_ok.push(false);
            sources_ok.push(false);
            diagnostics.push("HTTP 客户端构建失败，公告频道不可用".to_string());
        }
    }

    // 最多 10 条：按发布时间倒序，缺时间的排最后（绝不以查询时间补齐）。
    items.sort_by(|a, b| match (&a.published_at, &b.published_at) {
        (Some(x), Some(y)) => y.cmp(x),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => std::cmp::Ordering::Equal,
    });
    items.truncate(MAX_ANNOUNCEMENTS);

    let status = block_status(&sources_ok, &diagnostics);
    AnnouncementsBlock {
        status,
        queried_at: now_rfc3339(),
        source_queries: AnnouncementSources {
            general: general_query,
            api: api_query,
        },
        items,
        diagnostics,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::UnlockAnchor;
    use crate::pair::{LegState, LegStatus, PairState};
    use httpmock::MockServer;
    use serde_json::{Value, json};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    const DAY_MS: u64 = 86_400_000;

    static DIR_SEQ: AtomicU64 = AtomicU64::new(0);

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hltrader-snap-{}-{}",
            std::process::id(),
            DIR_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    const ACCOUNT_ADDRESS: &str = "0x7F2120F612B78D579197762196B8A202AB654096";

    // equity=1234.56；HYPE 多 120.5（uPNL 5.10）；BTC 空 0.06（uPNL −1.00）。
    const ACCOUNT: &str = r#"{
        "marginSummary": {"accountValue": "1234.56", "totalNtlPos": "7229.46", "totalRawUsd": "1234.56", "totalMarginUsed": "0.0"},
        "crossMarginSummary": {"accountValue": "1234.56", "totalNtlPos": "7229.46", "totalRawUsd": "1234.56", "totalMarginUsed": "0.0"},
        "crossMaintenanceMarginUsed": "0.0", "withdrawable": "1234.56", "time": 1760000000000,
        "assetPositions": [
            {"type": "oneWay", "position": {"coin": "HYPE", "szi": "120.5",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "30.0", "positionValue": "3629.46",
                "unrealizedPnl": "5.10", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "0.0",
                "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}},
            {"type": "oneWay", "position": {"coin": "BTC", "szi": "-0.06",
                "leverage": {"type": "cross", "value": 40}, "entryPx": "60000", "positionValue": "3600",
                "unrealizedPnl": "-1.00", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "0.0",
                "maxLeverage": 40, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
        ]
    }"#;

    // 两腿挂单 + 一个其他币种挂单（open_orders 只应计 2）。
    const OPEN_ORDERS: &str = r#"[
        {"timestamp": 1760000000000, "coin": "HYPE", "side": "B", "limitPx": "30", "sz": "1",
         "oid": 11, "origSz": "1", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false},
        {"timestamp": 1760000000000, "coin": "BTC", "side": "A", "limitPx": "60000", "sz": "0.01",
         "oid": 12, "origSz": "0.01", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false},
        {"timestamp": 1760000000000, "coin": "ETH", "side": "B", "limitPx": "3000", "sz": "1",
         "oid": 13, "origSz": "1", "cloid": null, "orderType": "Limit", "tif": "Alo", "reduceOnly": false}
    ]"#;

    // HYPE: funding 0.0000123 / mark 30.12 / oracle 30.11；BTC: 0.00001 / 60000 / 60010。
    const META_CTXS: &str = r#"[
        {"universe": [
            {"szDecimals": 2, "name": "HYPE", "maxLeverage": 5, "onlyIsolated": false},
            {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false}
        ]},
        [
            {"funding": "0.0000123", "markPx": "30.12", "oraclePx": "30.11"},
            {"funding": "0.00001", "markPx": "60000", "oraclePx": "60010"}
        ]
    ]"#;

    const META_CTXS_HYPE_MARK_ZERO: &str = r#"[
        {"universe": [
            {"szDecimals": 2, "name": "HYPE", "maxLeverage": 5, "onlyIsolated": false},
            {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false}
        ]},
        [
            {"funding": "0.0000123", "markPx": "0", "oraclePx": "30.11"},
            {"funding": "0.00001", "markPx": "60000", "oraclePx": "60010"}
        ]
    ]"#;

    fn dashboard_cfg(dir: &Path, anchors: Vec<UnlockAnchor>) -> DashboardConfig {
        DashboardConfig {
            account_address: ACCOUNT_ADDRESS.to_string(),
            receipt_dir: dir.to_path_buf(),
            unlock_anchors: anchors,
        }
    }

    fn anchor(date_utc: &str, label: &str, url: &str, verified: &str) -> UnlockAnchor {
        UnlockAnchor {
            date_utc: date_utc.to_string(),
            label: label.to_string(),
            source_url: url.to_string(),
            verified_utc: verified.to_string(),
        }
    }

    fn anchor_in_days(today: NaiveDate, days: i64, label: &str) -> UnlockAnchor {
        anchor(
            &(today + chrono::Duration::days(days)).to_string(),
            label,
            "https://example.com/src",
            "2026-10-01",
        )
    }

    // PairStore 种子状态（参照 pair.rs 测试的 seed_state 模式）。
    fn seed_state(cfg: &DashboardConfig, phase: Phase) -> PairState {
        let now_ms = 1760000000000u64;
        let leg = |coin: &str, side: &str, qty: &str, cloid: &str| LegState {
            coin: coin.into(),
            side: side.into(),
            target_qty: qty.parse().unwrap(),
            protect_px: Decimal::new(40, 0),
            cloid: cloid.into(),
            status: LegStatus::Filled,
            confirmed_qty: qty.parse().unwrap(),
            avg_px: Some(Decimal::new(40, 0)),
            oid: Some(11),
        };
        PairState {
            external_closure: None,
            accounting_start_ms: Some(now_ms),
            prior_oids: Vec::new(),
            prior_quantities: Vec::new(),
            audit_complete: true,
            version: 2,
            network: NETWORK.into(),
            account: cfg.account_address.clone(),
            action_id: "previous".into(),
            action: "open".into(),
            phase,
            created_at_utc: "2026-10-10T00:00:00Z".into(),
            created_at_ms: now_ms,
            equity_at_prepare: Decimal::new(1000, 0),
            long_leg: leg(
                LONG_COIN,
                "buy",
                "2.49",
                "0x11111111111111111111111111111111",
            ),
            short_leg: leg(
                SHORT_COIN,
                "sell",
                "0.00124",
                "0x22222222222222222222222222222222",
            ),
            start_positions: Vec::new(),
            unwinds: Vec::new(),
            detail: None,
            closed_at_utc: None,
        }
    }

    fn save_state(dir: &Path, state: &PairState) {
        fs::write(
            dir.join("pair-state.json"),
            serde_json::to_string(state).unwrap(),
        )
        .unwrap();
    }

    fn funding_entry(coin: &str, usdc: &str, rate: &str, time_ms: u64) -> Value {
        json!({
            "delta": {"type": "funding", "coin": coin, "usdc": usdc, "szi": "1",
                      "fundingRate": rate, "nSamples": null},
            "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
            "time": time_ms
        })
    }

    /// 标准 48 小时窗口内：HYPE +0.312（end−4h）、BTC −0.55（end−2h）、ETH 9.99（应被过滤）。
    fn standard_funding(now_ms: u64) -> String {
        json!([
            funding_entry("HYPE", "0.312", "0.0000123", now_ms - 4 * HOUR_MS),
            funding_entry("BTC", "-0.55", "0.00001", now_ms - 2 * HOUR_MS),
            funding_entry("ETH", "9.99", "0.0001", now_ms - 3 * HOUR_MS),
        ])
        .to_string()
    }

    fn candle(close_time: u64, coin: &str, close: &str) -> Value {
        json!({
            "t": close_time - DAY_MS, "T": close_time, "s": coin, "i": "1d",
            "o": close, "h": close, "l": close, "c": close, "v": "1", "n": 1
        })
    }

    /// 32 根已收盘日线：日期 today−32..today−1；最后一天可选抬高收盘价。
    fn candles_json(now_ms: u64, coin: &str, close: &str, last_close: Option<&str>) -> String {
        let today0 = now_ms - (now_ms % DAY_MS);
        let closes: Vec<Value> = (0..32)
            .rev()
            .map(|i| {
                let t = today0 - i * DAY_MS - 1;
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

    fn tg_message(channel: &str, id: u64, date: Option<&str>, body: &str) -> String {
        let time = date
            .map(|d| format!(r#"<time datetime="{d}"></time>"#))
            .unwrap_or_default();
        format!(
            r#"<div class="tgme_widget_message" data-post="{channel}/{id}"><div class="tgme_widget_message_text">{body}</div><a class="tgme_widget_message_date" href="https://t.me/{channel}/{id}">{time}</a></div>"#
        )
    }

    fn mock_clearinghouse(server: &MockServer, body: &'static str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(body);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body(OPEN_ORDERS);
        });
    }

    fn mock_exchange(server: &MockServer, meta_ctxs: &'static str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("metaAndAssetCtxs");
            then.status(200).body(meta_ctxs);
        });
    }

    fn mock_candles(server: &MockServer, now_ms: u64, days: usize) {
        for coin in [LONG_COIN, SHORT_COIN] {
            let base_close = if coin == LONG_COIN { "30" } else { "60000" };
            let last = if coin == LONG_COIN {
                Some("30.3")
            } else {
                None
            };
            let body = if days >= 32 {
                candles_json(now_ms, coin, base_close, last)
            } else {
                let today0 = now_ms - (now_ms % DAY_MS);
                let closes: Vec<Value> = (0..days as u64)
                    .rev()
                    .map(|i| candle(today0 - i * DAY_MS - 1, coin, base_close))
                    .collect();
                json!(closes).to_string()
            };
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes("candleSnapshot")
                    .body_includes(coin);
                then.status(200).body(body);
            });
        }
    }

    fn mock_funding(server: &MockServer, body: String) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body(body);
        });
    }

    fn mock_telegram(server: &MockServer, path: &str, status: u16, html: String) {
        server.mock(|when, then| {
            when.method(httpmock::Method::GET).path(path);
            then.status(status as i32)
                .header("content-type", "text/html; charset=utf-8")
                .body(html);
        });
    }

    fn general_html() -> String {
        format!(
            "{}{}",
            tg_message(
                "hyperliquid_announcements",
                2,
                Some("2026-10-16T10:00:00Z"),
                "Announcement A",
            ),
            tg_message(
                "hyperliquid_announcements",
                1,
                Some("2026-10-15T10:00:00Z"),
                "Older A"
            ),
        )
    }

    fn api_html() -> String {
        tg_message(
            "hyperliquid_api",
            5,
            Some("2026-10-17T09:00:00Z"),
            "API update B",
        )
    }

    /// 全源正常的 mock 环境。
    async fn happy_stage(now_ms: u64) -> MockServer {
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());
        server
    }

    fn yesterday(now_ms: u64) -> String {
        DateTime::<Utc>::from_timestamp_millis(now_ms as i64)
            .unwrap()
            .date_naive()
            .pred_opt()
            .unwrap()
            .to_string()
    }

    async fn collect_on(server: &MockServer, cfg: &DashboardConfig, now_ms: u64) -> Snapshot {
        collect_inner(
            &Adapter::with_base_url(&server.base_url()),
            cfg,
            now_ms,
            &server.base_url(),
        )
        .await
    }

    #[tokio::test]
    async fn happy_path_snapshot_contract() {
        let dir = tempdir();
        let cfg = dashboard_cfg(
            &dir,
            vec![anchor_in_days(Utc::now().date_naive(), 3, "团队解锁")],
        );
        save_state(&dir, &seed_state(&cfg, Phase::Open));
        let now_ms = realtime_ms();
        let server = happy_stage(now_ms).await;

        let snap = collect_on(&server, &cfg, now_ms).await;
        let line = to_json_line(&snap).unwrap();
        let v: Value = serde_json::from_str(&line).unwrap();

        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["network"], "testnet");
        assert_eq!(
            v["account_address"], "0x7f2120f612b78d579197762196b8a202ab654096",
            "账户地址 0x 小写"
        );
        assert!(v["queried_at"].as_str().unwrap().ends_with('Z'));
        let blocks = &v["blocks"];
        for key in ["positions", "funding", "ratio", "unlocks", "announcements"] {
            assert!(blocks.get(key).is_some(), "缺少区块 {key}");
        }

        // ---- positions ----
        let p = &blocks["positions"];
        assert_eq!(p["status"], "ok");
        assert!(p["queried_at"].as_str().unwrap().ends_with('Z'));
        for key in ["account", "market", "pair_state"] {
            let q = &p["source_queries"][key];
            assert_eq!(q["status"], "ok", "来源 {key} 应 ok");
            assert!(q["queried_at"].as_str().unwrap().ends_with('Z'));
        }
        assert!(p["diagnostics"].as_array().unwrap().is_empty());
        assert_eq!(p["equity"], "1234.56");
        assert!(p["equity"].is_string(), "金额必须是字符串");
        assert_eq!(p["notional_ratio"], "1.0082");
        assert_eq!(p["ratio_drift"], "0.81");
        assert_eq!(p["action_state"], "pair_open");
        assert_eq!(p["open_orders"], 2, "只计 HYPE/BTC 挂单");
        let pos = p["positions"].as_array().unwrap();
        assert_eq!(pos.len(), 2);
        assert_eq!(pos[0]["coin"], "HYPE");
        assert_eq!(pos[0]["side"], "long");
        assert_eq!(pos[0]["size"], "120.5");
        assert_eq!(pos[0]["mark_px"], "30.12");
        assert_eq!(pos[0]["unrealized_pnl"], "5.10");
        assert_eq!(pos[0]["notional"], "3629.46");
        assert_eq!(pos[1]["coin"], "BTC");
        assert_eq!(pos[1]["side"], "short");
        assert_eq!(pos[1]["size"], "0.06", "size 为非负绝对值");
        assert_eq!(pos[1]["notional"], "3600.00");

        // ---- funding ----
        let f = &blocks["funding"];
        assert_eq!(f["status"], "ok");
        for key in ["account", "market", "user_funding"] {
            assert_eq!(f["source_queries"][key]["status"], "ok");
        }
        assert_eq!(f["hourly_rates"]["HYPE"], "0.0000123");
        assert_eq!(f["hourly_rates"]["BTC"], "0.00001");
        assert_eq!(f["annualized"]["HYPE"], "10.77");
        assert_eq!(f["annualized"]["BTC"], "8.76");
        let est = &f["daily_estimate"];
        assert_eq!(est["direction"], "pay");
        assert_eq!(est["amount"], "0.2069");
        let window_end = f["window_end"].as_str().unwrap();
        let window_start = f["window_start"].as_str().unwrap();
        assert!(window_end.ends_with('Z') && window_start.ends_with('Z'));
        assert_eq!(
            window_start,
            rfc3339_of(now_ms - 48 * HOUR_MS),
            "window_start = window_end − 48h"
        );
        assert_eq!(f["window_complete"], true);
        assert_eq!(f["actual_48h_net"], "-0.238", "0.312 − 0.55，ETH 不计");
        let recent = f["recent_payments"].as_array().unwrap();
        assert_eq!(recent.len(), 2);
        assert_eq!(recent[0]["coin"], "BTC", "按时间倒序");
        assert_eq!(recent[0]["usdc"], "-0.55");
        assert_eq!(recent[0]["rate"], "0.00001");
        assert!(recent[0]["time"].as_str().unwrap().ends_with('Z'));
        assert_eq!(recent[1]["coin"], "HYPE");

        // ---- ratio ----
        let r = &blocks["ratio"];
        assert_eq!(r["status"], "ok");
        assert_eq!(r["source_queries"]["candles"]["status"], "ok");
        assert!(r["current"].is_string());
        assert!(r["ma30"].is_string());
        assert_eq!(r["vs_ma30_pct"], "0.97");
        assert_eq!(r["as_of_date"], yesterday(now_ms));

        // ---- unlocks ----
        let u = &blocks["unlocks"];
        assert_eq!(u["status"], "ok");
        assert_eq!(u["source_queries"]["config"]["status"], "ok");
        assert_eq!(u["note"], "日历预告不等于实际申领已发生");
        let anchors = u["anchors"].as_array().unwrap();
        assert_eq!(anchors.len(), 1);
        assert_eq!(anchors[0]["days_ahead"], 3);
        assert_eq!(anchors[0]["label"], "团队解锁");
        assert_eq!(anchors[0]["source_url"], "https://example.com/src");
        assert_eq!(anchors[0]["verified_utc"], "2026-10-01");

        // ---- announcements ----
        let a = &blocks["announcements"];
        assert_eq!(a["status"], "ok");
        assert_eq!(a["source_queries"]["general"]["status"], "ok");
        assert_eq!(a["source_queries"]["api"]["status"], "ok");
        let items = a["items"].as_array().unwrap();
        assert_eq!(items.len(), 3);
        for item in items {
            assert!(item["kind"] == "general" || item["kind"] == "api");
            assert_eq!(item["source"], "telegram");
            assert!(item["published_at"].as_str().unwrap().ends_with('Z'));
            assert!(item["url"].as_str().unwrap().starts_with("https://t.me/"));
        }
    }

    #[test]
    fn action_state_phase_mapping_all_nine() {
        let cases = [
            (Phase::Prepared, "pair_prepared"),
            (Phase::Opening, "pair_opening"),
            (Phase::Open, "pair_open"),
            (Phase::Closing, "pair_closing"),
            (Phase::Compensating, "pair_compensating"),
            (Phase::Incomplete, "pair_incomplete"),
            (Phase::Unknown, "pair_unknown"),
            (Phase::Closed, "pair_closed"),
            (Phase::Failed, "pair_failed"),
        ];
        for (phase, expected) in cases {
            assert_eq!(phase_json(phase), expected, "phase {phase:?}");
        }
    }

    #[tokio::test]
    async fn action_state_pair_unknown_is_not_unknown() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        save_state(&dir, &seed_state(&cfg, Phase::Unknown));
        let now_ms = realtime_ms();
        let server = happy_stage(now_ms).await;
        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["action_state"], "pair_unknown");
        assert_eq!(p["source_queries"]["pair_state"]["status"], "ok");
        assert_eq!(p["status"], "partial");
        assert!(
            p["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("待核对"))
        );
    }

    #[tokio::test]
    async fn action_state_identity_mismatch_degrades_only_pair_state() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let mut state = seed_state(&cfg, Phase::Open);
        state.account = "0x2222222222222222222222222222222222222222".into();
        save_state(&dir, &state);
        let now_ms = realtime_ms();
        let server = happy_stage(now_ms).await;
        let snap = collect_on(&server, &cfg, now_ms).await;
        let line = to_json_line(&snap).unwrap();
        let v: Value = serde_json::from_str(&line).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["action_state"], "unknown");
        assert_eq!(p["source_queries"]["pair_state"]["status"], "unavailable");
        assert_eq!(p["status"], "partial");
        assert!(
            p["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("其他账户或网络"))
        );
        // 其余 positions 数据仍在；诊断不得暴露本机路径。
        assert_eq!(p["equity"], "1234.56");
        assert_eq!(p["positions"].as_array().unwrap().len(), 2);
        assert!(!line.contains(dir.to_str().unwrap()), "不得包含本机路径");
    }

    #[tokio::test]
    async fn action_state_missing_or_unreadable_still_keeps_positions() {
        for unreadable in [false, true] {
            let dir = tempdir();
            let cfg = dashboard_cfg(&dir, vec![]);
            if unreadable {
                fs::create_dir(dir.join("pair-state.json")).unwrap();
            }
            let now_ms = realtime_ms();
            let server = happy_stage(now_ms).await;
            let snap = collect_on(&server, &cfg, now_ms).await;
            let line = to_json_line(&snap).unwrap();
            let v: Value = serde_json::from_str(&line).unwrap();
            let p = &v["blocks"]["positions"];
            assert_eq!(p["action_state"], "unknown", "unreadable={unreadable}");
            assert_eq!(
                p["source_queries"]["pair_state"]["status"], "unavailable",
                "unreadable={unreadable}"
            );
            assert_eq!(p["status"], "partial", "unreadable={unreadable}");
            assert_eq!(p["equity"], "1234.56", "其余数据仍在");
            assert!(p["positions"].is_array());
            assert!(!line.contains(dir.to_str().unwrap()));
        }
    }

    #[tokio::test]
    async fn account_failure_degrades_positions_only() {
        let dir = tempdir();
        let cfg = dashboard_cfg(
            &dir,
            vec![anchor_in_days(Utc::now().date_naive(), 2, "投资者解锁")],
        );
        save_state(&dir, &seed_state(&cfg, Phase::Open));
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(500).body("internal error");
        });
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["status"], "partial");
        assert_eq!(p["source_queries"]["account"]["status"], "unavailable");
        assert_eq!(p["source_queries"]["market"]["status"], "ok");
        assert!(p["equity"].is_null());
        assert!(p["positions"].is_null(), "账户失败不得伪装空仓");
        assert!(p["open_orders"].is_null());
        assert!(p["notional_ratio"].is_null());
        // funding 块共享 account 来源：同为 partial，费率与真实收付仍可用。
        let f = &v["blocks"]["funding"];
        assert_eq!(f["status"], "partial");
        assert_eq!(f["source_queries"]["account"]["status"], "unavailable");
        assert_eq!(f["hourly_rates"]["HYPE"], "0.0000123");
        assert_eq!(f["actual_48h_net"], "-0.238", "userFunding 独立于账户查询");
        assert!(f["daily_estimate"].is_null(), "估算依赖持仓");
        assert_eq!(v["blocks"]["ratio"]["status"], "ok");
        assert_eq!(v["blocks"]["unlocks"]["status"], "ok");
        assert_eq!(v["blocks"]["announcements"]["status"], "ok");
    }

    #[tokio::test]
    async fn user_funding_failure_keeps_rates() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(500).body("internal error");
        });
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let f = &v["blocks"]["funding"];
        assert_eq!(f["status"], "partial");
        assert_eq!(f["source_queries"]["user_funding"]["status"], "unavailable");
        assert_eq!(f["hourly_rates"]["HYPE"], "0.0000123", "费率仍可用");
        assert_eq!(f["annualized"]["BTC"], "8.76");
        assert!(f["daily_estimate"].is_object(), "估算与 userFunding 无关");
        assert_eq!(f["window_complete"], false);
        assert!(f["actual_48h_net"].is_null());
        assert!(f["recent_payments"].is_null());
    }

    #[tokio::test]
    async fn funding_window_filters_half_open_interval() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let start = now_ms - 48 * HOUR_MS;
        let body = json!([
            funding_entry("HYPE", "1.0", "0.0000123", start),
            funding_entry("HYPE", "2.0", "0.0000123", now_ms - 1),
            funding_entry("BTC", "4.0", "0.00001", now_ms),
            funding_entry("BTC", "8.0", "0.00001", start + 24 * HOUR_MS),
        ])
        .to_string();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, body);
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let f = &v["blocks"]["funding"];
        assert_eq!(f["window_start"], rfc3339_of(start));
        assert_eq!(f["window_end"], rfc3339_of(now_ms));
        assert_eq!(f["window_complete"], true);
        assert_eq!(
            f["actual_48h_net"], "11.0",
            "1+2+8 入窗，4 在 end 边界被排除"
        );
        let recent = f["recent_payments"].as_array().unwrap();
        assert_eq!(recent.len(), 3, "最多 3 条");
        assert_eq!(recent[0]["time"], rfc3339_of(now_ms - 1), "倒序第一条");
    }

    #[tokio::test]
    async fn empty_funding_window_is_zero_not_null() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, "[]".to_string());
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let f = &v["blocks"]["funding"];
        assert_eq!(f["window_complete"], true);
        assert_eq!(f["actual_48h_net"], "0");
        assert_eq!(f["recent_payments"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn candles_failure_makes_ratio_unavailable() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        save_state(&dir, &seed_state(&cfg, Phase::Open));
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot");
            then.status(500).body("internal error");
        });
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "unavailable");
        assert_eq!(r["source_queries"]["candles"]["status"], "unavailable");
        assert!(r["current"].is_null());
        assert!(r["ma30"].is_null());
        assert!(r["vs_ma30_pct"].is_null());
        assert!(r["as_of_date"].is_null());
        assert_eq!(v["blocks"]["positions"]["status"], "ok");
    }

    #[tokio::test]
    async fn insufficient_candles_degrade_ratio_with_reason() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 10);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let r = &v["blocks"]["ratio"];
        assert_eq!(r["status"], "unavailable");
        assert!(
            r["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("不足 30"))
        );
    }

    #[tokio::test]
    async fn announcements_single_source_failure_is_partial() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        // API 频道预览 302（与线报一致）：按不可用展示。
        server.mock(|when, then| {
            when.method(httpmock::Method::GET).path("/hyperliquid_api");
            then.status(302)
                .header("location", "https://t.me/hyperliquid_api");
        });

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let a = &v["blocks"]["announcements"];
        assert_eq!(a["status"], "partial");
        assert_eq!(a["source_queries"]["general"]["status"], "ok");
        assert_eq!(a["source_queries"]["api"]["status"], "unavailable");
        let items = a["items"].as_array().unwrap();
        assert!(items.iter().all(|i| i["kind"] == "general"));
        assert!(
            a["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("api"))
        );
    }

    #[tokio::test]
    async fn announcements_missing_time_outputs_null_with_diagnostic() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        let html = tg_message("hyperliquid_announcements", 7, None, "No clock post");
        mock_telegram(&server, "/hyperliquid_announcements", 200, html);
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let a = &v["blocks"]["announcements"];
        assert_eq!(a["status"], "partial");
        let items = a["items"].as_array().unwrap();
        let no_clock = items.iter().find(|i| i["text"] == "No clock post").unwrap();
        assert!(
            no_clock["published_at"].is_null(),
            "缺时间 → null，不用查询时间补齐"
        );
        assert!(
            a["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("published_at 为 null"))
        );
    }

    #[tokio::test]
    async fn announcements_both_sources_fail_is_unavailable() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        server.mock(|when, then| {
            when.method(httpmock::Method::GET)
                .path("/hyperliquid_announcements");
            then.status(500).body("boom");
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::GET).path("/hyperliquid_api");
            then.status(500).body("boom");
        });

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let a = &v["blocks"]["announcements"];
        assert_eq!(a["status"], "unavailable");
        assert_eq!(a["source_queries"]["general"]["status"], "unavailable");
        assert_eq!(a["source_queries"]["api"]["status"], "unavailable");
        assert_eq!(a["items"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn unlocks_filters_and_status_rules() {
        let today = NaiveDate::from_ymd_opt(2026, 10, 18).unwrap();
        let d = |days: i64| (today + chrono::Duration::days(days)).to_string();

        // 7 天边界：+7 含，+8 剔除；无效 URL / 未来核实 / 坏日期剔除。
        let cfg = dashboard_cfg(
            &tempdir(),
            vec![
                anchor(&d(7), "七天边界", "https://example.com/a", "2026-10-01"),
                anchor(&d(8), "八天外", "https://example.com/b", "2026-10-01"),
                anchor(&d(3), "正常", "ftp://example.com/c", "2026-10-01"),
                anchor(&d(3), "正常", "https://example.com/d", "2026-12-01"),
                anchor(
                    "not-a-date",
                    "坏日期",
                    "https://example.com/e",
                    "2026-10-01",
                ),
                anchor(&d(1), "有效", "https://example.com/f", "2026-10-18"),
            ],
        );
        let block = collect_unlocks(&cfg, today, "2026-10-18T08:00:00Z".to_string());
        assert_eq!(block.status, BlockStatus::Ok, "有有效锚点即为 ok");
        let labels: Vec<&str> = block.anchors.iter().map(|a| a.label.as_str()).collect();
        assert_eq!(labels, vec!["有效", "七天边界"], "按日期升序");
        assert_eq!(block.anchors[0].days_ahead, 1);
        assert_eq!(block.anchors[1].days_ahead, 7);
        assert_eq!(block.note, "日历预告不等于实际申领已发生");
        assert_eq!(block.queried_at, "2026-10-18T08:00:00Z");
        assert!(block.diagnostics.is_empty());

        // 全部有效但都在 7 天外：anchors 为空、状态 ok（仅未配置/全部无效才 partial）。
        let cfg = dashboard_cfg(
            &tempdir(),
            vec![anchor(
                &d(30),
                "远期",
                "https://example.com/g",
                "2026-10-01",
            )],
        );
        let block = collect_unlocks(&cfg, today, "2026-10-18T08:00:00Z".to_string());
        assert_eq!(block.status, BlockStatus::Ok);
        assert!(block.anchors.is_empty());
        assert!(block.diagnostics.is_empty());

        // 未配置 → partial + 空数组 + 诊断。
        let cfg = dashboard_cfg(&tempdir(), vec![]);
        let block = collect_unlocks(&cfg, today, "2026-10-18T08:00:00Z".to_string());
        assert_eq!(block.status, BlockStatus::Partial);
        assert!(block.anchors.is_empty());
        assert_eq!(block.diagnostics.len(), 1);

        // 全部无效 → partial + 空数组 + 诊断。
        let cfg = dashboard_cfg(
            &tempdir(),
            vec![
                anchor("bad", "坏", "https://example.com", "2026-10-01"),
                anchor(&d(2), "未核实", "https://example.com", "2999-01-01"),
            ],
        );
        let block = collect_unlocks(&cfg, today, "2026-10-18T08:00:00Z".to_string());
        assert_eq!(block.status, BlockStatus::Partial);
        assert!(block.anchors.is_empty());
        assert!(block.diagnostics[0].contains("全部无效"));
    }

    #[tokio::test]
    async fn same_direction_legs_get_diagnostic() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let account = ACCOUNT.replace(r#""szi": "-0.06""#, r#""szi": "0.06""#);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        // 动态账户响应：借用 mock_clearinghouse 的固定 ACCOUNT 不行，单独 mock。
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(account.clone());
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body(OPEN_ORDERS);
        });
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["status"], "partial");
        let pos = p["positions"].as_array().unwrap();
        assert_eq!(pos[1]["side"], "long", "按实际方向展示");
        assert_eq!(p["notional_ratio"], "1.0082", "名义比按数值照常计算");
        assert!(
            p["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("不符合 HYPE 多/BTC 空组合"))
        );
    }

    #[tokio::test]
    async fn non_positive_mark_nulls_notional_with_diagnostic() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        mock_clearinghouse(&server, ACCOUNT);
        mock_exchange(&server, META_CTXS_HYPE_MARK_ZERO);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["status"], "partial");
        let pos = p["positions"].as_array().unwrap();
        assert!(pos[0]["mark_px"].is_null(), "mark 非正 → null");
        assert!(pos[0]["notional"].is_null());
        assert_eq!(pos[1]["notional"], "3600.00", "另一腿不受影响");
        assert!(p["notional_ratio"].is_null());
        assert!(
            p["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("标记价格"))
        );
    }

    #[tokio::test]
    async fn flat_account_positions_empty_but_not_null() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let account = ACCOUNT.replace(
            r#""assetPositions": [
            {"type": "oneWay", "position": {"coin": "HYPE", "szi": "120.5",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "30.0", "positionValue": "3629.46",
                "unrealizedPnl": "5.10", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "0.0",
                "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}},
            {"type": "oneWay", "position": {"coin": "BTC", "szi": "-0.06",
                "leverage": {"type": "cross", "value": 40}, "entryPx": "60000", "positionValue": "3600",
                "unrealizedPnl": "-1.00", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "0.0",
                "maxLeverage": 40, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
        ]"#,
            r#""assetPositions": []"#,
        );
        let now_ms = realtime_ms();
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(account.clone());
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body("[]");
        });
        mock_exchange(&server, META_CTXS);
        mock_candles(&server, now_ms, 32);
        mock_funding(&server, standard_funding(now_ms));
        mock_telegram(&server, "/hyperliquid_announcements", 200, general_html());
        mock_telegram(&server, "/hyperliquid_api", 200, api_html());

        let snap = collect_on(&server, &cfg, now_ms).await;
        let v: Value = serde_json::from_str(&to_json_line(&snap).unwrap()).unwrap();
        let p = &v["blocks"]["positions"];
        assert_eq!(p["positions"].as_array().unwrap().len(), 0, "空仓是空数组");
        assert!(p["notional_ratio"].is_null());
        assert!(
            p["diagnostics"]
                .as_array()
                .unwrap()
                .iter()
                .any(|d| d.as_str().unwrap().contains("无 HYPE/BTC 持仓"))
        );
        // 资金费估算：无持仓 → null（合法空值，无诊断）。
        assert!(v["blocks"]["funding"]["daily_estimate"].is_null());
    }

    #[tokio::test]
    async fn serialization_is_single_line_without_extra_output() {
        let dir = tempdir();
        let cfg = dashboard_cfg(&dir, vec![]);
        let now_ms = realtime_ms();
        let server = happy_stage(now_ms).await;
        let snap = collect_on(&server, &cfg, now_ms).await;
        let line = to_json_line(&snap).unwrap();
        assert!(!line.contains('\n'), "stdout 只含一行 JSON");
        assert!(line.starts_with('{') && line.ends_with('}'));
        // 往返解析且顶层恰好五个键。
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v.as_object().unwrap().len(), 5);
        // 快照内任何字符串都不得包含本机路径或秘密线索。
        fn walk(value: &Value, out: &mut Vec<String>) {
            match value {
                Value::String(s) => out.push(s.clone()),
                Value::Array(a) => a.iter().for_each(|v| walk(v, out)),
                Value::Object(o) => o.values().for_each(|v| walk(v, out)),
                _ => {}
            }
        }
        let mut strings = Vec::new();
        walk(&v, &mut strings);
        assert!(
            strings.iter().all(|s| !s.contains(dir.to_str().unwrap())),
            "JSON 不得包含本机路径"
        );
        assert!(
            strings.iter().all(|s| !s.to_lowercase().contains("secret")),
            "JSON 不得包含秘密线索"
        );
    }
}
