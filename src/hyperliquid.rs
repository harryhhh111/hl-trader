//! Hyperliquid 测试网协议适配。
//!
//! 只面向官方测试网端点；写操作默认 dry-run（见 main.rs），本模块的
//! place_order/cancel_oid 只在显式 --execute 路径上被调用。
//! 金额与精度校验在本地完成：元数据缺失、非法精度、零/负值、超出名义上限
//! 一律拒绝，不静默截断、不猜默认值。

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hypersdk::Address;
#[cfg(test)]
use hypersdk::hypercore::Chain;
use hypersdk::hypercore::types::*;
use hypersdk::hypercore::{
    self, ActionError, Cloid, HttpClient, OidOrCloid, PerpMarket, PrivateKeySigner,
};
use rust_decimal::Decimal;

/// 官方测试网端点（MVP_PLAN §4：唯一允许的网络）。
pub const TESTNET_API_URL: &str = "https://api.hyperliquid-testnet.xyz";

/// 系统时间合理下限：2025-01-01T00:00:00Z（毫秒）。
const MIN_SANE_EPOCH_MS: u64 = 1_735_689_600_000;

/// 一天的毫秒数（日线蜡烛窗口计算）。
const DAY_MS: u64 = 86_400_000;

pub struct Adapter {
    client: HttpClient,
    nonces: AtomicU64,
}

/// 统一错误分类：网络失败 / 无权限 / 拒绝 / 结果未知必须明确区分。
#[derive(Debug, thiserror::Error)]
pub enum AdapterError {
    #[error(transparent)]
    Validation(#[from] ValidationError),
    #[error("结果未知（超时、连接中断或服务端异常，禁止自动重试写请求）: {0}")]
    Unknown(String),
    #[error("无权限（签名地址可能不是该账户的授权 agent）: {0}")]
    Permission(String),
    #[error("响应格式无法识别（结果未知）: {0}")]
    BadResponse(String),
    #[error("测试网元数据中找不到交易对 {0}")]
    UnknownCoin(String),
    #[error("系统时间异常: {0}")]
    Clock(String),
}

/// 本地校验错误（属于明确的输入错误，不是“结果未知”）。
#[derive(Debug, thiserror::Error)]
pub enum ValidationError {
    #[error("价格必须为正数")]
    NonPositivePrice,
    #[error("数量必须为正数")]
    NonPositiveSize,
    #[error("数量精度非法: {scale} 位小数超过该市场允许的 {max} 位")]
    SizePrecision { scale: u32, max: i64 },
    #[error("价格 {price} 不符合该市场 tick 精度（应取整为 {rounded}）；按规则拒绝，未静默截断")]
    PriceTick { price: Decimal, rounded: Decimal },
    #[error("价格 {0} 无法按该市场 tick 规则表示（量级可能超出市场范围）")]
    PriceTickInvalid(Decimal),
    #[error("名义金额 {notional} USD 超过本地上限 {cap} USD")]
    NotionalCap { notional: Decimal, cap: Decimal },
}

fn build_http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(10))
        // 不跟随任何重定向：测试网端点不应重定向，跨主机重定向必须视为异常。
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("构建 HTTP 客户端失败")
}

pub fn now_epoch_ms() -> Result<u64, AdapterError> {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| AdapterError::Clock(format!("系统时间早于 Unix 纪元: {e}")))?
        .as_millis() as u64;
    if ms < MIN_SANE_EPOCH_MS {
        return Err(AdapterError::Clock(format!(
            "系统时间（{ms} ms）早于 2025-01-01，拒绝签名；请先校准系统时钟（NTP）"
        )));
    }
    Ok(ms)
}

fn now_utc_rfc3339() -> String {
    // chrono 已由 hypersdk 引入；这里直接使用。
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

fn classify_message(msg: &str, _is_write: bool) -> AdapterError {
    let lower = msg.to_ascii_lowercase();
    if lower.contains("timed out") || lower.contains("timeout") || lower.contains("deadline") {
        return AdapterError::Unknown(format!("超时或连接中断: {msg}"));
    }
    // 传输层错误：请求未到达或未确认到达交易所，一律“结果未知”，
    // 绝不能按拒单处理（PHASE2_PLAN §7.1：只有交易所确认的拒绝才是拒单）。
    if lower.contains("error sending request")
        || lower.contains("error trying to connect")
        || lower.contains("connection refused")
        || lower.contains("tcp connect error")
        || lower.contains("dns error")
    {
        return AdapterError::Unknown(format!("网络层错误（交易所未确认收到请求）: {msg}"));
    }
    if msg.starts_with("HTTP 401") || msg.starts_with("HTTP 403") {
        return AdapterError::Permission(msg.to_string());
    }
    if msg.starts_with("HTTP") {
        return AdapterError::Unknown(format!("HTTP 层错误: {msg}"));
    }
    if lower.contains("body=") || lower.contains("error decoding") || lower.contains("expected") {
        return AdapterError::BadResponse(msg.to_string());
    }
    // hypersdk ActionError flattens transport errors and top-level rejection
    // into one string. Only typed per-order errors establish a rejection.
    AdapterError::Unknown(msg.to_string())
}

fn classify_action_error<T: std::fmt::Debug>(e: ActionError<T>) -> AdapterError {
    classify_message(e.message(), true)
}

fn classify_info_error(e: anyhow::Error) -> AdapterError {
    classify_message(&e.to_string(), false)
}

/// 解析用户提供的 cloid：0x 开头的 32 字节十六进制。
pub fn parse_cloid(s: &str) -> Result<Cloid, String> {
    let hex = s.strip_prefix("0x").unwrap_or(s);
    if hex.len() != 32 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(format!(
            "cloid 必须是 0x 开头的 128 位十六进制（32 个 hex 字符），实际长度 {}",
            hex.len()
        ));
    }
    let mut bytes = [0u8; 16];
    for (i, chunk) in bytes.iter_mut().enumerate() {
        *chunk = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).map_err(|e| e.to_string())?;
    }
    Ok(Cloid::from_slice(&bytes))
}

