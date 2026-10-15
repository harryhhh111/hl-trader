//! 配置与秘密文件加载。
//!
//! 安全约束（MVP_PLAN §4）：
//! - agent 私钥只从仓库外的秘密文件读取；拒绝符号链接、拒绝非当前所有者、
//!   拒绝组/其他用户有任何权限（要求 0o600 或更严）。
//! - 私钥不通过命令行参数传入、不打印到日志、不写入结果文件。

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use hypersdk::Address;
use hypersdk::hypercore::PrivateKeySigner;
use rust_decimal::Decimal;
use serde::Deserialize;
use zeroize::Zeroizing;

/// 本工具唯一允许的网络。
pub const NETWORK: &str = "hyperliquid-testnet";

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    /// 实际交易账户地址（公共地址；查询与下单都指向此账户，不用 agent 地址）。
    pub account_address: String,
    /// 仓库外 agent 私钥文件路径。
    pub secret_file: PathBuf,
    /// 单笔名义金额上限（USD）。这是防误操作的本地限制，不是账户级资金保护。
    pub max_notional_usd: Decimal,
    /// 回执目录（运行目录，不进 Git）。
    #[serde(default = "default_receipt_dir")]
    pub receipt_dir: PathBuf,
}

fn default_receipt_dir() -> PathBuf {
    PathBuf::from(".hl-trader")
}

impl Config {
    pub fn account_address(&self) -> Result<Address, ConfigError> {
        Address::from_str(&self.account_address)
            .map_err(|e| ConfigError::Address(format!("{}: {e}", self.account_address)))
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("无法读取配置文件 {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("配置解析失败: {0}")]
    Parse(String),
    #[error("配置项 account_address 不是合法的 EVM 地址: {0}")]
    Address(String),
}

pub fn load_config(path: &Path) -> Result<Config, ConfigError> {
    let text = fs::read_to_string(path).map_err(|source| ConfigError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let cfg: Config = toml::from_str(&text).map_err(|e| ConfigError::Parse(e.to_string()))?;
    cfg.account_address()?;
    Ok(cfg)
}

#[derive(Debug, thiserror::Error)]
pub enum SecretError {
    #[error("秘密文件 {0} 是符号链接，已拒绝")]
    Symlink(PathBuf),
    #[error("秘密文件 {0} 所有者不是当前用户（owner={1}, euid={2}）")]
    Owner(PathBuf, u32, u32),
    #[error("秘密文件 {0} 权限过宽（mode={1:#o}），要求仅所有者可读写（0o600 或更严）")]
    Permissions(PathBuf, u32),
    #[error("读取秘密文件 {0}: {1}")]
    Io(PathBuf, #[source] std::io::Error),
    #[error("秘密文件内容不是合法的 32 字节十六进制私钥")]
    Key(String),
}

/// 当前进程的有效用户 id（读 /proc/self/status，避免引入 libc 依赖）。
fn current_euid() -> u32 {
    let status = fs::read_to_string("/proc/self/status").unwrap_or_default();
    for line in status.lines() {
        if let Some(first) = line
            .strip_prefix("Uid:")
            .and_then(|r| r.split_whitespace().next())
        {
            return first.parse().unwrap_or(u32::MAX);
        }
    }
    u32::MAX
}

/// 加载 agent 私钥。仅用于显式 `--execute` 的写操作；dry-run 不调用本函数。
pub fn load_agent_signer(secret_path: &Path) -> Result<PrivateKeySigner, SecretError> {
    let md = fs::symlink_metadata(secret_path)
        .map_err(|e| SecretError::Io(secret_path.to_path_buf(), e))?;
    if md.file_type().is_symlink() {
        return Err(SecretError::Symlink(secret_path.to_path_buf()));
    }
    let euid = current_euid();
    if md.uid() != euid {
        return Err(SecretError::Owner(
            secret_path.to_path_buf(),
            md.uid(),
            euid,
        ));
    }
    let mode = md.mode() & 0o777;
    if mode & 0o077 != 0 {
        return Err(SecretError::Permissions(secret_path.to_path_buf(), mode));
    }

    let bytes = Zeroizing::new(
        fs::read(secret_path).map_err(|e| SecretError::Io(secret_path.to_path_buf(), e))?,
    );
    let text = std::str::from_utf8(&bytes)
        .map_err(|e| SecretError::Key(format!("不是 UTF-8 文本: {e}")))?;
    let hex = text.trim();
    let signer = PrivateKeySigner::from_str(hex)
        .or_else(|_| PrivateKeySigner::from_str(&format!("0x{hex}")))
        .map_err(|e| SecretError::Key(e.to_string()))?;
    Ok(signer)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("hltrader-cfg-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn write_secret(dir: &Path, name: &str, mode: u32) -> PathBuf {
        let p = dir.join(name);
        fs::write(
            &p,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        )
        .unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(mode)).unwrap();
        p
    }

    #[test]
    fn rejects_symlink() {
        let dir = tempdir();
        let real = write_secret(&dir, "real.key", 0o600);
        let link = dir.join("link.key");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(matches!(
            load_agent_signer(&link),
            Err(SecretError::Symlink(_))
        ));
    }

    #[test]
    fn rejects_group_readable() {
        let dir = tempdir();
        let p = write_secret(&dir, "wide.key", 0o640);
        assert!(matches!(
            load_agent_signer(&p),
            Err(SecretError::Permissions(m, _)) if m == p
        ));
    }

    #[test]
    fn rejects_world_readable() {
        let dir = tempdir();
        let p = write_secret(&dir, "wide.key", 0o644);
        assert!(matches!(
            load_agent_signer(&p),
            Err(SecretError::Permissions(_, _))
        ));
    }

    #[test]
    fn accepts_owner_only_600() {
        let dir = tempdir();
        let p = write_secret(&dir, "ok.key", 0o600);
        assert!(load_agent_signer(&p).is_ok());
    }

    #[test]
    fn rejects_invalid_key_material() {
        let dir = tempdir();
        let p = dir.join("bad.key");
        fs::write(&p, "not-a-hex-key\n").unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(matches!(load_agent_signer(&p), Err(SecretError::Key(_))));
    }

    #[test]
    fn config_roundtrip_and_address_validation() {
        let dir = tempdir();
        let cfg_path = dir.join("hl-trader.toml");
        fs::write(
            &cfg_path,
            r#"
account_address = "0x1111111111111111111111111111111111111111"
secret_file = "/tmp/agent.key"
max_notional_usd = "50"
"#,
        )
        .unwrap();
        let cfg = load_config(&cfg_path).unwrap();
        assert_eq!(cfg.max_notional_usd, Decimal::new(50, 0));
        assert!(cfg.account_address().is_ok());

        let bad = dir.join("bad.toml");
        fs::write(
            &bad,
            r#"account_address = "not-an-address"
secret_file = "/tmp/agent.key"
max_notional_usd = "50"
"#,
        )
        .unwrap();
        assert!(matches!(load_config(&bad), Err(ConfigError::Address(_))));
    }
}
