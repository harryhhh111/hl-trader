//! v2 看板 pair 配置（JSON，多 pair 方案 §4/§5.3）：严格解析。
//!
//! - 拒绝重复 JSON 键（serde_json 默认会静默吞掉后出现的重复键，这里在
//!   MapAccess 层检查）、重复 pair id、未知字段（deny_unknown_fields）与错误类型。
//! - `account_address` 必须是合法非零 EVM 地址（统一小写）或显式 `null`；
//!   空串、占位文字与零地址都是配置错误。`enabled`/`unlock_anchors` 必须显式给出。
//! - `config_revision` 为配置原始字节的 SHA-256 小写 hex（调用前后比对，文件在
//!   采集中途被替换必须报错重试，不能把旧快照放进新配置缓存）。

use std::collections::HashSet;
use std::fmt;
use std::path::Path;

use hypersdk::Address;
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use sha2::Digest;

pub const CONFIG_VERSION: u64 = 1;

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("无法读取配置 {path}: {source}")]
    Io {
        path: std::path::PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("配置解析失败: {0}")]
    Parse(String),
    #[error("配置校验失败: {0}")]
    Invalid(String),
}

/// 网络仅接受 mainnet|testnet；URL 固定映射，无任意 URL（client.rs）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Network {
    Mainnet,
    Testnet,
}

impl Network {
    pub fn label(self) -> &'static str {
        match self {
            Network::Mainnet => "mainnet",
            Network::Testnet => "testnet",
        }
    }
}

/// 契约 PairDescriptor：字段与 §5.3 完全一致（anchors 不序列化进目录）。
#[derive(Debug, Clone, Serialize)]
pub struct PairDescriptor {
    pub id: String,
    pub label: String,
    pub network: Network,
    pub dex: String,
    pub coins: [String; 2],
    pub account_address: Option<String>,
    pub unlocks_enabled: bool,
}

/// 人工核实的解锁锚点（输入字段固定；有效性在快照时逐条判定）。
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UnlockAnchor {
    pub date_utc: String,
    pub label: String,
    pub source_url: String,
    pub verified_utc: String,
}

#[derive(Debug, Clone)]
pub struct DashboardPair {
    pub descriptor: PairDescriptor,
    /// 配置中的 enabled：false 的 pair 不出目录、CLI 按未知 id（404 语义）处理。
    pub enabled: bool,
    pub unlock_anchors: Option<Vec<UnlockAnchor>>,
}

#[derive(Debug, Clone)]
pub struct DashboardConfig {
    /// 配置原始字节的 SHA-256（小写 hex）。
    pub revision: String,
    pub pairs: Vec<DashboardPair>,
}

impl DashboardConfig {
    /// 未知或已停用的 pair id 一律返回 None（CLI 按 404 语义处理）。
    pub fn enabled_pair(&self, id: &str) -> Option<&DashboardPair> {
        self.pairs
            .iter()
            .find(|p| p.enabled && p.descriptor.id == id)
    }
}

