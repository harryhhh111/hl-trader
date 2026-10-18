//! v2 多 pair 主网只读看板（多 pair 方案 §5.1）：配置解析、只读客户端、快照采集。
//!
//! 与 v1（测试网单 pair、`status --json`）相互独立：本模块不读 TOML 交易配置、
//! 不装载签名器、不触碰 v1 PairStore；交易 Adapter 仍限定测试网。

pub mod client;
pub mod config;
pub mod snapshot;
