# stock_data → hl-trader 资讯增量同步接口开发合同 v1

状态：开发合同，目标消费端尚未实现，真实联调尚未执行。

## 1. 目的与基线

本片只实现一个接口链路：`stock_data` 作为既有公告/媒体资讯的上游，`hl-trader` 通过专用认证 API 增量读取事件，写入自己的 PostgreSQL，并由现有资讯页面读取。不得因此开放源数据库、导入源仓库运行代码或把浏览器请求转发到源站。

文档编写时核对的基础版本：

- `hl-trader`：`5a18c306ada12b840d8717cd64b446e3be4ec91d`。
- `stock_data` 已提交 HEAD：`38dfeb45f624ba5275522741d5c2601c2e4fcf9d`。
- 下述源侧 API 位于上述 HEAD 之后的**未提交工作树**，不是已发布接口。源侧 agent 须先审核、验证并单独提交；仅 pull `stock_data` 的 `main` 不会自动获得这些改动。

源侧实际实现位置：

- 路由与 DTO：`web/routes/hl_trader_integration.py`
- 游标与分页：`web/services/hl_trader_news_sync_service.py`
- 事件快照：`core/announcements/news_sync.py`
- 业务事务内发事件：`core/announcements/store.py`
- DDL：`scripts/add_watchlist_news_sync.sql`，并同步到 `scripts/watchlist_announcements.sql`
- bootstrap：`scripts/backfill_watchlist_news_sync_events.py`
- 脱敏样本：`tests/fixtures/integrations/hl_trader_news_sync_v1.json`
- 源侧回归：`tests/test_web/test_hl_trader_news_sync_api.py`、`tests/test_announcements_news_sync.py`

## 2. 与其他接口的边界

本接口只传输公告/媒体条目的不可变事件快照、证券关联、中文增强和来源状态。

- 不传输正文，不新增正文提炼，不共享财务、全市场行情、研究资料或交易数据。
- `hl-trader` 行情仍由自己的 worker 独立采集。
- 正向 full 迁移、baseline、rollback delta 继续使用 `docs/migration/DATA_CONTRACT.md`；本 API 不是通用增量合并、CDC 或回退通道。
- bootstrap 只回填每个逻辑公告的最高 revision 和来源当前快照，不包含完整历史 revision、run、recovery 或 gap 明细，因此**不能替代 full 导入**；空库启动后的数据完整性和回退能力不得按 full 迁移宣称。
- v1 同时支持两种明确入口：已完成 full 导入的既有库继续增量；全新空业务库通过 bootstrap 建立当前快照。空库不需要伪造 full lineage，但必须在同步状态中持久化 `bootstrap_in_progress`，直到固定上界的全部页面提交完成。
- 空库 bootstrap 允许直接接收每个逻辑公告的最高 revision，并把它作为本地首条快照；这只表示“从上游当前版本开始”，不补造上游历史 revision。bootstrap 完成后，缺失本地逻辑公告却直接收到 revision>1 仍是错误。
- 一个 `hl-trader` 数据库在 v1 只绑定一个 `source_instance`，不合并多个上游。

## 3. HTTP 接口

### 3.1 请求

```http
GET /api/v1/integrations/hl-trader/news/sync?cursor=<opaque>&limit=200
Authorization: Bearer <专用只读 token>
Accept: application/json
```

完整 URL 由目标侧配置的 HTTPS origin 与上述固定路径组成。不得接受任意路径、URL 中的用户名/密码/token，或不校验证书的 HTTP 连接；跨公网仅使用 HTTPS 或私有隧道。客户端不跟随重定向。

参数：

| 参数 | 规则 |
| --- | --- |
| `cursor` | 首次请求省略；之后原样传入上一响应的 `next_cursor`。不得自行生成、解码后改写或只用其中的时间字段 |
| `limit` | 整数 `1..200`，默认及正常值均为 `200`；URL 编码由 HTTP 库完成 |

该接口是服务器间 GET，不使用 cookie、浏览器 session 或 CSRF。响应是顶层 `NewsSyncResponse`，**没有** `hl-trader` 页面 API 所用的 `{data: ...}` 包装；不要复用要求该包装的前端 client。

### 3.2 凭据与响应头

