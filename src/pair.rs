//! pair 执行器：多 HYPE 永续 + 空 BTC 永续等名义 pair 的开/关。
//!
//! 实现 PHASE2_PLAN §2（开仓）、§3（平仓）、§3.1（动作记录与恢复）：
//! - taker IOC 限价 + 保护价（买 = ask+2tick，卖 = bid−2tick），目标是立即成交；
//! - 顺序执行：先 HYPE 多腿，确认全部成交后再 BTC 空腿；
//! - 异常按文档表格处理：拒单/零成交不送第二腿；部分成交只减仓撤回已确认量；
//!   结果未知立即停止写操作，只读核对，不盲目反向；
//! - 补偿是 reduce-only IOC + 新 cloid 的有记录动作，每腿至多一次，不是原请求重试；
//! - 状态文件发送前持久化，原子写；进程互斥锁防止并发写；
//! - 平仓只减仓，绝不反向开仓；审计记录归档不删除。

use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;

use hypersdk::hypercore::types::TimeInForce;
use hypersdk::hypercore::{Cloid, PerpMarket, PrivateKeySigner};
use rust_decimal::Decimal;
use rust_decimal::prelude::RoundingStrategy;
use serde::{Deserialize, Serialize};

use crate::config::{Config, NETWORK};
use crate::hyperliquid::{
    AccountView, Adapter, AdapterError, BboView, OrderIntent, PlaceOutcome, cloid_hex, find_market,
    now_epoch_ms, parse_cloid,
};

pub const LONG_COIN: &str = "HYPE";
pub const SHORT_COIN: &str = "BTC";

const STATE_FILE: &str = "pair-state.json";
const LOCK_FILE: &str = "pair.lock";
/// 保护价带：best 价格外扩的 tick 数（文档 §2）。
const PROTECT_TICKS: i64 = 2;
const EXTERNAL_CLOSE_DETAIL: &str =
    "检测到两腿持仓已在外部归零，按已平仓处理；如有开仓前既有持仓请自行核对";

/// 已核对两腿均为零且无挂单；只记录观测，不虚构外部成交。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExternalClosure {
    pub observed_at_utc: String,
    pub prior_expected_positions: Vec<(String, Decimal)>,
}

#[derive(Debug, thiserror::Error)]
pub enum PairError {
    #[error("pair 写锁 {0} 被占用或无法获取；请等待现有写进程结束，不要删除锁文件")]
    Locked(PathBuf),
    #[error(transparent)]
    Adapter(#[from] AdapterError),
    #[error("本地状态错误: {0}")]
    State(String),
    #[error("状态无法可靠保存或交易结果待核对: {0}")]
    Unresolved(String),
    #[error("操作被拒绝: {0}")]
    Refused(String),
}

/// 本次调用的终局类别，映射进程退出码（0 成功 / 1 已知失败或残仓 / 3 结果未知）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Ok,
    Failed,
    Unknown,
}

pub struct PairReport {
    pub outcome: Outcome,
    pub lines: Vec<String>,
}

impl std::fmt::Debug for PairReport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "PairReport({:?}, {} lines)",
            self.outcome,
            self.lines.len()
        )
    }
}

impl PairReport {
    fn ok(lines: Vec<String>) -> Self {
        Self {
            outcome: Outcome::Ok,
            lines,
        }
    }
    fn failed(lines: Vec<String>) -> Self {
        Self {
            outcome: Outcome::Failed,
            lines,
        }
    }
    fn unknown(lines: Vec<String>) -> Self {
        Self {
            outcome: Outcome::Unknown,
            lines,
        }
    }
    pub fn into_exit_code(self) -> (std::process::ExitCode, Vec<String>) {
        let code = match self.outcome {
            Outcome::Ok => 0u8,
            Outcome::Failed => 1,
            Outcome::Unknown => 3,
        };
        (std::process::ExitCode::from(code), self.lines)
    }
}

// ---------- 状态（PHASE2_PLAN §3.1） ----------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Prepared,
    Opening,
    Open,
    Closing,
    Compensating,
    Incomplete,
    Unknown,
    Closed,
    Failed,
}

impl std::fmt::Display for Phase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            Phase::Prepared => "prepared",
            Phase::Opening => "opening",
            Phase::Open => "open",
            Phase::Closing => "closing",
            Phase::Compensating => "compensating",
            Phase::Incomplete => "incomplete",
            Phase::Unknown => "unknown",
            Phase::Closed => "closed",
            Phase::Failed => "failed",
        };
        f.write_str(s)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LegStatus {
    Prepared,
    Pending,
    Filled,
    PartFilled,
    Rejected,
    Unknown,
}

/// 一条腿的计划与已确认结果。数量/价格只记录交易所确认过的值，绝不虚构。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LegState {
    pub coin: String,
    /// "buy" / "sell"（下单方向；平仓腿是反向的 reduce-only 单）。
    pub side: String,
    pub target_qty: Decimal,
    pub protect_px: Decimal,
    pub cloid: String,
    pub status: LegStatus,
    /// 交易所确认的成交量（0 = 未成交）。
    pub confirmed_qty: Decimal,
    pub avg_px: Option<Decimal>,
    pub oid: Option<u64>,
}

/// 一次补偿（撤回）动作的记录：新 cloid、只减仓、发送前落盘。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UnwindRecord {
    #[serde(default)]
    pub oid: Option<u64>,
    #[serde(default)]
    pub confirmed_qty: Decimal,
    #[serde(default = "unknown_leg_status")]
    pub status: LegStatus,
    pub coin: String,
    pub side: String,
    pub qty: Decimal,
    pub protect_px: Decimal,
    pub cloid: String,
    pub result: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairState {
    #[serde(default)]
    pub external_closure: Option<ExternalClosure>,
    #[serde(default)]
    pub accounting_start_ms: Option<u64>,
    #[serde(default)]
    pub prior_oids: Vec<u64>,
    #[serde(default)]
    pub prior_quantities: Vec<(u64, Decimal)>,
    #[serde(default)]
    pub audit_complete: bool,
    pub version: u8,
    pub network: String,
    pub account: String,
    pub action_id: String,
    /// "open" / "close"。
    pub action: String,
    pub phase: Phase,
    pub created_at_utc: String,
    /// 核算区间起点（开仓动作创建时间；close 重放时沿用）。
    pub created_at_ms: u64,
    pub equity_at_prepare: Decimal,
    pub long_leg: LegState,
    pub short_leg: LegState,
    /// 动作开始时 pair 两币种的持仓快照（szi 原值，含符号）。
    pub start_positions: Vec<(String, Decimal)>,
    pub unwinds: Vec<UnwindRecord>,
    pub detail: Option<String>,
    pub closed_at_utc: Option<String>,
}

pub struct PairStore {
    dir: PathBuf,
}

pub struct PairGuard<'a> {
    _store: &'a PairStore,
    _lock: crate::write_lock::WriteLock,
}

fn unknown_leg_status() -> LegStatus {
    LegStatus::Unknown
}

impl PairStore {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    pub fn state_path(&self) -> PathBuf {
        self.dir.join(STATE_FILE)
    }

    fn lock_path(&self) -> PathBuf {
        self.dir.join(LOCK_FILE)
    }

    /// 持久锁文件 + 内核互斥；进程退出自动释放，不删除文件。
    pub fn acquire(&self) -> Result<PairGuard<'_>, PairError> {
        fs::create_dir_all(&self.dir)
            .map_err(|e| PairError::State(format!("创建状态目录: {e}")))?;
        let mut perm = fs::metadata(&self.dir)
            .map_err(|e| PairError::State(format!("读取状态目录权限: {e}")))?
            .permissions();
        perm.set_mode(0o700);
        fs::set_permissions(&self.dir, perm)
            .map_err(|e| PairError::State(format!("收紧状态目录权限: {e}")))?;
        let lock = crate::write_lock::WriteLock::at(&self.lock_path())
            .map_err(|_| PairError::Locked(self.lock_path()))?;
        Ok(PairGuard {
            _store: self,
            _lock: lock,
        })
    }

    pub fn load(&self) -> Result<Option<PairState>, PairError> {
        let path = self.state_path();
        if !path.exists() {
            return Ok(None);
        }
        let text = fs::read_to_string(&path)
            .map_err(|e| PairError::State(format!("读取状态文件 {path:?}: {e}")))?;
        serde_json::from_str(&text)
            .map(Some)
            .map_err(|e| PairError::State(format!("解析状态文件 {path:?}: {e}")))
    }

    /// 原子写：临时文件（0600、落盘）+ rename。落盘失败 = 停止后续写操作（文档 §3.1）。
    pub fn save_atomic(&self, state: &PairState) -> Result<(), PairError> {
        let json = serde_json::to_string_pretty(state)
            .map_err(|e| PairError::State(format!("序列化状态: {e}")))?;
        let tmp = self
            .dir
            .join(format!("pair-state.tmp.{}", std::process::id()));
        let mut f = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| PairError::State(format!("写临时状态文件: {e}")))?;
        use std::io::Write as _;
        f.write_all(json.as_bytes())
            .map_err(|e| PairError::State(format!("写临时状态文件: {e}")))?;
        f.sync_all()
            .map_err(|e| PairError::State(format!("落盘临时状态文件: {e}")))?;
        fs::rename(&tmp, self.state_path())
            .map_err(|e| PairError::State(format!("原子替换状态文件: {e}")))?;
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|e| PairError::State(format!("同步状态目录: {e}")))?;
        Ok(())
    }

    /// 审计归档：closed/failed 的旧状态改名保留，不删除（文档 §3）。
    pub fn archive(&self) -> Result<(), PairError> {
        let path = self.state_path();
        if !path.exists() {
            return Ok(());
        }
        let st = self
            .load()?
            .ok_or_else(|| PairError::State("归档前状态文件消失".into()))?;
        let target = self
            .dir
            .join(format!("pair-state-{}-{}.json", st.action_id, st.phase));
        // Keep the active record until its replacement is durable. An interrupted
        // transition must never leave an empty state directory.
        let bytes = fs::read(&path).map_err(|e| PairError::State(e.to_string()))?;
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&target)
        {
            Ok(mut file) => {
                use std::io::Write;
                file.write_all(&bytes)
                    .and_then(|_| file.sync_all())
                    .map_err(|e| PairError::State(format!("归档状态: {e}")))?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read(&target).map_err(|e| PairError::State(e.to_string()))? != bytes {
                    return Err(PairError::State("同名审计记录内容不同，拒绝覆盖".into()));
                }
            }
            Err(e) => return Err(PairError::State(format!("归档状态: {e}"))),
        }
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|e| PairError::State(format!("同步归档目录: {e}")))
    }
}

// ---------- 纯计算（可单测） ----------

/// 数量按市场精度向下截断（只舍不入，PHASE2_PLAN §2）。
pub fn floor_qty(qty: Decimal, sz_decimals: i64) -> Decimal {
    qty.round_dp_with_strategy(sz_decimals.max(0) as u32, RoundingStrategy::ToZero)
}

/// taker 保护价：买 = ask + 2·tick，卖 = bid − 2·tick，
/// 且必须本身满足市场 tick 规则；算不出合法保护价即拒绝（文档 §2）。
pub fn protect_price(market: &PerpMarket, buy: bool, bbo: &BboView) -> Result<Decimal, PairError> {
    let (basis, sign) = if buy {
        (
            bbo.ask.ok_or_else(|| {
                PairError::Refused(format!("{} 卖一价缺失，无法确定买单保护价", market.name))
            })?,
            Decimal::ONE,
        )
    } else {
        (
            bbo.bid.ok_or_else(|| {
                PairError::Refused(format!("{} 买一价缺失，无法确定卖单保护价", market.name))
            })?,
            -Decimal::ONE,
        )
    };
    let tick = market.tick_for(basis).ok_or_else(|| {
        PairError::Refused(format!("{} 保护价基准 {basis} 无法计算 tick", market.name))
    })?;
    let cand = basis + sign * tick * Decimal::from(PROTECT_TICKS);
    if cand > Decimal::ZERO && market.round_price(cand) == Some(cand) {
        return Ok(cand);
    }
    Err(PairError::Refused(format!(
        "{} 无法在 {basis} 附近确定合法的 taker 保护价",
        market.name
    )))
}

/// 目标数量：同时满足市价与保护价名义上限，向下截断（文档 §2）。
/// 返回的数量保证：qty > 0 且 qty × protect ≤ target_notional。
pub fn target_qty(
    target_notional: Decimal,
    mid: Decimal,
    protect_px: Decimal,
    sz_decimals: i64,
    coin: &str,
) -> Result<Decimal, PairError> {
    if mid <= Decimal::ZERO {
        return Err(PairError::Refused(format!(
            "{coin} 市价为非正值，无法计算数量"
        )));
    }
    if protect_px <= Decimal::ZERO {
        return Err(PairError::Refused("保护价必须为正".into()));
    }
    let qty = floor_qty(
        (target_notional / mid).min(target_notional / protect_px),
        sz_decimals,
    );
    if qty <= Decimal::ZERO || qty * protect_px < Decimal::TEN {
        return Err(PairError::Refused(format!(
            "{coin} 数量向下截断后为零或名义低于最小可下单值，拒绝开仓"
        )));
    }
    Ok(qty)
}

/// 每腿目标名义：min(用户参数, 配置单笔上限, 配置比例 × 权益)。必须为正（文档 §2）。
pub fn leg_notional(
    requested: Option<Decimal>,
    cfg: &Config,
    equity: Decimal,
) -> Result<Decimal, PairError> {
    if equity <= Decimal::ZERO {
        return Err(PairError::Refused(format!(
            "账户权益非正（{equity}），拒绝开仓"
        )));
    }
    let frac_cap = equity * cfg.leg_fraction;
    let cap = requested
        .unwrap_or(frac_cap)
        .min(cfg.max_notional_usd)
        .min(frac_cap);
    if cap <= Decimal::ZERO {
        return Err(PairError::Refused("每腿目标名义非正，拒绝开仓".into()));
    }
    Ok(cap)
}

