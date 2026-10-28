# 独立应用迁移执行记录

## 固定基线

- 工作目录：`/home/vinci/projects/hl-trader`
- 开发分支：`codex/independent-app-migration`
- hl-trader 起始 HEAD：`960f6d7f106559531421f4b31c431ea5420cc926`（`main`）
- stock_data 固定 HEAD：`cc62434d126cdfc1dc40456f430d943f7d40d7cd`（`main`），与合同编写值一致，无需重定来源版本
- hl-trader 起始未提交内容：用户已修改 `README.md`；未跟踪 `docs/INDEPENDENT_APP_MIGRATION_EXECUTION_PLAN.md`。实施时保留，不 reset 或覆盖
- stock_data 起始未提交内容：边界方案、新闻正文方案、证据目录及 `scripts/verify_news_body_gate0.py`；均保持只读，不属于本次复制来源
- 当前进程未设置 `STOCK_MARKETS`；未读取源 `.env`。合同已确认源环境为 `STOCK_MARKETS=US`，不在源环境执行 A/HK 同步、DDL 或迁移

## A：基线与骨架

状态：完成，阶段提交 `b2743b0d758ab218563bddddba5aba24eb50552b`。

已落地：独立 Python 包、三类 DSN 无回退、SQL migration、operation control 默认 maintenance、单 Web advisory lifecycle lock、FastAPI app factory、React/Vite 基础、同源 v1 client、来源清单及测试骨架。

工作目录除前端命令外均为仓库根；前端命令在 `frontend/`。起始 HEAD 见上文，实际验证结果：

- `python3 -m venv .venv && .venv/bin/python -m pip install -e "backend[test]"`：安装成功；`pip freeze --exclude-editable` 生成 23 项精确锁定依赖。
- `.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q`：9 passed，3 deselected。
- 在本机 `initdb` 新建的 `/tmp/hl-app-pg-A.XA3YDb` 临时实例上，显式注入 `HL_APP_TEST_DATABASE_URL` 后运行 DB 套件：3 passed，9 deselected；覆盖空 schema 初始化/重复迁移、运行账号无 DDL/control 写权限、第二 Web 生命周期锁拒绝。另以 `HL_APP_MIGRATION_DATABASE_URL` 实际运行 `python -m hl_app.db migrate` 两次，首次应用 `001_operation_control`，重复执行无变更。
- `npm ci && npm run test:unit && npm run build`：3 个 client case 实际执行并通过，TypeScript/Vite 构建成功。因初始依赖审计发现漏洞，升级 Vite、React Router 和 esbuild 后 `npm audit` 为 0 vulnerabilities。
- `cargo fmt --check`：通过；`cargo test --offline`：154 passed、0 failed、1 ignored（既有公开来源 smoke）；`cargo clippy --offline --all-targets -- -D warnings`：通过。
- 验证中遇到主机磁盘 ENOSPC。仅清理可重新下载的包/浏览器缓存并重建 Rust `target`；Rust 编译使用 `CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0` 降低中间产物，未减少或跳过任何测试。旧 `/tmp` 数据库备份和 stock_data 数据未改动。

未完成：A 阶段以外的业务页面、认证和采集；真实外部来源、生产数据库、部署和真实采集均未执行。阶段提交号已补记于上文。

## B：登录与 Hyperliquid

状态：完成，阶段提交 `3cf306d3d4854faffaae6307399faff183056987`。阶段起点 HEAD 为 A 提交 `b2743b0d758ab218563bddddba5aba24eb50552b`。

已落地：持久化 `auth_control/auth_epoch`、带 epoch 和 CSRF 哈希的会话、管理端 initialize/rotate、持久化登录限速、全数据 API 鉴权、Origin/CSRF 检查、单 Web 锁失效终止采集与关闭流程；v2 目录/快照/公告、严格 DTO 与身份校验、30 秒缓存、单飞和最多两个子进程；React 登录/总览/详情/图表与同源 API；目标内脱敏共享 fixture。v1 Web 路由、分析链接和旧配置均未注册。

实际验证：

- `.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q`：101 passed，17 deselected。覆盖 v2 配置/身份/块级语义、E1–E3 类型破坏、七类真实子进程边界、同 key 单飞、并发上限和锁失效终止。
- 在 `/tmp/hl-app-pg-B.JMz6FI` 临时 PostgreSQL 上显式运行 DB 套件：17 passed，101 deselected。覆盖登录/注销/过期、哈希存储、epoch 轮换幂等、旧配置拒绝、多认证组件会话、轮换与签发竞争、持久化限速，以及完成登录后的六组 E1–E3 HTTP 用例和未登录 401。
- `npm run test:unit && npm run build`：32 个前端 case 实际执行通过，其中 Hyperliquid 26、登录 2、API client 4；生产构建成功。
- `python3 tests/fixtures/hyperliquid/normalize.py --check`：通过。样本只含虚构地址，Python/React 路径均指向本仓库。