pub fn cloid_hex(c: Cloid) -> String {
    format!("{c:#x}")
}

// ---------- 视图 ----------

pub struct MarketView {
    pub coin: String,
    pub mid: Option<Decimal>,
    pub sz_decimals: i64,
    pub tick: String,
    pub query_time_utc: String,
}

pub struct PositionView {
    pub coin: String,
    pub side: &'static str,
    pub size: Decimal,
    pub entry_px: Option<Decimal>,
    pub unrealized_pnl: Decimal,
}

pub struct OpenOrderView {
    pub coin: String,
    pub side: &'static str,
    pub limit_px: Decimal,
    pub sz: Decimal,
    pub oid: u64,
}

pub struct AccountView {
    pub account_value: Decimal,
    pub available_margin: Decimal,
    pub positions: Vec<PositionView>,
    pub open_orders: Vec<OpenOrderView>,
    pub query_time_utc: String,
}

pub struct OrderStatusView {
    pub finished: bool,
    pub filled: bool,
    pub oid: u64,
    pub status: String,
    pub coin: String,
    pub side: &'static str,
    pub limit_px: Decimal,
    pub sz: Decimal,
}

/// 盘口第一层买/卖价（任一方向缺盘口即为 None）。
pub struct BboView {
    pub bid: Option<Decimal>,
    pub ask: Option<Decimal>,
    pub time_ms: u64,
}

/// 单个永续市场的当前资金费与标记/预言机价格（metaAndAssetCtxs）。
pub struct AssetCtxView {
    /// 当前小时资金费率（小数，如 0.0001 = 0.01%）。
    pub funding: Decimal,
    pub mark_px: Decimal,
    pub oracle_px: Decimal,
}

/// 账户一条资金费收付记录（userFunding）。
pub struct FundingPaymentView {
    pub coin: String,
    pub time_ms: u64,
    /// 本次收付的 USDC 金额（正 = 收到，负 = 付出）。
    pub usdc: Decimal,
    pub funding_rate: Decimal,
}

/// 账户一条成交记录（userFillsByTime）。
pub struct FillView {
    pub coin: String,
    pub px: Decimal,
    pub sz: Decimal,
    pub buy: bool,
    pub time_ms: u64,
    pub fee: Decimal,
    pub fee_token: String,
    pub closed_pnl: Decimal,
    pub oid: u64,
}

#[derive(Debug)]
pub enum PlaceOutcome {
    Resting {
        oid: u64,
    },
    Filled {
        oid: u64,
        total_sz: Decimal,
        avg_px: Decimal,
    },
    Rejected(String),
}

#[derive(Debug)]
pub enum CancelOutcome {
    Canceled,
    Rejected(String),
}

fn side_str(side: Side) -> &'static str {
    match side {
        Side::Bid => "buy",
        Side::Ask => "sell",
    }
}

impl Adapter {
    /// 生产入口：官方测试网 + 受限 HTTP 客户端（禁重定向、带超时）。
    pub fn testnet() -> Self {
        let client = hypercore::testnet().with_http_client(build_http_client());
        Self {
            client,
            nonces: AtomicU64::new(0),
        }
    }

    /// 测试专用入口：指向本地 mock 服务器。不暴露给 CLI。
    #[cfg(test)]
    pub(crate) fn with_base_url(base: &str) -> Self {
        let client = HttpClient::new(Chain::Testnet)
            .with_url(base.parse().expect("mock url"))
            .with_http_client(build_http_client());
        Self {
            client,
            nonces: AtomicU64::new(0),
        }
    }

    /// 每次签名前核对系统时间并保证 nonce 单调唯一（单进程串行写）。
    pub fn next_nonce(&self) -> Result<u64, AdapterError> {
        let now = now_epoch_ms()?;
        let mut prev = self.nonces.load(Ordering::Acquire);
        loop {
            let candidate = now.max(prev.saturating_add(1));
            match self.nonces.compare_exchange_weak(
                prev,
                candidate,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(candidate),
                Err(p) => prev = p,
            }
        }
    }

    /// 进程启动时的时间自检（execute 路径调用）。
    pub fn check_clock() -> Result<(), AdapterError> {
        now_epoch_ms().map(|_| ())
    }

    pub async fn perp_markets(&self) -> Result<Vec<PerpMarket>, AdapterError> {
        self.client.perps().await.map_err(classify_info_error)
    }

    pub async fn market(&self, coin: &str) -> Result<MarketView, AdapterError> {
        let perps = self.perp_markets().await?;
        let m = find_market(&perps, coin)?;
        let mids = self
            .client
            .all_mids(None)
            .await
            .map_err(classify_info_error)?;
        Ok(MarketView {
            coin: coin.to_string(),
            mid: mids.get(coin).copied(),
            sz_decimals: m.sz_decimals,
            tick: format!("{:?}", m.table),
            query_time_utc: now_utc_rfc3339(),
        })
    }

