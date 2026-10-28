# hl-trader 独立应用迁移执行方案（Kimi 执行合同）

日期：2026-10-28。

状态：实施方案已编写，尚未实施。用户将本文交给 Kimi 并要求按本文执行后，按 A→H 顺序连续完成本仓库的开发、离线验证和交付；每阶段报告结果，无须重复询问已确定的产品范围。真实部署和生产数据操作的授权另见第 2 节。

本次评审修订：正式切换改用停写后的完整包导入，不实现源到目标的增量合并；增加基线快照、目标回退差异导出及恢复演练；本轮限定单 Web 进程；认证采用持久化 auth_epoch；明确运行、迁移、测试三类数据库配置。以下正文、命令及验收条件均按这些决定执行。

| Kimi 评审项 | 本版处理及位置 |
| --- | --- |
| 1. 最终增量未定义 | 第 9.1–9.4、12.3 节改为停写后 full→空业务库；普通 import 拒绝增量/覆盖 |
| 2. 回退无反向交接 | 第 9.5–9.7、12.4 节增加注册基线、完整回退快照/差异、三方恢复及演练 |
| 3. 全局并发与多 Web 冲突 | 第 4、6、8.3–8.4 节限定单 Web/单采集 worker、生命周期锁及各命名池范围 |
| 4. token 轮换字段不闭合 | 第 5.1 节增加 auth_control、session epoch、原子轮换及旧配置拒绝 |
| 5. DSN 与账号入口不一致 | 第 4、9.4 节固定三类数据库环境配置，命令不传明文 DSN，缺失不回退 |
| 6. 历史验收术语未展开 | 第 11.2 节直接列出 R1–R9 的含义、正确结果与来源测试，第 11.3 节定义三类异常及 v2 迁入要求 |

## 1. 目标与最终产品

将 stock_data 中的 Hyperliquid、精简关注股、每日资讯迁入本仓库，形成独立 React + FastAPI + PostgreSQL + worker 应用。现有 Rust CLI 保留，Web 只调用其 v2 只读 dashboard 能力。新服务器不依赖 stock_data 的数据库、API、源代码目录、Python 环境或常驻进程。

本文完整承接已确认的边界，不要求执行者再打开旧方案才能理解范围。边界来源为 stock_data 的 `docs/HL_TRADER_MIGRATION_BOUNDARY_PLAN.md`（2026-10-28 用户确认版）。此前该文档中“下一片仅讨论骨架”的执行安排，由本次用户要求的完整执行方案替代；范围未扩大到交易、财务或正文提炼。

| 模块 | 必须交付 | 明确排除 |
| --- | --- | --- |
| Hyperliquid | 多 pair 总览、详情、方向摘要、仓位、盈亏、资金费、比价图、官方动态、已有解锁锚点；独立公开地址配置 | Web 下单、撤单、签名、自动再平衡；交易引擎改造 |
| 关注股 | 三市场列表、分组、理由、研究链接、元数据编辑、未复权日线及近 20 日图、公告阅读、AI 理由草稿 | 页面搜索、新增、移除；全市场证券目录和个股分析页 |
| 每日资讯 | 美股 SEC/IR/媒体、折叠、多公司关联、日期/公司/来源筛选、中文增强、现有重要性置顶与来源状态 | 新增正文抓取/提炼、付费来源调研、行业新闻体系 |
| 运行基础 | 登录、全部数据 API 鉴权、独立建库迁移、独立任务调度、完整包导入、基线/回退导出与演练 | 连接旧数据库作为日常依赖；复制 stock_data 全局 scheduler；通用增量同步/双向合并系统 |

产品入口为 `/login`、`/hyperliquid`、`/hyperliquid/pairs/:pairId`、`/watchlist`、`/news`；已登录默认进入 `/hyperliquid`。保留现有页面的有效布局与交互，不在迁移中整体重做视觉设计。删除所有 `/analyzer`、股票搜索和旧股票工作台的导航与深链。

关注股行情支持 `US`、`CN_HK`、`CN_A`；资讯先支持现有美股范围。非美股资讯明确显示“不支持/未配置”，不显示成“已检查，无新闻”。新部署只有一个同源入口，开发时允许 Vite 代理本机后端；生产不要求新增公网 API 端口。

## 2. 执行授权、仓库边界与工作方式

1. 当前仓库为 `/home/vinci/projects/hl-trader`。只能在本仓库修改代码、测试、脚本与构建配置。
2. `/home/vinci/projects/stock_data` 是只读迁移来源。可以阅读和向目标复制所需文件，不修改其代码、测试、脚本、配置、启动方式或数据库；需要源侧调整或正式导出时，说明需求交用户转给 stock_data agent。
3. 本方案交付开发与验证能力。部署、重启现有服务、推送远端、生产数据库导出/导入、启停真实采集及切换，均等待用户明确授权。任何真实交易、私钥读取或下单不属于迁移验证。
4. 默认测试使用虚构地址、固定时间、fixture、本机 mock 和明确指定的临时测试数据库。实际公开地址不写入测试、截图样例或提交。外部只读 smoke 与模型付费调用单独显式执行，不混入默认回归。
5. 保留其他任务已有的未提交文件。新建开发分支时使用 `codex/independent-app-migration`；按阶段本地提交，只提交本阶段文件，不运行 `git add .`。提交不等于推送或部署。
6. 用户已确定新增、移除证券通过脚本完成，但脚本具体设计后续再谈。本轮不自行设计证券管理 CLI/页面：交付数据库约束、领域层校验与测试 fixture，为后续脚本复用；历史导入工具属于本轮，不能借它恢复通用的网页新增/删除接口。
7. 普通技术细节在本文约束内自行处理。若发现现有实现与本文冲突，以用户确认范围和本文合同为准，记录差异；实质性产品变更才需反馈。外部材料缺失时先完成可独立验证的部分，不虚报完成外部验收。

## 3. 执行前基线与源文件清单

开始时记录 `pwd`、两个仓库 HEAD、`git status --short` 和本机 `STOCK_MARKETS`（只读取该配置项，不能输出整个 `.env`）。本文编写时：

- hl-trader HEAD：`960f6d7f106559531421f4b31c431ea5420cc926`。
- stock_data HEAD：`cc62434d126cdfc1dc40456f430d943f7d40d7cd`；边界方案是尚未提交的文档，按已确认内容读取，不丢弃。
- 当前 stock_data 为 `STOCK_MARKETS=US`。不得在其生产环境运行 A/HK 同步、DDL 或迁移。
- 已有验收基线见 [HYPERLIQUID_V2_ACCEPTANCE.md](HYPERLIQUID_V2_ACCEPTANCE.md)：Rust 154 通过、1 个既有 live smoke 忽略；上轮 Web 正式 Python 回归 171 项，前端 Hyperliquid 回归 26 项。数量仅供防遗漏，不能作为本次结果照抄。

若 HEAD 已变化，先检查与本次模块相关的差异，再固定新的来源版本。不得自动 reset 或覆盖新工作。

| 目标能力 | stock_data 中需阅读/按依赖迁移的来源 |
| --- | --- |
| Hyperliquid 后端 | `web/routes/hyperliquid.py`、`web/services/hyperliquid_pairs_service.py`；从 `hyperliquid_service.py` 提取有界子进程工具，不为此整体注册 v1 接口 |
| Hyperliquid 前端 | `frontend/src/pages/hyperliquid-page.tsx`、`hyperliquid-pair-page.tsx`、`components/hyperliquid/`、对应类型和测试 |
| 关注股/资讯 API | `web/routes/watchlist.py`；`web/services/watchlist_service.py`、`announcements_service.py`、`news_service.py`、`research_reader.py` |
| 采集和增强 | `core/announcements/{sync,store,sec_source,ir_source,media_source,llm_enhance}.py`、`core/sec_client.py`；身份模块仅复用规则，替换旧库查询 |
| 行情 | `core/fetchers/daily_quote.py` 的单证券历史能力及必要 HTTP 工具；阅读旧 `core/sync/daily_quote.py` 识别并拆除全市场、视图和 stock_info 依赖 |
| Schema | `scripts/watchlist_tables.sql`、`watchlist_announcements.sql`、`sec_request_gate.sql`；另核对这些文件之外实际使用的必需字段 |
| 前端基础 | watchlist/news 页面、相关组件、类型、布局、主题与 ECharts wrapper；从 API client 拆掉旧双服务器分流 |
| 回归 | `tests/test_web/test_hyperliquid*.py`、watchlist/news/announcements/research 测试，`tests/test_announcements_{sec,ir,media}.py`、`test_sec_client.py`、`test_llm_enhance.py`、`test_daily_news_baseline.py` 及相应前端测试 |