未完成：真实浏览器、真实公开账户、生产 OS 权限和部署验收；这些留待 G/授权后验收，未宣称完成。B 阶段提交号已补记于上文。

## C：关注股与行情

状态：完成，阶段提交 `bc97012110e79f7ece36eb0bf7b8e096a3bc5249`。阶段起点 HEAD 为 B 提交 `3cf306d3d4854faffaae6307399faff183056987`。

已落地：最小 securities/watchlist/daily_quotes schema、统一代码与显式行情源映射、启用项列表、白名单元数据 PATCH、只查库的三市场行情、近 20 日断线走势、腾讯单证券未复权日线采集及 90/365 日有界回补。没有 create/delete/search/analyzer API 或入口，没有 stock_info、全市场扫描和视图刷新依赖。

实际验证：

- Python 离线套件：120 passed，22 deselected；覆盖代码/CIK/来源校验、类别股、非有限价格、缺前收、35% 跳变、4 日滞后、三市场收盘边界和无新增/删除路由。
- 在 `/tmp/hl-app-pg-C.letpNd` 临时 PostgreSQL 上运行 DB 套件：22 passed，120 deselected；覆盖三市场同码隔离、迁移与约束、维护态拒绝写入、白名单更新、只枚举启用项、停用前后重新核对、已知价格不被缺值覆盖。
- 前端完整单测 33 项（新增关注股表格 1 项）及生产构建通过；页面不含新增、移除、搜索或分析入口。
- 修复迁移验证发现的 PostgreSQL 兼容问题：不用 `isfinite(double precision)`，改为显式 NaN/Infinity 检查；迁移连接在成功和异常时均确定性关闭，避免 traceback 保留会话锁。修复后整套 DB 用例无悬挂通过。

未完成：US/A/HK 真实来源可达性、公告和 AI；真实网络采集未执行。C 阶段提交号已补记于上文。

## D：公告与资讯

状态：完成，阶段提交 `abdad943febf62040f21ff1cf8924e4127fa3021`。阶段起点 HEAD 为 C 提交 `bc97012110e79f7ece36eb0bf7b8e096a3bc5249`。

已落地：来源注册、不可变 revision、首版多证券关联及公司级理由、check/gap/run/recovery、SEC submissions 与旧文件分页恢复、Nebius/Q4 IR、RSS/Atom 与 Finnhub 媒体适配、共享 SEC gate、公告摘要/详情、每日信息流折叠与重要性置顶。读取 API 只查库，不调用采集器或模型；非美股明确不支持。

实际验证：

- Python 离线套件：129 passed，32 deselected；覆盖 48 小时折叠边界、未知时间、多公司理由、SEC 时间精度、Nebius/Q4/Finnhub 适配及置顶窗口。
- 在 `/tmp/hl-app-pg-D.eCrJLN` 临时 PostgreSQL 上运行 DB 套件：32 passed，129 deselected；覆盖 revision/关联继承、停用后不新增关联、失败 gap、非美股状态、SEC 分页预算不推进检查点及下一轮恢复。
- SEC gate 使用四个真实本地进程和本机 HTTP mock 验证最多 2 次/秒；429 持久化冷却、协调失败零请求和任意外部 URL 拒绝均通过。
- 前端 35 项单测及生产构建通过；新增每日资讯 2 项，公告摘要/详情、折叠公司标签和理由展示接入关注股与资讯页。

未完成：真实 SEC/IR/媒体出口与既有来源状态调整、AI 和常驻 worker；未执行真实网络采集。D 阶段提交号已补记于上文。

## E：AI 与专属调度

状态：完成，阶段提交 `e8e41552b7177abe32f983731019638c8de620b9`。阶段起点 HEAD 为 D 提交 `abdad943febf62040f21ff1cf8924e4127fa3021`。

已落地：固定 Git commit 研究读取、理由草稿与 `[通用草稿]`、有界 mmx 子进程、媒体中文增强与官方 importance、Web/worker 两个各 1 并发命名池、单实例 worker 生命周期锁、quotes/announcements/news_enhancement 三类任务和 dry-run。Web 不执行资讯增强，常驻与一次性 worker 共用锁，默认全部关闭。

实际验证：