fn gen_cloid() -> Result<Cloid, PairError> {
    use std::io::Read as _;
    let mut bytes = [0u8; 16];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| PairError::State(format!("生成 cloid 需要 /dev/urandom: {e}")))?;
    Ok(Cloid::from_slice(&bytes))
}

fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

// ---------- 执行 ----------

/// 一条腿的下单结果（交易所确认口径）。
enum LegResult {
    Filled {
        oid: u64,
        qty: Decimal,
        avg_px: Decimal,
    },
    PartFilled {
        oid: u64,
        qty: Decimal,
        avg_px: Decimal,
    },
    Rejected(String),
    Unknown(String),
}

/// 一条待发送的 IOC 腿（cloid 由调用方给出，与状态文件记录的意图一致）。
struct LegSend<'m> {
    market: &'m PerpMarket,
    buy: bool,
    qty: Decimal,
    protect_px: Decimal,
    reduce_only: bool,
    cloid: Cloid,
}

/// 发送单条 IOC 腿。
async fn send_leg(adapter: &Adapter, signer: &PrivateKeySigner, leg: &LegSend<'_>) -> LegResult {
    let intent = OrderIntent {
        asset: leg.market.index,
        buy: leg.buy,
        price: leg.protect_px,
        size: leg.qty,
        cloid: leg.cloid,
        tif: TimeInForce::Ioc,
        reduce_only: leg.reduce_only,
    };
    match adapter.place_order(signer, &intent).await {
        Ok(PlaceOutcome::Filled {
            oid,
            total_sz,
            avg_px,
        }) if total_sz == leg.qty && avg_px > Decimal::ZERO => LegResult::Filled {
            oid,
            qty: total_sz,
            avg_px,
        },
        // Hyperliquid 对未成交即终结的 IOC 返回 totalSz=0/avgPx=0：零成交且订单
        // 已终结是交易所确认完毕的已知结果，与拒单同等处理（规格 §2），不得进 Unknown。
        Ok(PlaceOutcome::Filled {
            total_sz, avg_px, ..
        }) if total_sz.is_zero() && avg_px.is_zero() => {
            LegResult::Rejected("IOC 零成交且订单已终结".into())
        }
        Ok(PlaceOutcome::Filled {
            oid,
            total_sz,
            avg_px,
        }) if total_sz >= Decimal::ZERO && total_sz < leg.qty && avg_px > Decimal::ZERO => {
            LegResult::PartFilled {
                oid,
                qty: total_sz,
                avg_px,
            }
        }
        // 真正的畸形响应（如 total_sz > leg.qty，或 total_sz > 0 但 avg_px = 0）。
        Ok(PlaceOutcome::Filled { .. }) => LegResult::Unknown("成交数量或价格不合法".into()),
        Ok(PlaceOutcome::Rejected(msg)) => LegResult::Rejected(msg),
        Ok(PlaceOutcome::Resting { oid }) => {
            // IOC 不应挂单；若交易所报告 resting，按“未确认成交”保守处理为未知。
            LegResult::Unknown(format!("IOC 单意外挂单 oid={oid}，结果未知，请核对"))
        }
        // 超时/断线/响应无法识别：结果未知，禁止自动重试（文档 §2 异常表）。
        Err(e @ (AdapterError::Unknown(_) | AdapterError::BadResponse(_))) => {
            LegResult::Unknown(e.to_string())
        }
        // HTTP 401/403：交易所明确拒绝、订单确定未提交，与拒单同等处理
        // （与 order 命令口径一致）；429/限流等仍走上方/下方 Unknown（保守）。
        Err(AdapterError::Permission(m)) => LegResult::Rejected(format!("权限拒绝: {m}")),
        Err(e) => LegResult::Unknown(e.to_string()),
    }
}

/// 补偿结果。
enum UnwindResult {
    AllGone,
    Residual(String),
    Unknown(String),
}

/// 合并两腿补偿的结局：任一未知 → 整体未知；任一残仓 → failed；全撤成功 → failed
/// （原动作仍失败，补偿成功只代表敞口已消除——文档 §2）。
fn merge_unwind(r1: UnwindResult, r2: UnwindResult, lines: &mut Vec<String>) -> Outcome {
    match (r1, r2) {
        (UnwindResult::Unknown(m), _) | (_, UnwindResult::Unknown(m)) => {
            lines.push(format!("补偿: {m}；结果未知，退出码 3"));
            Outcome::Unknown
        }
        (UnwindResult::Residual(m1), UnwindResult::Residual(m2)) => {
            lines.push(format!("补偿: 残仓 {m1}; {m2}；退出码 1"));
            Outcome::Failed
        }
        (UnwindResult::Residual(m), UnwindResult::AllGone)
        | (UnwindResult::AllGone, UnwindResult::Residual(m)) => {
            lines.push(format!("补偿: 残仓 {m}；退出码 1"));
            Outcome::Failed
        }
        (UnwindResult::AllGone, UnwindResult::AllGone) => {
            lines.push("补偿: 两腿均已确认撤回；原开仓仍记为失败".into());
            Outcome::Failed
        }
    }
}

fn phase_of_outcome(o: Outcome) -> Phase {
    match o {
        Outcome::Ok => Phase::Open,
        Outcome::Failed => Phase::Failed,
        Outcome::Unknown => Phase::Unknown,
    }
}

/// 只减仓撤回 qty（本轮动作已确认建立的量）。每腿至多调用一次（文档 §2 异常表）。
/// 发送前先把补偿意图落盘；新 cloid 独立记录。
async fn unwind_leg(
    adapter: &Adapter,
    signer: &PrivateKeySigner,
    market: &PerpMarket,
    state: &mut PairState,
    leg_is_long: bool,
    qty: Decimal,
    store: &PairStore,
) -> UnwindResult {
    if qty <= Decimal::ZERO {
        return UnwindResult::AllGone;
    }
    let user = match state.account.parse() {
        Ok(user) => user,
        Err(e) => return UnwindResult::Unknown(format!("账户地址损坏: {e}")),
    };
    let actual = match adapter.account(user).await {
        Ok(view) => view,
        Err(e) => return UnwindResult::Unknown(e.to_string()),
    };
    if let Err(e) = validate_positions(state, &actual) {
        return UnwindResult::Unknown(e.to_string());
    }
    let (coin, unwind_buy) = if leg_is_long {
        (state.long_leg.coin.clone(), false) // 平多 = 卖
    } else {
        (state.short_leg.coin.clone(), true) // 平空 = 买
    };
    let cloid = match gen_cloid() {
        Ok(c) => c,
        Err(e) => return UnwindResult::Unknown(e.to_string()),
    };

    // 发送前记录补偿意图（文档 §3.1：每次补偿前也先记录独立 cloid）。
    state.phase = Phase::Compensating;
    state.unwinds.push(UnwindRecord {
        oid: None,
        confirmed_qty: Decimal::ZERO,
        status: LegStatus::Prepared,
        coin: coin.clone(),
        side: if unwind_buy { "buy" } else { "sell" }.into(),
        qty,
        protect_px: Decimal::ZERO, // 发送前回填
        cloid: cloid_hex(cloid),
        result: "sending".into(),
    });
    if let Err(e) = store.save_atomic(state) {
        return UnwindResult::Unknown(format!("补偿意图落盘失败，已停止写操作: {e}"));
    }

    // 补偿按当时盘口重算保护价；缺盘口 = 无法补偿（未知）。
    let bbo = match adapter.bbo(&coin).await {
        Ok(b) => b,
        Err(e) => return UnwindResult::Unknown(format!("补偿前无法获取 {coin} 盘口: {e}")),
    };
    let protect = match protect_price(market, unwind_buy, &bbo) {
        Ok(p) => p,
        Err(e) => return UnwindResult::Unknown(format!("补偿保护价非法: {e}")),
    };
    if let Some(last) = state.unwinds.last_mut() {
        last.protect_px = protect;
        last.status = LegStatus::Pending;
    }
    if let Err(e) = store.save_atomic(state) {
        return UnwindResult::Unknown(format!("状态落盘失败，停止写操作: {e}"));
    }

    let res = send_leg(
        adapter,
        signer,
        &LegSend {
            market,
            buy: unwind_buy,
            qty,
            protect_px: protect,
            reduce_only: true,
            cloid,
        },
    )
    .await;
    let result_str = match &res {
        LegResult::Filled { oid, qty, avg_px } => {
            format!("unwound fully oid={oid} qty={qty} avg_px={avg_px}")
        }
        LegResult::PartFilled { oid, qty, .. } => {
            format!("unwound partially oid={oid} qty={qty}，仍有残仓")
        }
        LegResult::Rejected(m) => format!("unwind rejected: {m}"),
        LegResult::Unknown(m) => format!("unwind unknown: {m}"),
    };
    if let Some(last) = state.unwinds.last_mut() {
        last.result = result_str.clone();
        match &res {
            LegResult::Filled { oid, qty, .. } | LegResult::PartFilled { oid, qty, .. } => {
                last.oid = Some(*oid);
                last.confirmed_qty = *qty;
                last.status = if *qty == last.qty {
                    LegStatus::Filled
                } else {
                    LegStatus::PartFilled
                };
            }
            LegResult::Rejected(_) => last.status = LegStatus::Rejected,
            LegResult::Unknown(_) => last.status = LegStatus::Unknown,
        }
    }
    if let Err(e) = store.save_atomic(state) {
        return UnwindResult::Unknown(format!("状态落盘失败，停止写操作: {e}"));
    }

    if !matches!(res, LegResult::Unknown(_)) {
        let actual = match adapter.account(user).await {
            Ok(view) => view,
            Err(e) => return UnwindResult::Unknown(e.to_string()),
        };
        if let Err(e) = validate_positions(state, &actual) {
            return UnwindResult::Unknown(e.to_string());
        }
    }
    match res {
        LegResult::Filled { .. } => UnwindResult::AllGone,
        LegResult::PartFilled { qty: filled, .. } => {
            UnwindResult::Residual(format!("{coin} 补偿部分成交，残留 {}", qty - filled))
        }
        LegResult::Rejected(m) => {
            UnwindResult::Residual(format!("{coin} 补偿被拒（{m}），本轮建立的 {qty} 仍在"))
        }
        LegResult::Unknown(m) => UnwindResult::Unknown(format!("{coin} 补偿结果未知: {m}")),
    }
}

/// 开仓前检查（文档 §2）：任一腿有持仓/挂单，或存在未完结动作，拒绝。
fn preflight_open(store: &PairStore, view: &AccountView) -> Result<(), PairError> {
    if let Some(st) = store.load()?
        && !matches!(st.phase, Phase::Closed | Phase::Failed)
    {
        return Err(PairError::Refused(format!(
            "存在未完成动作 {}（phase={}，action={}）。先核对交易所状态；确认无持仓无挂单后可人工归档 {}",
            st.action_id,
            st.phase,
            st.action,
            store.state_path().display()
        )));
    }
    for p in &view.positions {
        if p.coin == LONG_COIN || p.coin == SHORT_COIN {
            return Err(PairError::Refused(format!(
                "账户已有 {} 持仓（szi={}），拒绝新开仓",
                p.coin, p.size
            )));
        }
    }
    for o in &view.open_orders {
        if o.coin == LONG_COIN || o.coin == SHORT_COIN {
            return Err(PairError::Refused(format!(
                "账户有 {} 挂单 oid={}，先处理挂单再开仓",
                o.coin, o.oid
            )));
        }
    }
    Ok(())
}

fn build_leg(
    coin: &str,
    side: &str,
    qty: Decimal,
    protect: Decimal,
) -> Result<LegState, PairError> {
    Ok(LegState {
        coin: coin.into(),
        side: side.into(),
        target_qty: qty,
        protect_px: protect,
        cloid: cloid_hex(gen_cloid()?),
        status: LegStatus::Prepared,
        confirmed_qty: Decimal::ZERO,
        avg_px: None,
        oid: None,
    })
}

/// 开仓计划：两腿保护价与数量。dry-run 与 execute 共用同一计算。
struct OpenPlan {
    long_qty: Decimal,
    short_qty: Decimal,
    long_protect: Decimal,
    short_protect: Decimal,
    target: Decimal,
    mid_long: Decimal,
    mid_short: Decimal,
    book_time_ms: u64,
    /// 非致命警示（如市场仅支持逐仓），回执原样展示。
    warnings: Vec<String>,
}

