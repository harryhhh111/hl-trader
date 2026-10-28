# hl-trader

Hyperliquid 命令行工具：包含**测试网**交易与 pair 操作，以及独立的 v2 多 pair 主网/测试网**只读看板**。

**默认 dry-run**：不加 `--execute` 时，不加载私钥、不生成签名、不发送任何写请求。

## 文档

- 独立 Web 应用迁移执行方案（供 Kimi 按阶段实施）：[INDEPENDENT_APP_MIGRATION_EXECUTION_PLAN.md](docs/INDEPENDENT_APP_MIGRATION_EXECUTION_PLAN.md)。
- 当前 v2 验收状态与上线前待办：[HYPERLIQUID_V2_ACCEPTANCE.md](docs/HYPERLIQUID_V2_ACCEPTANCE.md)。
- 第三方依赖与许可：[THIRD_PARTY.md](THIRD_PARTY.md)。
- 历史设计方案（仅用于追溯）：[MVP_PLAN.md](docs/archive/MVP_PLAN.md)、[PHASE2_PLAN.md](docs/archive/PHASE2_PLAN.md)。
- 独立应用迁移记录：[来源清单](docs/migration/SOURCE_MAP.md)、[执行进展](docs/migration/PROGRESS.md)。

## 独立 Web 应用（迁移实施中）

需要 Python 3.11+、Node.js 22+ 和独立 PostgreSQL。应用只读取显式 `HL_APP_` 环境配置，不加载旧项目 `.env`。运行、迁移和测试分别使用 `HL_APP_DATABASE_URL`、`HL_APP_MIGRATION_DATABASE_URL`、`HL_APP_TEST_DATABASE_URL`，三者互不回退；示例见 `.env.example`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e backend
.venv/bin/python -m hl_app.db migrate
.venv/bin/python -m hl_app.auth initialize
.venv/bin/python -m hl_app
.venv/bin/python -m hl_app.workers --dry-run
.venv/bin/python -m hl_app.workers --once --kind quotes
.venv/bin/python -m hl_app.workers --once --kind announcements
# 只有明确启用后才使用：python -m hl_app.workers --run

cd frontend
npm ci
npm run test:unit
npm run build
cd ..