创建 `docs/migration/SOURCE_MAP.md`，逐项记录来源提交、源路径、目标路径、直接和隐含依赖、保留/改造/排除理由。复制时保留已有版权与许可声明，更新 `THIRD_PARTY.md` 的新增依赖/资源来源记录，不替项目擅自选择许可证。

特别检查：默认研究仓库相邻路径、固定 nvm/mmx 路径、旧 `.env` 自动加载、旧 `db.Connection`、stock_info/证券符号表查询、物化视图刷新、全市场扫描、旧 `/api/us` 与 `/api/cn`、测试从旧仓库引用 fixture。这些都必须消除运行依赖。

## 4. 目标目录、配置与进程合同

沿用 Rust 根目录；Python 使用专属包名，避免复制旧 `web`/`core` 根包引起误导入。采用下列目录；若细分文件不同，交付时更新 SOURCE_MAP，模块职责不变。

```text
Cargo.toml / Cargo.lock / src/       # 原 Rust CLI 保留
backend/
  pyproject.toml                    # Python 3.11+，最小依赖及测试配置
  requirements.lock                # 实际验证过的精确依赖锁定；记录生成方法
  hl_app/
    config.py / db.py / auth.py
    api.py / __main__.py
    routes/                        # session、hyperliquid、watchlist、news
    services/                      # 业务读取、元数据编辑、AI 草稿
    collectors/                    # quotes、sec、ir、media、llm
    workers/                       # 调度、整轮任务、一次性采集命令
    migrations/                    # 有序 SQL、schema_version 管理
    migration/                     # 完整包校验/导入、基线/回退导出、核对
  tests/                           # 单测、API、DB 与采集集成测试
frontend/
  package.json / package-lock.json  # 最小所需依赖，esbuild 等直接使用者显式声明
  src/ / e2e/                      # 页面、单测和浏览器用例
tests/fixtures/                    # 本仓库拥有的跨层固定样本
deploy/                            # 示例服务/代理配置，仅提供不启用
docs/migration/                    # 执行进展、来源清单、数据合同、验证证据
.env.example                       # 仅占位，不能含实际凭据和账户地址
```

- FastAPI 统一 `/api/v1`，由 app factory 显式注册本应用路由，不能导入 stock_data 的全局 app。React API client 同源访问；HTTP envelope 沿用 `{data: ...}`，错误用安全的 `detail`。
- PostgreSQL 使用新库及新凭据，迁移账号与运行账号分开；Web 和 worker 独立进程，不在 import、Web startup 或 GET 请求中启动 scheduler。先实现统一 schema 迁移，不引入第二种持久化数据库。
- 本轮每个目标应用库只运行一个 Web 服务进程，生产 workers 固定为 1，不使用滚动重叠启动或多个容器副本。Web 生命周期持有独立连接上的固定 PostgreSQL advisory lock；第二个 Web 实例即使更换端口也应启动失败，不能通过换实例 ID 绕开。锁连接失效时停止接受数据请求并终止采集子进程；进程管理器确认旧进程退出后才重启，不将该机制声称为跨主机高可用或无缝故障切换。
- 数据库连接使用目标项目配置和参数化 SQL，最小复用 psycopg2 风格；不迁入 stock_data 的通用全表 upsert 和财务逻辑。
- 配置统一使用 `HL_APP_` 前缀，仅读取显式环境或目标项目明确指定的配置文件。不向上搜索 `.env`，不支持回退到旧服务器配置。
- 定义并文档化：`DATABASE_URL`、`MIGRATION_DATABASE_URL`、`TEST_DATABASE_URL`、`AUTH_TOKEN`、会话 TTL/安全 cookie、可信 origin、`TRADER_BIN`、`PAIRS_CONFIG`、`RESEARCH_REPO_PATH`、`MMX_BIN`、`MMX_MODEL`、SEC User-Agent 与采集间隔、IR/媒体凭据、采集和增强开关；均加 `HL_APP_` 前缀。数据库配置入口与权限见下表，禁止相互回退。
- collector/scheduler 默认关闭，生产 URL/数据库由用户提供。非法配置启动时明确失败；合法的“未配置可选功能”必须出可见状态，不能偷偷使用历史路径或旧凭据。
- 临时 venv、node_modules、dist、日志、会话凭据、数据 bundle、真实配置、浏览器产物加入目标 `.gitignore`。保留锁文件与脱敏 fixture；嵌套 `.env.example` 也应可提交。

| 配置入口 | 使用者与权限合同 |
| --- | --- |
| `HL_APP_DATABASE_URL` | Web 和 worker 的运行账号；必要业务读写，不拥有 schema DDL、导入控制或认证版本管理权限 |
| `HL_APP_MIGRATION_DATABASE_URL` | `db migrate`、认证初始化/轮换、完整包 import 的 dry-run/apply、基线与回退导出工具；明确的目标迁移账号。导出只读业务数据，可写导入/导出控制账本 |
| `HL_APP_TEST_DATABASE_URL` | 测试 runner 明确指定的一次性 PostgreSQL；测试隔离 namespace 内模拟运行/迁移账号，不允许读取前两项作为缺省值 |

`inspect` 为纯文件校验，不要求数据库。其他命令缺少自身所需配置即失败，错误中只显示配置项名称，不输出 DSN。部署准备步骤明确让运维从秘密配置文件或进程环境注入 URL；不把凭据写在命令行、shell 历史、报告或提交中，Web/worker 进程不注入迁移账号。测试 harness 为子进程注入的运行/迁移连接只能由指定的一次性测试库派生。

建议稳定命令合同如下，执行者须实际实现这些入口并同步 README；不能只在文档中列出不存在的命令：

```sh
# 安装/锁依赖按 backend/README 或根 README 的精确步骤执行
# migrate/auth/import/export 的管理进程已注入 HL_APP_MIGRATION_DATABASE_URL
# 初始化/轮换另注入 HL_APP_AUTH_TOKEN；不在命令参数中传入秘密
.venv/bin/python -m hl_app.db migrate
.venv/bin/python -m hl_app.auth initialize # 仅首次初始化，已有认证记录则拒绝覆盖
.venv/bin/python -m hl_app.auth rotate     # 显式轮换，原子提升 auth_epoch
# Web/worker 进程仅注入 HL_APP_DATABASE_URL，不注入迁移账号
.venv/bin/python -m hl_app                 # 单 Web 进程，不启动采集
.venv/bin/python -m hl_app.workers --dry-run
.venv/bin/python -m hl_app.workers --once --kind quotes
.venv/bin/python -m hl_app.workers --once --kind announcements
.venv/bin/python -m hl_app.workers --run   # 仅在明确启用后启动常驻调度
.venv/bin/python -m hl_app.migration inspect --bundle /path/to/bundle
.venv/bin/python -m hl_app.migration maintenance --enable
.venv/bin/python -m hl_app.migration import --bundle /path/to/bundle --dry-run
.venv/bin/python -m hl_app.migration import --bundle /path/to/bundle --apply
.venv/bin/python -m hl_app.migration export --purpose baseline --output /path/to/baseline
.venv/bin/python -m hl_app.migration activate --baseline /path/to/baseline
.venv/bin/python -m hl_app.migration rollback-export --baseline /path/to/baseline --output /path/to/rollback
```

Python 包在目标 `.venv` 中安装，命令从仓库根即可运行，不靠临时 `PYTHONPATH` 指向旧仓库。示例展示不同进程身份下的命令合同，不是可整体粘贴到生产执行的脚本；认证轮换、基线/回退导出也不因此获得真实环境操作授权。`--once` 和 `--apply` 只描述能力，不能因此操作真实来源或生产库。

## 5. 登录与运行权限

### 5.1 本人登录

采用单用户 token 登录，不引入注册、多用户或外部身份服务。服务端从秘密配置读取足够强的 token，缺失时拒绝提供数据，不允许开发模式默认放行。