async fn plan_open(
    adapter: &Adapter,
    cfg: &Config,
    equity: Decimal,
    requested: Option<Decimal>,
) -> Result<(OpenPlan, PerpMarket, PerpMarket), PairError> {
    let perps = adapter.perp_markets().await?;
    let long_m = find_market(&perps, LONG_COIN)?.clone();
    let short_m = find_market(&perps, SHORT_COIN)?.clone();
    let target = leg_notional(requested, cfg, equity)?;
    let user = cfg
        .account_address()
        .map_err(|e| PairError::State(e.to_string()))?;
    let mut warnings = Vec::new();
    for market in [&long_m, &short_m] {
        if market.delisted {
            return Err(PairError::Refused(format!(
                "{} 已退市,不可交易",
                market.name
            )));
        }
        if market.isolated_margin {
            // 市场元数据 onlyIsolated（交易所层面禁止全仓，如测试网 HYPE strictIsolated）：
            // 已拍板接受逐仓，自担单腿独立强平风险，不再检查/设置 cross_margin。
            warnings.push(format!(
                "警告: {} 市场仅支持逐仓(isolated),接受单腿独立强平风险(保证金例外,见 PHASE2_PLAN)",
                market.name
            ));
            continue;
        }
        if !adapter.cross_margin(user, &market.name).await? {
            return Err(PairError::Refused(format!(
                "{} 未配置全仓保证金模式",
                market.name
            )));
        }
    }
    let bbo_long = adapter.bbo(LONG_COIN).await?;
    let bbo_short = adapter.bbo(SHORT_COIN).await?;
    let mid = |b: &BboView| -> Result<Decimal, PairError> {
        match (b.bid, b.ask) {
            (Some(bid), Some(ask)) => Ok((bid + ask) / Decimal::TWO),
            _ => Err(PairError::Refused(
                "缺盘口（买/卖一价不完整），拒绝发送".into(),
            )),
        }
    };
    let mid_long = mid(&bbo_long)?;
    let mid_short = mid(&bbo_short)?;
    let long_protect = protect_price(&long_m, true, &bbo_long)?;
    let short_protect = protect_price(&short_m, false, &bbo_short)?;
    let long_qty = target_qty(
        target,
        mid_long,
        long_protect,
        long_m.sz_decimals,
        LONG_COIN,
    )?;
    let short_qty = target_qty(
        target,
        mid_short,
        short_protect,
        short_m.sz_decimals,
        SHORT_COIN,
    )?;
    Ok((
        OpenPlan {
            long_qty,
            short_qty,
            long_protect,
            short_protect,
            target,
            mid_long,
            mid_short,
            book_time_ms: bbo_long.time_ms.max(bbo_short.time_ms),
            warnings,
        },
        long_m,
        short_m,
    ))
}

/// 开仓（PHASE2_PLAN §2）。signer 仅 execute 路径提供；dry-run 不加载私钥。
pub async fn run_open(
    adapter: &Adapter,
    signer: Option<&PrivateKeySigner>,
    cfg: &Config,
    requested: Option<Decimal>,
    execute: bool,
) -> Result<PairReport, PairError> {
    let requested = requested
        .map(|r| {
            if r <= Decimal::ZERO {
                Err(PairError::Refused("--notional 必须为正数".into()))
            } else {
                Ok(r)
            }
        })
        .transpose()?;

    let store = PairStore::new(cfg.receipt_dir.clone());
    let _guard = if execute {
        Some(store.acquire()?)
    } else {
        None
    };
    // 预览也做完整检查（dry-run 无副作用，但不省检查）。
    let account = adapter
        .account(
            cfg.account_address()
                .map_err(|e| PairError::State(e.to_string()))?,
        )
        .await?;
    if let Some(st) = store.load()? {
        validate_identity(&st, cfg)?;
    }
    preflight_open(&store, &account)?;
    let equity = account.account_value;
    let (plan, _, _) = plan_open(adapter, cfg, equity, requested).await?;

    let mut lines = vec![
        format!("network: {NETWORK}"),
        format!("account: {}", cfg.account_address),
        format!(
            "计划: 多 {LONG_COIN} {lq} @≤{lp} + 空 {SHORT_COIN} {sq} @≥{sp}（IOC taker，保护价带 2 tick）",
            lq = plan.long_qty,
            lp = plan.long_protect,
            sq = plan.short_qty,
            sp = plan.short_protect,
        ),
        format!(
            "每腿目标名义 ≤ {} USD（min(参数, 配置单笔上限, 配置比例×权益)）；市价 mid: {LONG_COIN}={}, {SHORT_COIN}={}；盘口时间 {}ms",
            plan.target, plan.mid_long, plan.mid_short, plan.book_time_ms
        ),
        "说明: 两腿按各自市价/保护价计算，名义可能不完全相等，成交后显示实际值，不自动补量".into(),
    ];
    lines.extend(plan.warnings.iter().cloned());
    if let Some(r) = requested {
        lines.push(format!("请求名义 {r} USD，限额后 {}", plan.target));
    }

    if !execute {
        lines.push("dry-run: 未加载私钥、未下单。确认无误后加 --execute 显式提交。".into());
        return Ok(PairReport::ok(lines));
    }
    let signer = match signer {
        Some(s) => s,
        None => {
            return Err(PairError::Refused(
                "execute 路径缺少签名器（内部错误）".into(),
            ));
        }
    };

    // execute 前重查（预览与实际发送之间可能已过去任意时间）。
    let account = adapter
        .account(
            cfg.account_address()
                .map_err(|e| PairError::State(e.to_string()))?,
        )
        .await?;
    preflight_open(&store, &account)?;
    let (plan, long_m, short_m) = plan_open(adapter, cfg, account.account_value, requested).await?;
    if store.load()?.is_some() {
        store.archive()?; // 旧 closed/failed 状态归档，审计保留
    }

    let now_ms = now_epoch_ms()?;
    let mut state = PairState {
        external_closure: None,
        accounting_start_ms: Some(now_ms),
        prior_oids: Vec::new(),
        prior_quantities: Vec::new(),
        audit_complete: true,
        version: 2,
        network: NETWORK.into(),
        account: cfg.account_address.clone(),
        action_id: format!("open-{}", cloid_hex(gen_cloid()?)),
        action: "open".into(),
        phase: Phase::Prepared,
        created_at_utc: now_utc(),
        created_at_ms: now_ms,
        equity_at_prepare: account.account_value,
        long_leg: build_leg(LONG_COIN, "buy", plan.long_qty, plan.long_protect)?,
        short_leg: build_leg(SHORT_COIN, "sell", plan.short_qty, plan.short_protect)?,
        start_positions: account
            .positions
            .iter()
            .filter(|p| p.coin == LONG_COIN || p.coin == SHORT_COIN)
            .map(|p| (p.coin.clone(), p.size))
            .collect(),
        unwinds: Vec::new(),
        detail: None,
        closed_at_utc: None,
    };

    // 互斥锁覆盖整个写阶段；发送前落盘意图（文档 §3.1：禁止仅在成交后写状态）。
    store.save_atomic(&state)?;
    state.phase = Phase::Opening;
    state.long_leg.status = LegStatus::Pending;
    store.save_atomic(&state)?;

    // ---- 第一腿：HYPE 多 ----
    let long_cloid = parse_cloid(&state.long_leg.cloid)
        .map_err(|e| PairError::State(format!("状态文件 cloid 损坏: {e}")))?;
    lines.push(format!(
        "[腿1] {LONG_COIN} 买 {} @≤{}",
        state.long_leg.target_qty, state.long_leg.protect_px
    ));
    let long_res = send_leg(
        adapter,
        signer,
        &LegSend {
            market: &long_m,
            buy: true,
            qty: state.long_leg.target_qty,
            protect_px: state.long_leg.protect_px,
            reduce_only: false,
            cloid: long_cloid,
        },
    )
    .await;

    let long_confirmed = match long_res {
        LegResult::Rejected(m) => {
            state.long_leg.status = LegStatus::Rejected;
            state.phase = Phase::Failed;
            state.detail = Some(format!("第一腿拒单: {m}"));
            persist(&store, &state)?;
            lines.push(format!("结果: 第一腿被拒（{m}），未发送第二腿，开仓失败"));
            return Ok(PairReport::failed(lines));
        }
        LegResult::Unknown(m) => {
            state.long_leg.status = LegStatus::Unknown;
            state.phase = Phase::Unknown;
            state.detail = Some(format!("第一腿结果未知: {m}"));
            persist(&store, &state)?;
            lines.push(format!(
                "结果: 第一腿结果未知（{m}），已停止写操作；请按 cloid 核对交易所状态后再处理，退出码 3"
            ));
            return Ok(PairReport::unknown(lines));
        }
        LegResult::PartFilled { oid, qty, avg_px } => {
            state.long_leg.status = LegStatus::PartFilled;
            state.long_leg.confirmed_qty = qty;
            state.long_leg.avg_px = Some(avg_px);
            state.long_leg.oid = Some(oid);
            persist(&store, &state)?;
            lines.push(format!(
                "结果: 第一腿部分成交 {qty}（oid={oid}），未发送第二腿，只减仓撤回"
            ));
            let r = unwind_leg(adapter, signer, &long_m, &mut state, true, qty, &store).await;
            let outcome = match r {
                UnwindResult::AllGone => {
                    state.detail = Some("第一腿部分成交已撤回".into());
                    lines.push("补偿: 已撤回全部已确认成交量；原开仓记为失败".into());
                    Outcome::Failed
                }
                UnwindResult::Residual(m) => {
                    state.detail = Some(m.clone());
                    lines.push(format!("补偿: {m}；退出码 1"));
                    Outcome::Failed
                }
                UnwindResult::Unknown(m) => {
                    state.detail = Some(m.clone());
                    lines.push(format!("补偿: {m}；退出码 3"));
                    Outcome::Unknown
                }
            };
            state.phase = phase_of_outcome(outcome);
            persist(&store, &state)?;
            return Ok(match outcome {
                Outcome::Ok => PairReport::ok(lines),
                Outcome::Failed => PairReport::failed(lines),
                Outcome::Unknown => PairReport::unknown(lines),
            });
        }
        LegResult::Filled { oid, qty, avg_px } => {
            state.long_leg.status = LegStatus::Filled;
            state.long_leg.confirmed_qty = qty;
            state.long_leg.avg_px = Some(avg_px);
            state.long_leg.oid = Some(oid);
            persist(&store, &state)?;
            lines.push(format!("成交: 腿1 全成 {qty} @ {avg_px}（oid={oid}）"));
            qty
        }
    };

    // ---- 第二腿：BTC 空 ----
    state.short_leg.status = LegStatus::Pending;
    persist(&store, &state)?;
    let short_cloid = parse_cloid(&state.short_leg.cloid)
        .map_err(|e| PairError::State(format!("状态文件 cloid 损坏: {e}")))?;
    lines.push(format!(
        "[腿2] {SHORT_COIN} 卖 {} @≥{}",
        state.short_leg.target_qty, state.short_leg.protect_px
    ));
    let short_res = send_leg(
        adapter,
        signer,
        &LegSend {
            market: &short_m,
            buy: false,
            qty: state.short_leg.target_qty,
            protect_px: state.short_leg.protect_px,
            reduce_only: false,
            cloid: short_cloid,
        },
    )
    .await;

    let outcome = match short_res {
        LegResult::Filled { oid, qty, avg_px } => {
            state.short_leg.status = LegStatus::Filled;
            state.short_leg.confirmed_qty = qty;
            state.short_leg.avg_px = Some(avg_px);
            state.short_leg.oid = Some(oid);
            state.phase = Phase::Open;
            state.detail = Some("pair 开仓完成".into());
            lines.push(format!("成交: 腿2 全成 {qty} @ {avg_px}（oid={oid}）"));
            let long_ntl = state.long_leg.confirmed_qty * state.long_leg.avg_px.unwrap_or_default();
            let short_ntl =
                state.short_leg.confirmed_qty * state.short_leg.avg_px.unwrap_or_default();
            let denom = long_ntl.max(short_ntl);
            let dev = if denom.is_zero() {
                Decimal::ZERO
            } else {
                ((long_ntl - short_ntl).abs() / denom * Decimal::new(100, 0)).round_dp(2)
            };
            lines.push(format!(
                "名义(按成交均价): {LONG_COIN} {long_ntl} USD / {SHORT_COIN} {short_ntl} USD（两腿偏差 {dev}%）"
            ));
            Outcome::Ok
        }
        LegResult::Rejected(m) => {
            state.short_leg.status = LegStatus::Rejected;
            state.detail = Some(format!("第二腿拒单: {m}"));
            lines.push(format!(
                "结果: 第二腿被拒（{m}），只减仓撤回第一腿已确认仓位 {long_confirmed}"
            ));
            let r = unwind_leg(
                adapter,
                signer,
                &long_m,
                &mut state,
                true,
                long_confirmed,
                &store,
            )
            .await;
            let outcome = match r {
                UnwindResult::AllGone => {
                    state.detail = Some(format!("第二腿拒单({m})，第一腿已撤回"));
                    lines.push("补偿: 第一腿已全部撤回；原开仓记为失败".into());
                    Outcome::Failed
                }
                UnwindResult::Residual(rm) => {
                    state.detail = Some(rm.clone());
                    lines.push(format!("补偿: {rm}；退出码 1"));
                    Outcome::Failed
                }
                UnwindResult::Unknown(rm) => {
                    state.detail = Some(rm.clone());
                    lines.push(format!("补偿: {rm}；退出码 3"));
                    Outcome::Unknown
                }
            };
            state.phase = phase_of_outcome(outcome);
            outcome
        }
        LegResult::PartFilled { oid, qty, avg_px } => {
            state.short_leg.status = LegStatus::PartFilled;
            state.short_leg.confirmed_qty = qty;
            state.short_leg.avg_px = Some(avg_px);
            state.short_leg.oid = Some(oid);
            lines.push(format!(
                "结果: 第二腿部分成交 {qty}（oid={oid}），撤回第一腿 {long_confirmed} 与第二腿已确认 {qty}"
            ));
            let r1 = unwind_leg(
                adapter,
                signer,
                &long_m,
                &mut state,
                true,
                long_confirmed,
                &store,
            )
            .await;
            if let UnwindResult::Unknown(ref message) = r1 {
                state.phase = Phase::Unknown;
                state.detail = Some(message.clone());
                persist(&store, &state)?;
                lines.push(format!("补偿结果未知，停止后续写操作: {message}"));
                return Ok(PairReport::unknown(lines));
            }
            let r2 = unwind_leg(adapter, signer, &short_m, &mut state, false, qty, &store).await;
            let outcome = merge_unwind(r1, r2, &mut lines);
            state.phase = phase_of_outcome(outcome);
            outcome
        }
        LegResult::Unknown(m) => {
            state.short_leg.status = LegStatus::Unknown;
            state.phase = Phase::Unknown;
            state.detail = Some(format!(
                "第二腿结果未知: {m}；第一腿 {long_confirmed} 仍持有"
            ));
            lines.push(format!(
                "结果: 第二腿结果未知（{m}），停止写操作；第一腿 {long_confirmed} {LONG_COIN} 仍在，请核对，退出码 3"
            ));
            Outcome::Unknown
        }
    };

    persist(&store, &state)?;
    if outcome == Outcome::Ok {
        let user = cfg
            .account_address()
            .map_err(|e| PairError::State(e.to_string()))?;
        let actual = adapter
            .account(user)
            .await
            .map_err(|e| PairError::Unresolved(e.to_string()))?;
        validate_positions(&state, &actual)?;
    }
    Ok(match outcome {
        Outcome::Ok => PairReport::ok(lines),
        Outcome::Failed => PairReport::failed(lines),
        Outcome::Unknown => PairReport::unknown(lines),
    })
}

