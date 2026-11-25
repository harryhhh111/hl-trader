# 项目功能与验收状态

核对日期：2026-11-25。本页区分当前代码、回归和实际部署证据；真实订单仍需独立验收。早期源码归档、运行模块对照与验证结果见[交付基线](deployment/RELEASE_BASELINE_2026_11_20.md)，后续变更以各自部署记录和本页核对日期为准。

## 已实现的能力

| 能力 | 当前代码状态 | 主要提交或依据 |
| --- | --- | --- |
| 独立 Web 应用 | React、FastAPI、PostgreSQL；Hyperliquid 看板、关注股、资讯、AI 草稿；原迁移 A–H 已交付 | [迁移执行记录](migration/PROGRESS.md)、[历史验收证据](migration/ACCEPTANCE.md) |
| 目标仓位调整与托管钱包 | 公开地址切换、托管 API Wallet、交易历史；托管路径包含授权核对、challenge、钱包版本、请求幂等及 memfd 传钥；同账户可跨 pair 共享钱包并串行化写入。2026-11-20 调仓弹窗已改为仅使用托管钱包，底层浏览器签名兼容接口及其他操作的签名入口保留 | `17f530f`、`c79c441`、`3b828b6`；[弹窗部署记录](deployment/MANAGED_ONLY_ADJUSTMENT_2026_11_20.md) |
| 实时盘口、滑点和深度保护 | 弹窗每 5 秒读取两腿 BBO；最大滑点默认 5 bps、允许 1–50，按保护价核对所需数量深度，每腿签名或发送前重读盘口；结果与历史展示成交滑点和未发送状态。2026-11-19 已部署 Web、执行器及前端 | `e6de848`；[盘口与滑点合同](HYPERLIQUID_ADJUSTMENT_LIVE_QUOTES.md)、[部署记录](deployment/ADJUSTMENT_DEPTH_2026_11_19.md) |
| 统一账户权益与比价展示 | Unified / Portfolio 模式读取账户级 portfolio 权益，失败时保留独立仓位并降级权益字段；比价和 30MA 使用五位有效数字。11 月 19 日执行器升级覆盖权益修复的问题已于 2026-11-20 经授权修复部署；HYPE/BTC、NBIS/CRWV 真实公开账户只读核验均返回 account=ok、账户总权益和完整来源键 | `72f3d36`；[权益修复部署记录](deployment/ACCOUNT_EQUITY_2026_11_20.md) |
| MU / SNDK Pair | `mu-sndk` 使用 `xyz:MU` 多 / `xyz:SNDK` 空，匹配现有 13 MU 多仓和 130 SNDK 空仓，Cross 2x。2026-11-25 已将 `vinci` 账户绑定到当前登录账号；Rust 与后端看板的 account/positions/ratio/funding 均为 ok，增仓前检查通过，未执行调仓。代码新增 `target_leg_weights`，手动调仓、比例偏离和再平衡共用该配置，其他 Pair 默认 1:1；MU/SNDK 拟随执行器部署设置 `["0.8","1"]`，使 SNDK:MU 目标名义比为 1:0.8，当前运行配置尚未启用该权重 | `8eb00bf`；`hl-dashboard-pairs.example.json`；真实公开账户只读核验 |
| 账号注册与登录 | `account` 模式提供邮箱密码注册/登录，Hyperliquid 资源接入用户归属；另保留 token 和 Cloudflare Access 模式 | `0c516bb`；认证异常不回退 token 表单的修复为 `d030122` |
| 更新日志 | `/changelog` 展示人工维护的发布说明；页面条目不是完整 Git 提交清单 | `cb3cc0e` |
| 开发验证环境 | 提供 `make test-fast/test-db/test-e2e/test-gateway/verify`；E2E 固定 UTF-8、当前源码和非 root 镜像，Rust 工具链与 Docker Cargo 使用持久缓存 | [运行手册](deployment/INDEPENDENT_APP.md#4-标准验证) |
| Gateway 动态解析 Web 地址 | nginx 通过 Docker DNS 在请求时解析 Web，缓存 5 秒；Web 重建换 IP 后不再依赖人工 reload。2026-11-25 已部署；隔离测试验证 gateway 不 reload 自动跟随 Web 新地址 | `deploy/docker/nginx.conf`、`make test-gateway`；`.runtime/deployments/manual-recovery-20261125T162003Z.deployed.json` |
| 发送前未知调仓恢复 | 托管请求只有在动作/订单标识为空、两腿未发送且没有匹配执行回执时，才允许“核对未发送并解除阻塞”；有真实标识或回执仍需人工核对。2026-11-25 已部署并将历史阻塞记录标为 `failed/not_sent_confirmed`，真实只读预览恢复，未发送订单 | `resolve-not-sent`；后端 322 项离线＋102 项数据库测试，Chromium 78 项通过、2 项跳过；同上部署证据 |
| 小额调仓差额处理 | 某腿保护价名义不足 10 USD 时跳过并提示，不再阻止另一腿符合条件的订单 | `9e3ad44` |
| Pair 止盈止损 | 两腿当前未实现盈亏合计、USD 阈值、只读 worker、页面触发提醒和人工确认平仓；规则与事件按用户、pair 和账户关联 | `87ffcd8`；[风险监控说明](HYPERLIQUID_PAIR_RISK_AUTOMATION.md) |
| 杠杆与增仓前检查 | 显示并手动调整逐合约 Cross / isolated 和 1–3x；增仓在签名、提交及逐腿发送前重读设置，纯减仓和平仓不被该检查阻塞 | `5b9dbcc`、`950e9a4`；[保证金模式说明](HYPERLIQUID_MARGIN_MODES.md) |
| 逐仓保证金补足 | isolated 仓位低于名义价值 80% 时显示比例和所需金额，由用户通过托管或浏览器 API Wallet 人工确认；签名前后及执行后核对实际仓位和保证金 | `19e9ce6`；运行中 Web/worker 的保证金模块与当前源码哈希一致，真实操作尚未验收；[保证金模式说明](HYPERLIQUID_MARGIN_MODES.md) |
| 阈值再平衡（人工版） | 固定目标、连续两次偏离确认、候选腿只读预览、冷却和人工执行回写；未超阈值腿不动，风险退出优先。已部署，真实订单仍需验收 | `8f89e69`；[阈值再平衡合同](HYPERLIQUID_THRESHOLD_REBALANCING.md)；migration `012` |
| 关注管理与行情更新 | 支持新增、重复校验、停用、恢复和账号级关注隔离；证券、行情与公告共享。新增/恢复由 worker 优先回补，页面展示待采集、更新中、成功、失败及安全错误摘要。已部署；2026-11-18 独立启用行情，CRWV/NBIS 首轮真实回补成功，最新日线为 11 月 17 日，周期为一小时；公告和 stock_data 同步仍关闭 | migration `014`；[行情启用记录](deployment/WATCHLIST_QUOTES_2026_11_18.md) |
| stock_data 资讯同步消费端 | 严格 DTO、来源映射、事件收据、页事务、bootstrap/status/activate 与 worker 已实现；同步来源不进入本地直接采集。2026-11-13 已部署 migration `013` 和消费端，后续运行模块核对一致；同步仍关闭，生产 bootstrap、激活和真实联调尚未验收 | `bed940f`；[同步合同](STOCK_DATA_NEWS_SYNC_CONTRACT.md)、[交付基线](deployment/RELEASE_BASELINE_2026_11_20.md) |
| 公司研究 HTML 阅读与同步 skill | 已部署公司单入口、隔离阅读与独立 HTML 模块导航；使用 `sync-company-research` 拉取研究库当前分支上游并原子发布不可变快照。2026-11-20 最新同步为 7 家公司、27 份 HTML；公网真实账号浏览器操作未另行验收 | `5a81b32`、`c974e7f`；[研究发布与同步流程](deployment/RESEARCH_RELEASE_WORKFLOW.md) |

当前有序数据库迁移为 `001`–`014`。新增代码或迁移文件不代表运行环境已经应用；执行迁移、重建容器和变更真实交易配置仍需明确授权。

运行基线的 `014_watchlist_ownership_quotes` 已于 2026-11-15 应用，本轮将此前未跟踪的迁移和配套源码纳入版本管理，不重复执行生产迁移。Web、gateway 和 worker 分别沿用已验证镜像；源码归档不代表本轮重新构建或部署全部服务。

## 尚未实现或仍需外部验收

- **全自动止盈止损**：当前 worker 只读监控，不签名、不下单。触发后由用户重新预览并人工确认；盈亏不含历史已实现盈亏、资金费和手续费。
- **stock_data 真实联调与启用**：消费端代码和离线 fixture 已实现，但源侧发布、`allowed_domain`、HTTPS、专用 token、生产 bootstrap 和激活仍未验收。该链路不依赖历史 full 包或 full lineage。
- **真实交易与公网浏览器验收**：人工调仓、风险平仓、阈值再平衡、杠杆和保证金操作仍需各自的真实订单或操作证据；研究内容同步已核验挂载文件与内网 gateway，尚未另行完成公网登录后浏览器验收。

## 验证与部署证据的适用范围

- [v2 看板记录](HYPERLIQUID_V2_ACCEPTANCE.md)保留的是 2026-10-19 的跨仓库回归与主网只读对照；其中账户配置、进程和配置哈希属于当次证据。
- [独立迁移验收](migration/ACCEPTANCE.md)记录的是 2026-10-28/29 的 A–H 基线。旧测试数量、截图和“未执行”项目不自动适用于后续提交。
- `5b47100` 的提交记录注明：仅该修复的隔离副本有 200 项后端测试通过，当时完整工作区为 234 项通过。它也不是当前 `87ffcd8` 的回归结果。
- [托管钱包文档](HYPERLIQUID_MANAGED_API_WALLET_PLAN.md)记录 2026-11-04 的本机 Docker 部署；[Cross / 3x 上线记录](deployment/CROSS_3X_2026_11_12.md)另记录 2026-11-12 的服务、资源、认证和规则装载核验。二者均不代替真实交易验收。
- 当前版本的验证命令见[运行手册](deployment/INDEPENDENT_APP.md#4-标准验证)。文档中的测试数量必须与对应提交和运行记录一起理解，不作为未来版本的永久结论。

## 文档使用顺序

1. 先看本页了解当前能力与未完成项，再看各功能说明。
2. 安装、认证、worker 和部署步骤使用[独立运行手册](deployment/INDEPENDENT_APP.md)。
3. [原迁移执行方案](INDEPENDENT_APP_MIGRATION_EXECUTION_PLAN.md)、`migration/` 的 A–H 证据和历史来源映射用于追溯原迁移范围。Web 交易与用户注册等后续能力已超出原只读迁移范围。
4. 原 full 导入/回退工具仍有独立合同；不要将其当作新资讯同步链路的启动要求，也不要将其业务 JSONL 快照当成钱包、认证或风险规则的完整备份。

多个 pair 的保证金边界和当前看板口径见[保证金模式说明](HYPERLIQUID_MARGIN_MODES.md)。
