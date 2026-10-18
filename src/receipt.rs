//! 本地订单意图/回执（MVP_PLAN §4）。
//!
//! - 执行写请求前，把账户、订单意图和 cloid 原子写入回执文件；
//!   文件权限 0600，运行目录不进 Git。
//! - 回执不包含私钥、签名或可重放的请求正文。
//! - 相同 cloid 不自动再次提交：锁文件（create_new，跨进程互斥）保证
//!   最多一个进程能为同一 cloid 创建意图记录。此机制不能替代交易所状态核对。

use std::fmt;
use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiptState {
    /// 意图已记录，交易所结果未知。
    Submitted,
    /// 交易所确认挂单（返回 oid）。
    Resting,
    /// 订单已成交（数量与价格只来自交易所响应）。
    Filled,
    /// 交易所明确拒绝。
    Rejected,
    /// 超时/中断/未知格式，结果未知，需人工核对。
    Unknown,
}

impl fmt::Display for ReceiptState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            ReceiptState::Submitted => "submitted",
            ReceiptState::Resting => "resting",
            ReceiptState::Filled => "filled",
            ReceiptState::Rejected => "rejected",
            ReceiptState::Unknown => "unknown",
        };
        f.write_str(s)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Receipt {
    pub version: u8,
    pub network: String,
    pub account: String,
    pub coin: String,
    pub side: String,
    pub price: String,
    pub size: String,
    pub cloid: String,
    pub created_at_utc: String,
    pub state: ReceiptState,
    /// 交易所分配的真实订单 ID；只有交易所响应/查询能给出，绝不虚构。
    pub oid: Option<u64>,
    /// 人类可读的结果说明（拒单原因、成交摘要等）。
    pub detail: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum ReceiptError {
    #[error(
        "相同 cloid 已有回执记录，拒绝重复提交。请先核对: hl-trader status --address <账户地址> --cloid {0}"
    )]
    Duplicate(String),
    #[error(
        "相同 cloid 的提交正在进行中（锁存在但无回执）。结果未知，请人工核对: hl-trader status --address <账户地址> --cloid {0}。\
         确认交易所侧无该订单后，可人工删除 {0}.lock 再重试"
    )]
    InProgress(String),
    #[error("回执目录 {0}: {1}")]
    Io(PathBuf, #[source] std::io::Error),
    #[error("回执序列化失败: {0}")]
    Json(String),
}

pub struct ReceiptStore {
    dir: PathBuf,
}

impl ReceiptStore {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    fn lock_path(&self, cloid: &str) -> PathBuf {
        self.dir.join(format!("{cloid}.lock"))
    }

    fn receipt_path(&self, cloid: &str) -> PathBuf {
        self.dir.join(format!("{cloid}.json"))
    }

    /// 创建意图回执并持有互斥锁，直到 guard 被 drop。
    ///
    /// - 回执已存在 → Duplicate（提醒去查交易所真实状态）
    /// - 锁存在但无回执 → InProgress（另一进程在提交，或上次在发送前崩溃）。
    ///   保守处理为“结果未知”：不抢锁、不删除锁、不重复提交（PHASE2_PLAN §3.1：
    ///   锁不能因“回执存在”而被删除；删除活锁会让两个进程同时拥有同一 cloid）。
    pub fn begin(&self, receipt: &Receipt) -> Result<ReceiptGuard<'_>, ReceiptError> {
        fs::create_dir_all(&self.dir).map_err(|e| ReceiptError::Io(self.dir.clone(), e))?;
        // 目录本身也收紧到仅所有者可读写。
        let mut perm = fs::metadata(&self.dir)
            .map_err(|e| ReceiptError::Io(self.dir.clone(), e))?
            .permissions();
        perm.set_mode(0o700);
        fs::set_permissions(&self.dir, perm).map_err(|e| ReceiptError::Io(self.dir.clone(), e))?;

        let cloid = receipt.cloid.clone();
        // 回执已存在 = 这个 cloid 已经走过完整提交流程，拒绝重复提交。
        if self.receipt_path(&cloid).exists() {
            return Err(ReceiptError::Duplicate(cloid));
        }
        self.acquire_lock(&cloid)?;
        let guard = ReceiptGuard {
            store: self,
            cloid: cloid.clone(),
        };
        // A previous holder may have finished between the initial read and locking.
        if self.receipt_path(&cloid).exists() {
            return Err(ReceiptError::Duplicate(cloid));
        }
        self.write(&cloid, receipt)?;
        Ok(guard)
    }

    fn acquire_lock(&self, cloid: &str) -> Result<(), ReceiptError> {
        let lock = self.lock_path(cloid);
        let try_create = || {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&lock)
                .map(|_| ())
        };
        match try_create() {
            Ok(()) => Ok(()),
            // 锁已存在：无论是活锁还是陈旧锁，一律不删除（删除可能删掉另一进程的活锁，
            // 造成两个进程同时提交同一 cloid）。保守报“结果未知”，由人工核对后处理。
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                Err(ReceiptError::InProgress(cloid.to_string()))
            }
            Err(e) => Err(ReceiptError::Io(lock, e)),
        }
    }

    /// 原子写：先写临时文件（0600、落盘），再 rename 覆盖正式回执。
    fn write(&self, cloid: &str, receipt: &Receipt) -> Result<(), ReceiptError> {
        let json =
            serde_json::to_string_pretty(receipt).map_err(|e| ReceiptError::Json(e.to_string()))?;
        let tmp = self.dir.join(format!("{cloid}.tmp.{}", std::process::id()));
        let mut f = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| ReceiptError::Io(tmp.clone(), e))?;
        use std::io::Write as _;
        f.write_all(json.as_bytes())
            .map_err(|e| ReceiptError::Io(tmp.clone(), e))?;
        f.sync_all().map_err(|e| ReceiptError::Io(tmp.clone(), e))?;
        fs::rename(&tmp, self.receipt_path(cloid))
            .map_err(|e| ReceiptError::Io(self.receipt_path(cloid), e))?;
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|e| ReceiptError::Io(self.dir.clone(), e))?;
        Ok(())
    }

    pub fn load(&self, cloid: &str) -> Result<Option<Receipt>, ReceiptError> {
        let path = self.receipt_path(cloid);
        if !path.exists() {
            return Ok(None);
        }
        let text = fs::read_to_string(&path).map_err(|e| ReceiptError::Io(path.clone(), e))?;
        let receipt: Receipt =
            serde_json::from_str(&text).map_err(|e| ReceiptError::Json(e.to_string()))?;
        Ok(Some(receipt))
    }
}