- 源侧环境：`STOCK_HL_TRADER_READ_TOKEN`；缺失或长度不足 24 时接口以 503 fail closed。
- 目标侧拟新增环境：`HL_APP_STOCK_DATA_READ_TOKEN`；与源侧 token 相同，但只注入 worker，不注入 Web、前端或构建参数。
- 使用至少 32 随机字节生成的 base64url 高熵 token。撤销/轮换只替换专用 token，不复用登录 token、数据库密码或交易凭据。
- 源侧以 `STOCK_NEWS_SOURCE_INSTANCE` 配置稳定、唯一的实例名；不要长期依赖默认值冒充不同环境。
- 成功响应包含 `Cache-Control: private, no-store`。日志不得记录 Authorization、token、完整 URL query 或事件正文。
- gzip 是可选优化，不是合同前提。若客户端启用压缩，2 MiB 上限必须作用于解压后的响应。

### 3.3 状态码

| 状态 | 含义与消费端动作 |
| --- | --- |
| 200 | 严格校验完整响应后才能应用；空数组只表示该固定窗口没有事件 |
| 400 | cursor 非法或越过当前事件上界；保留本地 cursor，报警并停止本轮，禁止自动从 0 重放 |
| 401 | token 缺失/错误/已撤销；停止并报警 |
| 422 | 请求参数不符合约束；检查 client，不重试同一请求 |
| 429 | 源侧限流；保留 cursor，按 `Retry-After` 等待，不并发绕过 |
| 500 | 查询失败或单事件超过大小上限；不是空成功，保留 cursor |
| 503 | 源侧 token 未有效配置或其他服务不可用；保留 cursor并报警 |
| 网络超时、非 JSON、未知字段、非法字段类型、超过 2 MiB | 与失败响应相同，不提交本页、不推进 cursor |

源侧当前限速是**每有效 token、每进程、每分钟 60 次**的内存窗口，不是持久化或跨副本全局限速。消费端仍须自行节流，分页请求建议至少间隔 1 秒。

## 4. 响应 DTO

所有响应及事件字段按当前 Pydantic `extra="forbid"` 合同严格校验。`schema_version` 必须恰好为整数 `1`；未知事件类型、缺字段或额外字段均使本页失败。时间字符串必须带时区，消费端解析后统一 UTC；`null` 不得补成当前时间、0 或空数组。

顶层字段：

| 字段 | 类型与含义 |
| --- | --- |
| `schema_version` | 固定 `1` |
| `source_instance` | 上游稳定实例标识 |
| `snapshot_at` | 本批窗口时间；同一固定上界的各页保持一致 |
| `next_cursor` | 下一请求的不透明游标，始终保存最新值，包括空页 |
| `has_more` | 当前固定上界内是否还有下一页 |
| `sources` | 本页的 `source` 事件数组 |
| `items` | 本页的公告/媒体事件数组 |

`NewsSource` 字段：

| 分组 | 字段 |
| --- | --- |
| 事件身份 | `sync_seq: int`、`event_kind: "source"` |
| 来源自然键 | `source_id: int`、`market: str`、`source_kind: str`、`platform: str|null`、`issuer_identifier: str`、`entry_url: str` |
| 来源配置 | `allowed_domain: str`、`source_timezone: str|null`、`parser_version: int`、`interval_minutes: int|null`、`enabled: bool`、`source_status: str`、`status_reason: str|null` |
| 检查状态 | `effective_interval_minutes: int|null`、`last_attempt_at: str|null`、`last_attempt_status: str|null`、`last_success_at: str|null`、`next_due_at: str|null`、`checked_through: str|null`、`last_error_class: str|null` |
| gap 汇总 | `open_gaps: int`、`earliest_gap_start: str|null` |

`NewsItem` 字段：

| 分组 | 字段 |
| --- | --- |
| 事件身份 | `sync_seq: int`、`event_kind: str` |
| 来源与逻辑身份 | `market`、`source_id`、`source_kind`、`platform`、`issuer_identifier`、`entry_url`、`source_record_id`、`revision: int` |
| 原文与版本 | `title`、`form_type: str|null`、`original_url`、`excerpt: str|null`、`content_hash: str|null`、`content_nature: str|null`、`media_source: str|null`、`is_backfill: bool` |
| 发布时间 | `published_at: str|null`、`published_at_precision: str`、`source_published_raw: str|null`、`source_timezone: str|null` |
| 观察时间 | `first_observed_at: str`、`last_seen_at: str` |
| 上游增强 | `title_zh: str|null`、`summary_zh: str|null`、`importance: str|null` |
| 事件时来源状态 | `source_status: str`、`status_reason: str|null`、`parser_version: int` |
| 公司关联 | `relations: list<{market, stock_code, relation_reason: str|null}>` |