fn persist(store: &PairStore, state: &PairState) -> Result<(), PairError> {
    store.save_atomic(state).map_err(|e| {
        PairError::Unresolved(format!(
            "{e}; 已停止后续写操作。动作 {}: {} {:?} 已确认 {}, {} {:?} 已确认 {}; 补偿 {:?}",
            state.action_id,
            state.long_leg.cloid,
            state.long_leg.status,
            state.long_leg.confirmed_qty,
            state.short_leg.cloid,
            state.short_leg.status,
            state.short_leg.confirmed_qty,
            state.unwinds
        ))
    })
}

fn position(view: &AccountView, coin: &str) -> Decimal {
    view.positions
        .iter()
        .find(|p| p.coin == coin)
        .map(|p| p.size)
        .unwrap_or_default()
}

fn no_orders(view: &AccountView) -> Result<(), PairError> {
    if let Some(o) = view
        .open_orders
        .iter()
        .find(|o| o.coin == LONG_COIN || o.coin == SHORT_COIN)
    {
        return Err(PairError::Unresolved(format!(
            "{} 挂单 oid={} 未终结，请查单或撤指定单",
            o.coin, o.oid
        )));
    }
    Ok(())
}

fn signed(qty: Decimal, side: &str) -> Decimal {
    if side == "buy" { qty } else { -qty }
}

fn expected_position(state: &PairState, coin: &str) -> Decimal {
    let start = state
        .start_positions
        .iter()
        .find(|(c, _)| c == coin)
        .map(|(_, q)| *q)
        .unwrap_or_default();
    let leg = if coin == LONG_COIN {
        &state.long_leg
    } else {
        &state.short_leg
    };
    start
        + signed(leg.confirmed_qty, &leg.side)
        + state
            .unwinds
            .iter()
            .filter(|u| u.coin == coin)
            .map(|u| signed(u.confirmed_qty, &u.side))
            .sum::<Decimal>()
}

fn validate_positions(state: &PairState, view: &AccountView) -> Result<(), PairError> {
    no_orders(view)?;
    for coin in [LONG_COIN, SHORT_COIN] {
        let expected = expected_position(state, coin);
        let actual = position(view, coin);
        if expected != actual {
            return Err(PairError::Unresolved(format!(
                "{coin} 持仓与动作记录不一致: 预期 {expected}, 实际 {actual}; 请等待交易所更新或核对外部交易"
            )));
        }
    }
    Ok(())
}

fn validate_identity(state: &PairState, cfg: &Config) -> Result<(), PairError> {
    if state.network != NETWORK
        || state.account.parse::<hypersdk::Address>().ok() != cfg.account_address().ok()
    {
        return Err(PairError::Refused(
            "状态文件的账户或网络与配置不一致".into(),
        ));
    }
    Ok(())
}

async fn reconcile_leg(
    adapter: &Adapter,
    user: hypersdk::Address,
    since: u64,
    leg: &mut LegState,
) -> Result<(), PairError> {
    if !matches!(leg.status, LegStatus::Pending | LegStatus::Unknown) {
        return Ok(());
    }
    let cloid = parse_cloid(&leg.cloid).map_err(PairError::State)?;
    let status = adapter
        .query_status(user, cloid)
        .await
        .map_err(|e| PairError::Unresolved(e.to_string()))?
        .ok_or_else(|| {
            PairError::Unresolved(format!("cloid {} 未找到，不能据此认定未成交", leg.cloid))
        })?;
    if !status.finished || status.coin != leg.coin || status.side != leg.side {
        return Err(PairError::Unresolved(format!(
            "cloid {} 尚未终结或订单信息不一致",
            leg.cloid
        )));
    }
    let fills = adapter
        .user_fills_since(user, since)
        .await
        .map_err(|e| PairError::Unresolved(e.to_string()))?;
    let mut qty = Decimal::ZERO;
    let mut notional = Decimal::ZERO;
    for f in fills.iter().filter(|f| f.oid == status.oid) {
        if f.coin != leg.coin || f.buy != (leg.side == "buy") {
            return Err(PairError::Unresolved("成交记录与订单意图不一致".into()));
        }
        qty += f.sz;
        notional += f.sz * f.px;
    }
    if qty > leg.target_qty || (status.filled && qty != leg.target_qty) {
        return Err(PairError::Unresolved(format!(
            "cloid {} 成交记录尚不完整或数量异常",
            leg.cloid
        )));
    }
    leg.confirmed_qty = qty;
    leg.oid = Some(status.oid);
    leg.avg_px = if qty > Decimal::ZERO {
        Some(notional / qty)
    } else {
        None
    };
    leg.status = if qty == leg.target_qty {
        LegStatus::Filled
    } else if qty > Decimal::ZERO {
        LegStatus::PartFilled
    } else {
        LegStatus::Rejected
    };
    Ok(())
}

async fn reconcile_state(
    adapter: &Adapter,
    cfg: &Config,
    state: &mut PairState,
) -> Result<AccountView, PairError> {
    validate_identity(state, cfg)?;
    let user = cfg
        .account_address()
        .map_err(|e| PairError::State(e.to_string()))?;
    reconcile_leg(adapter, user, state.created_at_ms, &mut state.long_leg).await?;
    reconcile_leg(adapter, user, state.created_at_ms, &mut state.short_leg).await?;
    for unwind in &mut state.unwinds {
        let mut leg = LegState {
            coin: unwind.coin.clone(),
            side: unwind.side.clone(),
            target_qty: unwind.qty,
            protect_px: unwind.protect_px,
            cloid: unwind.cloid.clone(),
            status: unwind.status,
            confirmed_qty: unwind.confirmed_qty,
            avg_px: None,
            oid: unwind.oid,
        };
        reconcile_leg(adapter, user, state.created_at_ms, &mut leg).await?;
        unwind.oid = leg.oid;
        unwind.status = leg.status;
        unwind.confirmed_qty = leg.confirmed_qty;
        unwind.result = format!("核对完成: {:?}, 成交 {}", leg.status, leg.confirmed_qty);
    }
    let view = adapter
        .account(user)
        .await
        .map_err(|e| PairError::Unresolved(e.to_string()))?;
    // 已有外部归零记录必须重新核对实际零仓。即使新仓恰好等于旧成交量，
    // 也不能把它当成旧 pair 自动平掉。旧版本只写 detail，按精确标记迁移。
    let legacy_external_close = state.phase == Phase::Closed
        && state.closed_at_utc.is_some()
        && state.detail.as_deref() == Some(EXTERNAL_CLOSE_DETAIL);
    if state.external_closure.is_some() || legacy_external_close {
        no_orders(&view)?;
        if state.phase != Phase::Closed
            || [LONG_COIN, SHORT_COIN]
                .iter()
                .any(|coin| !position(&view, coin).is_zero())
        {
            return Err(PairError::Unresolved(
                "外部归零记录与当前状态或持仓不一致，请核对新增的外部交易".into(),
            ));
        }
        record_external_closure(state);
        return Ok(view);
    }
    if let Err(e) = validate_positions(state, &view) {
        // 非 Closed 状态下（含 open/incomplete/failed/unknown），若交易所已无挂单且两腿
        // 实际持仓均已在外部归零，说明持仓被外部（人工或第三方）全部平掉——这是交易
        // 所确认的已知终局，按已平仓处理并继续走调用方的正常归零路径；部分持仓、有挂
        // 单等其它不一致仍按 Unresolved 上报，不猜测归属（规格 §3）。
        let externally_closed = !matches!(state.phase, Phase::Closed)
            && no_orders(&view).is_ok()
            && position(&view, LONG_COIN).is_zero()
            && position(&view, SHORT_COIN).is_zero();
        if externally_closed {
            record_external_closure(state);
        } else {
            return Err(e);
        }
    }
    Ok(view)
}

fn record_external_closure(state: &mut PairState) {
    if state.external_closure.is_none() {
        state.external_closure = Some(ExternalClosure {
            observed_at_utc: now_utc(),
            prior_expected_positions: [LONG_COIN, SHORT_COIN]
                .iter()
                .map(|coin| ((*coin).into(), expected_position(state, coin)))
                .collect(),
        });
    }
    state.phase = Phase::Closed;
    state.closed_at_utc.get_or_insert_with(now_utc);
    state.audit_complete = false;
    state.detail = Some(EXTERNAL_CLOSE_DETAIL.into());
}

fn collect_quantities(state: &PairState) -> Vec<(u64, Decimal)> {
    let mut quantities: std::collections::BTreeMap<_, _> =
        state.prior_quantities.iter().copied().collect();
    for leg in [&state.long_leg, &state.short_leg] {
        if let Some(oid) = leg.oid {
            quantities.insert(oid, leg.confirmed_qty);
        }
    }
    for unwind in &state.unwinds {
        if let Some(oid) = unwind.oid {
            quantities.insert(oid, unwind.confirmed_qty);
        }
    }
    quantities.into_iter().collect()
}

fn collect_oids(state: &PairState) -> Vec<u64> {
    let mut ids = state.prior_oids.clone();
    ids.extend(
        [state.long_leg.oid, state.short_leg.oid]
            .into_iter()
            .flatten(),
    );
    ids.extend(state.unwinds.iter().filter_map(|u| u.oid));
    ids.sort_unstable();
    ids.dedup();
    ids
}