pub fn revision_of(bytes: &[u8]) -> String {
    let mut hasher = sha2::Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// 读取并校验配置；任何解析/校验失败都是明确的配置错误（进程非 0 退出）。
pub fn load(path: &Path) -> Result<DashboardConfig, ConfigError> {
    let raw = std::fs::read(path).map_err(|source| ConfigError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let revision = revision_of(&raw);
    let text =
        String::from_utf8(raw).map_err(|_| ConfigError::Parse("配置不是 UTF-8 文本".into()))?;
    let checked: NoDupValue =
        serde_json::from_str(&text).map_err(|e| ConfigError::Parse(e.to_string()))?;
    let root = RawRoot::deserialize(checked.0).map_err(|e| ConfigError::Parse(e.to_string()))?;
    if root.config_version != CONFIG_VERSION {
        return Err(ConfigError::Invalid(format!(
            "config_version 必须是 {CONFIG_VERSION}，实际 {}",
            root.config_version
        )));
    }
    let mut ids = HashSet::new();
    for pair in &root.pairs {
        if !ids.insert(pair.id.clone()) {
            return Err(ConfigError::Invalid(format!(
                "pair id \"{}\" 重复",
                pair.id
            )));
        }
    }
    let pairs = root
        .pairs
        .into_iter()
        .map(validate_pair)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(DashboardConfig { revision, pairs })
}

/// 重新读取文件并计算 revision（采集结束后的变更检测；读失败按变更处理）。
pub fn revision_at(path: &Path) -> Result<String, ConfigError> {
    let raw = std::fs::read(path).map_err(|source| ConfigError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    Ok(revision_of(&raw))
}

// ---------- 重复键检测 ----------

/// 透明包装：任意 JSON 值递归反序列化；map 的每一层都拒绝重复键。
#[derive(Debug, Deserialize)]
#[serde(transparent)]
struct NoDupValue(#[serde(deserialize_with = "de_no_dup_value")] serde_json::Value);

fn de_no_dup_value<'de, D>(deserializer: D) -> Result<serde_json::Value, D::Error>
where
    D: Deserializer<'de>,
{
    struct DupCheck;
    impl<'de> Visitor<'de> for DupCheck {
        type Value = serde_json::Value;
        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
            f.write_str("任意 JSON 值（对象不允许重复键）")
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
            let mut seen = HashSet::new();
            let mut obj = serde_json::Map::new();
            while let Some(key) = map.next_key::<String>()? {
                if !seen.insert(key.clone()) {
                    return Err(serde::de::Error::custom(format!(
                        "重复的 JSON 键 \"{key}\""
                    )));
                }
                let value: NoDupValue = map.next_value()?;
                obj.insert(key, value.0);
            }
            Ok(Value::Object(obj))
        }
        fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
            let mut out = Vec::new();
            while let Some(item) = seq.next_element::<NoDupValue>()? {
                out.push(item.0);
            }
            Ok(Value::Array(out))
        }
        fn visit_bool<E: serde::de::Error>(self, b: bool) -> Result<Value, E> {
            Ok(Value::Bool(b))
        }
        fn visit_i64<E: serde::de::Error>(self, n: i64) -> Result<Value, E> {
            Ok(Value::from(n))
        }
        fn visit_u64<E: serde::de::Error>(self, n: u64) -> Result<Value, E> {
            Ok(Value::from(n))
        }
        fn visit_f64<E: serde::de::Error>(self, n: f64) -> Result<Value, E> {
            serde_json::Number::from_f64(n)
                .map(Value::Number)
                .ok_or_else(|| serde::de::Error::custom("非法浮点数"))
        }
        fn visit_str<E: serde::de::Error>(self, s: &str) -> Result<Value, E> {
            Ok(Value::String(s.to_string()))
        }
        fn visit_string<E: serde::de::Error>(self, s: String) -> Result<Value, E> {
            Ok(Value::String(s))
        }
        fn visit_unit<E: serde::de::Error>(self) -> Result<Value, E> {
            Ok(Value::Null)
        }
    }
    deserializer.deserialize_any(DupCheck)
}

// ---------- 原始结构（serde 类型检查） ----------

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRoot {
    config_version: u64,
    pairs: Vec<PairConfigRaw>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairConfigRaw {
    id: String,
    label: String,
    // 必须显式给出 true/false；省略即缺字段错误。
    enabled: bool,
    network: Network,
    dex: String,
    coins: [String; 2],
    // 用 Value 接收以区分「缺失（错误）」与「显式 null（不适用）」：
    // Option 字段缺失会被 serde 静默补成 None。
    account_address: serde_json::Value,
    unlock_anchors: serde_json::Value,
}

// ---------- 校验 ----------

fn validate_pair(raw: PairConfigRaw) -> Result<DashboardPair, ConfigError> {
    let PairConfigRaw {
        id,
        label,
        enabled,
        network,
        dex,
        coins,
        account_address,
        unlock_anchors,
    } = raw;
    if !valid_pair_id(&id) {
        return Err(ConfigError::Invalid(format!(
            "pair id \"{id}\" 不符合 ^[a-z0-9]+(?:-[a-z0-9]+)*$（最长 64）"
        )));
    }
    if label.trim().is_empty() {
        return Err(ConfigError::Invalid(format!(
            "pair \"{id}\" 的 label 不能为空"
        )));
    }
    for coin in &coins {
        if coin.trim().is_empty() {
            return Err(ConfigError::Invalid(format!(
                "pair \"{id}\" 的 coins 含空合约标识"
            )));
        }
        if dex.is_empty() {
            if coin.contains(':') {
                return Err(ConfigError::Invalid(format!(
                    "pair \"{id}\" 的 dex 为空，但合约 \"{coin}\" 含 \":\" 前缀"
                )));
            }
        } else if !coin.starts_with(&format!("{dex}:")) {
            return Err(ConfigError::Invalid(format!(
                "pair \"{id}\" 的合约 \"{coin}\" 缺少 dex 前缀 \"{dex}:\""
            )));
        }
    }
    if coins[0] == coins[1] {
        return Err(ConfigError::Invalid(format!(
            "pair \"{id}\" 的两腿合约必须不同"
        )));
    }
    let account_address = match &account_address {
        serde_json::Value::Null => None,
        serde_json::Value::String(s) if s.trim().is_empty() => {
            return Err(ConfigError::Invalid(format!(
                "pair \"{id}\" 的 account_address 为空串（合法地址或显式 null）"
            )));
        }
        serde_json::Value::String(s) => {
            let addr: Address = s.parse().map_err(|e| {
                ConfigError::Invalid(format!("pair \"{id}\" 的 account_address 非法: {e}"))
            })?;
            if addr == Address::ZERO {
                return Err(ConfigError::Invalid(format!(
                    "pair \"{id}\" 的 account_address 是零地址（不允许占位）"
                )));
            }
            Some(format!("{addr}").to_lowercase())
        }
        _ => {
            return Err(ConfigError::Invalid(format!(
                "pair \"{id}\" 的 account_address 必须是字符串或 null"
            )));
        }
    };
    let unlock_anchors = match &unlock_anchors {
        serde_json::Value::Null => None,
        serde_json::Value::Array(items) => Some(
            items
                .iter()
                .map(|item| {
                    UnlockAnchor::deserialize(item.clone()).map_err(|e| {
                        ConfigError::Invalid(format!(
                            "pair \"{id}\" 的 unlock_anchors 条目非法: {e}"
                        ))
                    })
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        _ => {
            return Err(ConfigError::Invalid(format!(
                "pair \"{id}\" 的 unlock_anchors 必须是数组或 null"
            )));
        }
    };
    Ok(DashboardPair {
        descriptor: PairDescriptor {
            id,
            label,
            network,
            dex,
            coins,
            account_address,
            unlocks_enabled: unlock_anchors.is_some(),
        },
        enabled,
        unlock_anchors,
    })
}

/// `^[a-z0-9]+(?:-[a-z0-9]+)*$`，且总长 ≤ 64。
fn valid_pair_id(id: &str) -> bool {
    if id.is_empty() || id.len() > 64 {
        return false;
    }
    let mut segment_start = true;
    for c in id.chars() {
        if c == '-' {
            if segment_start {
                return false;
            }
            segment_start = true;
        } else if c.is_ascii_lowercase() || c.is_ascii_digit() {
            segment_start = false;
        } else {
            return false;
        }
    }
    !segment_start
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load_str(text: &str) -> Result<DashboardConfig, ConfigError> {
        let mut tmp = std::env::temp_dir();
        tmp.push(format!(
            "hltrader-dashcfg-{}-{}.json",
            std::process::id(),
            revision_of(text.as_bytes())[..8].to_owned()
        ));
        std::fs::write(&tmp, text).unwrap();
        let result = load(&tmp);
        let _ = std::fs::remove_file(&tmp);
        result
    }

    const VALID: &str = r#"{
        "config_version": 1,
        "pairs": [
            {
                "id": "hype-btc",
                "label": "HYPE / BTC",
                "enabled": true,
                "network": "mainnet",
                "dex": "",
                "coins": ["HYPE", "BTC"],
                "account_address": null,
                "unlock_anchors": []
            },
            {
                "id": "nbis-crwv",
                "label": "NBIS / CRWV",
                "enabled": true,
                "network": "mainnet",
                "dex": "xyz",
                "coins": ["xyz:NBIS", "xyz:CRWV"],
                "account_address": "0x627F37fb0D7BC5552c404F00622F651737d7Ce92",
                "unlock_anchors": null
            }
        ]
    }"#;

    #[test]
    fn valid_config_parses_and_normalizes() {
        let cfg = load_str(VALID).unwrap();
        assert_eq!(cfg.pairs.len(), 2);
        assert_eq!(cfg.revision.len(), 64, "SHA-256 hex");
        let hype = &cfg.pairs[0];
        assert_eq!(hype.descriptor.id, "hype-btc");
        assert_eq!(hype.descriptor.network, Network::Mainnet);
        assert!(hype.descriptor.account_address.is_none());
        assert!(hype.descriptor.unlocks_enabled);
        let nbis = &cfg.pairs[1];
        assert_eq!(
            nbis.descriptor.account_address.as_deref(),
            Some("0x627f37fb0d7bc5552c404f00622f651737d7ce92"),
            "地址统一小写"
        );
        assert!(!nbis.descriptor.unlocks_enabled, "null anchors = 不适用");
        assert!(nbis.unlock_anchors.is_none());
        assert!(cfg.enabled_pair("hype-btc").is_some());
        assert!(cfg.enabled_pair("missing").is_none());
    }

    #[test]
    fn rejects_duplicate_json_keys_at_all_levels() {
        let dup_top = VALID.replace(
            "\"config_version\": 1,",
            "\"config_version\": 1, \"config_version\": 1,",
        );
        assert!(matches!(
            load_str(&dup_top),
            Err(ConfigError::Parse(m)) if m.contains("重复")
        ));
        let dup_pair = VALID.replace(
            "\"id\": \"hype-btc\",",
            "\"id\": \"hype-btc\", \"id\": \"other\",",
        );
        assert!(matches!(
            load_str(&dup_pair),
            Err(ConfigError::Parse(m)) if m.contains("重复")
        ));
        // 锚点条目内的重复键。
        let with_anchor = VALID.replace(
            "\"unlock_anchors\": []",
            r#""unlock_anchors": [{"date_utc": "2026-11-06", "date_utc": "2026-11-07", "label": "x", "source_url": "u", "verified_utc": "v"}]"#,
        );
        assert!(matches!(
            load_str(&with_anchor),
            Err(ConfigError::Parse(m)) if m.contains("重复")
        ));
    }

    #[test]
    fn rejects_schema_errors() {
        let dup_id = VALID.replace("\"nbis-crwv\"", "\"hype-btc\"");
        assert!(matches!(
            load_str(&dup_id),
            Err(ConfigError::Invalid(m)) if m.contains("重复")
        ));
        let unknown_field = VALID.replace(
            "\"config_version\": 1,",
            "\"config_version\": 1, \"mystery\": 1,",
        );
        assert!(matches!(
            load_str(&unknown_field),
            Err(ConfigError::Parse(m)) if m.contains("unknown field") || m.contains("未知")
        ));
        let bad_version = VALID.replace("\"config_version\": 1", "\"config_version\": 2");
        assert!(matches!(
            load_str(&bad_version),
            Err(ConfigError::Invalid(m)) if m.contains("config_version")
        ));
        let three_coins = VALID.replace(
            "\"coins\": [\"HYPE\", \"BTC\"]",
            "\"coins\": [\"HYPE\", \"BTC\", \"ETH\"]",
        );
        assert!(matches!(load_str(&three_coins), Err(ConfigError::Parse(_))));
        let missing_enabled = VALID.replace("\"enabled\": true,", "");
        assert!(matches!(
            load_str(&missing_enabled),
            Err(ConfigError::Parse(m)) if m.contains("enabled")
        ));
        let missing_anchors = VALID.replace(
            "\"account_address\": null,\n                \"unlock_anchors\": []",
            "\"account_address\": null",
        );
        assert!(matches!(
            load_str(&missing_anchors),
            Err(ConfigError::Parse(m)) if m.contains("unlock_anchors")
        ));
    }

    #[test]
    fn rejects_bad_ids_and_addresses() {
        for bad in [
            "Hype-BTC",
            "hype_btc",
            "-hype",
            "hype-",
            "hype--btc",
            "hype.btc",
        ] {
            let text = VALID.replace("\"hype-btc\"", &format!("\"{bad}\""));
            assert!(
                matches!(load_str(&text), Err(ConfigError::Invalid(_))),
                "id {bad} 应被拒绝"
            );
        }
        let zero = VALID.replace(
            "null,\n                \"unlock_anchors\": []",
            "\"0x0000000000000000000000000000000000000000\",\n                \"unlock_anchors\": []",
        );
        assert!(matches!(
            load_str(&zero),
            Err(ConfigError::Invalid(m)) if m.contains("零地址")
        ));
        let empty_addr = VALID.replace(
            "null,\n                \"unlock_anchors\": []",
            "\"\",\n                \"unlock_anchors\": []",
        );
        assert!(matches!(
            load_str(&empty_addr),
            Err(ConfigError::Invalid(m)) if m.contains("空串")
        ));
        let placeholder = VALID.replace(
            "null,\n                \"unlock_anchors\": []",
            "\"your-address-here\",\n                \"unlock_anchors\": []",
        );
        assert!(matches!(
            load_str(&placeholder),
            Err(ConfigError::Invalid(_))
        ));
    }

    #[test]
    fn rejects_dex_prefix_mismatch() {
        let no_prefix = VALID.replace(
            "\"coins\": [\"xyz:NBIS\", \"xyz:CRWV\"]",
            "\"coins\": [\"NBIS\", \"CRWV\"]",
        );
        assert!(matches!(
            load_str(&no_prefix),
            Err(ConfigError::Invalid(m)) if m.contains("前缀")
        ));
        let stray_prefix = VALID.replace(
            "\"coins\": [\"HYPE\", \"BTC\"]",
            "\"coins\": [\"xyz:HYPE\", \"BTC\"]",
        );
        assert!(matches!(
            load_str(&stray_prefix),
            Err(ConfigError::Invalid(m)) if m.contains("\":\"")
        ));
        let same_coins = VALID.replace(
            "\"coins\": [\"HYPE\", \"BTC\"]",
            "\"coins\": [\"BTC\", \"BTC\"]",
        );
        assert!(matches!(
            load_str(&same_coins),
            Err(ConfigError::Invalid(m)) if m.contains("必须不同")
        ));
    }

    #[test]
    fn testnet_pair_allowed_but_fixed_network() {
        let text = VALID.replace("\"network\": \"mainnet\"", "\"network\": \"testnet\"");
        let cfg = load_str(&text).unwrap();
        assert_eq!(cfg.pairs[0].descriptor.network, Network::Testnet);
        let bad = VALID.replace("\"network\": \"mainnet\"", "\"network\": \"devnet\"");
        assert!(matches!(load_str(&bad), Err(ConfigError::Parse(_))));
    }
}
