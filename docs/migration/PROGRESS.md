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

状态：代码与验证完成，待阶段提交。阶段起点 HEAD 为 B 提交 `3cf306d3d4854faffaae6307399faff183056987`。

已落地：最小 securities/watchlist/daily_quotes schema、统一代码与显式行情源映射、启用项列表、白名单元数据 PATCH、只查库的三市场行情、近 20 日断线走势、腾讯单证券未复权日线采集及 90/365 日有界回补。没有 create/delete/search/analyzer API 或入口，没有 stock_info、全市场扫描和视图刷新依赖。

实际验证：

- Python 离线套件：120 passed，22 deselected；覆盖代码/CIK/来源校验、类别股、非有限价格、缺前收、35% 跳变、4 日滞后、三市场收盘边界和无新增/删除路由。
- 在 `/tmp/hl-app-pg-C.letpNd` 临时 PostgreSQL 上运行 DB 套件：22 passed，120 deselected；覆盖三市场同码隔离、迁移与约束、维护态拒绝写入、白名单更新、只枚举启用项、停用前后重新核对、已知价格不被缺值覆盖。
- 前端完整单测 33 项（新增关注股表格 1 项）及生产构建通过；页面不含新增、移除、搜索或分析入口。
- 修复迁移验证发现的 PostgreSQL 兼容问题：不用 `isfinite(double precision)`，改为显式 NaN/Infinity 检查；迁移连接在成功和异常时均确定性关闭，避免 traceback 保留会话锁。修复后整套 DB 用例无悬挂通过。

未完成：US/A/HK 真实来源可达性、公告和 AI；真实网络采集未执行。C 阶段提交号在后续记录中补充。