cargo fmt --check
cargo test --offline
cargo clippy --offline --all-targets -- -D warnings
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q
# 仅显式指定一次性测试库后执行：
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m db -q
```

Web 固定单进程，启动时持有 PostgreSQL 生命周期锁，不自动建表或启动采集；新库业务状态默认为 maintenance。后端测试、前端开发服务器和 Rust CLI 均为本机开发入口，不代表部署或真实来源验收。

## 构建

需要 Rust 1.94+（开发用的是 stable 1.99）：

```sh
cargo build --release
```

## 配置

复制 `hl-trader.example.toml` 为 `hl-trader.toml`（或任意路径，用 `--config` 指定），填写：

- `account_address`：实际交易账户地址（公共地址，查询与下单都指向它）。
- `secret_file`：**仓库外**的 agent 私钥文件路径。要求：不是符号链接、所有者是当前用户、权限 0o600 或更严。
  主钱包授权（agent 注册）请在可信设备上自行完成，本工具不接收助记词、不提供主钱包签名。
- `max_notional_usd`：单笔名义金额本地上限（防误操作，不是账户级资金保护）。
- `receipt_dir`：回执目录（默认 `.hl-trader`，权限 0700，回执 0600，已被 .gitignore 忽略）。

## 命令

底层调试命令（第一步）：

```sh
hl-trader market --coin BTC
hl-trader account --address 0x<账户地址>
hl-trader order --coin BTC --side buy --price 60000 --size 0.001 --cloid 0x<32位hex> [--tif alo|ioc]   # 预览
hl-trader order --coin BTC --side buy --price 60000 --size 0.001 --cloid 0x<32位hex> --execute          # 签名提交
hl-trader status --address 0x<账户地址> --cloid 0x<32位hex>   # 查单（兼容旧用法）
hl-trader cancel --coin BTC --oid 12345 [--execute]
```

日常 pair 操作（第二步；多 HYPE 永续 + 空 BTC 永续，等名义，taker IOC + 2 tick 保护价）：

```sh
hl-trader status                          # 看板：仓位 + 资金费 + 动态（只读，随时可跑）
hl-trader open                            # 预览开仓（dry-run）
hl-trader open --execute                  # 真实开仓（每腿 ≤ min(配置单笔上限, leg_fraction×权益)）
hl-trader open --notional 50 --execute    # 指定每腿 USD 名义
hl-trader close                           # 预览平仓（reduce-only，只减仓）
hl-trader close --execute                 # 真实平仓 + 核算（手续费/实现盈亏/资金费）
hl-trader margin --coin BTC --leverage 3 --execute  # 设置某币种为全仓(cross)杠杆（维护命令）
```

只读看板（`status`，不带 `--cloid`）：

- 资金费分别展示两腿当前小时费率、简单年化、日净收付估算，以及过去 48 小时实际净收付和最近 3 笔记录；单个来源失败不隐藏其他有效数据。
- HYPE/BTC 使用截至昨日 UTC 的连续 30 个共同收盘日，标注收盘日期；缺日或数据过期时显示不可用。
- 解锁展示配置中未来 7 天的全部锚点（含当天，去重），附来源和人工核实日期；没有配置就明确提示，实际申领另标注未核实。
- 官方动态读取官方 Telegram [公告频道](https://t.me/hyperliquid_announcements)与 [API 更新频道](https://t.me/hyperliquid_api)的公开预览，每个频道最多展示 3 条，附发布时间、原文链接、查询时间。无需登录或 token；超时、页面变化、过旧公告明确提示。
- 行情和账户仍属于测试网；公开公告可能涉及不同网络，以原文为准。看板每次按需查询，不保存日报或后台推送。

### status --json（结构化快照，供 Web 看板调用）

```sh
hl-trader --config /固定路径/hl-trader-dashboard.toml status --json
```

- stdout 只输出**一个单行紧凑 JSON 对象**（`schema_version: 1`、固定五区块
  `positions`/`funding`/`ratio`/`unlocks`/`announcements`）；进程日志与诊断只走 stderr。
- 金额/价格/数量/费率一律十进制字符串，未知为 `null` 并附中文诊断，绝不补 0。
- 每块含 `status`（`ok`/`partial`/`unavailable`）、块级 `queried_at`、`source_queries`
  （固定来源键，单源失败只降级依赖它的字段）、`diagnostics`（`ok` 时为空数组）。
- 单源查询 8s 期限、五块并发、块内串行；不重试、不触发任何写操作。
- 退出码：`0` 快照成功（区块级降级不算失败）；`2` 配置错误等致命失败（stderr 说明，
  stdout 不出 JSON）；非 0 即失败，调用方不解析其 stdout。
- `--json` 与 `--cloid` 互斥；JSON 模式使用配置中的账户地址（忽略 `--address`），
  普通文本看板（不带 `--json`/`--cloid`）保持不变。
- 看板模式**不装载/不读取交易私钥**，即使配置里存在 `secret_file`。它用独立的只读
  配置入口：只需要 `account_address`、**绝对路径**的 `receipt_dir`（pair 状态只读位置）
  与可选 `unlock_anchors`；`secret_file`/`max_notional_usd`/`leg_fraction` 不要求、不校验。
  交易命令的严格配置校验不受影响。快照 JSON 不含私钥、秘密文件路径、完整配置、
  完整状态文件或本机路径；pair 状态不可读时动作状态降级为 `unknown`，其余数据保留。

看板专用最小配置示例（可与交易配置同文件，也可单独一份无秘密配置）：

```toml
account_address = "0x<账户地址>"
receipt_dir = "/绝对路径/到/.hl-trader"   # 必须绝对；仅读取 pair 状态

