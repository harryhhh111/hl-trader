//! All CLI writers share an OS lock keyed by agent, independent of receipt_dir.
//! Lock files are never unlinked: the kernel releases the lock on process exit.
use std::fs::{self, File, OpenOptions, TryLockError};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::Path;

use hypersdk::Address;

pub struct WriteLock {
    _file: File,
}

impl WriteLock {
    pub fn agent(address: Address) -> Result<Self, String> {
        let uid = fs::metadata("/proc/self").map_err(|e| e.to_string())?.uid();
        let dir = std::path::PathBuf::from("/tmp").join(format!("hl-trader-writers-{uid}"));
        match fs::DirBuilder::new().mode(0o700).create(&dir) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e.to_string()),
        }
        let md = fs::symlink_metadata(&dir).map_err(|e| e.to_string())?;
        if !md.is_dir() || md.uid() != uid || md.mode() & 0o077 != 0 {
            return Err("agent 锁目录类型、所有者或权限不安全".into());
        }
        Self::at(&dir.join(format!("testnet-{address}.lock")))
    }

    pub fn at(path: &Path) -> Result<Self, String> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(path)
            .map_err(|e| format!("打开锁 {}: {e}", path.display()))?;
        file.try_lock().map_err(|e| match e {
            TryLockError::WouldBlock => format!("另一写进程持有锁 {}", path.display()),
            TryLockError::Error(e) => format!("获取锁 {}: {e}", path.display()),
        })?;
        Ok(Self { _file: file })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_agent_is_exclusive_and_drop_releases_without_unlink() {
        let address = Address::from_slice(&[0x37; 20]);
        let first = WriteLock::agent(address).unwrap();
        assert!(WriteLock::agent(address).is_err());
        drop(first);
        assert!(WriteLock::agent(address).is_ok());
    }

    #[test]
    fn child_lock_probe() {
        let Ok(path) = std::env::var("HL_TRADER_LOCK_TEST_PATH") else {
            return;
        };
        if std::env::var("HL_TRADER_LOCK_TEST_MODE").as_deref() == Ok("held") {
            assert!(WriteLock::at(Path::new(&path)).is_err());
        } else {
            let _lock = WriteLock::at(Path::new(&path)).unwrap();
            std::process::exit(0); // no destructors: kernel must release the lock
        }
    }

    #[test]
    fn process_exit_releases_lock_and_other_process_cannot_bypass_it() {
        let path =
            std::env::temp_dir().join(format!("hltrader-process-lock-{}", std::process::id()));
        let child = |mode: &str| {
            std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "write_lock::tests::child_lock_probe"])
                .env("HL_TRADER_LOCK_TEST_PATH", &path)
                .env("HL_TRADER_LOCK_TEST_MODE", mode)
                .output()
                .unwrap()
        };
        let held = WriteLock::at(&path).unwrap();
        assert!(child("held").status.success());
        drop(held);
        assert!(child("crash").status.success());
        assert!(WriteLock::at(&path).is_ok());
    }
}