- `POST /api/v1/session/login` 校验 token，返回随机不透明会话 cookie；session 表存会话 token 的哈希、创建/到期时间、撤销状态及签发时的 `auth_epoch`，不存明文 token。明确配置 TTL，默认 7 天；不把登录 token 作为长期 cookie，不记入 URL/日志/前端存储。
- 新增单行 `auth_control`，存持久化的当前 `auth_epoch` 和高熵登录 token 的不可逆指纹（如 SHA-256，不存明文）。`HL_APP_AUTH_TOKEN` 至少具有 256 bit 随机熵，不能用普通短口令代替。`auth initialize` 仅在无记录时初始化，已有记录禁止自动重置。
- `GET /api/v1/session` 返回认证状态；`POST /api/v1/session/logout` 撤销会话、删除 cookie。每次登录/签发和受保护数据请求都以数据库 `auth_control` 为权威：会话 epoch 必须匹配，进程配置 token 的指纹也必须匹配；不依赖进程本地长期缓存。数据库不可用时拒绝认证，不放行缓存会话。
- 轮换必须用 `auth rotate`：从管理进程的秘密配置读取新 token，在一个事务中更换指纹、提升 epoch 并撤销旧会话；同一 token 的重复轮换不重复提升版本。会话签发与轮换对控制行加兼容的事务锁，不能先检查旧版本、在轮换后仍返回有效旧会话。运行账号可读控制行但不可提升/重写版本。
- 仅更改环境变量不算完成轮换：新 token 与数据库指纹不符时启动/登录明确失败。轮换后仍持旧 token 的进程不得签发会话或返回数据，也不得自动将数据库指纹改回旧值。按新的配置启动唯一 Web 后，旧 cookie 失效；重启不得重置 epoch。测试覆盖旧/新配置认证组件及轮换与登录并发，生产不为测试启动多个 Web 实例。
- cookie 为 HttpOnly、SameSite，生产 Secure；仅 localhost 开发配置可显式关闭 Secure。写请求要求可信 Origin 与 CSRF token/header；登录也防跨站提交。禁止泛域名 credential CORS。
- 除最小健康检查、登录/会话状态外，所有数据接口、目录、详情、读行情的 POST、AI 生成、元数据编辑均先鉴权。未登录 API 返回 401；页面跳转登录，不能仅依靠 React 路由隐藏数据。
- 对登录尝试设置有界限速，窗口计数存目标 PostgreSQL，重启不能清零规避；有界查询/分页在唯一 Web 进程内执行。AI 按第 8.3 节的 Web/worker 两个并发池限制。原有管理员 cookie 逻辑不能原样复制成只保护编辑的策略。

### 5.2 交易资源隔离

沿用已确认的部署要求：Web 使用与交易程序不同的 OS 身份，无 root/交易用户组/切换交易用户能力，不继承签名环境变量；交易私钥仅交易身份可读。Web 不得修改交易配置、状态、回执、锁、源码、二进制及可替换它们的父目录。同 UID 加 ACL 不算隔离。

Web 对 Rust 只构造固定参数数组：`[TRADER_BIN, '--config', PAIRS_CONFIG, 'dashboard', '--pair', pair_id, '--json']` 或固定 `--announcements` 形式。不经过 shell，不接受浏览器提交的命令、配置路径或 URL。pair_id 必须在服务端已验证目录内。

本机离线用假秘密和临时交易文件验证权限；最终 OS 身份和生产权限仅在部署授权后验证。单靠单元测试断言命令参数不能宣称生产隔离已通过。

## 6. Hyperliquid 迁入合同

迁入现有 v2 服务与前端，不迁入 v1 `status --json` 的旧 Web 路由；Rust 原有 v1 和交易 CLI 保留可用。

- 目录、pair 快照、公告路径分别为 `/api/v1/hyperliquid/pairs`、`/pairs/{id}/dashboard`、`/announcements`。保持目录/快照 schema_version、字段名和 envelope，不另造不兼容 DTO。
- pair 配置仍为 JSON `config_version=1`，revision 为原始字节 SHA-256；主网/测试网、DEX、有序两腿、归一化账户和稳定 pair ID 全部参与身份校验。
- 实际运行配置手工提供；新仓库示例用 null 地址。不得复制生产配置、旧机器绝对路径或 agent 私钥。
- 保留 30 秒缓存、按 revision/网络/账户/DEX/coins/ID 隔离、同 key 单飞及最多 2 个 Hyperliquid 采集子进程。缓存、单飞和信号量位于第 4 节唯一 Web 进程内；worker 不调用这条采集入口，因此本轮整个受管应用的该类子进程上限也是 2，不包含用户另行手工启动的 Rust CLI。30 秒为整个子进程期限，stdout/stderr 各 1 MiB；二进制非阻塞排空、单一 monotonic deadline，失败杀死回收并释放资源。不得通过增加 Web workers 扩大上限；多 Web 扩容需另定共享缓存/单飞/许可合同后再支持。
- 块级完整 DTO、固定 source keys、有限十进制字符串/null、合法时间、资金费 48 小时半开窗口、明细倒序及完整性状态继续严格校验；坏块单独降级，身份错误整份拒绝且不缓存。已知零与未知不混淆。
- 单腿盈亏、缺失费率、未持仓腿缺失、下架、部分历史、MA 预热和缺日断线沿用已修复语义。保持两腿“谁多谁空”摘要与中文诊断映射。
- 前端按 revision、ID、网络、DEX、有序 coins、账户验证完整身份。跨 key 不沿用旧快照，同 key 刷新可保留带时间的旧读数；先刷新目录再刷新快照；慢旧请求不覆盖新身份；保留有界目录重试，关闭自动轮询和聚焦刷新。
- 测试 fixture 进入本仓库 `tests/fixtures/hyperliquid/`，Python 与 React 实读同一组样本；保留来源说明和确定性生成/更新步骤，不依赖旧目录。测试地址必须虚构，日期固定。

## 7. 数据模型、证券维护与行情

### 7.1 最小目标 schema

实施 SQL 应写入有序 migrations，维护 schema version，在空库可一次初始化且重复执行安全；不执行 stock_data 的 `init_pg.sql`/`us_tables.sql` 等全量脚本。最低逻辑结构如下，物理表名可复用已有 watchlist 名称以减少无意义改写：

| 实体 | 关键约束/数据 |
| --- | --- |
| security | `(market, stock_code)` 唯一且标准化；名称、币种、显式行情源标识/后缀，必要的可选行业上下文；CIK 可空但合法时为标准形式；多证券可共用 CIK |
| watchlist_item | FK 到 security；每证券至多一条关系；分组、理由、研究链接、排序、enabled、时间。移除为停用，保留历史身份 |
| daily_quote | `(market, stock_code, trade_date)` 唯一；FK；有限正价格、来源、币种、`raw_unadjusted` 口径，采集时间；缺失不造零值 |
| announcement_source | 来源种类、发行人/查询配置、稳定入口、enabled、blocked/pending 状态、parser_version 等现有合同 |
| announcement / security relation | 来源记录逻辑 ID、revision、原文、时间精度/时区、首次发现、回补标识、证券关联、内容性质、中文增强、importance |
| check_state / gap / run / recovery | 来源级检查点、到期/覆盖范围、缺口、尝试与失败、恢复进度，按原合同迁入 |
| sec_request_gate | 新库共享限流、退避/冷却和异常恢复；所有 SEC 入口同一把协调锁 |
| schema/auth/session/operation control | 迁移版本、auth_control/auth_epoch、带 epoch 的会话、持久化登录限速、导入/基线/导出账本与身份映射、维护/切换状态；不能靠进程内字典记录正式操作结果 |

证券规范化必须在写入和查询两端统一，DB 约束防重复；US 大写，HK 五位代码，A 股六位代码，币种分别 USD/HKD/CNY。同一 ticker 跨市场不能串号。来源代码与标准代码分开，不从页面显示名猜测交易所后缀；美股类别股/历史符号有别名时显式映射，不把“点和横线替换”当通用身份解析。

CIK 是发行人身份，不给它加“每证券独占”约束；缺失/无效/映射冲突只阻塞该证券 SEC 能力，不阻塞列表、行情和其他来源。SEC 不依赖财务表解析或 companyfacts 成功。

### 7.2 新增/移除的本轮界限

前端和 API 不注册通用 watchlist create/delete；只允许对已存在且启用的关系 PATCH 分组、理由、研究链接等白名单元数据。不能通过 PATCH 修改 market、code、CIK、行情源或 enabled，变相恢复网页新增/移除。

本轮用领域层与测试数据验证未来脚本必须遵守的事务和幂等约束，不交付未讨论的管理 CLI。用户后续提供/确认脚本后复用同一校验；现有数据首次进入新库走第 9 节的正式导入工具。

移除一只证券后停止其行情和新增资讯关联，不删历史。当它与其他启用证券共用 CIK 时，SEC 共享来源继续；最后一个依赖证券停用后才停止共享采集。全局媒体来源同理：不能因移除一个公司禁用其他公司仍依赖的来源。采集进行中被停用时，写入前重新核对关联资格，不重新启用已移除项。

### 7.3 关注证券行情链路

仅枚举本库启用的关注证券；不得复制旧同步任务中遍历 stock_info、拉全市场实时行情、自动发现全部交易所后缀或刷新财务视图的流程。当前来源若需要 US 后缀，在最小资料内显式登记并校验。

