# Hyperliquid 多 Pair 看板验收状态

> 状态核对：2026-11-11；下文原始验收日期为 2026-10-19。
> 适用范围：R1–R9 及后续两项 P2 的代码整改、当次离线回归和真实主网只读数据对照已通过。本页保留该历史基线，不代表当前部署配置、运行进程或全部后续功能的验收状态。独立 Web、托管调仓和风险监控的现状见[项目状态](PROJECT_STATUS.md)；后续本地浏览器记录另见[迁移验收](migration/ACCEPTANCE.md)和[托管钱包说明](HYPERLIQUID_MANAGED_API_WALLET_PLAN.md)。

## 历史验证配置（2026-10-19）

当次外部配置 `hl-dashboard-pairs.json` 包含两组启用的主网 pair，账户地址均已填写；该文件不作为仓库内当前部署配置提供。[示例配置](../hl-dashboard-pairs.example.json)中的 `null` 仅是占位。后续版本支持按登录用户切换公开账户，实际配置与当前账户须在运行环境核对。

| Pair | 网络 | DEX | 公开账户地址 |
|---|---|---|---|
| HYPE/BTC | mainnet | 原生 | 已配置 |
| NBIS/CRWV | mainnet | `xyz` | 已配置 |

当次配置的 SHA-256 / `config_revision` 为 `f97f2fc632ed0b7d30da5b5f4082cfb6031a7e7cf0fc28ef5d63eee5984417c7`；不作为当前部署版本的预期值。

## 历史离线回归基线（2026-10-19）

- hl-trader `50dab22`：`cargo fmt --check`、`cargo clippy --offline --all-targets -- -D warnings` 通过；`cargo test --offline` **154 通过、0 失败、1 个既有 live smoke 忽略**。
- stock_data `914a3d8`：三份正式 Hyperliquid Python 测试 **171 通过、0 失败**；原始 3 个 HTTP 复现另行通过，合计 **174 通过、0 失败**。
- 同一 stock_data 提交的前端 Hyperliquid 回归 **26 通过、0 失败**；TypeScript 与 Vite 生产构建通过。新增白话诊断映射和两腿方向摘要后，前端用例数从最初基线的 16 增至 26；方向摘要现在按配置顺序直接展示“谁多谁空”，不再只显示“两腿反向”。
- 共享 Rust fixture 继续被 Python 与前端测试实读。本轮所有离线测试不访问真实账户、不读取私钥、不发送订单。

完整缺陷描述、R1–R9 修复提交映射和各轮复验记录见[历史验收档案](archive/HYPERLIQUID_V2_ACCEPTANCE_HISTORY.md)。

## 真实主网只读对照（2026-10-19 已通过）

本轮只使用公开账户地址和公开 `/info` 数据，不读取私钥、不发送订单、不重启服务。

- 当次 Web 服务进程 PID `2582943` 启动于 `2026-10-19 11:21:19 +0800`；当次采集子进程命令为 `/home/vinci/projects/hl-trader/target/debug/hl-trader --config /home/vinci/projects/hl-trader/hl-dashboard-pairs.json dashboard --pair <id> --json`。这些值仅用于历史追溯，不描述当前服务。
- Web 目录接口返回的两个地址、网络、DEX、合约顺序与配置文件一致，`config_revision` 与当前文件 SHA-256 完全一致。
- HYPE/BTC：Web 快照 `queried_at=2026-10-19T06:42:06Z`，CLI 快照 `queried_at=2026-10-19T06:43:47Z`。HYPE 多 `24.6`、BTC 空 `0.024`；CLI 时点两腿未实现盈亏合计 `-8.16403`，48 小时资金费净额 `-0.257247`。
- NBIS/CRWV：Web 快照 `queried_at=2026-10-19T06:44:05Z`，CLI 快照 `queried_at=2026-10-19T06:44:22Z`。NBIS 多 `6.0`、CRWV 空 `16.0`；CLI 时点两腿未实现盈亏合计 `10.9244`，48 小时资金费净额 `0.226646`。
- 独立对照共 **122 项通过、0 失败**：持仓方向、数量、开仓价、保证金模式与交易所一致；标记价、名义和未实现盈亏在几秒查询差对应的实时漂移容差内一致；两组 48 小时资金费各 96 笔目标合约记录，合计与交易所逐分一致；最近收付均能在交易所记录中找到；最新共同收盘日、收盘比价和 30 日均线与交易所日 K 重算结果一致。
- `account=partial` 是统一账户权益口径不适用单 DEX 余额的预期降级；HYPE/BTC `unlocks=partial` 对应 `unlock_anchors=[]`，NBIS/CRWV `unlocks=null` 对应配置为不适用。三者均不是本轮对照失败。

## 当前版本的复验待办

1. **当前提交的回归与浏览器复验**：历史迁移和托管钱包已有本地 Chromium 记录；新增账号、盘口、风险规则等需随目标版本核对单测、DB、build 和 E2E 结果，包含跨用户、跨 pair、跨账户切换及监控过期状态。历史通过数量不能替代当前结果。
2. **实际部署版本核对**：托管钱包文档记录过 2026-11-04 本机 Docker 部署，但本次没有读取运行环境。核对当前镜像/二进制、`001`–`010` 迁移、配置、静态资源、SPA fallback、worker 和回退条件，不能继续以旧 PID 或 stock_data 版本判断当前独立站状态。部署、重启、推送和发布仍需明确授权。
3. **真实来源和交易边界**：2026-10-19 的主网只读对照不证明当前账户、托管执行或风险平仓已通过真实交易验收；需使用各自经授权的验收记录。
