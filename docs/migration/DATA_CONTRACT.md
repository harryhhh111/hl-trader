# 独立应用数据迁移与回退合同 v1

本合同对应执行方案第 9 节。只允许停写后的 full→空业务库，不实现增量覆盖、CDC 或双向合并。正式源侧导出与恢复工具由 stock_data agent 实现；本仓库提供目标工具、格式、映射和参考演练。

## 1. 通用包格式

每个包是私有目录，含 `manifest.json`。full/baseline/rollback_snapshot 按下列十个实体各有一个 UTF-8 JSONL 文件，必需实体即使 0 行也必须声明并提供空文件：

- `securities`
- `watchlist_items`
- `daily_quotes`（正向 full 可在 scope 明确省略，但文件仍需存在）
- `watchlist_announcement_source`
- `watchlist_announcement`
- `watchlist_announcement_security`
- `watchlist_announcement_check_state`
- `watchlist_announcement_gap`
- `watchlist_announcement_run`
- `watchlist_announcement_recovery`

每行一个 JSON 对象，无数组包装。时间使用带时区 ISO 字符串，比较时统一 UTC；未知为 JSON null。range 使用 PostgreSQL 文本形式并统一 UTC。`manifest.files` 固定 entity、相对 path、schema、encoding、records 和内容 SHA-256；禁止绝对路径、路径穿越和重复自然键。包不含认证、数据库、模型、交易或私钥凭据。

manifest 固定 `bundle_version=1`，并包含：

- `bundle_id`：同 ID 内容不可变；
- `kind` / `direction`：`full/stock_data_to_hl`、`baseline/hl_snapshot`、`rollback_snapshot/hl_snapshot`、`rollback_delta/hl_to_stock_data`；
- `source_instance`、`source_commit`、`source_schema`；
- `scope` 与完整 `entities`；
- `cutoff_at`、`snapshot_id`、`quiesced`；full、baseline 和 rollback_snapshot 必须为停写一致性快照；
- `baseline_id` / `baseline_hash`：正向 full 为 null，回退包绑定注册基线；
- full lineage（baseline 的 `full_bundle_id/full_manifest_sha256`）。

`inspect` 只做文件、校验和、自然键和依赖检查，不连接数据库。正向 import 拒绝 incremental、rollback_delta 和错误方向。

## 2. 自然键与身份映射

| 实体 | 自然键 |
| --- | --- |
| securities | `(market, stock_code)` |
| watchlist_items | `security_id`；源侧映射为 `(market, stock_code)` |
| daily_quotes | `(market, stock_code, trade_date)` |
| announcement_source | `(market, source_kind, issuer_identifier, entry_url)` |
| announcement | `(source_id, source_record_id, revision)` |
| announcement_security | `(announcement_id, market, stock_code)`；源侧映射为逻辑公告首版+证券键 |
| check_state / recovery | `(market, issuer_identifier, source_id)` |
| gap / run | 源稳定 ID；源侧导出不得临时重编 |

源物理 ID 仅作映射输入。空目标导入可以保留这些 ID，但仍写入 `migration_id_map(bundle_id, entity, source_id, natural_key, target_id)`。公告关联必须指向首版，后续 revision 通过 `(source_id, source_record_id)` 继承。多证券可共 CIK；媒体关联保留公司级理由。

不迁 auth/session/login rate limit、交易配置/账本/锁、旧 DSN 和内核 advisory lock。SEC gate 单独初始化。源在途任务按中断/待恢复记录，不能标成功。

## 3. full 导入

管理命令只读取 `HL_APP_MIGRATION_DATABASE_URL`，不接收明文 DSN 参数，也不回退运行账号。新库 operation control 默认 `maintenance`。