- 重用可独立的单证券历史接口；默认历史回补从最近约 90 个自然日起取，必要时有限扩展到足够 20 个有效交易日。上市不足、停牌、来源缺口分别标注，不无限回补或造连续价格。
- 回补和增量都存已收盘日线，以市场本地交易日期/时区识别当日是否收盘；不要把盘中快照当完整日线。允许同一源对最近窗口重查，按同 key 幂等更新，合法缺值不覆盖已知值。
- 页面维持原未复权收盘价和较前条有效入库日线的展示口径；最新/前收无效时不跨缺值计算。近 20 日保留缺失，沿用超过 35% 跳变待核实、超过 4 自然日未更新的现有提示，不擅改成投资指标。
- API 明确 market/currency/source/price_basis、行情日期、前次日期、缺失/无效/滞后状态。读取只查库；来源失败不阻止关注项显示。
- 三市场网络可达性在新服务器单独验证，当前 US 机器不代表 A/HK 已验证；离线样本先覆盖三市场映射、日期、币种、类别股和缺行情。

## 8. 公告、资讯、AI 与调度

### 8.1 公告/资讯存储和读取

沿用现有来源注册、记录幂等、revision、证券关联和展示折叠合同。页面显示逻辑条目，不能复制“折叠后结果”作为数据库事实；同一媒体记录多公司关联不能用“最后一只公司”覆盖前者。保留公司级关联理由，不能无归属地拼接或虚构。

- SEC accession、IR 稳定 ID/规范 URL、媒体稳定键和来源身份继续可追溯；revision 是独立版本，修改不得覆盖全部历史。
- 保留 SEC 接收时间语义、原时区、日期/秒精度、未知时间和首次发现时间；日期未知不能补成午夜或抓取时刻冒充发布时间。
- 每日信息流按北京时间自然日，分页/筛选/折叠、48 小时折叠边界与未知时间处理，沿用当前已测规则。
- 重要性置顶沿用源 `news_service.pinned()` 当前规则和实际时间窗口，迁移前为此固化 fixture。不要根据旧注释臆测窗口，也不新增手工置顶/已读系统。
- 来源开关、blocked/pending、失败、缺口和 AI 可用性单独显示。“查询失败”不能化成空列表正常态。API 不能调用抓取器或现场模型增强；查询不写检查点。

### 8.2 SEC、IR 与媒体采集

- 复用 SEC gate 的跨进程 DB 锁、共享许可、共享退避、超时预算与异常恢复，迁入 `sec_request_gate` 的初始化 SQL。公告、身份确认、分页和重试统一经过 gate，沿用项目确定的最多 2 次/秒上限；不按证券、worker 或请求分发独立额度。协调不可用时拒绝请求，不能退回本地不限流。
- 不迁 companyfacts/财务任务，也不为初始化身份拉全市场目录；人工资料已提供 CIK 时直接验证使用。实际公网出口与旧系统共用代理/NAT 时，真实启用前解决共享出口协调；不能连接旧库充当独立运行前提。
- 保留固定回补起点、分页推进、防重复、预算耗尽不提前推进未完成项、gap/recovery、重启恢复等语义。导入检查点后不能将 blocked 来源自动改回 enabled。
- IR/媒体沿用独立 HTTP 层、来源预算和退避，不占 SEC gate，不绕过 403/验证码。MU/WDC 等未实测来源保留原状态；新服务器可达性变化只能显式验证后调整。
- 每轮以本库启用关注证券计算候选，来源注册与状态不依赖旧 stock_info。缺 CIK/凭据/来源配置记明确状态，其他来源继续；空关注列表应显示“没有采集目标”，不是声称外部来源已成功检查。

### 8.3 两类 AI 能力都保留

**关注理由生成**：保留现有 mmx 适配模式，由用户安装 AI 工具；迁入工具路径、模型、调用超时/输出上限、JSON 校验和并发限制的显式配置。请求只针对已登记关注证券，返回草稿供人工修改和保存，不自动覆盖已保存理由。

研究读取迁入现有 Git 固定快照、显式入口和章节摘录规则，不能执行研究仓库代码或遍历任意路径。研究根目录必须由管理员配置。有效仓库中“没有该证券条目”可按现有规则生成带 `[通用草稿]` 的结果；目录不可读、Git/配置损坏须明确报错，不静默当作没有研究。行业上下文从新库可选资料获取，缺失不访问旧 stock_info。

**资讯中文增强/重要性**：媒体生成中文标题、现有素材摘要和 high/normal；SEC/IR 只生成 importance，保持现有范围。增强是采集任务中的独立步骤，单条失败仍入原始条目，增强字段 null，记录 warning/失败计数并在来源状态体现；不把失败默认填 normal、伪造中文内容或阻塞整轮。沿用存量不自动回填和已有成功结果幂等复用规则。

凭据不提交、不返回 API，不复制当前机器 mmx 登录目录；生产身份是否可调用工具由部署 smoke 确认。默认测试用假子进程/mock，不真实调用模型。工具缺失时人工编辑仍可用，状态不能冒充 AI 已可用。

本轮 AI 并发明确分成两个命名池：唯一 Web 中“理由草稿”最多 1 个调用，唯一采集/增强 worker 中“资讯增强”最多 1 个调用；每池内超额请求明确返回忙或有界等待，同一项目同时最多 2 个受管模型调用。不声称覆盖用户手工调用或其他项目的模型额度。worker 的整生命周期独占规则见第 8.4 节，不能在另起一次性进程中绕开；后续多 Web/多增强 worker 扩容须先改为共享协调。

### 8.4 专属 worker

只注册 `quotes`、`announcements` 及本应用所需的增强步骤；明确本地配置、schedule anchor、到期时间、最久未检查优先、整轮锁、单来源预算。worker 可 `--dry-run` 列出任务/来源/开关，不能执行任何网络请求。

本轮采集/增强 worker 同样只允许运行一个实例，常驻和 `--once` 入口共用独立于 Web 的固定 PostgreSQL 生命周期锁，锁占用时明确拒绝/报告已有任务，不另开并行采集/增强进程。锁失效时停止新请求并终止模型子进程；进程管理器确认退出后才重启。保留来源整轮锁及 SEC gate，不能因部署为单 worker 删除跨进程限流和恢复机制。SEC gate 的多进程回归使用独立测试客户端，验证机制正确，不代表生产支持任意多个 worker。

Web 进程不注册任务；worker 中断后检查点、未完成 gap 和来源状态可恢复。不迁全市场财务、指数、行业、估值、筛选器、回测、旧物化视图刷新任务。日常运行不要求先运行旧 scheduler。

## 9. 完整包迁移、基线与回退导出合同

### 9.1 本轮交付和真实执行分开

本轮采用“预演 + 正式停写后完整包导入”，不实现源到目标的增量合并、CDC 或覆盖导入。预演库与正式目标业务库隔离，正式库不接收预演数据；正式包在源侧停止相关写入后导出，并导入空业务库。若预演表明停写窗口不可接受，报告测得耗时，由用户另定增量方案；不得擅自把完整包当增量覆盖已有库。

本轮实现并用虚构数据验证完整包校验/导入、基线导出、目标回退差异导出及核对。正式源侧导出、回退恢复工具由用户交 stock_data agent 按本合同实现/执行；本 agent 不改源仓库代码、不操作源生产库。正式 bundle 或源侧适配器暂未提供时，先完成目标代码、fixture 和本地演练，真实迁移/回退就绪状态仍不得标完成。

### 9.2 包格式、方向与完整性

在 `docs/migration/DATA_CONTRACT.md` 固定 bundle version=1，统一 UTF-8 JSONL + manifest，时间使用带明确时区的 ISO 字符串，未知为 JSON null。至少定义下列字段与类型：

| manifest 字段 | 合同 |
| --- | --- |
| `bundle_id` / `bundle_version` | 全局唯一批次 ID / 固定 1；同 ID 不允许不同内容 |
| `kind` / `direction` | 正向导入仅接受 `full` / `stock_data_to_hl`；目标基线为 `baseline` / `hl_snapshot`；回退快照为 `rollback_snapshot` / `hl_snapshot`；差异交接为 `rollback_delta` / `hl_to_stock_data` |
| `source_instance` / `source_commit` / `source_schema` | 数据来源身份和格式依据；不是秘密连接信息 |
| `scope` / `entities` | 固定市场/证券范围及依赖闭包、实体清单；必需实体即使 0 行也必须有文件和显式计数，不能以缺文件表示空数据 |
| `cutoff_at` / `snapshot_id` / `quiesced` | UTC 导出截止点、一致性快照标识、是否停写；正式 full、baseline、rollback_snapshot 必须停相关业务写入并来自一致性快照，不能逐表用更新时间拼接 |
| `files` | 文件路径、schema/编码版本、记录数及内容 SHA-256；禁止绝对路径、路径穿越和重复自然键 |
| `baseline_id` / `baseline_hash` | 正向 full 为 null；回退包必须绑定目标库注册的基线及其内容校验和，不以时间戳猜测基线 |