`source_id` 是上游物理 ID，只用于上游映射与诊断，不能要求目标自增 ID 相同。条目里的 `source_status/status_reason/parser_version` 是该条目事件时的快照；目标来源当前状态只由 `source` 事件更新，避免旧条目覆盖新状态。

## 5. 游标与分页算法

源端事件表以全局递增 `sync_seq` 排序，业务记录与事件在同一事务提交。payload 是事件发生时的不可变快照。

消费端按以下算法工作：

1. 本地无 cursor 时省略参数。源端固定 `after=0`、`upper=当前 max(sync_seq)`，并生成 `snapshot_at`。
2. 源端读取 `after < sync_seq <= upper`，按 `sync_seq` 升序，最多先取 `limit+1` 行以判断 `has_more`。
3. 同一批所有后续页都携带固定 `upper` 和 `snapshot_at`。本批开始后新写入的事件不得混入本批。
4. 若响应大小超过 2 MiB，源端从本页尾部缩减事件并令 `has_more=true`；`next_cursor` 必须指向最后一条实际返回的事件。单条仍超限则返回 500。
5. 消费端将 `sources` 与 `items` 合并后按 `sync_seq` 升序应用，不得先应用所有 sources 再应用所有 items。
6. 本页全部事件和 `next_cursor` 在同一目标 DB 事务提交后才请求下一页。任何失败都整页回滚。
7. `has_more=true` 时继续请求；`false` 时本轮结束。此时 `next_cursor` 表示已读完当前上界，下一轮用它让源端开启新的当前上界。
8. 空页也必须原子保存 `next_cursor`；不得清空 cursor 或把空页当作上游故障。
9. 每轮设置页数/事件预算，建议最多 100 页或 20,000 事件。达到预算可正常结束并保留 cursor，下轮继续；不得丢弃剩余事件。

消费端不得用 `updated_at`、`published_at` 或本地时间替代 cursor。源端不得清理事件表；v1 没有消费确认协议，不能假设某条事件已不再需要。

## 6. 事件语义与幂等

当前事件类型：

| `event_kind` | 应用语义 |
| --- | --- |
| `source` | 来源配置、检查状态或 gap 汇总快照 |
| `announcement` | 新逻辑公告或新 revision 的完整条目快照；revision 由 `revision` 字段判断，不靠 event_kind 猜测 |
| `relation` | 某逻辑公告的当前完整关联集合及该 revision 快照 |
| `llm` | 指定 revision 的中文标题、摘要或重要性发生实际变化后的完整条目快照 |

重复抓取且内容/关联未变化不产生条目事件；`last_seen_at` 不是每次抓取的完整心跳副本。来源检查状态由 `source` 事件表达。

目标必须保存事件收据，至少含 `(source_instance, sync_seq, event_kind, payload_sha256, applied_at)`，主键为 `(source_instance, sync_seq)`：

- 首次事件：应用并写收据。
- 同 key、同规范内容 hash：视为重放，跳过业务写但可在同一事务推进 cursor。
- 同 key、不同 hash 或 event_kind：报冲突并停止，不覆盖。
- 不得仅凭目标现有某些字段相同就跳过收据和 cursor。

对逻辑公告以“映射后的本地 `source_id` + `source_record_id` + `revision`”识别：

- 新 revision 追加不可变行，不覆盖旧 revision。
- 同 revision 的标题、URL、内容 hash、发布时间等原始字段不一致时拒绝；`content_hash=null` 时比较完整原始字段，不能把 null 当作任意内容都相同。
- 相同原始 revision 的 `relation`/`llm` 重放可更新允许变化的关联、`last_seen_at` 和增强字段。
- 已完成 bootstrap/full 的目标完全缺少某逻辑公告时，只接受 `revision=1`；直接收到 revision>1 说明历史依赖缺失，整页失败。`bootstrap_in_progress=true` 的空库初始化例外：可接收上游为该逻辑公告提供的最高 revision 作为本地首条快照，但不补造更低 revision，也不把它登记为 full lineage。
- 目标已有更高 revision，而上游事件要求回写较低 revision 的原始字段时拒绝；不得让旧事件覆盖当前展示版本。
- 新 revision 的关联仍锚定该逻辑公告的首版。`relation` 及携带关联的条目事件以上游 `relations` 为完整快照，在该逻辑公告范围内核对新增与消失项，不影响其他公告。
- 关联表没有证券外键；按 `(market, stock_code)` 原样保存上游关联及理由。页面继续只展示目标当前启用关注项，不因同步自动新增/启用关注证券。

