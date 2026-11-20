# 2026-11-20 源码收尾与运行基线

源码收尾阶段将此前已部分上线的工作树改动纳入本地版本管理，并纠正当前功能状态；该阶段没有推送、重启服务、执行生产迁移或真实交易，公司研究仅更新内容快照。随后用户另行授权部署账户权益修复，实际发布与只读复验见[部署记录](ACCOUNT_EQUITY_2026_11_20.md)。

## 源码归档范围

- Rust 看板通过 `portfolio` 查询 Unified / Portfolio 最新账户权益，失败时保留独立仓位；后端同步固定来源键，前端比价和 30MA 使用五位有效数字。
- 关注关系按账号隔离；新增、停用和恢复复用共享证券、日线与公告。行情 worker 优先回补待采集证券，记录成功、失败与安全错误摘要；资讯、公告和 AI 查询同时应用关注归属。
- `014_watchlist_ownership_quotes.sql`、旧 full 包兼容、参考回退适配以及相应数据库、页面和浏览器回归一并归档。该迁移已于 11 月 15 日应用，本轮不再次执行。
- 清理 AI 路由重复导入 `Request`，行为不变。更新 README、状态总表、资讯同步合同和研究同步记录。

业务源码提交为 `72f3d36`（账户权益与比价）及 `7b18dae`（关注股管理、行情状态和 migration `014`），标准 Rust 验证入口修复为 `7157925`。源码归档不等于将运行服务更新到对应提交。

## 运行模块与版本差异

本轮只读比较了 15 个相关 Python/SQL 文件的安装哈希和工作树哈希，证据为 `.runtime/verification/closeout-20261120-modules.json`：

- Web 的 13 个文件一致；`routes/ai.py` 仅差本轮重复导入清理，`workers/__init__.py` 与独立行情 worker 的后续版本不同。Web 不运行调度器。
- worker 的 14 个文件一致，仅 `routes/ai.py` 存在上述导入差异。
- 二者的 `014_watchlist_ownership_quotes.sql`、`services/watchlist.py`、`services/news_sync.py` 和 `services/hyperliquid_margin.py` 均与本地源码一致。模块存在不替代真实采集、同步或交易验收。
- 资讯同步已于 11 月 13 日按 `bed940f` 部署，migration `013` 已应用；当次 bootstrap 为 `not_started`、事件数为 0。后续记录和模块核对未发现启用证据，同步仍关闭。旧文档“未部署”改为“已部署但未启用/联调”。

部署前发现一项版本缺口：11 月 19 日按 `e6de848` 构建的交易 CLI 没有包含当时未提交的权益修复；后端已经要求 `account.source_queries` 恰好包含 `mode/balances/portfolio`，旧 CLI 仍只返回前两项，账户权益区块会被严格校验降级，其他持仓区块独立处理。当时只读核对运行二进制包含旧诊断、不包含新 portfolio 诊断，与构建记录和源码对照相符。该缺口现已按后续明确授权修复并完成真实公开账户只读复验。

不能将 11 月 13 日的权益部署记录继续当作当前 CLI 已具备该能力的证据。后续镜像必须同时包含本轮 Rust 修复和对应后端合同；源码归档与构建不构成部署授权。现有研究发布工具要求 CLI 哈希保持一致，不能用它绕过执行器升级的专门核对。

## 部署前运行基线

| 服务 | 本轮核对的镜像 | 范围 |
| --- | --- | --- |
| Web | `sha256:f0870d90dd5e0db5ae320010abfa32d683a0ce3597df27afe689b1cc2af3b2d4` | 11 月 20 日研究独立导航修复，包含 11 月 19 日执行器 |
| gateway | `sha256:f6ef5bed141b450691c6959ef3e02621fb733207481c573d8a1e24f3c6cbdebe` | 托管钱包调仓弹窗 |
| worker | `sha256:5f148193557064ae7655542a690c141251910f3a982ad9626c19e0b71d6c21b6` | 独立行情开启，公告和 stock_data 同步关闭 |

上述部署前 CLI SHA-256 为 `69d05b02eea5343b67e09b811e118e150d8a7dad7628951006e8c07849f0752e`。后续权益修复将 Web 更新为 `sha256:87595dae9fef150b5387ffa8bffb830a8bb82d4f81757bb1a144578e3d9010b7`，CLI 更新为 `cba86b2d40f47527511d3106cd7927a64fb3c8b073da98fff8f693c0b6e7d2ad`，gateway 和 worker 镜像保留。不能由 Git HEAD 推断运行镜像。