manifest 不包含登录、数据库、模型、私钥等凭据。普通正向 `import` 明确拒绝 incremental、rollback_delta 和错误方向；回退差异格式是定向恢复交接，不是本轮增加通用增量导入的入口。

### 9.3 实体范围、自然键及空库规则

- 含迁移范围内当前关注关系及所需最小证券资料，也包含历史公告关联所依赖的已移除证券身份；不附带全量 stock_info/财务。源侧物理移除的关注关系不伪造为启用关系，历史证券身份/关联仍保留。行情可不导出，但必须在 manifest 明确该实体被允许省略；目标重新回补。
- 公告/新闻需包含来源、逻辑记录及所有需保留 revision、证券关联、中文增强/importance，以及 check_state、gap、run、recovery。只导出当前页面可见条目不合格。
- 源自增 ID 仅是映射输入。来源键固定为规范化的 `(market, source_kind, issuer_identifier, entry_url)`；记录键为 `(source_key, source_record_id, revision)`；证券关联使用记录逻辑键及 `(market, stock_code)`，按现有首版关联/修订继承合同转换。check_state/recovery 使用发行人和来源键，gap/run 必须在 DATA_CONTRACT 中固定可追溯的键，不允许导出时随意重编。目标新增来源也必须有同样的自然键。
- 在事务中映射 source_id、announcement_id、FK，不能丢掉关联首版记录或通过源 ID 猜测目标关联。保留源 ID→自然键→目标 ID 账本，后续导出和源侧恢复可逆追溯。
- 不迁登录会话、auth_control、登录限速、交易配置/账本/锁、旧数据库连接和内核 advisory lock 状态。采集业务检查点保留；源运行中任务最终停止后，按既有语义记录中断/待恢复，不能伪装仍在目标运行或已成功。目标 SEC gate 单独初始化，不导入锁；共出口下源侧未过期退避不能因迁移绕过，启用前单独核对。
- 首次 full 只允许目标**业务实体为空**，允许 schema、认证和未启动的基础控制表已初始化。实体非空、已有其他成功批次或预演数据时拒绝导入；不提供 `--force`/自动清表/替换合并。改用另一明确准备的空目标库，不自动删除现库。
- 只有同 `bundle_id` 且校验和一致的已成功包可幂等重跑：验证导入后各业务自然键/规范值仍与该批次结果一致时返回已导入，不写数据。不一致则报告“导入后已有变化”并拒绝，不用旧包修复/覆盖新值；同 ID 内容变化、不同 full 包、缺实体/坏引用均拒绝。事务失败未提交业务数据和成功账本时允许修复后重新导入。

### 9.4 导入、维护状态与核对

`inspect` 不连接数据库，只检查 manifest、格式、校验和与依赖完整性。import 的 `--dry-run` 和 `--apply` 均只读取显式注入的 `HL_APP_MIGRATION_DATABASE_URL`，缺失即失败；不接受命令行明文 DSN，不回退到运行账号。dry-run 可查目标但不写；apply 先完整校验，再在维护独占锁内事务导入，不猜旧库或自动使用生产库。

目标持久化 operation control：`maintenance` / `active`，新库默认 maintenance。导入/基线/回退工具要求 maintenance，并使用所有目标业务写入口共同遵守的数据库锁协议：先阻止新业务写入、等待在途事务结束，再取得独占锁和一致性快照。worker 不发新请求，Web 元数据编辑/AI 结果保存拒绝写入；认证会话等非迁移域可以独立工作。未来用户维护脚本也必须遵守此协议，直接 SQL 维护只能在用户确认停写窗口进行。不能仅靠 manifest 的 `quiesced=true` 当作已经停写的证明。

`maintenance --enable` 和 `activate --baseline` 也只使用迁移数据库配置。前者先持久化关闭业务写入，再按明确期限排空在途任务/事务；超时保留 maintenance 并报告未排空，不能出具停写证明。后者核对第 9.5 节已注册基线、当前业务哈希和人工切换检查结果，事务记录 cutover baseline 并设置 active；不自动启动 scheduler。已 active 的同基线重复激活只返回当前登记结果，不能重新设定回退基线。启用后的基线不可被普通 export 替换；后续确需换基线必须另行制定维护流程。

必须输出：各实体输入/新增/复用/冲突/拒绝数量、逻辑条目和 revision 数、关联数量、来源状态、检查点与 gap/recovery 摘要；核对关键自然键集合，不能只比较总条数。对照样本包含多证券共 CIK、多公司媒体、修改版本、未知时间、blocked/pending 和未完成恢复。

默认导入不发网络请求、不调用模型、不启用 scheduler。失败不得推进导入账本为成功；业务数据和成功账本同事务回滚，报告安全可读。完整包可含自源侧导出时已更新的元数据、停用及最新来源/checkpoint/gap/recovery，它们是空库中的初始状态，不按 updated_at 比较或“取最大值”合并。

### 9.5 切换基线与目标完整快照

正式 full 导入核对后，目标仍处于 maintenance，运行 `export --purpose baseline`。使用 `HL_APP_MIGRATION_DATABASE_URL`，对业务域只读，导出规范化自然键/值、源目标字段映射、来源状态及检查点；将批次、目标实例、原 full 的 ID/hash、scope、snapshot 和业务内容哈希注册到目标 baseline ledger。schema/认证/锁不纳入业务快照。

所有状态/关联必需文件齐全；行情基线记录目标当时已有值或显式空实体，便于识别之后的新行情。文件先写私有临时目录，完成校验后原子发布，随后登记账本；登记失败的目录不算有效基线。原 full 的源侧冻结包与目标基线均完整保留，不能只保存哈希而丢掉前值。基线注册后仍不得写业务；只有用户授权切换后，通过管理入口将该基线标记为 cutover baseline，并在同一受控转换中设置 active，才允许业务写入。

数据改动发生在基线生成之后、active 之前时，拒绝启用并重新核对生成基线。新库首次启用前必须存在已核对基线；基线与其注册 ledger 是区分迁移前已有数据和切换后变化的依据，不能用 `created_at > cutoff_at` 代替。

### 9.6 目标回退差异导出

真实回退先按第 12.4 节将新侧置 maintenance，停采集/编辑、排空在途任务并备份。运行 `rollback-export --baseline ... --output ...`，只使用 `HL_APP_MIGRATION_DATABASE_URL`，在同一一致性快照中输出完整 `rollback_snapshot`、`rollback_delta` 和核对报告；业务只读，允许写导出账本。无需连接旧库，也不直接执行源侧恢复。

- 校验基线 ID/hash、目标实例、schema、原 full lineage 与 scope，必须匹配当前注册 cutover baseline。缺基线、伪造基线、旧环境基线或非 maintenance 均拒绝；导出截止点不得早于基线，同 ID 内容不可变。
- 差异通过“当前完整自然键集合/规范字段值”与“基线集合/值”比较，不依赖可能遗漏的 updated_at。每项携带实体、自然键、`op=insert|update|delete`、before/after 完整规范值及各自哈希：新增 before=null，修改两者齐全，删除 after=null。停用表现为 enabled 的 update；意外物理删除也必须显式导出 delete，不从快照中悄悄消失。
- 覆盖关注元数据/启用状态、最小资料、来源配置、全部 revision/证券关联、中文增强/importance、新行情以及业务采集状态。原有基线记录不得被重复伪装为新增；不相关旧库/财务数据、认证和交易资源不导出。
- 不以目标自增 ID 要求源侧原样写入，使用第 9.3 节自然键和映射。差异包保留两个快照的引用/哈希；丢失依赖实体、缺 before 值或未知 schema 时拒绝生成“可恢复”报告。
- 相同维护状态/快照重复导出应得到相同业务内容哈希；新导出使用独立批次，不覆盖旧产物。包和报告均为私有数据，使用受限权限且不提交 Git。

### 9.7 源侧恢复合同与演练

源侧恢复由 stock_data agent 实现/执行，目标仓库交付字段映射、差异包和测试参考适配，不新增可直连旧生产库的写入命令。必须在授权切换前验证源 schema 能表达恢复结果；如 enabled、最小资料或来源字段需要转换，在 DATA_CONTRACT 明确到字段，源侧无法表示时先解决，不能静默丢字段。源侧仍采用物理移除关注关系时，将停用映射为移除当前关系但保留证券/新闻历史，不删除历史关联。