[[unlock_anchors]]
date_utc = "2026-11-06"
label = "团队份额月度解锁"
source_url = "https://example.com/verified-source"
verified_utc = "2026-10-15"
```

调用方进程契约（stock_data 侧）：参数数组调用（不经 shell），30s 进程期限、1 MiB
stdout 上限；非 0 退出码、超时或非法 JSON 均为失败，不伪造快照。

### dashboard（v2 多 pair 主网只读看板）

独立于 v1 的新入口（多 pair 方案）：同一二进制，不同的配置与网络边界。

```sh
# /path/to/pairs.json 见 hl-dashboard-pairs.example.json
hl-trader --config /固定路径/pairs.json dashboard --pair nbis-crwv --json
hl-trader --config /固定路径/pairs.json dashboard --announcements --json
```

- `--config` 在这个命令下是 **JSON pair 配置**；其他命令仍是原 TOML 交易配置。
- `--pair <id>` 与 `--announcements` 二选一（clap 互斥），`--json` 必带。
- 本命令使用独立配置解析器和只读 info 客户端：**不通过交易 load_config、不实例化
  签名器、不读 agent 私钥/receipt_dir、不获取交易锁**，只发 `/info` 类请求。
- 交易 Adapter 与 `NETWORK` 常量仍为测试网；本命令的 mainnet/testnet 来自 pair 配置
  并映射固定官方 URL，无任意 URL。HIP-3 合约用完整标识（如 `xyz:NBIS`）精确匹配，
  不会简化成 `NBIS`；`userFunding` 按官方 API 不带 dex 参数。
- 输出契约为 `schema_version: 2`、固定五区块、数值一律十进制字符串、分源时间与诊断；
  完整 DTO 由 `src/dashboard/snapshot.rs` 定义，Web 契约测试实读本仓库
  `tests/fixtures/hyperliquid/` 中由 Rust 离线 mock 生成并脱敏的共享 fixture。
  `unlock_anchors=null` 的 pair 输出 `blocks.unlocks=null`；账户地址为 `null` 的 pair
  只展示公开行情，账户/仓位块明确 unavailable（`items=null`，不是空数组）。
- 退出码：`0` 成功（区块级降级不算失败）；`1` 配置非法/进程失败/采集期配置被替换
  （后端转 503）；`2` 未知或已停用 pair id（后端转 404）。stdout 只含一行 JSON，
  日志与诊断走 stderr。

JSON pair 配置字段（CLI 与独立 Web 使用同一份受控文件，Web 由 `HL_APP_PAIRS_CONFIG` 指向）：

| 字段 | 说明 |
|---|---|
| `config_version` | 固定 `1`；拒绝未知字段、重复 JSON 键、重复 pair id |
| `pairs[].id` | `^[a-z0-9]+(?:-[a-z0-9]+)*$`，≤64，全局唯一 |
| `enabled` | 布尔，必须显式；`false` 不出目录、CLI 按 404 处理 |
| `network` | 仅 `mainnet`/`testnet`，固定 URL 映射，禁止自动回退 |
| `dex` | HIP-3 DEX 名；空串 = 原生。`coins` 必须与 dex 前缀一致 |
| `coins` | 恰好两个不同的完整永续标识，顺序即比价分子/分母 |
| `account_address` | 合法非零 EVM 地址（统一小写）或显式 `null`；空串/占位/零地址是错误 |
| `unlock_anchors` | `null` = 不适用；`[]` = 适用未配置；条目字段 `date_utc/label/source_url/verified_utc` |

`config_revision` 为配置原始字节的 SHA-256（小写 hex）；采集结束会重读文件比对，
中途被替换则明确报错（非 0），旧快照不会进入新配置缓存。

pair 语义（详见 docs/archive/PHASE2_PLAN.md §2/§3）：

- 顺序执行：先 HYPE 多腿，确认全成再 BTC 空腿；两腿不原子。
- IOC 腿单「零成交且订单已终结」（totalSz=0/avgPx=0）是交易所确认的已知结局，与拒单同等
  处理（第一腿不发第二腿、第二腿只减仓撤回），不进结果未知；HTTP 401/403 权限拒绝同理
  按已知失败（退出码 1）报告。
- 第二腿失败：只减仓撤回本次已确认建立的仓位（reduce-only IOC、新 cloid、每腿至多一次）；
  即使全部撤回成功，原开仓仍记为失败。
- 任何写请求结果未知（超时/断线/无法识别）：立即停止写操作，退出码 3，按 cloid/持仓核对后再处理，
  绝不盲目反向或自动重试。
- 平仓只减仓、绝不反向开仓；两腿实际持仓均为零且订单确认终结才报成功；已知残仓退出码 1，
  可再次 `close --execute` 按最新残仓继续。
- 状态文件 `.hl-trader/pair-state.json`（0600）发送前持久化，原子替换后同步目录；保存失败即停止后续交易。
  每次平仓使用新动作和 cloid，旧动作先复制归档，保留开仓及此前平仓记录。
- 所有写命令共用按 agent 地址区分的内核锁（`/tmp/hl-trader-writers-<uid>/`），pair 状态另有 `pair.lock`。
  锁在执行前读取状态之前获取，进程退出自动释放。锁文件留在磁盘是正常现象，勿删除。

## 中断与恢复

超时或进程中断后，先用 `status --cloid` 和 `account` 查看交易所记录，再运行 `close` 预览。
`close` 会先核对旧动作中可能已经发送的订单、实际成交量和持仓；预览不修改状态、不加载私钥。
核对通过后，`close --execute` 按实际残仓创建新的只减仓动作。已确认部分成交或拒单留下的残仓允许再次平仓。

订单未找到、未终结、成交记录缺失或实际持仓与动作记录不符时，返回 3 并阻止后续交易。
例外：尚未标记 `closed` 的动作，在旧订单均已核清、交易所已无挂单、且两腿实际持仓均在外部
归零（人工或第三方平仓）时，`close` 按已平仓处理并标记 `closed`（回执与状态注明外部归零），无需手工删
状态文件；部分持仓、有挂单等其它不一致仍返回 3，不猜测仓位归属。
外部归零会保存独立的核对时间和原预期仓位，保留原始成交记录，并标注核算不完整。
后续重复 `close` 仍会重新确认零仓且无挂单；出现新仓时停止，不把新仓当作旧 pair 平掉。
旧版仅在说明中标记外部归零的 `closed` 记录，会在核对通过后由 `close --execute` 补齐记录；预览不落盘。
单次“未找到”不能证明未成交；不要删除状态文件或重发原订单。交易所数据尚未更新时，可稍后再次只读核对。
旧版或丢失状态无法完整追溯时，核算会标注不完整；无法核清的旧订单仍阻止新增写请求。

开仓检查两腿全仓模式和最小名义金额，不自动修改杠杆/保证金设置；可用 `hl-trader margin --coin X
--leverage N [--execute]` 显式设置某币种为全仓（onlyIsolated 市场会被交易所拒绝，这是交易所级限制）。
例外：市场元数据声明 onlyIsolated（如测试网 HYPE strictIsolated，交易所拒绝 "Cross margin is not
allowed for this asset"）时接受该腿逐仓并打印警告，自担单腿独立强平风险；主网 HYPE 无此限制，仍
要求全仓。历史成交和资金费分页去重；
成交按本 pair 的订单 ID 与已确认数量核对，缺失记录、分页无法推进或达到历史保留上限时不声称完整核算。

交易命令端点硬编码为 `https://api.hyperliquid-testnet.xyz`，不支持主网写操作、不允许任意 URL 覆盖、不跟随重定向；v2 `dashboard` 的独立只读网络边界见上文。

