# 独立应用迁移来源清单

固定来源：`stock_data` HEAD `cc62434d126cdfc1dc40456f430d943f7d40d7cd`；目标基线：`hl-trader` HEAD `960f6d7f106559531421f4b31c431ea5420cc926`。源仓库未提交的 `docs/HL_TRADER_MIGRATION_BOUNDARY_PLAN.md` 是已确认边界输入，其余未提交文件不属于本次来源。

本清单随 A–H 实际迁移更新。源仓库仅只读，所有适配和新增代码进入 `hl-trader`。

| 能力 | 来源路径 | 目标路径 | 直接/隐含依赖与处理 |
| --- | --- | --- | --- |
| 应用配置与进程边界 | 无直接代码复制；执行合同第 4 节 | `backend/hl_app/config.py`、`db.py`、`api.py` | 新建显式 `HL_APP_` 配置、三类独立 DSN、单 Web 生命周期锁；排除旧 `.env`、旧全局 app 和自动 scheduler |
| Python 基础 | `requirements.txt`、`requirements-dev.txt`（版本与测试工具参考） | `backend/pyproject.toml`、`backend/requirements.lock` | 仅引入 FastAPI、uvicorn、psycopg2 与测试依赖；排除 akshare/pandas 等全市场或财务依赖，后续按需增加 |
| React 基础与 API client | `frontend/src/main.tsx`、`frontend/src/lib/api/client.ts`、Vite/TypeScript 配置 | `frontend/` | 保留 React/Vite/Query 习惯；client 改为同源 `/api/v1`，删除 `/api/us`、`/api/cn` 双服务器分流 |
| Hyperliquid 后端 | `web/routes/hyperliquid.py`、`web/services/hyperliquid_pairs_service.py`、`web/services/hyperliquid_service.py` | `backend/hl_app/routes/hyperliquid.py`、`services/hyperliquid*` | B 阶段迁入 v2；仅提取有界子进程工具，排除 v1 路由、旧配置和旧全局 app |
| Hyperliquid 前端 | `frontend/src/pages/hyperliquid-page.tsx`、`hyperliquid-pair-page.tsx`、`components/hyperliquid/`、对应类型与测试 | `frontend/src/pages/`、`components/hyperliquid/`、`lib/types/hyperliquid.ts` | B 阶段迁入；依赖 React Query、React Router、既有样式与共享 fixture，排除分析导航 |
| Hyperliquid 共享样本 | `tests/fixtures/hyperliquid/` 及生成说明 | `tests/fixtures/hyperliquid/` | B 阶段复制并脱敏/固定时间；Rust、Python、React 实读目标内样本，不引用旧目录 |
| 关注股与资讯 API | `web/routes/watchlist.py`；`web/services/watchlist_service.py`、`announcements_service.py`、`news_service.py` | `backend/hl_app/routes/`、`services/` | C/D 阶段按依赖迁入；旧 `db.Connection`、stock_info、财务和旧路由分流替换为目标数据层 |
| 研究与 AI | `web/services/research_reader.py`、`core/announcements/llm_enhance.py` 及对应测试 | `backend/hl_app/services/`、`collectors/` | E 阶段迁入；研究根、mmx 路径和模型显式配置，排除相邻路径、nvm/mmx 固定路径及旧行业表查询 |
| 行情 | `core/fetchers/daily_quote.py` 的单证券历史能力；`core/sync/daily_quote.py` 仅作耦合核对 | `backend/hl_app/collectors/quotes*` | C 阶段提取；只枚举启用关注证券，排除 stock_info 全市场扫描、实时全量行情和物化视图 |
| 公告与采集 | `core/announcements/{sync,store,sec_source,ir_source,media_source}.py`、`core/sec_client.py` | `backend/hl_app/collectors/`、`services/news*` | D 阶段迁入；替换身份查询，保留 SEC gate、revision、关联、gap/recovery，排除 companyfacts |
| Schema | `scripts/watchlist_tables.sql`、`watchlist_announcements.sql`、`sec_request_gate.sql` 及实际字段使用点 | `backend/hl_app/migrations/` | 按阶段形成有序最小迁移；不执行旧全量建表脚本，不迁财务表 |
| 页面与图表 | watchlist/news 页面、相关组件、布局、主题、`components/charts/echarts-wrapper.tsx` | `frontend/src/` | C/D 阶段迁入有效布局；排除搜索、新增/移除、analyzer、旧股票工作台 |
| 回归 | 执行合同第 3 节列出的 Python/前端测试 | `backend/tests/`、`frontend/src/**/*.test.*`、`frontend/e2e/` | 随模块迁移并适配鉴权、目标 fixture 和包名；参数化边界不能因改名或合并而丢失 |
| 数据迁移 | 无源侧代码修改；目标合同第 9 节 | `backend/hl_app/migration/`、`docs/migration/DATA_CONTRACT.md` | F 阶段新建 full/baseline/rollback 工具与临时源参考演练；正式源导出/恢复交 stock_data agent |

## B 阶段测试追溯

