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

## 许可与来源处理

`stock_data` 是本次功能代码的直接迁移来源，不据此改变 hl-trader 自身许可证。复制文件时保留其中已有版权、许可和来源声明；第三方包及资源的直接依赖记录进入根 `THIRD_PARTY.md`，精确版本分别由 Python/Node 锁文件固定。