在上游同步模式下，条目的 `title_zh/summary_zh/importance` 由上游权威提供；目标不得再次调用模型覆盖它们。模型失败或目标本地增强失败不得清空已接收原文。

## 7. 目标库映射与新增状态

### 7.1 来源映射

以来源自然键 `(market, source_kind, issuer_identifier, entry_url)` 查找目标 `watchlist_announcement_source`，再持久化 `(source_instance, upstream_source_id) → local_source_id` 映射。映射后校验两边自然键仍一致。

`NewsSource` 必须显式携带 `allowed_domain`，且其值必须与 `entry_url` 的 HTTPS 主机一致；源侧需同步补充该 DTO 字段和 fixture。目标按以下规则处理：

- full 已迁入的来源可以映射并更新。
- 空库 bootstrap 遇到未知来源时，可在 `ingest_mode=stock_data` 下自动注册来源和映射；不得因此把来源加入本机直接 SEC/IR/媒体采集队列，也不得把上游 blocked/pending 自动改成可直接采集。
- 已完成初始化的库遇到未知来源，仍须先经受控管理流程注册，再恢复同步。
- 自然键变化、域名不一致或映射冲突必须停止本页并报警；不得用空字符串伪造 `allowed_domain`。
- 上游同步来源不需要本机直接采集所用的 `query_config`；不得为了兼容直接采集而伪造查询配置。

来源配置写入现有 source 表；检查字段在 `effective_interval_minutes` 非 null 时写入现有 check_state，全部为 null 时表示尚无检查状态，不创建假记录。

### 7.2 gap 的限制

v1 只有 `open_gaps` 和 `earliest_gap_start`，没有各 gap 的自然键、区间、resolved_at 或完整变化事件。消费端不得根据这两个汇总值伪造、删除或解决目标 `watchlist_announcement_gap` 行。

实现时新增上游来源状态投影，至少保存 `local_source_id`、`open_gaps`、`earliest_gap_start`、`sync_seq`、`updated_at`。上游模式下资讯状态区读取该投影；full 导入的 gap 明细仍保留作历史证据。若以后要求逐 gap 同步/恢复，须另增版本化 DTO 和事件合同。

### 7.3 同步状态与事务

建议新增以下目标私有状态表：

- `hl_news_sync_state`：绑定的 `source_instance`、最新 `next_cursor`、`snapshot_at`、`last_sync_seq`、`bootstrap_in_progress`、bootstrap 固定上界、最近尝试/成功时间和安全错误摘要。只有首次无 cursor 且尚无事件收据时才可进入 bootstrap；中断后按已保存 cursor 和固定上界续传，直到 `has_more=false` 的最后一页提交后才清除该标志。
- `hl_news_sync_event_receipt`：第 6 节的事件收据。
- `hl_news_sync_source_map`：保存上游来源 ID 到本地来源 ID 的映射。
- 上游来源状态投影表：保存上述 `local_source_id` 和 gap 汇总等字段。

状态表使用目标运行账号必要的最小读写权限，不能向 Web 页面暴露 cursor 或 token。游标推进与事件写入同事务；网络请求期间不保持业务写事务，但整个 worker 任务必须持有现有 `business_task` 共享锁，以便 maintenance 能阻止并排空网络后的落库。

如果响应中的 `source_instance` 与本地绑定不同，拒绝本页。更换上游不是清空状态重放，须另行迁移审核。

## 8. hl-trader worker 接入

拟新增显式配置，默认关闭：

```dotenv
HL_APP_NEWS_SYNC_ENABLED=false
HL_APP_STOCK_DATA_BASE_URL=https://stock-data.example.com
HL_APP_STOCK_DATA_READ_TOKEN=
HL_APP_NEWS_SYNC_INTERVAL_SECONDS=60
```

这些变量是本文拟新增合同，当前代码尚未实现。token 不写 `.env.example` 实值，不进前端、命令行参数或 Git。

接入规则：