## 当前源码验证

先执行 `make verify`，退出码为 0；随后发现其中 Rust 的 198 项来自共享 target 内旧 crate 的编译缓存，不能用作当前 Rust 修复证据。隔离提交构建重新编译并通过 200 项测试；标准入口也已修正为先清理本 crate 的 dev 产物，再执行 fmt、Clippy 和测试。修正后的 `make test-rust` 退出码为 0，当前结果如下：

| 验证 | 结果 |
| --- | --- |
| Rust | fmt、Clippy 通过；当前源码 200 项通过、1 项既有公网手工检查忽略 |
| 后端离线 | 321 项通过，100 项数据库测试排除 |
| 一次性 PostgreSQL | 100 项通过，321 项离线测试排除；临时容器自动清理 |
| 前端单元测试 | 12 个测试入口，共 72 项通过 |
| TypeScript / Vite | 生产构建通过 |
| 发布与研究同步工具 | 30 项通过 |
| Chromium 桌面/手机 | 76 项通过、2 项仅 account 模式适用的注册用例跳过 |

汇总为 `.runtime/verification/closeout-20261120.json`，完整日志为 `closeout-20261120.log`，修正后的 Rust 日志为同目录 `closeout-20261120-rust.log`。测试使用当前源码、fixture 和独立临时数据库，没有访问真实账户或发送订单。依赖弃用、SSR 渲染、测试缓存权限及前端大 chunk 警告保留于日志，不作为真实功能验收。

## 权益修复镜像的构建与核验

复用 `deploy/build_adjustment_release.py`，从提交 `7b18dae` 导出 Rust 和固定三个调仓模块，以当前 Web 的已验证镜像标签为基础构建。构建记录为 `.runtime/deployments/equity-closeout-20261120-build.json`，日志为 `.runtime/verification/equity-closeout-20261120-build.log`。

- 候选 Web：`hl-app-web:equity-closeout-7b18dae`，镜像 ID `sha256:87595dae9fef150b5387ffa8bffb830a8bb82d4f81757bb1a144578e3d9010b7`。
- 候选 CLI SHA-256：`cba86b2d40f47527511d3106cd7927a64fb3c8b073da98fff8f693c0b6e7d2ad`。
- 70 个已安装 Python/SQL 文件与基础镜像逐文件哈希一致；CLI 包含新的 portfolio 权益处理，两个正式快照 fixture 的账户块均通过镜像内后端校验。Rust 相对 `e6de848` 只改变两个看板文件。
- 重新只读核验线上服务与 27 份研究 HTML，服务实例、镜像、环境与挂载指纹仍与研究发布后相同。

镜像核验证据为 `.runtime/deployments/equity-closeout-20261120-verified.json`。构建工具同时生成 gateway 候选，但本修复只需更新 Web 执行器，再重新加载现有 gateway 的 nginx 配置以解析新 Web 地址；worker 和 gateway 镜像继续沿用。切换前须获明确授权，保留回退镜像，确认没有执行中的调仓请求，并在切换后复验配置、快照和账户块合同。不能使用要求 CLI 哈希不变的普通研究发布模式执行此次升级。

该候选已于 2026-11-20 08:10 UTC 经用户另行授权部署，仅更新 Web，并重新加载现有 gateway 配置；两个 pair 的权益及持仓区块只读复验均为 `ok`。部署证据为 `.runtime/deployments/equity-closeout-20261120T080959Z-c01dcd3d.deployed.json`，账户复验为同目录 `equity-closeout-20261120-live-accounts.json`。

## 研究内容更新

已安装并使用 [sync-company-research](../../skills/sync-company-research/SKILL.md)。研究库 `main/origin/main` 从 `79466e96` 快进至 `6132e968fe4c067382813f350fe114e091c0e0f0`，工作区保持干净。

线上从 7 家公司、21 份 HTML 更新为 7 家公司、27 份 HTML；新增 QXO 六个模块，QXO 首页同时更新。新快照为 `s1cee4a3ba5dcbc6b149e3a12798a688a`，旧快照保留。展示文件及原件哈希、内网 gateway、健康和匿名鉴权复验通过，服务实例、镜像、配置及 CLI 保持一致。

发布证据为 `.runtime/deployments/research-sync-20261120T072336Z-97ca7fdf.json`。本次不改写研究正文或索引；demo 和纯行业条目继续排除。公网登录后浏览器阅读与研究财务事实未在本轮验收。完整操作入口见[研究发布流程](RESEARCH_RELEASE_WORKFLOW.md)。