| 数据类别 | 恢复规则 |
| --- | --- |
| 关注理由、分组、链接、启用及来源配置 | 以冻结 full 的源侧规范视图作 before，目标变化作 after；源当前值等于 before 才应用变化，已等于 after 则幂等复用，两者都不同报告冲突。不能按“最后更新时间较新”覆盖 |
| 新公告/新闻/revision/关联/行情 | 按自然键/FK 映射补入；相同键和值复用，不同内容拒绝自动覆盖并核对。物理删除单独审核，不默认级联清理 |
| checkpoint、gap、recovery、运行状态 | 同时保留两侧状态为证据，不盲目取最新 cursor/最大日期。以冻结的源侧已验证覆盖点为保守恢复起点，保持未解决 gap，并为到回退截止点未证明覆盖的区间登记 gap/recovery 后幂等重采；新来源从其明确回补起点开始 |
| 来源健康与运行控制 | 配置启用变化按业务三方核对；blocked/pending、冷却和中断任务保守恢复，不因目标曾成功就自动清掉旧 gap/解除 blocked，也不把目标运行中任务标成功 |

checkpoint 只有在源侧验证所有对应记录/关联完整、覆盖证明有效后才允许推进；无法证明则保留 gap 而不是追求表面“最新”。恢复不得自动调用模型重写已验证增强结果。旧侧未迁移的财务等字段不在恢复写入范围。

本轮在临时源 schema + 目标库上演练：full 导入→基线→active 后修改理由/停用、新增 revision/多证券关联/增强与行情、推进检查点→maintenance→回退导出→参考源恢复。验证每一业务变化有已恢复/幂等复用/明确待人工处理的结果，目标快照保留全部原值，恢复后的逻辑键、值、revision 和关联与预期一致，缺口不被擦除；另测源侧并发改动冲突及重复恢复。正式源侧 agent 的工具和生产兼容性演练未通过前，不得将真实回退标记可用或批准正式切换。

## 10. A–H 实施顺序与阶段完成条件

阶段之间按依赖推进，不把“已阅读、已出目录、旧测试通过”当作实现完成。每阶段将实际命令、工作目录、HEAD、结果及未完成项记入 `docs/migration/PROGRESS.md`，完成后提交相应代码/测试/文档。出现可修复失败先修复再继续；不可用外部条件如实记录，不无限等待或跳过硬性代码检查。

| 阶段 | 实现内容 | 必须验证的完成条件 |
| --- | --- | --- |
| A 基线与骨架 | SOURCE_MAP；Python 包、最小依赖/配置/DB migration；React 基础及同源 client；README 与忽略规则 | 独立安装、构建，空目标 DB 建库迁移；运行/迁移/测试配置无回退，权限分开；唯一 Web 生命周期锁有效；不自动导入旧配置或启动任务；旧 Rust 离线基线可跑 |
| B 登录与 Hyperliquid | 本人登录、auth_control/session epoch、全 API 鉴权、v2 服务/页面和共享 fixture；移除分析链接 | 未登录读接口 401；轮换/旧配置/并发签发/重启验证；持久化登录限速；第 11.3 节 E1 持仓 coin 类型错误、E2 收付 coin 类型错误、E3 diagnostics 类型错误均仅降级对应块；第 11.2 节身份切换、慢请求与刷新顺序通过；全应用该入口最多 2 个子进程；无交易调用 |
| C 关注股与行情 | security/watchlist/quote schema；列表、白名单 PATCH、单证券采集/增量和三市场图表；去网页新增/移除 | 三市场样本与跨市场同码不串；空/缺/无效/跳变/滞后状态正确；启用目标之外不采集；无旧搜索依赖 |
| D 公告/资讯 | 全相关 schema、来源身份适配、SEC gate、SEC/IR/媒体采集、信息流/折叠/置顶 | 新库单独可用；来源失败与 gap 可见；多证券共 CIK 及多公司媒体不丢关联；限流真实多进程集成通过 |
| E AI 与调度 | 研究 reader、AI 理由草稿、资讯增强、唯一专属 worker 和 dry-run | 模型失败不覆盖理由/不阻断原文入库；研究损坏明确失败；Web 不抓新闻/不调模型增强；两个 AI 命名池各最多 1 个；常驻/一次性 worker 不重叠；只运行本应用任务 |
| F 数据导入与回退导出 | 固定 full/baseline/rollback 格式；inspect/import/maintenance/activate/export/rollback-export；账本与核对；临时源恢复参考演练 | 空业务库 full 导入、幂等重跑/已变化拒绝、非空库/增量包拒绝、坏 FK/缺文件回滚、基线绑定和维护排空、回退完整快照/差异导出与参考恢复通过；没有旧生产库连接或自动启用采集 |
| G 独立性与交互验收 | 全层回归、浏览器测试、干净环境验证及权限测试材料 | 不挂载旧仓库、不提供旧凭据、屏蔽旧 API 仍能运行 mock 采集与读取；桌面/移动测试覆盖关键流程 |
| H 交付与部署准备 | 单 Web/单采集 worker 的服务/代理示例、完整包停写切换与回退清单、停写耗时测量、源侧交接和证据 | 可按文档在空环境复现；有临时源回退演练；源侧正式恢复适配未就绪时标记切换阻碍；各类验收状态分开；不自动部署或清理旧应用 |

若外部安装条件阻止真实浏览器或 PostgreSQL 集成，不得以 SSR/Mock 代替并标完成；先交付可运行用例和精确阻碍，继续完成不依赖该条件的工作，最终明确相应未通过项。

## 11. 验证矩阵与标准命令

### 11.1 必测矩阵

| 领域 | 关键用例 |
| --- | --- |
| 登录 | 未配置关闭数据；登录失败/到期/注销；auth_epoch 初始化/轮换/幂等/重启不重置、旧 cookie 失效、旧配置不能签发或读数据、轮换与签发竞争；限速重启不清零；跨站写拒绝；每一个数据路由都要求登录 |
| 进程/并发范围 | 第二个 Web/采集 worker 启动失败、不同端口也不绕过；锁失效拒绝新数据并结束子进程、重启无重叠；多请求下 Hyperliquid 单飞且最多 2 个，AI Web/worker 各最多 1 个 |
| Hyperliquid | 第 11.2 节逐项定义的 R1–R9 及第 11.3 节 E1–E3；完整 DTO；共享 fixture 实读、缺 fixture 应失败；同地址/null 账户切 pair、revision/网络/两腿变更、慢旧请求、目录失败 |
| 子进程 | flush 后 sleep、无输出、双管道压力、超限、EOF 后不退出、多字节输出；期限内杀死回收，许可恢复 |
| 证券/行情 | 标准代码/重复、币种和日期、类别股来源映射、缺 CIK 不妨碍行情、非正/缺失/非有限价格、已收盘日线、只选启用关注项 |
| 生命周期 | 共 CIK 移除其一继续、全部停用停采、历史关联保留、采集中停用不重新启用或新增关联 |
| 资讯 | 同源幂等/revision、多公司关联理由、折叠边界/来源数、未知时间、北京时间日期、重要性置顶窗口、blocked/pending/gap、不支持的市场 |
| 采集/恢复 | SEC 多进程共享限流和冷却、协调故障、分页不推进、预算耗尽、进程中断再启动、来源失败隔离；不重复启动整轮 |
| AI | mmx 缺失/超时/输出超限/JSON 错/字段错；研究安全路径/坏 Git/无条目；草稿不自动保存；原新闻与已有增强不受失败覆盖 |
| DB/配置与完整包 | 真 PostgreSQL 临时库迁移、约束/FK、事务；三种 URL 缺失不回退、账号权限不越界、DSN 不进日志；空业务库 full 成功，同包幂等，同 ID 不同哈希/已变化目标/非空目标/不同包/incremental 拒绝；缺文件/坏引用回滚，来源/checkpoint/gap 核对 |
| 基线/回退 | 无基线/错实例/错 lineage/错 schema/错 scope 拒绝；maintenance 阻止新写并排空、超时不出停写证明；activate 绑定基线且不自动采集；回退导出 insert/update/delete 前后值和完整快照，含停用/revision/多公司关联/增强/行情；参考恢复幂等、源侧三方冲突拒绝、checkpoint 保守恢复并留 gap；未通过源侧正式兼容性演练不得切换 |
| 独立运行 | 全新安装、无旧目录/凭据/API、只包含目标仓库资源；读取不触发采集；worker 无财务/全市场任务 |
| 浏览器 | 登录/退出、总览/详情切换、刷新失败、空态/缺行情/坏解锁块、关注元数据编辑、AI 草稿修改保存、资讯筛选分页置顶；页面无新增/移除/分析入口 |

### 11.2 R1–R9 的含义、断言与测试来源