    pub async fn account(&self, user: Address) -> Result<AccountView, AdapterError> {
        let state = self
            .client
            .clearinghouse_state(user, None)
            .await
            .map_err(classify_info_error)?;
        let orders = self
            .client
            .open_orders(user, None)
            .await
            .map_err(classify_info_error)?;
        Ok(AccountView {
            account_value: state.cross_margin_summary.account_value,
            available_margin: state.cross_margin_summary.available_margin(),
            positions: state
                .asset_positions
                .into_iter()
                .map(|p| {
                    let pos = p.position;
                    let side = pos.side();
                    PositionView {
                        coin: pos.coin,
                        side,
                        size: pos.szi,
                        entry_px: pos.entry_px,
                        unrealized_pnl: pos.unrealized_pnl,
                    }
                })
                .collect(),
            open_orders: orders
                .into_iter()
                .map(|o| OpenOrderView {
                    coin: o.coin,
                    side: side_str(o.side),
                    limit_px: o.limit_px,
                    sz: o.sz,
                    oid: o.oid,
                })
                .collect(),
            query_time_utc: now_utc_rfc3339(),
        })
    }

    pub async fn place_order(
        &self,
        signer: &PrivateKeySigner,
        intent: &OrderIntent,
    ) -> Result<PlaceOutcome, AdapterError> {
        let nonce = self.next_nonce()?;
        let batch = BatchOrder {
            orders: vec![OrderRequest {
                asset: intent.asset,
                is_buy: intent.buy,
                limit_px: intent.price,
                sz: intent.size,
                reduce_only: intent.reduce_only,
                order_type: OrderTypePlacement::Limit { tif: intent.tif },
                cloid: intent.cloid,
            }],
            grouping: OrderGrouping::Na,
            builder: None,
        };
        let statuses = self
            .client
            .place(signer, batch, nonce, None, None)
            .await
            .map_err(classify_action_error)?;
        let status = statuses
            .into_iter()
            .next()
            .ok_or_else(|| AdapterError::BadResponse("下单响应中没有任何订单状态".into()))?;
        Ok(match status {
            OrderResponseStatus::Resting { oid, .. } => PlaceOutcome::Resting { oid },
            OrderResponseStatus::Filled {
                oid,
                total_sz,
                avg_px,
            } => PlaceOutcome::Filled {
                oid,
                total_sz,
                avg_px,
            },
            OrderResponseStatus::Error(e) => PlaceOutcome::Rejected(e),
            other => {
                return Err(AdapterError::BadResponse(format!(
                    "未预期的订单状态: {other:?}"
                )));
            }
        })
    }

    pub async fn query_status(
        &self,
        user: Address,
        cloid: Cloid,
    ) -> Result<Option<OrderStatusView>, AdapterError> {
        let update = self
            .client
            .order_status(user, OidOrCloid::Right(cloid))
            .await
            .map_err(classify_info_error)?;
        Ok(update.map(|u| OrderStatusView {
            finished: u.status.is_finished() && !matches!(u.status, OrderStatus::Triggered),
            filled: u.status.is_filled(),
            oid: u.order.oid,
            status: u.status.to_string(),
            coin: u.order.coin,
            side: side_str(u.order.side),
            limit_px: u.order.limit_px,
            sz: u.order.sz,
        }))
    }

    pub async fn cancel_oid(
        &self,
        signer: &PrivateKeySigner,
        asset: usize,
        oid: u64,
    ) -> Result<CancelOutcome, AdapterError> {
        let nonce = self.next_nonce()?;
        let batch = BatchCancel {
            cancels: vec![Cancel { asset, oid }],
            fast: false,
        };
        let statuses = self
            .client
            .cancel(signer, batch, nonce, None, None)
            .await
            .map_err(classify_action_error)?;
        let status = statuses
            .into_iter()
            .next()
            .ok_or_else(|| AdapterError::BadResponse("撤单响应中没有任何状态".into()))?;
        Ok(match status {
            OrderResponseStatus::Success => CancelOutcome::Canceled,
            OrderResponseStatus::Error(e) => CancelOutcome::Rejected(e),
            other => {
                return Err(AdapterError::BadResponse(format!(
                    "未预期的撤单状态: {other:?}"
                )));
            }
        })
    }

    /// 盘口第一层买/卖价。pair 的 IOC 保护价以此计算（PHASE2_PLAN §2）。
    pub async fn bbo(&self, coin: &str) -> Result<BboView, AdapterError> {
        let book = self
            .client
            .l2_book(coin.to_string(), None, None)
            .await
            .map_err(classify_info_error)?;
        Ok(BboView {
            bid: book.best_bid().map(|l| l.px),
            ask: book.best_ask().map(|l| l.px),
            time_ms: book.time,
        })
    }

    /// 当前资金费率与标记/预言机价格（每小时结算，费率为小时口径）。
    pub async fn asset_ctx(&self, coin: &str) -> Result<AssetCtxView, AdapterError> {
        let v = self
            .client
            .meta_and_asset_ctxs(None)
            .await
            .map_err(classify_info_error)?;
        parse_asset_ctx(&v, coin)
    }