- dry-run：检查包、目标 maintenance、无同批次和空业务实体，不写数据库。
- apply：再次完整校验，在维护锁和单事务中按依赖顺序写入；业务数据、映射和成功账本一起提交或回滚。
- 仅允许空业务实体；schema、认证和基础控制可以已初始化。已有其他成功批次、预演数据或业务记录时拒绝，无 force、自动清表或合并。
- 同 bundle_id/hash 重跑：只有当前所有自然键和规范值仍等于包内容才返回 already_imported；发现变化则拒绝。相同 ID 不同 hash、不同 full 包、缺文件和坏 FK 均拒绝。
- 导入不发网络请求、不调用模型、不激活 scheduler，也不自动解除 blocked/pending。

输出 bundle/manifest/business hash、实体输入数、导入或幂等状态；逻辑公告和 revision、关联、来源、checkpoint/gap/recovery 通过实体计数和样本核对。不能只比较总行数。

## 4. 维护锁与激活

`maintenance --enable` 取得固定 PostgreSQL 会话 advisory lock，先把模式设为 maintenance；新业务事务因此不能开始，既有事务在取得锁前排空。不能取得锁或排空超时时保持 maintenance，不出具停写证明。所有业务写入口和迁移管理共用固定锁协议；直接 SQL 只能用于用户确认的维护窗口。

`export --purpose baseline` 要求 maintenance 和成功 full lineage。导出当前完整业务快照及 hash，先写私有临时目录，校验后原子发布，再登记 baseline ledger。目录登记失败不算有效基线。

`activate --baseline` 核对 baseline 文件、ledger、目标实例、full lineage 和当前 business hash，只有一致才把 cutover baseline 写入 operation control 并设 active。不会启动 scheduler。重复激活同一基线返回当前结果；基线后发生变化则拒绝。运行账号不能写 operation control、migration 或 baseline ledger。

## 5. 回退导出

真实回退先 maintenance、停采集/编辑、排空并备份。`rollback-export --baseline ... --output ...` 校验 cutover baseline、ledger、schema、scope 和 lineage，在同一快照输出：

1. `rollback_snapshot/`：目标当前全部业务实体；
2. `rollback_delta/changes.jsonl`：基线与当前自然键集合/完整值的差异；
3. `report.json`：基线、当前 hash 和变化数。

每项差异含 entity、natural_key、`op=insert|update|delete`、before/after 完整值及各自 hash。新增 before=null，删除 after=null，停用是 enabled update，物理删除必须显式出现。覆盖证券/关注元数据、行情、来源、revision/关联、增强和采集状态，不包含认证、财务、交易资源或 SEC 内核锁。重复同一快照导出的业务内容 hash 一致，新批次不覆盖旧目录。

## 6. 源侧恢复交接

stock_data agent 按映射恢复，不从目标自增 ID 反推源身份：

- 当前值等于 before 才应用 after；已等于 after 则幂等复用；两者不同报告冲突。禁止按 updated_at 强制覆盖。
- 新公告/revision/关联/行情按自然键补入，内容不同拒绝自动覆盖。物理删除单独审核。
- checkpoint/gap/recovery 同时保留两侧证据，以冻结 full 的已验证覆盖点为保守起点；未证明覆盖的区间保留或新增 gap/recovery 后幂等重采，不取最大 cursor。
- blocked/pending、冷却和中断状态保守恢复，不因目标曾成功就解除。恢复不调用模型重写增强。
- 源物理移除关注关系时，目标 enabled=false 映射为移除当前关系但保留证券和历史关联；不得删除历史公告。

本仓库 `migration/reference.py` 提供三方参考适配。临时演练覆盖 full 导入→baseline→active→理由/停用/revision/行情变化→maintenance→rollback→参考恢复，以及重复恢复和源侧并发冲突。正式源 schema 兼容性与恢复工具未由 stock_data agent 验收前，不得宣布真实切换或真实回退就绪。

## 7. 脱敏样本

`tests/fixtures/migration/full` 由 `build_fixture.py` 确定性生成，含共 CIK、多公司媒体、两个 revision、未知时间、blocked/pending 和未完成恢复。重建命令：

```sh
.venv/bin/python tests/fixtures/migration/build_fixture.py
.venv/bin/python -m hl_app.migration inspect --bundle tests/fixtures/migration/full
```