R1–R9 是下表九项既有缺陷的稳定追溯编号，不代表执行者需要另读历史方案。下列正确结果是迁入后必须保持的验收合同。Rust 测试留在本仓库；Python/React 来源测试需迁到目标目录并适配新包和鉴权，不能为了运行旧测试注册已排除的 v1 Web 接口。

来源路径约定：

- **Rust**：本仓库 `src/dashboard/snapshot.rs` 的 `tests` 模块，完整名称前缀为 `dashboard::snapshot::tests::`。
- **Pairs**：stock_data `tests/test_web/test_hyperliquid_pairs.py`。
- **Process**：stock_data `tests/test_web/test_hyperliquid.py`；R5 仅提取其中有界子进程测试，不需要迁其 v1 路由。
- **Fixture**：stock_data `tests/test_web/test_hyperliquid_fixtures.py`，实际读取 JSON 样本并做字段破坏。
- **Identity / Queries / Pages**：stock_data `frontend/src/components/hyperliquid/identity.test.ts`、`queries.test.ts`、`frontend/src/pages/hyperliquid-pages.test.tsx`。

| 编号与原缺陷 | 必须保持的正确结果 | 现有测试定位 |
| --- | --- | --- |
| R1：缺失费率补零，错误生成日收付估算 | 缺失/null/非法费率保持 null，有持仓腿所需费率或价格未知时估算 null；合法零/负费率保留，未持仓腿缺值不阻断另一腿估算 | Rust：`acceptance_missing_rate_must_not_be_zero`、`market_fields_preserve_invalid_zero_negative_and_delisted_states`、`missing_rate_on_unpositioned_leg_does_not_block_daily_estimate` |
| R2：切换 pair/账户/配置时混入旧身份快照 | revision、ID、网络、DEX、有序 coins、归一化地址全部匹配才显示；跨 key 不占用旧值，同 key refetch 可保留带时间的值；慢旧请求不覆盖新身份，目录失败不刷新 pair，成功后按新 revision 刷新 | Identity 的 9 项字段比较；Queries：`QueryObserver 跨 revision/pair 不保留旧快照，同 key 刷新保留旧值`、`慢旧请求迟到不会覆盖新 pair 的 QueryObserver 数据`、`手动刷新在目录失败时不请求任何 pair 快照`、`手动刷新按目录返回的新 revision 获取快照`；Pages：`详情页标题切换后，即使两组地址都为 null 也不渲染另一 pair 的旧读数`、`总览卡片只显示当前目录身份，串入的快照读数被隐藏` |
| R3：资金费历史完全失败被当成已知零 | 完整且无本 pair 记录为 `0`/`[]`/complete=true；不完整且无本 pair 有效记录为 null/null/false；已有部分记录允许部分合计（含相抵为零），仍 complete=false | Rust：`acceptance_failed_history_must_not_be_zero`、`funding_history_distinguishes_empty_partial_other_and_zero_sum` |
| R4：降级 DTO 缺字段使详情页崩溃 | 每种坏块返回完整 unavailable DTO、固定 source keys 和诊断，其他块保留；解锁坏块不访问未检查的 anchors.length，不显示正常空态；不适用的解锁块保持 null | Pairs：`test_degraded_block_has_full_dto_and_fixed_sources`、`test_disabled_pair_forces_unlocks_null_even_when_rust_sends_block`；Fixture：`test_fixture_field_destruction_degrades_block`；Pages：`解锁块不可用或 anchors 损坏时页面保留其他块且不显示正常空态` |
| R5：定长阻塞读使子进程期限失效 | 二进制非阻塞双管道读取，整个读/等待共用 deadline；超时/超限杀死回收；双管道无死锁、多字节不误切、EOF 后未退出仍受期限约束 | Process：`test_flushed_output_then_sleep_obeys_deadline`、`test_both_pipes_drained_under_pressure`、`test_multibyte_chars_split_across_chunks`、`test_no_output_child`、`test_oversized_output_kills_child`、`test_child_closing_output_but_not_exiting_is_killed`、`test_silent_child_timeout_reaps` |
| R6：单腿已知盈亏没有进入合计 | 完整仓位集合查询成功后，单腿取该腿盈亏、双腿求和、确认空仓为零；未知不能补零，其他币种不混入 | Rust：`acceptance_single_leg_pnl_must_be_preserved`、`second_single_leg_negative_pnl_must_be_preserved`、`position_states_flat_single_same_and_filtering` |
| R7：历史已预热，90 日展示前 29 天仍丢 MA | 先用完整已收盘历史计算 30 日 MA，再裁剪最近 90 日；121 个连续日恒定比价 8 时首尾 MA 都为 8；29/30 日边界正确、缺日继续断线 | Rust：`acceptance_ma_warmup_must_cover_first_display_day`、`ma_boundaries_and_warmup_gap_are_preserved`、`ratio_gap_breaks_ma_line` |
| R8：忽略 isDelisted，已下架合约被当正常行情 | 区分市场缺失、活跃、明确下架；下架行情不提供正常实时价格/比价/估算并有诊断，已知真实持仓及盈亏保留 | Rust：`acceptance_delisted_market_must_not_be_listed`、`market_fields_preserve_invalid_zero_negative_and_delisted_states` |
| R9：资金费窗口与区块语义校验不完整 | start<end、恰好 48 小时、明细属于半开窗口且倒序最多 3 条；完整性/收付 null 与状态一致；固定来源键、合法时间/字符串诊断、有限数字与列表类型；非法块完整降级 | Pairs：`test_invalid_block_semantics_degraded`；Fixture：`test_fixture_field_destruction_degrades_block`；配合 R4 的完整 DTO 用例检查降级结果 |

以上列出的是最低追溯集，不是删除其他现有回归的许可。迁入后在 SOURCE_MAP 和 ACCEPTANCE 中记录来源测试→目标测试的准确路径/名称；保留参数化边界，不能只按函数数量判断覆盖。

### 11.3 三类异常字段（E1–E3）与 HTTP 验收

这里的“三类异常”专指下列三种字段破坏，不能泛指超时、非法 JSON 或其他错误。每项都使用有效 pair 身份及有其他正常区块的快照，在完成登录后请求目标 v2 API：

| 编号 | 输入与现有测试名称 | 目标行为 |
| --- | --- | --- |
| E1：持仓行 coin 非字符串 | 将 `blocks.positions.items[0].coin` 改为 `[]`；Fixture 的 `test_bad_coin_degrades_only_affected_block_over_http`，参数 `block_name='positions', rows_key='items'` | HTTP 200，仅 positions 变为完整 unavailable DTO；先校验字符串再判断集合归属，不抛 unhashable TypeError |
| E2：收付明细 coin 非字符串 | 将 `blocks.funding.recent_payments[0].coin` 改为 `[]`；同一 Fixture 用例，参数 `block_name='funding', rows_key='recent_payments'` | HTTP 200，仅 funding 变为完整 unavailable DTO；其他已验证区块不变 |
| E3：diagnostics 不是字符串数组 | 将一个适用块的 diagnostics 分别改为 `1`、`true`、`{}`、`[42]`；源 Process 文件中的历史名称为 `test_v1_invalid_diagnostics_degrades_only_affected_block_over_http` | 在目标 v2 接口新增或适配为 `test_invalid_diagnostics_degrades_only_affected_v2_block_over_http`；HTTP 200，仅对应块完整降级；不得迭代任意标量、字符串化错误类型或把空对象当正常空数组 |

E3 的 v1 名称只用于定位原始复现，**目标不迁入 v1 Web 路由**；迁入其输入与“单块失败隔离”的断言，并验证目标 v2 wrapper。该目标测试名是本轮要求实现的名称，不表示目前已存在。

E1–E3 都必须断言：受影响块的全部数据字段与固定来源键存在、状态/诊断明确、未验证数值已清空，并将其他区块与破坏前 fixture 深比较为相同；单看 HTTP 200 不够。另用未登录请求确认鉴权仍返回 401，不能为异常测试绕过全站鉴权。

### 11.4 测试运行与环境

DB 集成必须使用明确的 `HL_APP_TEST_DATABASE_URL` 指向一次性数据库；测试创建唯一 schema/数据库，限制清理到自身命名空间，不接受生产 DSN fallback。默认离线单测不连 DB，DB 套件必须另行显式运行且不能被最终统计里的 skip 隐藏。SEC 网络用本地 mock，真实多进程共享同一临时 PostgreSQL gate，不能只 mock 一个 Python 字典。

为 npm 定义聚合 `test:unit`，包括已迁入的 Hyperliquid（当前来源 26 项）和资讯/关注股/登录行为；`test:e2e` 用真实 Chromium 驱动本地目标应用，至少桌面 1440×900 和移动 390×844。静态 renderToString 继续保留但不算真实交互。浏览器数据用目标测试 seed，不连旧服务。截图与报告不提交个人数据。