/// Closing is a new, archived action. Known residuals can be closed again;
/// uncertain orders must first be reconciled, including interrupted Closing.
pub async fn run_close(
    adapter: &Adapter,
    signer: Option<&PrivateKeySigner>,
    cfg: &Config,
    execute: bool,
) -> Result<PairReport, PairError> {
    let store = PairStore::new(cfg.receipt_dir.clone());
    let _guard = if execute {
        Some(store.acquire()?)
    } else {
        None
    };
    let mut existing = store.load()?;
    let user = cfg
        .account_address()
        .map_err(|e| PairError::State(e.to_string()))?;
    let view = if let Some(st) = &mut existing {
        let view = reconcile_state(adapter, cfg, st).await?;
        if execute {
            persist(&store, st)?;
        }
        view
    } else {
        adapter.account(user).await?
    };
    no_orders(&view)?;
    let hype = position(&view, LONG_COIN);
    let btc = position(&view, SHORT_COIN);
    if hype < Decimal::ZERO || btc > Decimal::ZERO {
        return Err(PairError::Refused(format!(
            "发现反向持仓: HYPE {hype}, BTC {btc}"
        )));
    }
    if hype.is_zero() && btc.is_zero() {
        if execute && let Some(st) = &mut existing {
            st.phase = Phase::Closed;
            st.closed_at_utc.get_or_insert_with(now_utc);
            persist(&store, st)?;
        }
        let mut lines = vec![
            format!("network: {NETWORK}"),
            "已核对 HYPE/BTC 持仓为零且无挂单".into(),
        ];
        if let Some(st) = &existing
            && st.external_closure.is_some()
        {
            lines.push("外部归零：保留原始成交记录".into());
            lines.extend(close_accounting(adapter, user, st, true).await);
        }
        return Ok(PairReport::ok(lines));
    }
    let perps = adapter.perp_markets().await?;
    let long_m = find_market(&perps, LONG_COIN)?;
    let short_m = find_market(&perps, SHORT_COIN)?;
    let mut plans = Vec::new();
    let mut lines = vec![format!("network: {NETWORK}"), format!("account: {user}")];
    for (is_long, market, qty) in [(true, long_m, hype), (false, short_m, -btc)] {
        if qty.is_zero() {
            continue;
        }
        if floor_qty(qty, market.sz_decimals) != qty {
            return Err(PairError::Refused(format!(
                "{} 实际仓位精度异常",
                market.name
            )));
        }
        let protect = protect_price(market, !is_long, &adapter.bbo(&market.name).await?)?;
        lines.push(format!(
            "计划: 平 {} {} @{}（reduce-only IOC）",
            market.name, qty, protect
        ));
        plans.push((is_long, market, qty));
    }
    if !execute {
        lines.push("dry-run: 仅核对和预览，未修改交易状态、未加载私钥、未下单".into());
        return Ok(PairReport::ok(lines));
    }
    let signer = signer.ok_or_else(|| PairError::Refused("execute 缺少签名器".into()))?;
    let now_ms = now_epoch_ms()?;
    let mut state = PairState {
        external_closure: None,
        version: 2,
        network: NETWORK.into(),
        account: cfg.account_address.clone(),
        action_id: format!("close-{}", cloid_hex(gen_cloid()?)),
        action: "close".into(),
        phase: Phase::Prepared,
        created_at_utc: now_utc(),
        created_at_ms: now_ms,
        equity_at_prepare: view.account_value,
        accounting_start_ms: existing.as_ref().and_then(|s| s.accounting_start_ms),
        prior_oids: existing.as_ref().map(collect_oids).unwrap_or_default(),
        prior_quantities: existing
            .as_ref()
            .map(collect_quantities)
            .unwrap_or_default(),
        audit_complete: existing.as_ref().is_some_and(|s| s.audit_complete),
        long_leg: build_leg(LONG_COIN, "sell", hype, Decimal::ZERO)?,
        short_leg: build_leg(SHORT_COIN, "buy", -btc, Decimal::ZERO)?,
        start_positions: vec![(LONG_COIN.into(), hype), (SHORT_COIN.into(), btc)],
        unwinds: Vec::new(),
        detail: None,
        closed_at_utc: None,
    };
    if existing.is_some() {
        store.archive()?;
    }
    persist(&store, &state)?;
    for (is_long, market, _) in plans {
        // Re-query before each close: do not close external or unexplained inventory.
        let current = adapter
            .account(user)
            .await
            .map_err(|e| PairError::Unresolved(e.to_string()))?;
        validate_positions(&state, &current)?;
        let qty = position(&current, &market.name).abs();
        if qty.is_zero() {
            continue;
        }
        let protect = protect_price(market, !is_long, &adapter.bbo(&market.name).await?)?;
        let leg = if is_long {
            &mut state.long_leg
        } else {
            &mut state.short_leg
        };
        leg.target_qty = qty;
        leg.protect_px = protect;
        leg.status = LegStatus::Pending;
        let cloid = parse_cloid(&leg.cloid).map_err(PairError::State)?;
        state.phase = Phase::Closing;
        persist(&store, &state)?;
        let res = send_leg(
            adapter,
            signer,
            &LegSend {
                market,
                buy: !is_long,
                qty,
                protect_px: protect,
                reduce_only: true,
                cloid,
            },
        )
        .await;
        let leg = if is_long {
            &mut state.long_leg
        } else {
            &mut state.short_leg
        };
        match res {
            LegResult::Filled { oid, qty, avg_px } | LegResult::PartFilled { oid, qty, avg_px } => {
                leg.status = if qty == leg.target_qty {
                    LegStatus::Filled
                } else {
                    LegStatus::PartFilled
                };
                leg.confirmed_qty = qty;
                leg.oid = Some(oid);
                leg.avg_px = Some(avg_px);
                lines.push(format!("成交: {} {qty} @ {avg_px} oid={oid}", market.name));
            }
            LegResult::Rejected(m) => {
                leg.status = LegStatus::Rejected;
                lines.push(format!("{} 拒绝: {m}", market.name));
            }
            LegResult::Unknown(m) => {
                leg.status = LegStatus::Unknown;
                state.phase = Phase::Unknown;
                state.detail = Some(m.clone());
                persist(&store, &state)?;
                lines.push(format!(
                    "结果未知，停止后续写操作: {m}; 请按记录 cloid 核对"
                ));
                return Ok(PairReport::unknown(lines));
            }
        }
        persist(&store, &state)?;
    }
    let actual = adapter
        .account(user)
        .await
        .map_err(|e| PairError::Unresolved(e.to_string()))?;
    if let Err(e) = validate_positions(&state, &actual) {
        state.phase = Phase::Unknown;
        state.detail = Some(e.to_string());
        persist(&store, &state)?;
        lines.push(e.to_string());
        return Ok(PairReport::unknown(lines));
    }
    let flat = [LONG_COIN, SHORT_COIN]
        .iter()
        .all(|c| position(&actual, c).is_zero());
    state.phase = if flat {
        Phase::Closed
    } else {
        Phase::Incomplete
    };
    state.closed_at_utc = if flat { Some(now_utc()) } else { None };
    state.detail = Some(
        if flat {
            "实际持仓已归零且无挂单"
        } else {
            "已知残仓，可再次显式 close"
        }
        .into(),
    );
    persist(&store, &state)?;
    lines.push(format!(
        "核对: HYPE {}, BTC {}; phase={}",
        position(&actual, LONG_COIN),
        position(&actual, SHORT_COIN),
        state.phase
    ));
    lines.extend(close_accounting(adapter, user, &state, !state.audit_complete).await);
    Ok(if flat {
        PairReport::ok(lines)
    } else {
        PairReport::failed(lines)
    })
}