/// 持锁 guard：存在期间其他进程不能用相同 cloid 建意图；drop 时释放锁。
pub struct ReceiptGuard<'a> {
    store: &'a ReceiptStore,
    cloid: String,
}

impl ReceiptGuard<'_> {
    /// 用最新状态重写回执（成交、拒单、结果未知时都要如实落盘）。
    pub fn update(&self, state: ReceiptState, oid: Option<u64>, detail: Option<String>) {
        if let Ok(Some(mut r)) = self.store.load(&self.cloid) {
            r.state = state;
            if oid.is_some() {
                r.oid = oid;
            }
            r.detail = detail;
            // 尽力写；写失败不应掩盖交易所结果，只报告原始错误。
            if let Err(e) = self.store.write(&self.cloid, &r) {
                eprintln!("回执更新失败，交易所结果仍以本次输出为准: {e}");
            }
        }
    }
}

impl Drop for ReceiptGuard<'_> {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.store.lock_path(&self.cloid));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static DIR_SEQ: AtomicU64 = AtomicU64::new(0);

    /// 每次调用生成唯一目录：默认并行测试运行时，同进程多个测试若共用
    /// PID 目录会互相删除对方文件（PHASE2_PLAN §7.1）。
    fn tempdir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hltrader-rcpt-{tag}-{}-{}",
            std::process::id(),
            DIR_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    fn sample(cloid: &str) -> Receipt {
        Receipt {
            version: 1,
            network: "hyperliquid-testnet".into(),
            account: "0x1111111111111111111111111111111111111111".into(),
            coin: "BTC".into(),
            side: "buy".into(),
            price: "60000".into(),
            size: "0.001".into(),
            cloid: cloid.into(),
            created_at_utc: "2026-10-14T00:00:00Z".into(),
            state: ReceiptState::Submitted,
            oid: None,
            detail: None,
        }
    }

    #[test]
    fn begin_writes_0600_receipt() {
        use std::os::unix::fs::MetadataExt;
        let dir = tempdir("perms");
        let store = ReceiptStore::new(dir.clone());
        let g = store.begin(&sample("0xabc")).unwrap();
        let path = dir.join("0xabc.json");
        assert!(path.exists());
        let md = fs::metadata(&path).unwrap();
        assert_eq!(md.mode() & 0o777, 0o600);
        drop(g);
        assert!(!dir.join("0xabc.lock").exists());
    }

    #[test]
    fn duplicate_cloid_rejected() {
        let dir = tempdir("dup");
        let store = ReceiptStore::new(dir);
        let g = store.begin(&sample("0xabc")).unwrap();
        // guard 未释放：回执已存在 → Duplicate（不重复提交）
        assert!(matches!(
            store.begin(&sample("0xabc")),
            Err(ReceiptError::Duplicate(_))
        ));
        drop(g);
        // guard 释放后回执仍在 → 依旧 Duplicate
        assert!(matches!(
            store.begin(&sample("0xabc")),
            Err(ReceiptError::Duplicate(_))
        ));
    }

    #[test]
    fn lock_without_receipt_means_in_progress() {
        let dir = tempdir("inprog");
        fs::create_dir_all(&dir).unwrap();
        // 模拟进程在“拿到锁之后、写回执之前”崩溃：锁存在但无回执。
        fs::write(dir.join("0xdead.lock"), "").unwrap();
        let store = ReceiptStore::new(dir);
        assert!(matches!(
            store.begin(&sample("0xdead")),
            Err(ReceiptError::InProgress(_))
        ));
    }

    #[test]
    fn concurrent_begin_only_one_wins() {
        let dir = tempdir("race");
        let store = std::sync::Arc::new(ReceiptStore::new(dir));
        let mut handles = Vec::new();
        for _ in 0..8 {
            let store = store.clone();
            handles.push(std::thread::spawn(move || {
                store.begin(&sample("0xrace")).is_ok()
            }));
        }
        let results: Vec<bool> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        assert_eq!(results.iter().filter(|ok| **ok).count(), 1);
    }

    #[test]
    fn update_persists_state() {
        let dir = tempdir("upd");
        let store = ReceiptStore::new(dir);
        {
            let g = store.begin(&sample("0xabc")).unwrap();
            g.update(
                ReceiptState::Resting,
                Some(12345),
                Some("resting on book".into()),
            );
        }
        let r = store.load("0xabc").unwrap().unwrap();
        assert_eq!(r.state, ReceiptState::Resting);
        assert_eq!(r.oid, Some(12345));
    }
}