1. worker 新增 `--kind news_sync`，并纳入现有唯一 WorkerLifecycle 锁；手工 `--once` 与常驻 scheduler 不得重叠。
2. `HL_APP_COLLECTORS_ENABLED` 仍是总开关，目标必须处于 active；maintenance 时保持空闲，不发新一轮上游请求。
3. 每页请求前后检查任务锁和 active 状态；每页事务调用现有 `business_write_guard`，确保维护期间返回的结果不再落库。
4. `HL_APP_NEWS_SYNC_ENABLED=true` 时，scheduler 用 `news_sync` 替代本地 `announcements` 轮询；手工本地 announcements 与上游模式同时启用应报配置错误，不形成双写。
5. 上游模式要求 `HL_APP_ENHANCEMENT_ENABLED=false`。关注理由 AI 草稿是独立功能，不受此限制。
6. 页面读取 API 只查目标 DB，永不触发现场上游请求。Web 进程不需要上游 token。
7. dry-run 只展示绑定实例、同步任务和本地状态，不访问真实上游。
8. 失败保留已提交页和 cursor，下一轮续传；不得把整轮重建为无 cursor bootstrap。

## 9. 两侧实施分工

### stock_data agent

- 审核并提交上述当前未提交实现；保持路由只读、认证 fail closed、事件与业务同事务。
- 对既有库使用专用增量 DDL；bootstrap 默认 dry-run，`--apply` 属于另行授权的真实数据操作。
- 保证所有支持的公告/revision/relation/LLM/source 业务写入口都产生事件；直接 SQL 绕过不在接口保证范围内。
- 保持脱敏 fixture 与 Pydantic DTO 同步，新增字段须先升版或明确兼容规则。
- 不在本片部署、重启、开放在公网或执行生产 bootstrap。

### hl-trader agent

- 新增有序 migration、严格 DTO、受限 HTTP client、来源映射、事件收据、页事务和 cursor 状态。
- 按第 8 节接入唯一 worker 和 maintenance，不修改 Rust 交易边界。
- 让现有新闻/公告读取使用同步后的本地表；上游 gap 汇总按第 7.2 节接入，不伪造 gap 明细。
- 将源侧脱敏 fixture 复制到目标 `tests/fixtures/integrations/`，目标测试实读本仓库副本，不依赖源目录。
- 更新 `.env.example`、部署模板、README 和实际验证记录；默认仍关闭同步。

## 10. 必测与验收

源侧现有测试命令（由 stock_data agent 在其环境执行）：

```sh
python -m pytest \
  tests/test_web/test_hl_trader_news_sync_api.py \
  tests/test_announcements_news_sync.py -q
```

其中集成测试会创建临时 PostgreSQL；固定测试端口被占用时应换可用测试环境，不得连接生产库。本文编写者未修改或执行这些源侧测试。

目标实现至少增加以下测试：

- 真实读取共享 fixture；中文、null 时间、多公司关联、blocked 来源均保持原义。
- 无 token/错误 token、400/401/422/429/500/503、超时、非法 JSON、未知字段、超限响应均不推进 cursor。
- 两页固定 upper 期间上游新增事件不混入；空页保存 cursor；下一轮能读到新事件。
- 页内第 N 条失败时整页回滚；重试幂等；同 sync_seq 不同 payload 拒绝。
- 空库 bootstrap 可接收最高 revision、自动注册仅限上游模式的来源，并在中断后续传；完成后不得补造历史 revision 或 full lineage。
- source_instance 改变、自然键冲突、域名不一致、同 revision 内容冲突均拒绝；初始化完成后缺失本地逻辑公告但 revision>1 仍拒绝，非 bootstrap 的未知来源也拒绝。
- relation 新增/消失按快照核对，共 CIK 证券不串号，不自动启用关注项。
- LLM 事件只更新指定 revision；重放不重复写；上游模式无本地资讯模型调用。
- 页提交与 cursor 同事务；第二个 worker 拒绝；maintenance 阻止新请求和在途结果落库。
- 使用本机 mock 上游完成真实 FastAPI + 临时 PostgreSQL + Chromium 登录、同步、筛选、关联和来源状态验收；读取页面不产生上游请求。

目标仓库标准回归仍须全部通过：

```sh
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q
# 仅显式一次性测试库：
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m db -q
cd frontend && npm run test:unit && npm run build && npm run test:e2e
```

真实 HTTPS、token 撤销/轮换、生产来源数据、停写迁移和正式启用均需单独授权并记录结果。本地 mock 通过不等于真实接口已经发布或联调完成。