/// 平仓核算：userFills（手续费、实现盈亏）+ userFunding（资金费净收付）。
/// rebuilt=true（状态文件丢失后重建）时无法确定持仓区间，如实标记不可完整归集。
async fn close_accounting(
    adapter: &Adapter,
    user: hypersdk::Address,
    state: &PairState,
    rebuilt: bool,
) -> Vec<String> {
    if state.external_closure.is_some() {
        return vec!["核算: 不可完整核算（外部归零，缺少对应平仓成交记录）".into()];
    }
    if rebuilt || state.accounting_start_ms.is_none() {
        return vec!["核算: 不可完整核算（状态文件丢失，无法确定持仓起始时间）".into()];
    }
    let start = state.accounting_start_ms.unwrap();
    let ids = collect_oids(state);
    let fills = adapter.user_fills_since(user, start).await;
    let fundings = adapter.user_funding_since(user, start).await;
    match (fills, fundings) {
        (Ok(fills), Ok(fundings)) => {
            if collect_quantities(state).iter().any(|(oid, expected)| {
                fills
                    .iter()
                    .filter(|f| f.oid == *oid)
                    .map(|f| f.sz)
                    .sum::<Decimal>()
                    != *expected
            }) || fills
                .iter()
                .filter(|f| ids.contains(&f.oid))
                .any(|f| f.fee_token != "USDC")
            {
                return vec!["核算: 不可完整核算（订单成交量不完整或手续费币种不是 USDC）".into()];
            }
            let mut fee = Decimal::ZERO;
            let mut pnl = Decimal::ZERO;
            for f in fills.iter().filter(|f| ids.contains(&f.oid)) {
                fee += f.fee;
                pnl += f.closed_pnl;
            }
            let mut funding_net = Decimal::ZERO;
            let mut funding_n = 0usize;
            for p in fundings
                .iter()
                .filter(|p| p.coin == LONG_COIN || p.coin == SHORT_COIN)
            {
                funding_net += p.usdc;
                funding_n += 1;
            }
            let mut lines = vec![format!(
                "核算(自 {}ms 起, 本 pair 订单): 手续费 {fee} USDC, 实现盈亏 {pnl} USDC, 资金费净收付 {funding_net} USDC（{funding_n} 笔）",
                start
            )];
            for f in fills.iter().filter(|f| ids.contains(&f.oid)) {
                let t = chrono::DateTime::<chrono::Utc>::from_timestamp_millis(f.time_ms as i64)
                    .map(|d| d.format("%Y-%m-%d %H:%M UTC").to_string())
                    .unwrap_or_else(|| format!("{}ms", f.time_ms));
                lines.push(format!(
                    "  成交: {t} {} {} {} @ {} (oid={}, fee {}, closedPnl {})",
                    f.coin,
                    if f.buy { "买" } else { "卖" },
                    f.sz,
                    f.px,
                    f.oid,
                    f.fee,
                    f.closed_pnl
                ));
            }
            lines.push("注: 手续费正值为支出；盈亏和资金费负值为损失/付出。另需考虑滑点".into());
            lines
        }
        (fills_res, fundings_res) => {
            let why = match (fills_res, fundings_res) {
                (Err(e), _) => format!("成交记录查询失败: {e}"),
                (_, Err(e)) => format!("资金费记录查询失败: {e}"),
                _ => unreachable!(),
            };
            vec![format!("核算: 不可完整核算（{why}）")]
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use httpmock::MockServer;
    use hypersdk::hypercore::PrivateKeySigner;
    use std::str::FromStr;
    use std::sync::atomic::{AtomicU64, Ordering};

    static DIR_SEQ: AtomicU64 = AtomicU64::new(0);

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hltrader-pair-{}-{}",
            std::process::id(),
            DIR_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn test_cfg(dir: &std::path::Path) -> Config {
        Config {
            account_address: "0x7F2120F612B78D579197762196B8A202AB654096".into(),
            secret_file: "/tmp/agent.key".into(),
            max_notional_usd: Decimal::new(100, 0),
            leg_fraction: Decimal::new(7, 1),
            unlock_anchors: Vec::new(),
            receipt_dir: dir.to_path_buf(),
        }
    }

    fn test_signer() -> PrivateKeySigner {
        PrivateKeySigner::from_str(
            "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        .unwrap()
    }

    // equity=1000，无持仓无挂单
    const ACCOUNT_EMPTY: &str = r#"{
        "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "0.0", "totalRawUsd": "1000.0", "totalMarginUsed": "0.0"},
        "crossMarginSummary": {"accountValue": "1000.0", "totalNtlPos": "0.0", "totalRawUsd": "1000.0", "totalMarginUsed": "0.0"},
        "crossMaintenanceMarginUsed": "0.0", "withdrawable": "1000.0", "time": 1760000000000,
        "assetPositions": []
    }"#;

    const ACCOUNT_POSITIONS: &str = r#"{
        "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "198.8", "totalRawUsd": "1000.0", "totalMarginUsed": "66.3"},
        "crossMarginSummary": {"accountValue": "1000.0", "totalNtlPos": "198.8", "totalRawUsd": "1000.0", "totalMarginUsed": "66.3"},
        "crossMaintenanceMarginUsed": "20.0", "withdrawable": "900.0", "time": 1760000000000,
        "assetPositions": [
            {"type": "oneWay", "position": {"coin": "HYPE", "szi": "2.49",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "40",
                "positionValue": "99.6", "unrealizedPnl": "1.0", "returnOnEquity": "0.01",
                "liquidationPx": null, "marginUsed": "33.2", "maxLeverage": 5,
                "cumFunding": {"allTime": "-1.0", "sinceOpen": "-1.0", "sinceChange": "0.0"}}},
            {"type": "oneWay", "position": {"coin": "BTC", "szi": "-0.00124",
                "leverage": {"type": "cross", "value": 3}, "entryPx": "80000",
                "positionValue": "99.2", "unrealizedPnl": "-0.5", "returnOnEquity": "-0.005",
                "liquidationPx": null, "marginUsed": "33.1", "maxLeverage": 40,
                "cumFunding": {"allTime": "0.5", "sinceOpen": "0.5", "sinceChange": "0.0"}}}
        ]
    }"#;

    // HYPE mid=40.05；BTC mid=80005
    const BBO_HYPE: &str = r#"{"coin": "HYPE", "time": 1760000000000, "snapshot": true,
        "levels": [[{"px": "40.0", "sz": "100", "n": 1}], [{"px": "40.1", "sz": "100", "n": 1}]]}"#;
    const BBO_BTC: &str = r#"{"coin": "BTC", "time": 1760000000000, "snapshot": true,
        "levels": [[{"px": "80000", "sz": "10", "n": 1}], [{"px": "80010", "sz": "10", "n": 1}]]}"#;

    const PAIR_META: &str = r#"{
        "universe": [
            {"szDecimals": 2, "name": "HYPE", "maxLeverage": 5, "onlyIsolated": false},
            {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "onlyIsolated": false}
        ],
        "collateralToken": 0
    }"#;

    const SPOT_META: &str = r#"{
        "universe": [],
        "tokens": [{
            "name": "USDC", "index": 0,
            "tokenId": "0x00000000000000000000000000000000",
            "szDecimals": 8, "weiDecimals": 8, "evmContract": null
        }]
    }"#;

    const PLACE_ERR: &str = r#"{"status": "ok", "response": {"type": "order",
        "data": {"statuses": [{"error": "position would exceed leverage limit"}]}}}"#;

    fn mock_pair_meta(server: &MockServer) {
        mock_pair_meta_margin(server, "cross");
    }

    fn mock_pair_meta_margin(server: &MockServer, mode: &'static str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(move |req| {
                let v: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                httpmock::HttpMockResponse::builder().status(200).body(serde_json::json!({
                    "user": v["user"], "coin": v["coin"], "leverage": {"type":mode, "value":3}
                }).to_string()).build()
            });
        });

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
            then.status(200).body(PAIR_META);
        });
    }

    fn account_with(hype: &str, btc: &str) -> String {
        let mut account: serde_json::Value = serde_json::from_str(ACCOUNT_POSITIONS).unwrap();
        account["assetPositions"][0]["position"]["szi"] = hype.into();
        account["assetPositions"][1]["position"]["szi"] = btc.into();
        account["assetPositions"]
            .as_array_mut()
            .unwrap()
            .retain(|p| {
                p["position"]["szi"]
                    .as_str()
                    .unwrap()
                    .parse::<Decimal>()
                    .unwrap()
                    != Decimal::ZERO
            });
        account.to_string()
    }

    fn mock_account_sequence(server: &MockServer, accounts: Vec<String>) {
        let next = std::sync::atomic::AtomicUsize::new(0);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
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
                .body_includes("frontendOpenOrders");
            then.status(200).body("[]");
        });
    }

    async fn close_sequence(accounts: Vec<String>) -> MockServer {
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        mock_account_sequence(&server, accounts);
        mock_bboes(&server);
        server
    }

    fn mock_account(server: &MockServer, body: &str) {
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
            then.status(200).body("[]");
        });
    }

    fn mock_bboes(server: &MockServer) {
        for (coin, body) in [(LONG_COIN, BBO_HYPE), (SHORT_COIN, BBO_BTC)] {
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/info")
                    .body_includes(r#""type":"l2Book""#)
                    .body_includes(coin);
                then.status(200).body(body);
            });
        }
    }

    /// 目标数量（equity=1000, max=100, frac=0.7 → 每腿目标 100 USD）
    const HYPE_QTY: &str = "2.49";
    const BTC_QTY: &str = "0.00124";
    const BTC_QTY_NEG: &str = "-0.00124";

    fn filled_body(oid: u64, qty: &str) -> String {
        format!(
            r#"{{"status": "ok", "response": {{"type": "order", "data": {{"statuses": [
                {{"filled": {{"totalSz": "{qty}", "avgPx": "40", "oid": {oid}}}}}]}}}}}}"#
        )
    }

    /// Hyperliquid 对未成交即终结的 IOC 的确认响应：totalSz=0, avgPx=0。
    fn zero_fill_body(oid: u64) -> String {
        format!(
            r#"{{"status": "ok", "response": {{"type": "order", "data": {{"statuses": [
                {{"filled": {{"totalSz": "0", "avgPx": "0", "oid": {oid}}}}}]}}}}}}"#
        )
    }

    /// mock 普通开仓腿（排除 reduce-only 补偿单，便于区分请求去向）。
    fn mock_place<'a>(server: &'a MockServer, asset: u64, body: &str) -> httpmock::Mock<'a> {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(format!(r#""a":{asset}"#))
                .body_excludes(r#""r":true"#);
            then.status(200).body(body);
        })
    }

    /// mock 开仓第二腿卖单（在 mock_place 基础上断言方向为卖）。
    fn mock_place_sell<'a>(server: &'a MockServer, asset: u64, body: &str) -> httpmock::Mock<'a> {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(format!(r#""a":{asset}"#))
                .body_includes(r#""b":false"#)
                .body_excludes(r#""r":true"#);
            then.status(200).body(body);
        })
    }

    /// 完整 open 场景 mock 集（空账户 + 盘口），返回 mock 服务器。
    async fn open_stage(_cfg: &Config) -> MockServer {
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        mock_account(&server, ACCOUNT_EMPTY);
        mock_bboes(&server);
        server
    }

    async fn open_sequence(accounts: Vec<String>) -> MockServer {
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        mock_account_sequence(&server, accounts);
        mock_bboes(&server);
        server
    }

    async fn run_open_on(server: &MockServer, cfg: &Config) -> PairReport {
        let adapter = Adapter::with_base_url(&server.base_url());
        run_open(&adapter, Some(&test_signer()), cfg, None, true)
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn open_happy_path() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_sequence(vec![
            ACCOUNT_EMPTY.into(),
            ACCOUNT_EMPTY.into(),
            ACCOUNT_POSITIONS.into(),
        ])
        .await;
        mock_place(&server, 0, &filled_body(11, HYPE_QTY));
        mock_place_sell(&server, 1, &filled_body(12, BTC_QTY));

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Ok, "lines={:?}", report.lines);
        let text = report.lines.join("\n");
        assert!(
            text.contains("名义(按成交均价): HYPE ") && text.contains("两腿偏差 "),
            "回执应显示两腿名义及偏差: {text}"
        );
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Open);
        assert_eq!(st.long_leg.confirmed_qty, Decimal::new(249, 2));
        assert_eq!(st.short_leg.confirmed_qty, Decimal::new(124, 5));
        assert!(st.unwinds.is_empty());
    }

    #[tokio::test]
    async fn open_leg1_rejected_sends_no_leg2() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        mock_place(&server, 0, PLACE_ERR);
        let leg2 = mock_place_sell(&server, 1, &filled_body(12, BTC_QTY));

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed);
        assert_eq!(leg2.calls(), 0, "第二腿不应被发送");
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert!(st.unwinds.is_empty(), "拒单不需要补偿");
    }

    #[tokio::test]
    async fn open_leg1_zero_fill_fails_known() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        mock_place(&server, 0, &zero_fill_body(11));
        let leg2 = mock_place_sell(&server, 1, &filled_body(12, BTC_QTY));
        let unwind_attempt = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(13, HYPE_QTY));
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed, "lines={:?}", report.lines);
        assert_eq!(leg2.calls(), 0, "第一腿零成交已终结，不应发送第二腿");
        assert_eq!(unwind_attempt.calls(), 0, "零成交无仓位可撤，不应补偿");
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_eq!(st.long_leg.status, LegStatus::Rejected);
        assert!(st.unwinds.is_empty());
        let text = report.lines.join("\n");
        assert!(text.contains("零成交"), "回执应说明零成交: {text}");
    }

    #[tokio::test]
    async fn open_leg1_permission_denied_fails_known() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        // HTTP 401：交易所明确拒绝、订单确定未提交，按拒单处理而非未知。
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(401).body("unauthorized");
        });
        let leg2 = mock_place_sell(&server, 1, &filled_body(12, BTC_QTY));

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed, "lines={:?}", report.lines);
        assert_eq!(leg2.calls(), 0, "权限拒绝时订单未提交，不应发送第二腿");
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_eq!(st.long_leg.status, LegStatus::Rejected);
        let text = report.lines.join("\n");
        assert!(text.contains("权限拒绝"), "回执应说明权限拒绝: {text}");
    }

    #[tokio::test]
    async fn open_leg1_partial_unwinds_reduce_only() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_sequence(vec![
            ACCOUNT_EMPTY.into(),
            ACCOUNT_EMPTY.into(),
            account_with("1", "0"),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_place(&server, 0, &filled_body(11, "1.0"));
        let unwind = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(13, "1.0"));
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(
            report.outcome,
            Outcome::Failed,
            "部分成交撤回后原开仓仍失败"
        );
        // 补偿单：a=0 的第二个请求。上面两个 mock 顺序匹配：无法区分方向，
        // 但 unwind 记录会留下证据。
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_eq!(st.unwinds.len(), 1);
        assert_eq!(st.unwinds[0].side, "sell", "平多 = 卖");
        assert!(unwind.calls() >= 1);
    }

    #[tokio::test]
    async fn open_leg1_unknown_stops_no_unwind() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#);
            then.status(500).body("internal error");
        });
        let unwind_attempt = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(PLACE_ERR);
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Unknown);
        assert_eq!(unwind_attempt.calls(), 0, "结果未知不得盲目反向");
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Unknown);
        assert!(st.unwinds.is_empty());
    }

    #[tokio::test]
    async fn open_leg2_rejected_unwinds_leg1() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_sequence(vec![
            ACCOUNT_EMPTY.into(),
            ACCOUNT_EMPTY.into(),
            account_with(HYPE_QTY, "0"),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_place(&server, 0, &filled_body(11, HYPE_QTY));
        mock_place_sell(&server, 1, PLACE_ERR);
        let unwind = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(13, HYPE_QTY));
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed, "lines={:?}", report.lines);
        assert_eq!(unwind.calls(), 1);
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_eq!(st.unwinds.len(), 1);
    }

    #[tokio::test]
    async fn open_leg2_zero_fill_unwinds_leg1() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_sequence(vec![
            ACCOUNT_EMPTY.into(),
            ACCOUNT_EMPTY.into(),
            account_with(HYPE_QTY, "0"),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_place(&server, 0, &filled_body(11, HYPE_QTY));
        mock_place_sell(&server, 1, &zero_fill_body(12));
        let unwind = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#)
                .body_includes(format!(r#""s":"{HYPE_QTY}""#));
            then.status(200).body(filled_body(13, HYPE_QTY));
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed, "lines={:?}", report.lines);
        assert_eq!(unwind.calls(), 1, "第二腿零成交应只减仓撤回腿1");
        let text = report.lines.join("\n");
        assert!(
            text.contains("原开仓记为失败"),
            "全部撤回后原开仓仍记为失败: {text}"
        );
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_eq!(st.short_leg.status, LegStatus::Rejected);
        assert_eq!(st.unwinds.len(), 1);
        assert_eq!(st.unwinds[0].side, "sell", "平多 = 卖");
        assert_eq!(st.unwinds[0].qty, Decimal::new(249, 2));
        assert_eq!(st.unwinds[0].confirmed_qty, Decimal::new(249, 2));
    }

    #[tokio::test]
    async fn open_leg2_unknown_keeps_leg1_honest() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        mock_place(&server, 0, &filled_body(11, HYPE_QTY));
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":false"#);
            then.status(500).body("internal error");
        });
        let unwind_attempt = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(PLACE_ERR);
        });

        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Unknown);
        assert_eq!(unwind_attempt.calls(), 0, "腿2未知时不得动腿1");
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.phase, Phase::Unknown);
        assert_eq!(st.long_leg.status, LegStatus::Filled);
        assert_eq!(st.long_leg.confirmed_qty, Decimal::new(249, 2));
    }

    #[tokio::test]
    async fn open_refused_when_existing_position() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        // 账户里有 HYPE 多头
        let with_pos = ACCOUNT_POSITIONS.replace(r#""szi": "-0.00124""#, r#""szi": "0.00124""#);
        mock_account(&server, &with_pos);
        mock_bboes(&server);

        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_open(&adapter, Some(&test_signer()), &cfg, None, true)
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::Refused(_)), "got {err}");
    }

    #[tokio::test]
    async fn open_refused_when_unfinished_state() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        // 预置一个未完结动作
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut st = seed_state(&cfg, Phase::Open);
        st.action_id = "previous-open".into();
        store.save_atomic(&st).unwrap();

        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_open(&adapter, Some(&test_signer()), &cfg, None, true)
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::Refused(_)), "got {err}");
    }

    #[tokio::test]
    async fn open_archives_old_finished_state() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = open_stage(&cfg).await;
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Failed)).unwrap();

        mock_place(&server, 0, PLACE_ERR);
        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Failed);

        // 旧状态被归档（审计保留），当前状态是新动作
        let archived = fs::read_dir(&cfg.receipt_dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with("pair-state-") && n.ends_with(".json"))
            .count();
        assert_eq!(archived, 1, "旧 closed/failed 状态应归档为一个文件");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Failed);
        assert_ne!(st.action_id, "previous");
    }

    #[test]
    fn qty_floor_and_cap_loop() {
        // 截断：100/40.05 = 2.4968… → 2.49
        let q = target_qty(
            Decimal::new(100, 0),
            Decimal::new(4005, 2),
            Decimal::new(40102, 3),
            2,
            LONG_COIN,
        )
        .unwrap();
        assert_eq!(q, Decimal::new(249, 2));
        assert!(q * Decimal::new(40102, 3) <= Decimal::new(100, 0));

        // 保护价抬升导致超限 → 继续减量到名义 ≤ 目标
        let q2 = target_qty(
            Decimal::new(100, 0),
            Decimal::new(4005, 2),
            Decimal::new(41000, 3),
            2,
            LONG_COIN,
        )
        .unwrap();
        assert!(q2 * Decimal::new(41000, 3) <= Decimal::new(100, 0));

        // 目标极小时截断后为零 → 拒绝（0.1/40.05 = 0.00249… → 0.00）
        assert!(
            target_qty(
                Decimal::new(1, 1),
                Decimal::new(4005, 2),
                Decimal::new(40102, 3),
                2,
                LONG_COIN
            )
            .is_err()
        );
    }

    #[test]
    fn protect_price_rules() {
        let m = hype_market();
        let bbo = BboView {
            bid: Some(Decimal::new(40, 0)),
            ask: Some(Decimal::new(401, 1)),
            time_ms: 0,
        };
        let p = protect_price(&m, true, &bbo).unwrap();
        assert_eq!(p, Decimal::new(40102, 3), "买 = ask + 2 tick(0.001)");
        let p = protect_price(&m, false, &bbo).unwrap();
        assert_eq!(p, Decimal::new(39998, 3), "卖 = bid − 2 tick");
        // 缺盘口 → 拒绝
        let empty = BboView {
            bid: None,
            ask: None,
            time_ms: 0,
        };
        assert!(protect_price(&m, true, &empty).is_err());
    }

    #[test]
    fn leg_notional_min_logic() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        // min(参数 500, 单笔上限 100, 0.7×1000=700) = 100
        let n = leg_notional(Some(Decimal::new(500, 0)), &cfg, Decimal::new(1000, 0)).unwrap();
        assert_eq!(n, Decimal::new(100, 0));
        // 参数缺失 → min(700, 100) = 100
        let n = leg_notional(None, &cfg, Decimal::new(1000, 0)).unwrap();
        assert_eq!(n, Decimal::new(100, 0));
        // 权益非正 → 拒绝
        assert!(leg_notional(None, &cfg, Decimal::ZERO).is_err());
    }

    #[test]
    fn store_mutex_blocks_second_holder() {
        let dir = tempdir();
        let store = PairStore::new(dir);
        let _g = store.acquire().unwrap();
        assert!(matches!(store.acquire(), Err(PairError::Locked(_))));
    }

    fn hype_market() -> PerpMarket {
        use hypersdk::hypercore::{PriceTick, SpotToken};
        PerpMarket {
            name: LONG_COIN.into(),
            index: 0,
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

    fn seed_state(cfg: &Config, phase: Phase) -> PairState {
        let now_ms = 1760000000000u64;
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
            long_leg: LegState {
                coin: LONG_COIN.into(),
                side: "buy".into(),
                target_qty: Decimal::new(249, 2),
                protect_px: Decimal::new(40102, 3),
                cloid: "0x11111111111111111111111111111111".into(),
                status: LegStatus::Filled,
                confirmed_qty: Decimal::new(249, 2),
                avg_px: Some(Decimal::new(40, 0)),
                oid: Some(11),
            },
            short_leg: LegState {
                coin: SHORT_COIN.into(),
                side: "sell".into(),
                target_qty: Decimal::new(124, 5),
                protect_px: Decimal::new(79998, 0),
                cloid: "0x22222222222222222222222222222222".into(),
                status: LegStatus::Filled,
                confirmed_qty: Decimal::new(124, 5),
                avg_px: Some(Decimal::new(80000, 0)),
                oid: Some(12),
            },
            start_positions: Vec::new(),
            unwinds: Vec::new(),
            detail: None,
            closed_at_utc: None,
        }
    }

    // ---------- 平仓场景 ----------

    async fn close_stage_async(cfg: &Config) -> MockServer {
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        mock_account(&server, ACCOUNT_POSITIONS);
        mock_bboes(&server);
        let _ = cfg;
        server
    }

    const CLOSE_FILLS: &str = r#"[
        {"coin":"HYPE","px":"40","sz":"2.49","side":"B","time":1760000000000,"startPosition":"0","dir":"Open Long","closedPnl":"0","hash":"0x1111111111111111111111111111111111111111111111111111111111111111","oid":11,"crossed":true,"fee":"0","tid":11,"cloid":null,"feeToken":"USDC"},
        {"coin":"BTC","px":"80000","sz":"0.00124","side":"A","time":1760000000000,"startPosition":"0","dir":"Open Short","closedPnl":"0","hash":"0x1111111111111111111111111111111111111111111111111111111111111111","oid":12,"crossed":true,"fee":"0","tid":12,"cloid":null,"feeToken":"USDC"},

        {"coin": "HYPE", "px": "42", "sz": "2.49", "side": "A", "time": 1760000100000,
         "startPosition": "2.49", "dir": "Close Long", "closedPnl": "4.98",
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "oid": 21, "crossed": true, "fee": "-0.09", "tid": 2, "cloid": null, "feeToken": "USDC"},
        {"coin": "BTC", "px": "79000", "sz": "0.00124", "side": "B", "time": 1760000100001,
         "startPosition": "-0.00124", "dir": "Close Short", "closedPnl": "-1.24",
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "oid": 22, "crossed": true, "fee": "-0.09", "tid": 3, "cloid": null, "feeToken": "USDC"}
    ]"#;

    const CLOSE_FUNDING: &str = r#"[
        {"delta": {"type": "funding", "coin": "HYPE", "usdc": "-1.0", "szi": "2.49",
                   "fundingRate": "0.0001", "nSamples": null},
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "time": 1760000000000},
        {"delta": {"type": "funding", "coin": "BTC", "usdc": "0.4", "szi": "-0.00124",
                   "fundingRate": "0.0001", "nSamples": null},
         "hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
         "time": 1760000000000}
    ]"#;

    fn mock_close_accounting(server: &MockServer) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFillsByTime");
            then.status(200).body(CLOSE_FILLS);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body(CLOSE_FUNDING);
        });
    }

    #[tokio::test]
    async fn close_happy_path_with_accounting() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_sequence(vec![
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            account_with("0", BTC_QTY_NEG),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_close_accounting(&server);
        // 平多 HYPE：a=0, b=false, r=true；平空 BTC：a=1, b=true, r=true
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(21, HYPE_QTY));
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":true"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_eq!(report.outcome, Outcome::Ok, "lines={:?}", report.lines);
        let text = report.lines.join("\n");
        assert!(text.contains("核算"), "应输出核算块: {text}");
        assert!(text.contains("-0.18"), "手续费合计: {text}");
        assert!(text.contains("3.74"), "实现盈亏合计 4.98-1.24: {text}");
        assert!(text.contains("-0.6"), "资金费净收付 -1.0+0.4: {text}");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Closed);
        assert_ne!(st.action_id, "previous");
        let previous =
            std::fs::read_to_string(cfg.receipt_dir.join("pair-state-previous-open.json")).unwrap();
        assert!(previous.contains("0x11111111111111111111111111111111"));
    }

    #[tokio::test]
    async fn close_refused_while_state_unresolved() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut unknown = seed_state(&cfg, Phase::Unknown);
        unknown.long_leg.status = LegStatus::Unknown;
        store.save_atomic(&unknown).unwrap();
        let server = close_stage_async(&cfg).await;

        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::Unresolved(_)), "got {err}");
    }

    #[tokio::test]
    async fn close_recovers_when_positions_closed_externally() {
        for phase in [
            Phase::Open,
            Phase::Incomplete,
            Phase::Failed,
            Phase::Unknown,
        ] {
            assert_external_close_is_repeatable(phase).await;
        }
    }

    async fn assert_external_close_is_repeatable(phase: Phase) {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        let original = seed_state(&cfg, phase);
        store.save_atomic(&original).unwrap();
        let before = fs::read(store.state_path()).unwrap();
        // 交易所已无任何持仓与挂单：open 状态下的 pair 被外部全部平掉。
        let server = close_sequence(vec![ACCOUNT_EMPTY.into()]).await;
        let writes = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let preview = run_close(&adapter, None, &cfg, false).await.unwrap();
        assert_eq!(preview.outcome, Outcome::Ok);
        assert!(preview.lines.join("\n").contains("不可完整核算"));
        assert_eq!(fs::read(store.state_path()).unwrap(), before);
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_eq!(report.outcome, Outcome::Ok, "lines={:?}", report.lines);
        assert_eq!(writes.calls(), 0, "外部已平仓，不应再发送任何订单");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Closed);
        assert!(st.closed_at_utc.is_some());
        assert!(!st.audit_complete);
        let closure = st.external_closure.as_ref().unwrap();
        assert_eq!(
            closure.prior_expected_positions,
            vec![
                (LONG_COIN.into(), original.long_leg.confirmed_qty),
                (SHORT_COIN.into(), -original.short_leg.confirmed_qty)
            ]
        );
        assert_eq!(
            serde_json::to_value(&st.long_leg).unwrap(),
            serde_json::to_value(&original.long_leg).unwrap()
        );
        assert_eq!(
            serde_json::to_value(&st.short_leg).unwrap(),
            serde_json::to_value(&original.short_leg).unwrap()
        );
        assert!(
            st.detail
                .as_deref()
                .unwrap_or_default()
                .contains("外部归零")
        );
        let saved = fs::read(store.state_path()).unwrap();
        for execute in [false, true, false] {
            let signer = test_signer();
            let again = run_close(&adapter, execute.then_some(&signer), &cfg, execute)
                .await
                .unwrap();
            assert_eq!(again.outcome, Outcome::Ok);
            assert!(again.lines.join("\n").contains("不可完整核算"));
            assert_eq!(fs::read(store.state_path()).unwrap(), saved);
        }
        assert_eq!(writes.calls(), 0);
    }

    #[tokio::test]
    async fn external_close_rejects_reappearing_positions_even_if_old_size_matches() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_sequence(vec![ACCOUNT_EMPTY.into(), ACCOUNT_POSITIONS.into()]).await;
        let writes = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        let saved = fs::read(store.state_path()).unwrap();
        for execute in [false, true] {
            let signer = test_signer();
            let err = run_close(&adapter, execute.then_some(&signer), &cfg, execute)
                .await
                .unwrap_err();
            assert!(matches!(err, PairError::Unresolved(_)));
            assert_eq!(fs::read(store.state_path()).unwrap(), saved);
        }
        assert_eq!(writes.calls(), 0);
    }

    #[tokio::test]
    async fn external_close_does_not_bypass_open_orders_or_unknown_legs() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut st = seed_state(&cfg, Phase::Open);
        record_external_closure(&mut st);
        store.save_atomic(&st).unwrap();
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(ACCOUNT_EMPTY);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body(r#"[{"coin":"HYPE","side":"B","limitPx":"40","sz":"1","oid":99,"timestamp":1760000000000,"origSz":"1","reduceOnly":false,"isPositionTpsl":false,"isTrigger":false,"orderType":"Limit","triggerCondition":"N/A","triggerPx":"0","children":[],"tif":"Gtc"}]"#);
        });
        let writes = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap_err();
        assert!(
            matches!(err, PairError::Unresolved(ref m) if m.contains("挂单")),
            "{err}"
        );
        assert_eq!(writes.calls(), 0);

        st.long_leg.status = LegStatus::Unknown;
        store.save_atomic(&st).unwrap();
        let empty = close_sequence(vec![ACCOUNT_EMPTY.into()]).await;
        empty.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("orderStatus");
            then.status(200).body(r#"{"status":"unknownOid"}"#);
        });
        let adapter = Adapter::with_base_url(&empty.base_url());
        let err = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap_err();
        assert!(
            matches!(err, PairError::Unresolved(ref m) if m.contains("未找到")),
            "{err}"
        );
    }

    #[tokio::test]
    async fn legacy_external_close_migrates_only_explicit_external_marker() {
        for external in [true, false] {
            let dir = tempdir();
            let cfg = test_cfg(&dir);
            let store = PairStore::new(cfg.receipt_dir.clone());
            let mut old = seed_state(&cfg, Phase::Closed);
            old.closed_at_utc = Some("2026-10-10T01:00:00Z".into());
            old.detail = external.then(|| EXTERNAL_CLOSE_DETAIL.into());
            let mut json = serde_json::to_value(&old).unwrap();
            json.as_object_mut().unwrap().remove("external_closure");
            let bytes = serde_json::to_vec(&json).unwrap();
            fs::write(store.state_path(), &bytes).unwrap();
            let server = close_sequence(vec![ACCOUNT_EMPTY.into()]).await;
            let adapter = Adapter::with_base_url(&server.base_url());
            let preview = run_close(&adapter, None, &cfg, false).await;
            assert_eq!(fs::read(store.state_path()).unwrap(), bytes);
            let execute = run_close(&adapter, Some(&test_signer()), &cfg, true).await;
            if external {
                assert_eq!(preview.unwrap().outcome, Outcome::Ok);
                assert_eq!(execute.unwrap().outcome, Outcome::Ok);
                let migrated = store.load().unwrap().unwrap();
                assert!(migrated.external_closure.is_some());
                assert!(!migrated.audit_complete);
                assert_eq!(migrated.closed_at_utc, old.closed_at_utc);
                assert_eq!(
                    run_close(&adapter, None, &cfg, false)
                        .await
                        .unwrap()
                        .outcome,
                    Outcome::Ok
                );
            } else {
                assert!(matches!(preview, Err(PairError::Unresolved(_))));
                assert!(matches!(execute, Err(PairError::Unresolved(_))));
                assert_eq!(fs::read(store.state_path()).unwrap(), bytes);
            }
        }
    }

    #[tokio::test]
    async fn close_unresolved_when_partial_external_position() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        // HYPE 仍有 1.0 残仓：部分持仓不一致不能按外部平仓处理，仍阻止写操作。
        let server = close_sequence(vec![account_with("1.0", BTC_QTY_NEG)]).await;
        let writes = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::Unresolved(_)), "got {err}");
        assert_eq!(writes.calls(), 0);
        assert_eq!(store.load().unwrap().unwrap().phase, Phase::Open);
    }

    #[tokio::test]
    async fn close_refused_on_reverse_position() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let server = MockServer::start_async().await;
        mock_pair_meta(&server);
        // HYPE 空头 = 反向持仓
        let reverse = ACCOUNT_POSITIONS.replace(r#""szi": "2.49""#, r#""szi": "-2.49""#);
        assert!(reverse.contains("-2.49"));
        mock_account(&server, &reverse);

        let adapter = Adapter::with_base_url(&server.base_url());
        let err = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap_err();
        assert!(
            matches!(err, PairError::Refused(ref m) if m.contains("反向")),
            "got {err}"
        );
    }

    #[tokio::test]
    async fn close_leg_rejected_continues_other_leg() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_sequence(vec![
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            account_with(HYPE_QTY, "0"),
        ])
        .await;
        mock_close_accounting(&server);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#);
            then.status(200).body(PLACE_ERR);
        });
        let btc_close = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_eq!(report.outcome, Outcome::Failed);
        assert_eq!(btc_close.calls(), 1, "一腿被拒后另一腿仍应继续");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Incomplete);
    }

    #[tokio::test]
    async fn close_leg_zero_fill_continues_other() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_sequence(vec![
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            account_with(HYPE_QTY, "0"),
        ])
        .await;
        mock_close_accounting(&server);
        // 腿1 HYPE 平多零成交（已知结局，按拒绝处理）；腿2 BTC 平空正常成交。
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(zero_fill_body(21));
        });
        let btc_close = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":true"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_eq!(report.outcome, Outcome::Failed, "lines={:?}", report.lines);
        assert_eq!(btc_close.calls(), 1, "一腿零成交后另一腿仍应继续");
        let text = report.lines.join("\n");
        assert!(text.contains("零成交"), "回执应说明零成交: {text}");
        assert!(text.contains("phase=incomplete"), "回执应提示残仓: {text}");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Incomplete);
        assert_eq!(st.long_leg.status, LegStatus::Rejected);
        assert_eq!(
            st.detail.as_deref().unwrap_or_default(),
            "已知残仓，可再次显式 close"
        );
    }

    #[tokio::test]
    async fn close_unknown_stops_other_leg() {
        let dir = tempdir();
        let cfg = test_cfg(&dir);
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_stage_async(&cfg).await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#);
            then.status(500).body("internal error");
        });
        let btc_close = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });

        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_eq!(report.outcome, Outcome::Unknown);
        assert_eq!(btc_close.calls(), 0, "结果未知必须停止后续写操作");
        let st = store.load().unwrap().unwrap();
        assert_eq!(st.phase, Phase::Unknown);
    }
    #[tokio::test]
    async fn review_unknown_unwind_must_stop_next_write() {
        let cfg = test_cfg(&tempdir());
        let server = open_sequence(vec![
            ACCOUNT_EMPTY.into(),
            ACCOUNT_EMPTY.into(),
            account_with(HYPE_QTY, "-0.0005"),
        ])
        .await;
        mock_place(&server, 0, &filled_body(11, HYPE_QTY));
        mock_place_sell(&server, 1, &filled_body(12, "0.0005"));
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.status(500).body("unknown write result");
        });
        let next_write = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":true"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(14, "0.0005"));
        });
        let report = run_open_on(&server, &cfg).await;
        assert_eq!(report.outcome, Outcome::Unknown);
        assert_eq!(
            next_write.calls(),
            0,
            "must stop after an unknown compensation"
        );
    }

    #[tokio::test]
    async fn review_known_residual_must_allow_close_preview() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        store
            .save_atomic(&seed_state(&cfg, Phase::Incomplete))
            .unwrap();
        let server = close_stage_async(&cfg).await;
        let adapter = Adapter::with_base_url(&server.base_url());
        let result = run_close(&adapter, None, &cfg, false).await;
        assert!(
            result.is_ok(),
            "known residual cannot be closed: {result:?}"
        );
    }

    #[tokio::test]
    async fn review_close_must_verify_actual_zero_positions() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        // The account continues to report nonzero positions even after filled responses.
        let server = close_sequence(vec![
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            account_with("0", BTC_QTY_NEG),
            ACCOUNT_POSITIONS.into(),
        ])
        .await;
        mock_close_accounting(&server);
        for (asset, buy, qty) in [(0u64, false, HYPE_QTY), (1, true, BTC_QTY)] {
            server.mock(|when, then| {
                when.method(httpmock::Method::POST)
                    .path("/exchange")
                    .body_includes(format!(r#""a":{asset}"#))
                    .body_includes(if buy { r#""b":true"# } else { r#""b":false"# })
                    .body_includes(r#""r":true"#);
                then.status(200).body(filled_body(20 + asset, qty));
            });
        }
        let adapter = Adapter::with_base_url(&server.base_url());
        let report = run_close(&adapter, Some(&test_signer()), &cfg, true)
            .await
            .unwrap();
        assert_ne!(
            report.outcome,
            Outcome::Ok,
            "must not claim flat while account still has positions"
        );
    }

    #[tokio::test]
    async fn persistence_failure_after_first_fill_stops_second_order() {
        let cfg = test_cfg(&tempdir());
        let server = open_stage(&cfg).await;
        let tmp = cfg
            .receipt_dir
            .join(format!("pair-state.tmp.{}", std::process::id()));
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#);
            then.respond_with(move |_| {
                fs::create_dir(&tmp).unwrap(); // fail the next atomic state write
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(filled_body(11, HYPE_QTY))
                    .build()
            });
        });
        let second = mock_place_sell(&server, 1, &filled_body(12, BTC_QTY));
        let adapter = Adapter::with_base_url(&server.base_url());
        let error = run_open(&adapter, Some(&test_signer()), &cfg, None, true)
            .await
            .unwrap_err();
        assert!(matches!(error, PairError::Unresolved(_)));
        assert!(error.to_string().contains(HYPE_QTY));
        assert_eq!(second.calls(), 0);
        let st = PairStore::new(cfg.receipt_dir.clone())
            .load()
            .unwrap()
            .unwrap();
        assert_eq!(st.long_leg.status, LegStatus::Pending); // recover by cloid, never resend
        assert_eq!(st.short_leg.status, LegStatus::Prepared);
    }

    #[tokio::test]
    async fn execute_locks_before_preflight_or_archiving() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Failed)).unwrap();
        let before = fs::read(store.state_path()).unwrap();
        let guard = store.acquire().unwrap();
        let adapter = Adapter::with_base_url("http://127.0.0.1:9");
        assert!(matches!(
            run_open(&adapter, Some(&test_signer()), &cfg, None, true).await,
            Err(PairError::Locked(_))
        ));
        assert_eq!(before, fs::read(store.state_path()).unwrap());
        drop(guard);
        assert!(store.acquire().is_ok()); // persistent lock file does not block recovery
    }

    fn mock_terminal_hype(server: &MockServer, status: &str, side: &str) {
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info").body_includes("orderStatus");
            then.status(200).body(serde_json::json!({
                "status":"order", "order": {"status":status, "statusTimestamp":1760000000000u64,
                    "order": {"timestamp":1760000000000u64,"coin":"HYPE","side":side,"limitPx":"40",
                        "sz":"0","origSz":HYPE_QTY,"oid":11,"orderType":"Limit","tif":"Ioc","reduceOnly":false,"cloid":"0x11111111111111111111111111111111"}}
            }).to_string());
        });
    }

    #[tokio::test]
    async fn unknown_fill_is_reconciled_without_writing_during_preview() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut state = seed_state(&cfg, Phase::Unknown);
        state.long_leg.status = LegStatus::Unknown;
        state.long_leg.confirmed_qty = Decimal::ZERO;
        state.long_leg.oid = None;
        store.save_atomic(&state).unwrap();
        let before = fs::read(store.state_path()).unwrap();
        let server = close_stage_async(&cfg).await;
        mock_terminal_hype(&server, "filled", "B");
        mock_close_accounting(&server);
        let write = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        assert_eq!(
            run_close(&adapter, None, &cfg, false)
                .await
                .unwrap()
                .outcome,
            Outcome::Ok
        );
        assert_eq!(write.calls(), 0);
        assert_eq!(before, fs::read(store.state_path()).unwrap());
    }

    #[tokio::test]
    async fn interrupted_closing_unknown_oid_cannot_be_resent() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut state = seed_state(&cfg, Phase::Closing);
        state.action = "close".into();
        state.start_positions = vec![
            (LONG_COIN.into(), Decimal::new(249, 2)),
            (SHORT_COIN.into(), -Decimal::new(124, 5)),
        ];
        state.long_leg.side = "sell".into();
        state.long_leg.status = LegStatus::Pending;
        state.long_leg.confirmed_qty = Decimal::ZERO;
        state.short_leg.side = "buy".into();
        state.short_leg.status = LegStatus::Prepared;
        state.short_leg.confirmed_qty = Decimal::ZERO;
        store.save_atomic(&state).unwrap();
        let server = close_stage_async(&cfg).await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("orderStatus");
            then.status(200).body(r#"{"status":"unknownOid"}"#);
        });
        let writes = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        assert!(matches!(
            run_close(&adapter, Some(&test_signer()), &cfg, true).await,
            Err(PairError::Unresolved(_))
        ));
        assert_eq!(writes.calls(), 0);
        assert_eq!(
            store.load().unwrap().unwrap().long_leg.cloid,
            state.long_leg.cloid
        );
    }

    #[tokio::test]
    async fn known_residual_can_be_closed_on_next_explicit_execute() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        store.save_atomic(&seed_state(&cfg, Phase::Open)).unwrap();
        let server = close_sequence(vec![
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            ACCOUNT_POSITIONS.into(),
            account_with(HYPE_QTY, "0"),
            account_with(HYPE_QTY, "0"),
            account_with(HYPE_QTY, "0"),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_close_accounting(&server);
        let calls = std::sync::atomic::AtomicUsize::new(0);
        let hype = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#)
                .body_includes(r#""b":false"#)
                .body_includes(r#""r":true"#);
            then.respond_with(move |_| {
                let body = if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                    PLACE_ERR.into()
                } else {
                    filled_body(21, HYPE_QTY)
                };
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(body)
                    .build()
            });
        });
        let btc = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":true"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        assert_eq!(
            run_close(&adapter, Some(&test_signer()), &cfg, true)
                .await
                .unwrap()
                .outcome,
            Outcome::Failed
        );
        let first = store.load().unwrap().unwrap();
        assert_eq!(first.phase, Phase::Incomplete);
        assert_eq!(
            run_close(&adapter, Some(&test_signer()), &cfg, true)
                .await
                .unwrap()
                .outcome,
            Outcome::Ok
        );
        let second = store.load().unwrap().unwrap();
        assert_ne!(first.action_id, second.action_id);
        assert_eq!(hype.calls(), 2);
        assert_eq!(btc.calls(), 1);
        assert_ne!(first.long_leg.cloid, second.long_leg.cloid);
        assert!(
            cfg.receipt_dir
                .join(format!("pair-state-{}-incomplete.json", first.action_id))
                .exists()
        );
    }

    #[tokio::test]
    async fn state_account_mismatch_stops_before_writes() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut state = seed_state(&cfg, Phase::Open);
        state.account = "0x1111111111111111111111111111111111111111".into();
        store.save_atomic(&state).unwrap();
        let adapter = Adapter::with_base_url("http://127.0.0.1:9");
        assert!(matches!(
            run_close(&adapter, Some(&test_signer()), &cfg, true).await,
            Err(PairError::Refused(_))
        ));
    }
    #[tokio::test]
    async fn recovered_closing_does_not_resend_already_filled_leg() {
        let cfg = test_cfg(&tempdir());
        let store = PairStore::new(cfg.receipt_dir.clone());
        let mut state = seed_state(&cfg, Phase::Closing);
        state.action = "close".into();
        state.audit_complete = false;
        state.start_positions = vec![
            (LONG_COIN.into(), Decimal::new(249, 2)),
            (SHORT_COIN.into(), -Decimal::new(124, 5)),
        ];
        state.long_leg.side = "sell".into();
        state.long_leg.status = LegStatus::Pending;
        state.long_leg.confirmed_qty = Decimal::ZERO;
        state.long_leg.oid = None;
        state.short_leg.side = "buy".into();
        state.short_leg.status = LegStatus::Prepared;
        state.short_leg.confirmed_qty = Decimal::ZERO;
        state.short_leg.oid = None;
        store.save_atomic(&state).unwrap();
        let server = close_sequence(vec![
            account_with("0", BTC_QTY_NEG),
            account_with("0", BTC_QTY_NEG),
            ACCOUNT_EMPTY.into(),
        ])
        .await;
        mock_terminal_hype(&server, "filled", "A");
        let mut fill: serde_json::Value =
            serde_json::from_str::<serde_json::Value>(CLOSE_FILLS).unwrap()[2].clone();
        fill["oid"] = 11u64.into();
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFillsByTime");
            then.status(200).json_body_obj(&vec![fill]);
        });
        let hype = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":0"#);
            then.status(500);
        });
        let btc = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes(r#""a":1"#)
                .body_includes(r#""b":true"#)
                .body_includes(r#""r":true"#);
            then.status(200).body(filled_body(22, BTC_QTY));
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        assert_eq!(
            run_close(&adapter, Some(&test_signer()), &cfg, true)
                .await
                .unwrap()
                .outcome,
            Outcome::Ok
        );
        assert_eq!(hype.calls(), 0);
        assert_eq!(btc.calls(), 1);
    }

    #[tokio::test]
    async fn accounting_detects_missing_partial_fills() {
        let cfg = test_cfg(&tempdir());
        let state = seed_state(&cfg, Phase::Open);
        let server = MockServer::start_async().await;
        let mut fills: serde_json::Value = serde_json::from_str(CLOSE_FILLS).unwrap();
        fills[0]["sz"] = "1.0".into();
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFillsByTime");
            then.status(200).json_body(fills);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding");
            then.status(200).body("[]");
        });
        let text = close_accounting(
            &Adapter::with_base_url(&server.base_url()),
            cfg.account_address().unwrap(),
            &state,
            false,
        )
        .await
        .join("\n");
        assert!(text.contains("不可完整核算"));
    }

    #[test]
    fn nonzero_quantity_below_minimum_notional_is_rejected() {
        assert!(
            target_qty(
                Decimal::new(9, 0),
                Decimal::new(40, 0),
                Decimal::new(40, 0),
                2,
                LONG_COIN
            )
            .is_err()
        );
    }

    #[tokio::test]
    async fn isolated_margin_refuses_pair_plan() {
        let cfg = test_cfg(&tempdir());
        let server = MockServer::start_async().await;
        mock_pair_meta_margin(&server, "isolated");
        let adapter = Adapter::with_base_url(&server.base_url());
        assert!(matches!(
            plan_open(&adapter, &cfg, Decimal::new(1000, 0), None).await,
            Err(PairError::Refused(_))
        ));
    }

    #[tokio::test]
    async fn only_isolated_market_accepted_with_warning() {
        let cfg = test_cfg(&tempdir());
        let server = MockServer::start_async().await;
        // 元数据声明 HYPE onlyIsolated（交易所层面禁止全仓，如测试网 HYPE strictIsolated）；
        // 在序列化副本上改字段，不动 PAIR_META 常量本身。
        let mut meta: serde_json::Value = serde_json::from_str(PAIR_META).unwrap();
        meta["universe"][0]["onlyIsolated"] = true.into();
        let meta = meta.to_string();
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(move |req| {
                let v: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                httpmock::HttpMockResponse::builder().status(200).body(serde_json::json!({
                    "user": v["user"], "coin": v["coin"], "leverage": {"type":"cross", "value":3}
                }).to_string()).build()
            });
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"spotMeta""#);
            then.status(200).body(SPOT_META);
        });
        let meta_body = meta.clone();
        server.mock(move |when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"meta""#);
            then.status(200).body(meta_body);
        });
        mock_bboes(&server);

        let adapter = Adapter::with_base_url(&server.base_url());
        let (plan, _, _) = plan_open(&adapter, &cfg, Decimal::new(1000, 0), None)
            .await
            .unwrap();
        assert!(
            plan.warnings
                .iter()
                .any(|w| w.contains(LONG_COIN) && w.contains("isolated")),
            "warnings={:?}",
            plan.warnings
        );
    }
}