- Python 离线套件：137 passed，36 deselected；覆盖研究未提交内容隔离、坏 Git/坏 schema、模型非法 JSON/字段、空输出和资讯并发上限。
- 在 `/tmp/hl-app-pg-E.Ebo2HM` 临时 PostgreSQL 上运行 DB 套件：38 passed，137 deselected；覆盖研究/通用草稿不保存、理由池上限、增强失败原文和既有字段不变、worker 独占及禁用态。
- 实际执行 `python -m hl_app.workers --dry-run`：仅输出本应用任务和空目标，无网络请求；`HL_APP_COLLECTORS_ENABLED=false ... --once --kind quotes` 返回 disabled。
- 前端 35 项单测与生产构建通过；AI 草稿只填入编辑框，人工修改后才可保存。`npm audit` 为 0 vulnerabilities。

未完成：真实 mmx/研究目录、生产调度启用、真实浏览器和部署；未调用真实模型或启动常驻采集。E 阶段提交号已补记于上文。

## F：数据导入与回退导出

状态：完成，阶段提交 `341e2d027a42a1b1224a15ce4bea318d5dc3aa98`。阶段起点 HEAD 为 E 提交 `e8e41552b7177abe32f983731019638c8de620b9`。

已落地：bundle v1 JSONL/manifest、依赖与自然键校验、空业务库 full dry-run/apply、映射账本、维护锁、baseline ledger、激活、完整 rollback snapshot 和 before/after delta、参考源三方恢复。`DATA_CONTRACT.md` 固定源侧导出与恢复交接；没有增量覆盖、旧生产库连接或自动启用采集。

实际验证：

- `python -m hl_app.migration inspect --bundle tests/fixtures/migration/full`：通过，十实体完整，公告 2、关联 2、来源 2、gap/recovery 各 1。
- 全套真实 PostgreSQL：42 passed，137 deselected。其中迁移演练覆盖 dry-run/apply/幂等重跑、同 ID 变化拒绝、坏 FK 回滚、baseline/重复 activate、维护态、回退 insert/update/delete、参考恢复幂等与源侧并发冲突。
- CLI 全链路在 `cli_f` schema 实际执行：migrate→dry-run→apply→baseline→activate→maintenance→rollback-export 全部成功；无业务变化时 delta=0。
- 修复了 range 数组显式类型转换、range 对象和 timestamptz 的 UTC 规范化，保证导入前后 business hash 稳定。

未完成：正式源 bundle、stock_data agent 正式恢复适配、真实停写窗口测量和生产迁移；这些外部条件未宣称完成。F 阶段提交号已补记于上文。

## G：独立性与交互验收

状态：完成，阶段提交 `cad4d68296f660886dafa2494d287292f8450fbb`。阶段起点 HEAD 为 F 提交 `341e2d027a42a1b1224a15ce4bea318d5dc3aa98`。

- 全层基线：Python 137 passed；Rust 154 passed、1 ignored，fmt/clippy 通过；前端 35 项及生产构建通过。
- 真实 Chromium：8/8 passed，桌面 1440×900、移动 390×844 各 4 项，覆盖登录/退出、pair 切换、坏解锁块、目录失败旧读数、元数据编辑、AI 草稿保存、资讯筛选和移动端溢出。截图在 `docs/migration/evidence/`。
- 干净副本 `/tmp/hl-app-clean-G.ueynta` 仅由目标文件创建，重新安装后 mock 写库 1 行、登录和列表读取成功，无 stock_data 目录、旧凭据或旧 API。
- 发现并修复目录失败时旧目录被卸载的问题：查询层恢复旧数据并保留 error 状态；E2E 先等待旧卡片存在再注入失败，且每次先构建当前 dist。
- 最终复核发现并修复 `initdb --no-locale` 新建的 SQL_ASCII 数据库执行含中文 migration 时的编码失败：所有应用连接统一设置 UTF-8 客户端编码，并增加连接回归。相同全新临时实例完整 DB 套件 43 passed、137 deselected；Python 离线 137 passed、43 deselected；Chromium 8/8 passed。Rust 补齐锁文件缓存后，fmt、154 passed/1 ignored 和 clippy 均通过。修复提交 `bfd621d`。

## H：交付与部署准备

状态：完成，阶段提交 `cad4d68296f660886dafa2494d287292f8450fbb`。

已交付 `DATA_CONTRACT.md`、`ACCEPTANCE.md`、`docs/deployment/INDEPENDENT_APP.md` 和 `deploy/` 单 Web/单 worker/nginx/数据库角色模板。根 README 已链接全部文档和实际命令。

正式源侧交接需求：stock_data agent 按 DATA_CONTRACT 实现/执行停写一致性 full 导出、正式源 schema 字段映射、三方回退恢复与兼容性验收；在此之前正式切换不就绪。

未执行：真实账户/来源/模型、生产身份权限、正式源 bundle、实际迁移、正式停写测量、部署、重启、推送和发布。代码迁移交付与本地验证完成，不宣称全部上线完成。
