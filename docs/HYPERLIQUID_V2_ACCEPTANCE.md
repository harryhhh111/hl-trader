# Hyperliquid 多 Pair 看板验收状态

> 更新时间：2026-10-19
> 当前结论：R1–R9 及后续两项 P2 的代码整改、最新离线回归和真实主网只读数据对照已通过；真实浏览器、部署权限与发布验收仍未完成，不能视为已经完成上线验收。

## 当前配置

实际配置 [hl-dashboard-pairs.json](../hl-dashboard-pairs.json) 已包含两组启用的主网 pair，账户地址均已填写；`hl-dashboard-pairs.example.json` 中的 `null` 仅是示例占位。

| Pair | 网络 | DEX | 公开账户地址 |
|---|---|---|---|
| HYPE/BTC | mainnet | 原生 | 已配置 |
| NBIS/CRWV | mainnet | `xyz` | 已配置 |

具体地址以实际配置文件为唯一来源。当前配置的 SHA-256 / `config_revision` 为 `f97f2fc632ed0b7d30da5b5f4082cfb6031a7e7cf0fc28ef5d63eee5984417c7`。

## 最新离线回归基线（2026-10-19）

- hl-trader `50dab22`：`cargo fmt --check`、`cargo clippy --offline --all-targets -- -D warnings` 通过；`cargo test --offline` **154 通过、0 失败、1 个既有 live smoke 忽略**。
- stock_data `5a39dd8`：三份正式 Hyperliquid Python 测试 **171 通过、0 失败**；原始 3 个 HTTP 复现另行通过，合计 **174 通过、0 失败**。
- 同一 stock_data 提交的前端 Hyperliquid 回归 **24 通过、0 失败**；TypeScript 与 Vite 生产构建通过。新增白话诊断映射后，前端用例数从此前基线的 16 增至 24。
- 共享 Rust fixture 继续被 Python 与前端测试实读。本轮所有离线测试不访问真实账户、不读取私钥、不发送订单。

完整缺陷描述、R1–R9 修复提交映射和各轮复验记录见[历史验收档案](archive/HYPERLIQUID_V2_ACCEPTANCE_HISTORY.md)。

## 真实主网只读对照（2026-10-19 已通过）

本轮只使用公开账户地址和公开 `/info` 数据，不读取私钥、不发送订单、不重启服务。

- 运行中 Web 服务进程 PID `2582943` 启动于 `2026-10-19 11:21:19 +0800`；实际采集子进程命令为 `/home/vinci/projects/hl-trader/target/debug/hl-trader --config /home/vinci/projects/hl-trader/hl-dashboard-pairs.json dashboard --pair <id> --json`。
- Web 目录接口返回的两个地址、网络、DEX、合约顺序与配置文件一致，`config_revision` 与当前文件 SHA-256 完全一致。
- HYPE/BTC：Web 快照 `queried_at=2026-10-19T06:42:06Z`，CLI 快照 `queried_at=2026-10-19T06:43:47Z`。HYPE 多 `24.6`、BTC 空 `0.024`；CLI 时点两腿未实现盈亏合计 `-8.16403`，48 小时资金费净额 `-0.257247`。
- NBIS/CRWV：Web 快照 `queried_at=2026-10-19T06:44:05Z`，CLI 快照 `queried_at=2026-10-19T06:44:22Z`。NBIS 多 `6.0`、CRWV 空 `16.0`；CLI 时点两腿未实现盈亏合计 `10.9244`，48 小时资金费净额 `0.226646`。
- 独立对照共 **122 项通过、0 失败**：持仓方向、数量、开仓价、保证金模式与交易所一致；标记价、名义和未实现盈亏在几秒查询差对应的实时漂移容差内一致；两组 48 小时资金费各 96 笔目标合约记录，合计与交易所逐分一致；最近收付均能在交易所记录中找到；最新共同收盘日、收盘比价和 30 日均线与交易所日 K 重算结果一致。
- `account=partial` 是统一账户权益口径不适用单 DEX 余额的预期降级；HYPE/BTC `unlocks=partial` 对应 `unlock_anchors=[]`，NBIS/CRWV `unlocks=null` 对应配置为不适用。三者均不是本轮对照失败。

## 上线前待办

1. **真实浏览器验收**：覆盖桌面与手机布局、总览与详情导航、正常/降级/错误状态，以及跨 pair、跨账户切换时不串用旧快照。
2. **部署与发布验收**：当前运行进程早于 stock_data 最新提交，虽 Hyperliquid 后端关键修复已包含在其启动时的代码中，仍需在明确授权后重启并核对进程、二进制、配置、静态资源、SPA fallback、部署权限与回退条件；推送和正式发布也须另行明确授权。