| 来源 | 目标与覆盖 |
| --- | --- |
| Rust `src/dashboard/snapshot.rs`：R1、R3、R6、R7、R8 合同所列测试 | Rust 原路径和测试名不变，154 项离线基线继续执行 |
| `tests/test_web/test_hyperliquid_pairs.py` | `backend/tests/test_hyperliquid_pairs.py`：目录、身份、缓存、R4 完整降级 DTO、R9 窗口与块语义；v1 正向测试替换为 v1 路由 404 |
| `tests/test_web/test_hyperliquid.py` 的 R5 有界子进程用例 | `backend/tests/test_process.py`：七个同名边界；不迁 v1 路由或其他 v1 测试 |
| `tests/test_web/test_hyperliquid_fixtures.py` | `backend/tests/test_hyperliquid_fixtures.py`：目标 fixture 实读、字段破坏、E1–E3；缺失 fixture 改为失败而非 skip |
| 前端 `identity.test.ts`、`queries.test.ts`、`format.test.ts`、`hyperliquid-pages.test.tsx` | 同名迁入 `frontend/src/components/hyperliquid/` 和 `frontend/src/pages/`，共 26 项，完整覆盖 R2 与页面降级 |
| 新增认证与进程合同 | `backend/tests/test_auth.py`、`test_auth_unit.py`、`test_hyperliquid_concurrency.py`、`test_lifecycle.py`；`frontend/src/pages/login-page.test.tsx` |

## C 阶段测试追溯

| 来源 | 目标与覆盖 |
| --- | --- |
| `web/services/watchlist_service.py` 的 `canonical_code`、行情摘要和元数据编辑 | `backend/hl_app/services/watchlist.py`：保留比较日期、缺值、35% 跳变和滞后语义；create/delete 不迁，新增 securities 与 enabled 边界 |
| `core/fetchers/daily_quote.py::fetch_tencent_hist` | `backend/hl_app/collectors/quotes.py`：单证券、固定腾讯 K 线、显式 quote_symbol/suffix、已收盘过滤；去除 pandas/akshare、stock_info、全市场 spot 与后缀发现依赖 |
| `core/sync/daily_quote.py`（耦合核对，不复制） | 目标采集只调用 `enabled_securities` 和 `collect_security`；不迁全市场循环、市值修正、视图刷新和旧 DB upsert |
| `tests/test_web/test_watchlist.py` 的行情/规范化用例 | `backend/tests/test_watchlist.py`：同名核心边界；旧登录/新增/删除和 AI 用例分别由 B/E 合同替代 |
| 新增 DB 与采集生命周期合同 | `backend/tests/test_watchlist_db.py`：三市场同码、约束、权限、维护态、停用和幂等写入 |
| `frontend/src/pages/watchlist-page.tsx` 与类型 | 目标同名页面保留筛选、表格、研究链接、编辑和断线 sparkline；删除搜索、新增/移除、分析链接和独立管理员登录 |

## D 阶段测试追溯

| 来源 | 目标与覆盖 |
| --- | --- |
| `scripts/watchlist_announcements.sql` | `backend/hl_app/migrations/004_announcements.sql`：保留来源/revision/check/gap/run/recovery；关联主键加入 market，并新增公司级 relation_reason |
| `scripts/sec_request_gate.sql` 与 `core/sec_client.py` | `005_sec_request_gate.sql`、`collectors/sec_client.py`：同一把 DB advisory lock、2 次/秒、共享退避和协调失败拒绝；HTTP 改为最小标准库实现，不迁旧配置/requests worker |
| `core/announcements/store.py` | `backend/hl_app/collectors/announcements.py`：自然键幂等、不可变 revision、首版关联、LLM 字段和恢复记录；写入关联前重新核对 enabled |
| `core/announcements/sec_source.py` | `collectors/sources.py::parse_sec/collect_source`：submissions 与旧文件分页，最多两页/轮，partial 不推进检查点，cursor 下轮恢复 |
| `core/announcements/ir_source.py` | `backend/hl_app/collectors/ir.py`：Nebius `__NEXT_DATA__` 与 Q4 JSON 适配；MU/WDC 等未验证来源仍只能由注册状态控制，不自动解除 blocked |
| `core/announcements/media_source.py` | `collectors/sources.py::parse_feed/parse_finnhub`：RSS/Atom 和 Finnhub 身份、时间、摘要与媒体名；凭据只读显式环境 |
| `web/services/announcements_service.py`、`news_service.py` | `backend/hl_app/services/announcements.py`、`news.py`：保留逻辑条目、48 小时折叠/置顶、未知时间、来源状态和 AI 降级；stock_info 改为本库 securities |
| `tests/test_daily_news_baseline.py` 及公告/SEC/IR/媒体测试 | `backend/tests/test_news.py`、`test_source_adapters.py`、`test_announcements_db.py`、`test_sec_client.py`：保留核心边界并新增真实多进程 gate 与分页恢复 |
| `frontend/src/pages/news-page.tsx`、`components/news/stock-relations.tsx` | 同名迁入；watchlist 增加公告摘要/详情，旧独立登录/采集提示改为目标配置 |

## 许可与来源处理

`stock_data` 是本次功能代码的直接迁移来源，不据此改变 hl-trader 自身许可证。复制文件时保留其中已有版权、许可和来源声明；第三方包及资源的直接依赖记录进入根 `THIRD_PARTY.md`，精确版本分别由 Python/Node 锁文件固定。