    /// Paginate with an inclusive timestamp cursor and deduplicate overlap.
    /// A saturated timestamp or retained-history limit is an error, never a complete history.
    pub async fn user_funding_since(
        &self,
        user: Address,
        start_ms: u64,
    ) -> Result<Vec<FundingPaymentView>, AdapterError> {
        let end = now_epoch_ms()?;
        let mut cursor = start_ms;
        let mut all = std::collections::BTreeMap::new();
        for _ in 0..1000 {
            let entries = self
                .client
                .user_funding(user, cursor, Some(end))
                .await
                .map_err(classify_info_error)?;
            let count = entries.len();
            let next = entries.iter().map(|e| e.time).max().unwrap_or(cursor);
            for e in entries {
                if e.time < cursor || e.time > end {
                    return Err(AdapterError::BadResponse("资金费记录超出请求区间".into()));
                }
                all.insert((e.time, e.hash.clone(), e.delta.coin.clone()), e);
            }
            if count < 500 {
                return Ok(all
                    .into_values()
                    .map(|e| FundingPaymentView {
                        coin: e.delta.coin,
                        time_ms: e.time,
                        usdc: e.delta.usdc,
                        funding_rate: e.delta.funding_rate,
                    })
                    .collect());
            }
            if next <= cursor {
                return Err(AdapterError::BadResponse(
                    "资金费分页无法推进，历史不完整".into(),
                ));
            }
            cursor = next;
        }
        Err(AdapterError::BadResponse(
            "资金费历史超过本次分页上限".into(),
        ))
    }

    pub async fn user_fills_since(
        &self,
        user: Address,
        start_ms: u64,
    ) -> Result<Vec<FillView>, AdapterError> {
        let end = now_epoch_ms()?;
        let mut cursor = start_ms;
        let mut all = std::collections::BTreeMap::new();
        for _ in 0..1000 {
            let entries = self
                .client
                .user_fills_by_time(user, cursor, Some(end))
                .await
                .map_err(classify_info_error)?;
            let count = entries.len();
            let next = entries.iter().map(|e| e.time).max().unwrap_or(cursor);
            for e in entries {
                if e.time < cursor || e.time > end {
                    return Err(AdapterError::BadResponse("成交记录超出请求区间".into()));
                }
                all.insert((e.coin.clone(), e.tid), e);
            }
            if all.len() >= 10000 {
                return Err(AdapterError::BadResponse(
                    "达到交易所成交保留上限，不能保证完整核算".into(),
                ));
            }
            if count < 2000 {
                let mut fills: Vec<_> = all
                    .into_values()
                    .map(|f| FillView {
                        coin: f.coin,
                        px: f.px,
                        sz: f.sz,
                        buy: matches!(f.side, Side::Bid),
                        time_ms: f.time,
                        fee: f.fee,
                        fee_token: f.fee_token,
                        closed_pnl: f.closed_pnl,
                        oid: f.oid,
                    })
                    .collect();
                fills.sort_by_key(|f| f.time_ms);
                return Ok(fills);
            }
            if next <= cursor {
                return Err(AdapterError::BadResponse(
                    "成交分页无法推进，历史不完整".into(),
                ));
            }
            cursor = next;
        }
        Err(AdapterError::BadResponse("成交历史超过本次分页上限".into()))
    }

    pub async fn cross_margin(&self, user: Address, coin: &str) -> Result<bool, AdapterError> {
        let data = self
            .client
            .active_asset_data(user, coin.into())
            .await
            .map_err(classify_info_error)?;
        if data.user != user || data.coin != coin {
            return Err(AdapterError::BadResponse(
                "保证金模式响应账户/市场不匹配".into(),
            ));
        }
        Ok(data.leverage.leverage_type == "cross")
    }

    /// 把某永续标的设置为全仓模式并指定杠杆（pair 开仓的前提条件）。
    pub async fn set_cross_leverage(
        &self,
        signer: &PrivateKeySigner,
        asset: usize,
        leverage: u32,
    ) -> Result<(), AdapterError> {
        let nonce = self.next_nonce()?;
        self.client
            .update_leverage(signer, asset, true, leverage, nonce, None, None)
            .await
            .map_err(classify_info_error)
    }

    /// 最近 days 天的日线收盘价（蜡烛收盘时间毫秒 + 收盘价）。
    pub async fn daily_closes(
        &self,
        coin: &str,
        days: u32,
    ) -> Result<Vec<(u64, Decimal)>, AdapterError> {
        let end = now_epoch_ms()?;
        let start = end.saturating_sub(u64::from(days) * DAY_MS);
        let candles = self
            .client
            .candle_snapshot(coin.to_string(), CandleInterval::OneDay, start, end)
            .await
            .map_err(classify_info_error)?;
        Ok(candles
            .into_iter()
            .map(|c| (c.close_time, c.close))
            .collect())
    }
}

