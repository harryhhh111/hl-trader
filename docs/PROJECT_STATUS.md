# 项目功能与验收状态

核对日期：2026-11-12。代码范围：`5b9dbcc` 及本次 Cross / 3x 下单前检查改动。本页区分代码、回归和实际部署证据；真实订单仍需独立验收。

## 已实现的能力

| 能力 | 当前代码状态 | 主要提交或依据 |
| --- | --- | --- |
| 独立 Web 应用 | React、FastAPI、PostgreSQL；Hyperliquid 看板、关注股、资讯、AI 草稿；原迁移 A–H 已交付 | [迁移执行记录](migration/PROGRESS.md)、[历史验收证据](migration/ACCEPTANCE.md) |
| 目标仓位调整与托管钱包 | 公开地址切换、浏览器本地/扩展钱包签名、托管 API Wallet、交易历史；托管路径包含授权核对、challenge、钱包版本、请求幂等及 memfd 传钥 | `17f530f`；[托管钱包说明](HYPERLIQUID_MANAGED_API_WALLET_PLAN.md) |
| 实时盘口和滑点 | 弹窗空闲时每 5 秒读取两腿 BBO；价格变化要求重新预览；结果与历史展示成交滑点 | `8280372`；[盘口与滑点合同](HYPERLIQUID_ADJUSTMENT_LIVE_QUOTES.md) |
| 账号注册与登录 | `account` 模式提供邮箱密码注册/登录，Hyperliquid 资源接入用户归属；另保留 token 和 Cloudflare Access 模式 | `0c516bb`；认证异常不回退 token 表单的修复为 `d030122` |
| 更新日志 | `/changelog` 展示人工维护的发布说明；页面条目不是完整 Git 提交清单 | `cb3cc0e` |
| 小额调仓差额处理 | 某腿保护价名义不足 10 USD 时跳过并提示，不再阻止另一腿符合条件的订单 | `9e3ad44` |
| Pair 止盈止损 | 两腿当前未实现盈亏合计、USD 阈值、只读 worker、页面触发提醒和人工确认平仓；规则与事件按用户、pair 和账户关联 | `87ffcd8`；[风险监控说明](HYPERLIQUID_PAIR_RISK_AUTOMATION.md) |
| 杠杆与增仓前检查 | 显示并手动调整逐合约 Cross / isolated 和 1–3x；增仓在签名、提交及逐腿发送前重读设置，纯减仓和平仓不被该检查阻塞 | `5b9dbcc` 及本次改动；[保证金模式说明](HYPERLIQUID_MARGIN_MODES.md) |

当前有序数据库迁移为 `001`–`011`。新增代码或迁移文件不代表运行环境已经应用；执行迁移、重建容器和变更真实交易配置仍需明确授权。

## 尚未实现或仍需外部验收

- **全自动止盈止损**：当前 worker 只读监控，不签名、不下单。触发后由用户重新预览并人工确认；盈亏不含历史已实现盈亏、资金费和手续费。
- **阈值再平衡**：[产品方案](HYPERLIQUID_THRESHOLD_REBALANCING.md)已明确，尚未实现。小额差额跳过功能不等于阈值再平衡。
- **stock_data 资讯同步消费端**：[开发合同](STOCK_DATA_NEWS_SYNC_CONTRACT.md)已定义“空资讯范围 bootstrap + 增量事件”，本仓库消费端和真实联调尚未完成。该链路不依赖历史 full 包或 full lineage。
- **真实交易及新功能部署验收**：需要核对实际镜像、数据库迁移、worker、配置和订单证据；源码存在、mock 测试或历史部署记录均不能替代。

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