## 语义与退出码

- HTTP 200 或顶层 `ok` 不等于成交；真实订单 ID、成交量、价格只来自交易所响应/查询。
- 退出码：`0` 成功；`2` 输入/配置/校验错误；`1` 明确的失败（拒单、无权限等）；
  `3` **结果未知**（超时、连接中断、响应格式无法识别）。退出码 3 时禁止直接重试下单，
  先运行 `status --cloid` 人工核对。写请求永不自动重试。
- `hypersdk` 将顶层业务错误和传输错误合并为字符串，无法可靠区分时保守返回 3；
  逐订单结构化拒单仍按明确拒绝处理，不用任意错误字符串认定交易未发生。
- `cloid` 幂等：相同 cloid 不会自动再次提交；本地回执（`.hl-trader/`）仅用于防误重发，
  不能替代交易所状态核对。
- 只撤 `--oid` 指定的订单；不批量撤单、不自动平仓；撤单失败/已成交会明确报告，
  不声称已经消除敞口。

## 离线测试

```sh
cargo test
```

测试基于本地 mock HTTP、临时文件和子进程锁，不访问主网/测试网，不需要真实私钥。
覆盖补偿未知后停止、落盘失败、残仓再次平仓、中断订单核对、最终持仓验证及分页边界。
交易 pair 的第二阶段测试网联调仍未验收；v2 主网只读看板的离线验收基线与上线前待办见[验收状态](docs/HYPERLIQUID_V2_ACCEPTANCE.md)。

手动公开来源检查不属于默认离线套件；它只读取公告网站，不加载交易配置：

```sh
cargo test intel::announcements::tests::public_sources_smoke -- --ignored --nocapture
```

公开预览不等同于完整历史 API；抓取失败不代表没有公告更新。

## 风险与已知限制（如实记录，docs/archive/PHASE2_PLAN.md §8）

- **无自动再平衡**：两腿名义随价格漂移，极端时 pair 退化近似单腿敞口；看板显示漂移%，
  由人决定是否 `close` 后重新 `open`（往返 taker 成本 ≈ 4×每腿名义比例×0.045% 权益）。
- 测试网验证不了主网的真实滑点、流动性与资金费水平；资金费净收付由当时两腿持仓和费率共同决定
  （费率正负决定多空收付方向，资金费每小时结算）。
- 两腿及补偿均非原子操作，可能留下已知残仓或结果未知；本工具不承诺自动恢复为无敞口状态。
  独立账户若同时被外部程序或人工交易，动作归属可能无法判定，此时停止写入。
- 边缘衰减：回测引用的 +90.8%/年是历史口径，未复核，不作为验收目标或未来收益承诺；
  status 仅提供观测信息。
- 官方动态依赖 Telegram 公开预览的可达性和页面结构，仅覆盖最近可见公告；失败不代表没有更新，可通过显示的原文入口或官方 X 人工核对。

## 许可与来源

- 本项目自身许可证：**待确认**（见 `docs/archive/MVP_PLAN.md` §2，未默认指定 MIT/AGPL）。
- 协议签名与类型复用社区 Rust SDK [`infinitefield/hypersdk`](https://github.com/infinitefield/hypersdk)
  （MPL-2.0，官方文档收录）。第三方许可明细见 `THIRD_PARTY.md`。