/// 从 metaAndAssetCtxs 原始响应中解析指定市场的 ctx。
///
/// 响应形态为 `[{"universe":[...]}, [{"funding":"...","markPx":"...","oraclePx":"..."},...]]`；
/// universe 下标与 ctxs 下标一一对应。任何字段缺失/非法都按 BadResponse（无法识别），
/// 不猜默认值。
fn parse_asset_ctx(v: &serde_json::Value, coin: &str) -> Result<AssetCtxView, AdapterError> {
    let arr = v.as_array().filter(|a| a.len() == 2).ok_or_else(|| {
        AdapterError::BadResponse("metaAndAssetCtxs 响应不是 [meta, ctxs]".into())
    })?;
    let universe = arr[0]
        .get("universe")
        .and_then(|u| u.as_array())
        .ok_or_else(|| AdapterError::BadResponse("metaAndAssetCtxs 缺少 universe 数组".into()))?;
    let ctxs = arr[1]
        .as_array()
        .ok_or_else(|| AdapterError::BadResponse("metaAndAssetCtxs 缺少 ctxs 数组".into()))?;
    let idx = universe
        .iter()
        .position(|m| m.get("name").and_then(|n| n.as_str()) == Some(coin))
        .ok_or_else(|| AdapterError::UnknownCoin(coin.to_string()))?;
    let ctx = ctxs.get(idx).ok_or_else(|| {
        AdapterError::BadResponse(format!(
            "metaAndAssetCtxs ctxs[{idx}] 缺失（universe 有 {coin}）"
        ))
    })?;
    let dec = |key: &str| -> Result<Decimal, AdapterError> {
        ctx.get(key)
            .and_then(|x| x.as_str())
            .and_then(|s| s.parse::<Decimal>().ok())
            .ok_or_else(|| {
                AdapterError::BadResponse(format!("metaAndAssetCtxs {coin}.{key} 缺失或非法"))
            })
    };
    Ok(AssetCtxView {
        funding: dec("funding")?,
        mark_px: dec("markPx")?,
        oracle_px: dec("oraclePx")?,
    })
}

pub struct OrderIntent {
    pub asset: usize,
    pub buy: bool,
    pub price: Decimal,
    pub size: Decimal,
    pub cloid: Cloid,
    /// 生效时间指令：Alo（maker-only）或 Ioc（taker，未成交即撤）。
    pub tif: TimeInForce,
    /// 只减仓单：只允许平掉现有持仓，不允许反向开仓（平仓/兜底必须使用）。
    pub reduce_only: bool,
}

pub fn find_market<'a>(
    perps: &'a [PerpMarket],
    coin: &str,
) -> Result<&'a PerpMarket, AdapterError> {
    perps
        .iter()
        .find(|m| m.name == coin)
        .ok_or_else(|| AdapterError::UnknownCoin(coin.to_string()))
}