执行者须让下列命令真实可运行，说明安装步骤；新增功能不能只提供源码没有运行入口：

```sh
# Rust：在仓库根目录
cargo fmt --check
cargo test --offline
cargo clippy --offline --all-targets -- -D warnings

# Python：目标包已安装进 .venv，pytest 配置在 backend/ 中
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q
# 显式提供一次性测试库后，运行真实 DB/跨进程集成
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m db -q

# 前端：在 frontend/ 中
npm ci
npm run test:unit
npm run build
npm run test:e2e
```

首次锁文件安装可需要网络；测试本身默认离线。Rust mock 需要本机监听权限，Node runner 在受限环境可能仅报告文件级通过，必须确认实际 case 被执行。不得把本机监听权限失败误认业务失败，也不得改测试吞掉失败。记录实际 test 数、失败数、skip/ignore 原因，不宣称代码没有运行过的命令已通过。

## 12. 独立环境、部署准备、切换与回退

本轮准备部署说明和示例，不执行真实操作。新服务器地址、数据库、公开账户配置、认证 token、AI 工具/凭据和研究资料由用户提供；不得把它们写入仓库。

### 12.1 独立环境验收

使用仅包含目标仓库的干净目录/容器和新的临时 DB 安装并运行。容器不是生产部署的强制选项，但验证环境必须证明旧目录不可访问、旧 API 不可访问、没有旧 DB 配置。构建依赖可提前装好，运行测试只访问本地 mock；完成一次采集写库、Web 登录及页面读取。提供启动命令、被禁依赖清单及结果。

代理示例使用一个站点：`/api/v1` 到后端，其余 SPA fallback；生产静态资源从目标 dist 发布。生产仅一个 Web 和一个采集/增强 worker，各持生命周期锁，服务分开；禁止多 Web workers/副本、滚动重叠或同时启动手工 worker。明确最小身份/文件权限和各进程配置：运行服务只注入运行 DSN，管理命令另行注入迁移 DSN；DB 不因应用启动自动重建。提供迁移/备份/恢复检查命令，但不运行。

### 12.2 获明确授权后才做的真实验收

逐项单独记录：Hyperliquid 两组公开地址只读对照；US/A/HK 各一证券来源可达性、代码、币种、已收盘日期与回补；SEC 真实出口及旧系统共出口处理；IR/媒体来源状态；AI 工具和研究目录；HTTPS 登录、Cookie/CSRF、多设备页面交互；真实 Web 身份对假秘密/临时交易资源的权限测试。

当前 [Hyperliquid 验收记录](HYPERLIQUID_V2_ACCEPTANCE.md) 中 2026-10-19 的旧环境主网对照可以保留为历史证据，不能替代新服务器、新配置、新 Web 的验证。

### 12.3 正式切换

1. 用户确认目标环境、迁移范围、备份位置和停写窗口；源侧操作交对应 agent。先在独立预演库完整导入/核对/基线/回退演练，测量停写后导出、传输、导入与核对耗时；源侧恢复工具及字段兼容性演练须已通过。窗口不可接受则停止准备，不自动改用增量合并。
2. 目标代码/DB schema/页面已验证，正式目标业务库为空且 maintenance，采集关闭。预演数据不复制进正式业务库；源侧和目标的完整备份、版本/范围清单及恢复路径可用。
3. 停旧侧**本次迁移模块**的采集和编辑写入，排空在途任务或记录可恢复中断，记录一致性截止点，导出最终 full 包。旧库相关写入一直保持停止；stock_data 财务等无关任务继续。不能对相关表仍可变的旧库逐表随意导出。
4. inspect→dry-run→apply 将该 full 包导入正式空业务库；核对自然键、规范字段值、revision/关联、来源与 checkpoint/gap/recovery，导出并注册目标基线，保留源冻结 full。禁止导入到预演库或已有业务数据的库，也不执行所谓最终增量。
5. 确认新侧权限、页面、真实来源准备和回退证据均满足；用户授权后用 activate 登记 cutover baseline 并开放目标业务写入，再开启目标对应任务和新入口。启用后所做真实 smoke/采集也算切换后变化，必须由回退导出覆盖。
6. 旧入口保留可回退，但不得继续接收会产生分歧的写入；正式切换未完成时新侧保持 maintenance。全流程不得出现双侧活跃业务写入；删除旧页面/表由用户另行确认。

### 12.4 回退

1. 获授权后将目标置 maintenance，停止新侧采集和编辑/维护脚本，排空在途请求，记录回退截止点并作完整备份。旧侧对应写入仍保持冻结；不要先恢复旧侧采集再处理差异。
2. 对注册的 cutover baseline 执行 rollback-export，生成第 9.6 节完整快照、带 before/after 的差异包及核对报告；校验哈希和 scope 后交源侧 agent。没有有效基线/包或仍有未排空写入时停止回退流程，不声称无数据变化。
3. 源侧先备份，在其维护窗口内按第 9.7 节三方核对恢复业务变化、自然键/FK 映射、增强及关联。检查点保守恢复，未证明覆盖的区间留 gap/recovery 并安排幂等重采。冲突和不可表示字段逐项解决，不用“较新时间戳”强制覆盖。
4. 核对所有目标侧变化均已恢复、幂等复用或有用户明确接受的处理决定；逻辑键/规范值/revision/关联/来源状态与预期一致，新增记录不丢、停用不复活，gap 不擦除。保留完整目标快照供追溯。
5. 核对通过后才恢复旧侧对应写入和访问，新侧保持停止。任一关键变化无法恢复且未获明确处置决定时，不宣布回退完成。

仅回切前端/恢复旧 dist 不能作为有新增数据后的完整回退。不得把交易账本、私钥、锁文件纳入应用回退覆盖；目标导出工具不自动获得写旧库或启停旧服务的权限。

## 13. 交付文件与完成判定

至少交付并在根 README 链接：

- `docs/migration/SOURCE_MAP.md`：实际来源版本、依赖闭包、目标映射及来源许可。
- `docs/migration/DATA_CONTRACT.md`：DDL/字段、full/baseline/rollback 格式、维护/激活、自然键与前后值、源侧恢复映射和核对合同；不留未定义的增量合并步骤。
- `docs/migration/PROGRESS.md`：阶段、实现/验证命令及提交、未完成项。
- `docs/migration/ACCEPTANCE.md`：测试矩阵与证据、桌面/移动截图路径、独立环境、临时源回退演练/正式源侧兼容性、停写窗口测量、真实来源与权限状态。
- `docs/deployment/INDEPENDENT_APP.md`：空环境安装、配置、建库、Web/worker、代理、备份、切换与回退；命令实际可用。
- 目标内可复现 fixture、Python/Node 锁依赖、测试入口、脱敏配置示例和仅模板形式的部署配置。

最后报告必须区分：**代码实现、离线回归、真实 DB 集成、真实浏览器、真实来源/账户、生产身份权限、实际数据迁移、正式发布**。没有证据的项写未执行/阻碍，不把构建成功当上线通过。

“代码迁移交付”要求 A–H 的本轮范围代码、mock/fixture 和本地验证已落实，所有已知代码问题修复，并提供实际本地提交号/测试结果。需要用户资料、源侧 agent 或部署授权的外部验收可以保留，但必须列明影响；此时不得宣称全部迁移上线完成。

## 14. 可直接交给 Kimi 的执行指令

> 在 `/home/vinci/projects/hl-trader` 按本方案 A→H 连续实施并自验，不再停留在熟悉仓库或输出另一个泛化方案。先读本仓库 AGENTS.md，固定两个仓库来源版本并建 SOURCE_MAP；stock_data 仅作只读来源，不修改其代码、测试、脚本或配置。将需要的前后端、最小数据层、采集、AI 和回归真正迁入目标仓库，保留 Rust 原有交易功能及只读边界。按单 Web/单采集 worker、持久化 auth_epoch、三类数据库配置和第 9 节完整包/基线/回退导出合同实现；不实现未经定义的增量覆盖或多 Web 扩容。去除网页新增/移除和分析入口，新增/移除管理脚本的具体设计留给后续，不能自行扩张范围。阶段内修复测试失败，按阶段提交代码和文档，持续报告进展，直至本轮可完成的交付全部完成。默认不连接真实账户/来源、不调用真实模型、不操作生产数据库；部署、重启、推送、生产数据迁移或启停真实采集等待用户明确授权。正式导出及回退恢复所需源侧工作写明交接需求，由用户转 stock_data agent；源侧真实恢复未验收不得宣称切换已就绪。最后给出提交号、实际验证结果、证据索引及未执行的外部验收，不将未验证项目说成已完成。
