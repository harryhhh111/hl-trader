# 第三方依赖与许可记录

本文件记录 hl-trader 的依赖来源与许可证（MVP_PLAN §2 要求）。
依赖版本由 `Cargo.lock` 固定；重新生成完整清单可用 `cargo tree` 或 `cargo about`。

## 直接依赖

| 依赖 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| hypersdk (infinitefield/hypersdk) | 0.3.0 | **MPL-2.0** | Hyperliquid 协议类型、EIP-712 签名（alloy）、HTTP 客户端 |
| clap | 4.6.7 | MIT OR Apache-2.0 | CLI 解析 |
| rust_decimal | 1.40.x | MIT | 十进制定点金额/数量计算（不用二进制浮点） |
| serde / serde_json | 1.x | MIT OR Apache-2.0 | 回执与响应序列化 |
| toml | 0.9.x | MIT OR Apache-2.0 | 配置文件 |
| tokio | 1.x | MIT | 异步运行时 |
| reqwest | 0.13.x | MIT OR Apache-2.0 | 受限 HTTP 客户端（禁重定向、带超时），注入 hypersdk |
| chrono | 0.4.45 | MIT OR Apache-2.0 | UTC 时间输出 |
| anyhow / thiserror | 1.x / 2.x | MIT OR Apache-2.0 / MIT OR Apache-2.0 | 错误处理 |
| zeroize | 1.x | MIT OR Apache-2.0 | 私钥文件读取后的内存清零 |
| httpmock（dev） | 0.8.3 | MIT | 离线 mock HTTP 测试服务器 |

## 关键传递依赖

- alloy 2.5.x（MIT OR Apache-2.0）：EIP-712 签名、EVM 类型。
- rustls / aws-lc-rs / ring（ISC/Apache/BSD 系列）：TLS。其中 `aws-lc-sys`、`ring`、`blst`、
  `c-kzg`、`secp256k1-sys` 含 C/汇编代码，许可证分别为混合宽松许可证（见 `cargo tree` 明细）。
- MPL-2.0 共 3 个 crate：`hypersdk`、`fastrlp`、`yawc`（后两者为 hypersdk 传递依赖）。

## MPL-2.0 说明

MPL-2.0 是文件级 copyleft：作为依赖链接/使用不受限，但若**修改** MPL 许可文件本身，
须公开修改后的文件源码并保留许可声明。当前我们对 hypersdk 未做任何修改，直接
crates.io 固定版本使用。

## SDK 能力核实记录（2026-10-14，对 hypersdk 0.3.0 源码核实）

- `TimeInForce::Alo`（post-only）✅；`Chain::Testnet` ✅；`hypercore::testnet()` 端点为
  `https://api.hyperliquid-testnet.xyz` ✅
- `Client::order_status(user, OidOrCloid::Right(cloid))` 支持按 cloid 查订单 ✅
- `place`/`cancel` 返回逐订单 `OrderResponseStatus`（Resting/Filled/Error），HTTP 200 与
  成交可区分 ✅
- `Client::with_url`/`with_http_client` 支持注入 mock 端点（仅测试使用）✅
- 备注：crates.io 0.3.0 无 feature 开关（README 中 `http`/`ws` feature 描述与发布版不一致，
  以 crates.io 源码为准）。
- cloid 为 128 位（0x + 32 个 hex 字符）。

## 本项目自身许可证

待用户确认（MVP_PLAN §2，未默认指定 MIT 或 AGPL）。