/// 本地校验：不静默截断、不猜默认值（MVP_PLAN §4）。
pub fn validate_order(
    market: &PerpMarket,
    price: Decimal,
    size: Decimal,
    max_notional: Decimal,
) -> Result<(), ValidationError> {
    if price <= Decimal::ZERO {
        return Err(ValidationError::NonPositivePrice);
    }
    if size <= Decimal::ZERO {
        return Err(ValidationError::NonPositiveSize);
    }
    if i64::from(size.scale()) > market.sz_decimals {
        return Err(ValidationError::SizePrecision {
            scale: size.scale(),
            max: market.sz_decimals,
        });
    }
    let rounded = market.round_price(price);
    match rounded {
        Some(rounded) if rounded == price => {}
        Some(rounded) => return Err(ValidationError::PriceTick { price, rounded }),
        None => return Err(ValidationError::PriceTickInvalid(price)),
    }
    let notional = price * size;
    if notional > max_notional {
        return Err(ValidationError::NotionalCap {
            notional,
            cap: max_notional,
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use httpmock::MockServer;
    use hypersdk::hypercore::{PriceTick, SpotToken};
    use std::str::FromStr;

    const SPOT_META: &str = r#"{
        "universe": [],
        "tokens": [{
            "name": "USDC", "index": 0,
            "tokenId": "0x00000000000000000000000000000000",
            "szDecimals": 8, "weiDecimals": 8, "evmContract": null
        }]
    }"#;

    const META: &str = r#"{
        "universe": [
            {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false},
            {"szDecimals": 4, "name": "ETH", "maxLeverage": 25, "onlyIsolated": false}
        ],
        "collateralToken": 0
    }"#;

    const MIDS: &str = r#"{"BTC":"65000.0","ETH":"3000.0"}"#;

    const CLEARINGHOUSE: &str = r#"{
        "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "60.0", "totalRawUsd": "1000.0", "totalMarginUsed": "12.0"},
        "crossMarginSummary": {"accountValue": "1000.0", "totalNtlPos": "60.0", "totalRawUsd": "1000.0", "totalMarginUsed": "12.0"},
        "crossMaintenanceMarginUsed": "5.0",
        "withdrawable": "988.0",
        "time": 1760000000000,
        "assetPositions": [{
            "type": "oneWay",
            "position": {
                "coin": "BTC", "szi": "0.001",
                "leverage": {"type": "cross", "value": 5},
                "entryPx": "60000", "positionValue": "60.0",
                "unrealizedPnl": "1.5", "returnOnEquity": "0.0125",
                "liquidationPx": null, "marginUsed": "12.0", "maxLeverage": 40,
                "cumFunding": {"allTime": "-0.1", "sinceOpen": "-0.1", "sinceChange": "0.0"}
            }
        }]
    }"#;

    const OPEN_ORDERS: &str = r#"[
        {"timestamp": 1760000000000, "coin": "BTC", "side": "B", "limitPx": "60000",
         "sz": "0.001", "oid": 99, "origSz": "0.001", "cloid": null,
         "orderType": "Limit", "tif": "Alo", "reduceOnly": false}
    ]"#;

    const ORDER_STATUS_OPEN: &str = r#"{
        "status": "order",
        "order": {"status": "open", "statusTimestamp": 1760000000000,
            "order": {"timestamp": 1760000000000, "coin": "BTC", "side": "B", "limitPx": "60000",
                "sz": "0.001", "oid": 99, "origSz": "0.001",
                "cloid": "0x11111111111111111111111111111111",
                "orderType": "Limit", "tif": "Alo", "reduceOnly": false}}
    }"#;

    const PLACE_RESTING: &str = r#"{
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1001}}]}}
    }"#;

    const PLACE_FILLED: &str = r#"{
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.001", "avgPx": "60000", "oid": 1002}}]}}
    }"#;

    const PLACE_ORDER_ERR: &str = r#"{
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "ALO order would cross the spread"}]}}
    }"#;

    const TOP_LEVEL_ERR: &str = r#"{"status": "err", "response": "Order rejected by exchange"}"#;

    const CANCEL_OK: &str = r#"{
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": [{"success": null}]}}
    }"#;

    const CANCEL_ERR: &str = r#"{
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": [{"error": "Order already filled"}]}}
    }"#;

    fn mock_meta(server: &MockServer) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"spotMeta""#);
            then.status(200).body(SPOT_META);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"meta""#);
            then.status(200).body(META);
        });
    }

    fn test_signer() -> PrivateKeySigner {
        PrivateKeySigner::from_str(
            "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        .unwrap()
    }

    fn cloid_ones() -> Cloid {
        parse_cloid("0x11111111111111111111111111111111").unwrap()
    }

    #[tokio::test]
    async fn market_view_parses_meta_and_mids() {
        let server = MockServer::start_async().await;
        mock_meta(&server);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"allMids""#);
            then.status(200).body(MIDS);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let view = a.market("BTC").await.unwrap();
        assert_eq!(view.mid, Some(Decimal::new(65000, 0)));
        assert_eq!(view.sz_decimals, 5);
    }

    #[tokio::test]
    async fn market_unknown_coin() {
        let server = MockServer::start_async().await;
        mock_meta(&server);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"allMids""#);
            then.status(200).body(MIDS);
        });
        let a = Adapter::with_base_url(&server.base_url());
        assert!(matches!(
            a.market("DOGE").await,
            Err(AdapterError::UnknownCoin(_))
        ));
    }

    #[tokio::test]
    async fn account_parses_state_and_orders() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(CLEARINGHOUSE);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body(OPEN_ORDERS);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let user: Address = "0x2222222222222222222222222222222222222222"
            .parse()
            .unwrap();
        let view = a.account(user).await.unwrap();
        assert_eq!(view.positions.len(), 1);
        assert_eq!(view.positions[0].coin, "BTC");
        assert_eq!(view.open_orders.len(), 1);
        assert_eq!(view.open_orders[0].oid, 99);
    }

    #[tokio::test]
    async fn place_receives_resting_oid() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes("\"orders\"");
            then.status(200).body(PLACE_RESTING);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let outcome = a.place_order(&test_signer(), &intent).await.unwrap();
        assert!(matches!(outcome, PlaceOutcome::Resting { oid: 1001 }));
    }

    #[tokio::test]
    async fn place_reports_filled_honestly() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body(PLACE_FILLED);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let outcome = a.place_order(&test_signer(), &intent).await.unwrap();
        match outcome {
            PlaceOutcome::Filled {
                oid,
                total_sz,
                avg_px,
            } => {
                assert_eq!(oid, 1002);
                assert_eq!(total_sz, Decimal::new(1, 3));
                assert_eq!(avg_px, Decimal::new(60000, 0));
            }
            other => panic!("expected filled, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn place_per_order_error_is_rejection_not_unknown() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body(PLACE_ORDER_ERR);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let outcome = a.place_order(&test_signer(), &intent).await.unwrap();
        assert!(matches!(outcome, PlaceOutcome::Rejected(_)));
    }

    #[tokio::test]
    async fn place_flattened_top_level_error_is_conservatively_unknown() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body(TOP_LEVEL_ERR);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let err = a.place_order(&test_signer(), &intent).await.unwrap_err();
        assert!(matches!(err, AdapterError::Unknown(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn place_http_500_is_unknown_not_rejected() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500).body("internal error");
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let err = a.place_order(&test_signer(), &intent).await.unwrap_err();
        assert!(matches!(err, AdapterError::Unknown(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn place_connection_refused_is_unknown_not_rejected() {
        // 传输层错误（交易所未确认收到请求）必须是“结果未知”，不能误报拒单
        //（PHASE2_PLAN §7.1）。127.0.0.1:9 为 discard 端口，连接应被拒绝。
        let a = Adapter::with_base_url("http://127.0.0.1:9");
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let err = a.place_order(&test_signer(), &intent).await.unwrap_err();
        assert!(matches!(err, AdapterError::Unknown(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn place_garbage_body_is_bad_response() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body("<html>not json</html>");
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: true,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Alo,
            reduce_only: false,
        };
        let err = a.place_order(&test_signer(), &intent).await.unwrap_err();
        assert!(
            matches!(err, AdapterError::BadResponse(_) | AdapterError::Unknown(_)),
            "got {err:?}"
        );
    }

    #[tokio::test]
    async fn status_by_cloid_open_order() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("orderStatus");
            then.status(200).body(ORDER_STATUS_OPEN);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let user: Address = "0x2222222222222222222222222222222222222222"
            .parse()
            .unwrap();
        let view = a.query_status(user, cloid_ones()).await.unwrap().unwrap();
        assert_eq!(view.oid, 99);
        assert_eq!(view.status.to_lowercase(), "open");
    }

    #[tokio::test]
    async fn status_by_cloid_unknown_oid() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("orderStatus");
            then.status(200).body(r#"{"status":"unknownOid"}"#);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let user: Address = "0x2222222222222222222222222222222222222222"
            .parse()
            .unwrap();
        assert!(a.query_status(user, cloid_ones()).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn cancel_success_and_already_filled() {
        let server = MockServer::start_async().await;
        let ok_mock = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""cancels":[{"a":0,"o":1001}]"#);
            then.status(200).body(CANCEL_OK);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let outcome = a.cancel_oid(&test_signer(), 0, 1001).await.unwrap();
        assert!(matches!(outcome, CancelOutcome::Canceled));
        ok_mock.delete_async().await;

        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body(CANCEL_ERR);
        });
        let outcome = a.cancel_oid(&test_signer(), 0, 1001).await.unwrap();
        assert!(matches!(outcome, CancelOutcome::Rejected(_)));
    }

    #[test]
    fn validate_order_rules() {
        let market = PerpMarket {
            name: "BTC".into(),
            index: 0,
            sz_decimals: 5,
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
            max_leverage: 40,
            isolated_margin: false,
            margin_mode: None,
            deployer_fee_scale: None,
            growth_mode: false,
            aligned_quote_token: false,
            delisted: false,
            table: PriceTick::for_perp(5),
        };
        let max = Decimal::new(100, 0);
        assert!(validate_order(&market, Decimal::new(60000, 0), Decimal::new(1, 3), max).is_ok());
        assert!(matches!(
            validate_order(&market, Decimal::ZERO, Decimal::new(1, 3), max),
            Err(ValidationError::NonPositivePrice)
        ));
        assert!(matches!(
            validate_order(&market, Decimal::new(60000, 0), Decimal::ZERO, max),
            Err(ValidationError::NonPositiveSize)
        ));
        assert!(matches!(
            validate_order(&market, Decimal::new(60000, 0), Decimal::new(1, 6), max),
            Err(ValidationError::SizePrecision { .. })
        ));
        assert!(matches!(
            validate_order(&market, Decimal::new(60000, 0), Decimal::new(2, 0), max),
            Err(ValidationError::NotionalCap { .. })
        ));
    }

    #[test]
    fn cloid_parse_roundtrip() {
        let c = cloid_ones();
        assert_eq!(cloid_hex(c), "0x11111111111111111111111111111111");
        assert!(parse_cloid("0x1234").is_err());
        assert!(parse_cloid("0xzzzz11111111111111111111111111").is_err());
    }

    #[test]
    fn nonce_is_monotonic_and_unique() {
        let a = Adapter::with_base_url("http://127.0.0.1:9");
        let n1 = a.next_nonce().unwrap();
        let n2 = a.next_nonce().unwrap();
        assert!(n2 > n1);
    }

    const L2_BOOK_BTC: &str = r#"{
        "coin": "BTC", "time": 1760000000000, "snapshot": true,
        "levels": [[{"px": "65000", "sz": "1", "n": 1}],
                   [{"px": "65010", "sz": "2", "n": 1}]]
    }"#;

    const META_CTXS: &str = r#"[
        {"universe": [{"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false}]},
        [{"funding": "0.0001", "markPx": "65000", "oraclePx": "65005",
          "openInterest": "10", "prevDayPx": "64000"}]
    ]"#;

    const USER_FUNDING: &str = r#"[
        {"delta": {"type": "funding", "coin": "BTC", "usdc": "0.5", "szi": "0.001",
                   "fundingRate": "0.0001", "nSamples": null},
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "time": 1760000000000}
    ]"#;

    const USER_FILLS: &str = r#"[
        {"coin": "BTC", "px": "65000", "sz": "0.001", "side": "B", "time": 1760000000000,
         "startPosition": "0", "dir": "Open Long", "closedPnl": "1.5",
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "oid": 5, "crossed": true, "fee": "-0.1", "tid": 1, "cloid": null, "feeToken": "USDC"}
    ]"#;

    const CANDLES: &str = r#"[
        {"t": 1759276800000, "T": 1759363200000, "s": "BTC", "i": "1d",
         "o": "64000", "h": "66000", "l": "63000", "c": "65000", "v": "100", "n": 10}
    ]"#;

    #[tokio::test]
    async fn bbo_parses_best_bid_ask() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"l2Book""#);
            then.status(200).body(L2_BOOK_BTC);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let bbo = a.bbo("BTC").await.unwrap();
        assert_eq!(bbo.bid, Some(Decimal::new(65000, 0)));
        assert_eq!(bbo.ask, Some(Decimal::new(65010, 0)));
        assert_eq!(bbo.time_ms, 1760000000000);
    }

    #[tokio::test]
    async fn asset_ctx_parses_funding_and_prices() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("metaAndAssetCtxs");
            then.status(200).body(META_CTXS);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let ctx = a.asset_ctx("BTC").await.unwrap();
        assert_eq!(ctx.funding, Decimal::new(1, 4));
        assert_eq!(ctx.mark_px, Decimal::new(65000, 0));
        assert_eq!(ctx.oracle_px, Decimal::new(65005, 0));
        // 未知币种 → UnknownCoin
        assert!(matches!(
            a.asset_ctx("DOGE").await,
            Err(AdapterError::UnknownCoin(_))
        ));
    }

    #[tokio::test]
    async fn user_funding_since_parses_entries() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body(USER_FUNDING);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let user: Address = "0x2222222222222222222222222222222222222222"
            .parse()
            .unwrap();
        let entries = a.user_funding_since(user, 0).await.unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].coin, "BTC");
        assert_eq!(entries[0].usdc, Decimal::new(5, 1));
        assert_eq!(entries[0].funding_rate, Decimal::new(1, 4));
    }

    #[tokio::test]
    async fn user_fills_since_parses_fee_and_pnl() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFillsByTime");
            then.status(200).body(USER_FILLS);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let user: Address = "0x2222222222222222222222222222222222222222"
            .parse()
            .unwrap();
        let fills = a.user_fills_since(user, 0).await.unwrap();
        assert_eq!(fills.len(), 1);
        assert!(fills[0].buy);
        assert_eq!(fills[0].fee, Decimal::new(-1, 1));
        assert_eq!(fills[0].closed_pnl, Decimal::new(15, 1));
        assert_eq!(fills[0].oid, 5);
    }

    #[tokio::test]
    async fn daily_closes_parse() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot");
            then.status(200).body(CANDLES);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let closes = a.daily_closes("BTC", 32).await.unwrap();
        assert_eq!(closes.len(), 1);
        assert_eq!(closes[0].1, Decimal::new(65000, 0));
    }

    #[tokio::test]
    async fn place_ioc_and_reduce_only_wire_format() {
        let server = MockServer::start_async().await;
        let m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes("Ioc")
                .body_includes(r#""r":true"#);
            then.status(200).body(PLACE_FILLED);
        });
        let a = Adapter::with_base_url(&server.base_url());
        let intent = OrderIntent {
            asset: 0,
            buy: false,
            price: Decimal::new(60000, 0),
            size: Decimal::new(1, 3),
            cloid: cloid_ones(),
            tif: TimeInForce::Ioc,
            reduce_only: true,
        };
        let outcome = a.place_order(&test_signer(), &intent).await.unwrap();
        assert!(matches!(outcome, PlaceOutcome::Filled { .. }));
        assert_eq!(m.calls(), 1);
    }
    #[tokio::test]
    async fn funding_pagination_deduplicates_boundary() {
        let server = MockServer::start_async().await;
        let prototype: serde_json::Value =
            serde_json::from_str::<serde_json::Value>(USER_FUNDING).unwrap()[0].clone();
        let page: Vec<_> = (1000..1500u64)
            .map(|time| {
                let mut e = prototype.clone();
                e["time"] = time.into();
                e
            })
            .collect();
        let mut last = prototype.clone();
        last["time"] = 1500u64.into();
        let first = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""startTime":0"#);
            then.status(200).json_body_obj(&page);
        });
        let second = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""startTime":1499"#);
            then.status(200)
                .json_body_obj(&vec![page[499].clone(), last]);
        });
        let entries = Adapter::with_base_url(&server.base_url())
            .user_funding_since(Address::ZERO, 0)
            .await
            .unwrap();
        assert_eq!(entries.len(), 501);
        assert_eq!(first.calls(), 1);
        assert_eq!(second.calls(), 1);
        assert_eq!(entries.last().unwrap().time_ms, 1500);
    }

    #[tokio::test]
    async fn fills_pagination_deduplicates_trade_ids() {
        let server = MockServer::start_async().await;
        let prototype: serde_json::Value =
            serde_json::from_str::<serde_json::Value>(USER_FILLS).unwrap()[0].clone();
        let page: Vec<_> = (1000..3000u64)
            .map(|time| {
                let mut e = prototype.clone();
                e["time"] = time.into();
                e["tid"] = time.into();
                e
            })
            .collect();
        let mut last = prototype.clone();
        last["time"] = 3000u64.into();
        last["tid"] = 3000u64.into();
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""startTime":0"#);
            then.status(200).json_body_obj(&page);
        });
        let second = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""startTime":2999"#);
            then.status(200)
                .json_body_obj(&vec![page[1999].clone(), last]);
        });
        let entries = Adapter::with_base_url(&server.base_url())
            .user_fills_since(Address::ZERO, 0)
            .await
            .unwrap();
        assert_eq!(entries.len(), 2001);
        assert_eq!(second.calls(), 1);
    }

    #[tokio::test]
    async fn saturated_timestamp_reports_incomplete_history() {
        let server = MockServer::start_async().await;
        let mut item: serde_json::Value =
            serde_json::from_str::<serde_json::Value>(USER_FUNDING).unwrap()[0].clone();
        item["time"] = 1000u64.into();
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info");
            then.status(200).json_body_obj(&vec![item; 500]);
        });
        assert!(matches!(
            Adapter::with_base_url(&server.base_url())
                .user_funding_since(Address::ZERO, 1000)
                .await,
            Err(AdapterError::BadResponse(_))
        ));
    }

    #[tokio::test]
    async fn set_cross_leverage_wire_format() {
        let server = MockServer::start_async().await;
        let m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes("updateLeverage")
                .body_includes(r#""isCross":true"#)
                .body_includes(r#""leverage":3"#);
            then.status(200)
                .body(r#"{"status":"ok","response":{"type":"default"}}"#);
        });
        let a = Adapter::with_base_url(&server.base_url());
        a.set_cross_leverage(&test_signer(), 7, 3).await.unwrap();
        assert_eq!(m.calls(), 1);
    }
}
