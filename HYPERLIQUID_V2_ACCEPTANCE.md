# Hyperliquid 多 Pair 看板验收记录

日期：2026-10-19。结论：**暂不通过**。多 pair 页面、独立账户配置、主网只读客户端及 v1 兼容结构已建立，但仍有下述可复现的数据正确性与失败处理问题。

## 验收基线与结果

- hl-trader：`b48f8987abb9498a824ea33891ccb381afacf677`。
- stock_data：`3ce1b2639b3701e6d995b3ec31708e38b8d38c08`，包含 Stage A/B/C。用户确认 Kimi 已结束，以当前文件验收。
- 方案：`/home/vinci/projects/stock_data/docs/HYPERLIQUID_MULTIPAIR_PLAN.md`。
- 原有 Rust 测试：144 通过，1 项 live smoke 忽略；fmt、clippy 通过。首次沙箱运行因禁止绑定 localhost 而失败，允许本机 mock 后通过，不算产品缺陷。
- Python 看板测试：59 通过。
- 最新前端 `tsc && vite build` 通过，产物在 `/tmp/hl-v2-acceptance-dist`；未发布。
- 在 `/tmp/hl-acceptance-auzf1vx2` 的源码副本增加 5 项验收断言，均暴露原测试未覆盖的失败，见 R1/R3/R6/R7/R8。业务源码未修改。
- 额外通过实际 TanStack QueryObserver 复现 R2，通过实际 Python 服务函数复现 R4/R5/R9。
- 本次未用真实账户进行线上烟测，未读取私钥、发送订单或重启服务；未完成浏览器视觉/移动端交互全验收。构建与库级复现不能代替这些验证。
- 验收初期前端仍在修改；最终提交已补齐详情路由/导航，并修复当时的 TypeScript 编译错误。这些已解决事项不列为最终缺陷。

## R1 [P1] 缺失资金费率被补成零，继续生成错误估算

位置：[snapshot.rs:351](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:351)，消费处 `build_funding_block`。

`parse_asset_ctx` 将缺失、null 或解析失败的 funding 都 `unwrap_or(Decimal::ZERO)`。只要合约还在 universe，费率就会被当作有效零值，年化及日收付估算继续输出。

复现：使用现有 `META_XYZ` 与 `ACCOUNT_TWO_LEGS` fixture，仅删除 NBIS 的 funding 字段。输出 NBIS 费率 `"0"`，仍给出预计每日收取 `4.6138`；此时 NBIS 实际费率未知，不能得到组合净收付。

修复：每个行情字段保留 Option/解析错误，缺失费率输出 null 并诊断；有仓位腿所需费率缺失时 daily_estimate=null。明确区分交易所返回的合法零费率与缺失。补缺失/null/非法字符串及合法零费率的测试。

## R2 [P1] 切换 Pair 或账户配置时沿用上一份快照

位置：[总览 PairCard:23](/home/vinci/projects/stock_data/frontend/src/pages/hyperliquid-page.tsx:23)、[详情查询:236](/home/vinci/projects/stock_data/frontend/src/pages/hyperliquid-pair-page.tsx:236)、[详情展示条件:286](/home/vinci/projects/stock_data/frontend/src/pages/hyperliquid-pair-page.tsx:286)。

两处都使用无条件 `placeholderData: keepPreviousData`。query key 虽含 revision，但 observer 会跨 key 保留旧数据。总览标题/地址来自新 descriptor，读数来自旧 snapshot；详情只比较地址，两个 pair 复用同一账户或都未配置时无法阻止串用。

复现：用项目实际 `@tanstack/query-core`，从 hype-btc 切到 nbis-crwv，下一请求保持 pending，得到 `isPlaceholderData=true`、`data.pair.id=hype-btc`；两个地址均 null 时通过当前详情展示条件。总览同 ID 换地址也没有快照身份保护。

修复：跨 revision/账户/pair 时不保留旧快照；同 key 刷新由查询缓存自然保留。展示前核对 revision、pair ID、网络、DEX、coins、地址；目录刷新应先完成再按最新 key 刷新快照。覆盖同地址切 pair、两组 null 地址、同 ID 换账户及网络、请求失败等情况。

## R3 [P2] 资金费历史完全失败仍返回净收付 0

位置：[snapshot.rs:942](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:942)、[snapshot.rs:961](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:961)。

分页即使首个请求失败，也返回 `FundingHistory {records:[], complete:false, error:Some(...)}`，外层把它包装成 Some。构建区块对空 records 求和后输出 `actual_48h_net="0"`、`recent_payments=[]`。虽然窗口标不完整，但用户仍看到了凭空生成的数字零。

复现：传入首请求 HTTP 500、没有任何有效记录的 history，断言应为 null，实际得到 Some("0")。

修复：完整成功无记录才返回 0/[]；不完整且未获得可用的该 pair 记录时返回 null/null。部分有效记录可以展示部分合计，完整性与文案保持区分。

## R4 [P2] 后端降级区块缺少字段，反而让详情页崩溃

位置：[hyperliquid_pairs_service.py:307](/home/vinci/projects/stock_data/web/services/hyperliquid_pairs_service.py:307)、[详情解锁区块:186](/home/vinci/projects/stock_data/frontend/src/pages/hyperliquid-pair-page.tsx:186)。

`_unavailable_block` 只返回 status/time/sources/diagnostics，不返回该块的完整数据形状。对启用解锁的 HYPE/BTC，anchors 非法或缺失时后端返回一个 truthy 的 unavailable 对象，详情仍执行 `unlocks.anchors.length`，会读取 undefined.length。

复现：从 HYPE/BTC fixture 删除 anchors，调用实际 `_validate_v2_snapshot`，返回的 unlocks 只有上述四个通用字段。不是安全地显示单块不可用。

修复：按 block 类型生成完整降级 DTO（未知数值 null，锚点空数组加不可用诊断），固定 source keys；前端先识别不可用状态再渲染内容，不能让单块错误影响全页。增加错误快照经真实校验器到前端的验收用例。

## R5 [P2] 子进程“有界读取”仍可阻塞，超时期限失效

位置：[hyperliquid_service.py:64](/home/vinci/projects/stock_data/web/services/hyperliquid_service.py:64)；v1/v2 都使用这个 helper。

selector 只保证已有一部分数据可读，但 `text=True` 管道的 `.read(65536)` 会尝试读取指定字符数，可能一直等到 EOF，期间不再检查 deadline，也不读取另一个输出管道。子进程先打印少量内容再阻塞，可能卡住采集许可；stdout/stderr 还可能互相等待。

复现：子进程 print 一行并 flush，然后 sleep(2)，调用 `_communicate_bounded(..., timeout=0.2)`，约 2.01 秒才报超时，而非 0.2 秒。若子进程持续不退出，阻塞可持续更久。

修复：使用非阻塞二进制管道及 `os.read`，或有界异步流并带整体 deadline；同时排空两路，所有异常路径 kill/wait/close。测试少量输出后停住、双管道压力、超限、无输出超时。

## R6 [P2] 单腿持仓的已知未实现盈亏没有进入合计

位置：[snapshot.rs:772](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:772)。

flat 分支填 0，双腿分支求和，但 single_leg 分支只设置状态，`unrealized_total` 留为 None。账户查询已确认另一腿无仓位，因此剩下一腿的已知盈亏应成为两腿合计。

复现：保留 fixture 中唯一 NBIS 仓位（盈亏 37.5），行级盈亏存在，合计为 null。

修复：在成功确认完整仓位集合后，对所有实际持仓行的有效盈亏求和；确认空仓才合计 0。保留多/空两种单腿与缺失盈亏的测试。

## R7 [P2] 拉了预热历史，但 90 天图的前 29 天仍没有 MA

位置：[snapshot.rs:1053](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:1053)、[snapshot.rs:1075](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:1075)。

ratios map 在 90 天展示循环内才逐日填充。虽然客户端拉取 121 天数据，展示窗口以前的 29 天比价没有放入 MA 计算。因此 90 天视图的前 29 个 MA 点必定为 null，即便完整历史足够。

复现：121 个连续共同日、恒定比价 8，第一展示日 MA 应为 8，实际为 None。最近一天 MA 不受这个边界影响。

修复：先构建完整历史比价并计算滚动均线，再裁剪 90 天；缺日继续断线。验证第一展示日及 29/30 天历史边界。

## R8 [P2] 忽略元数据 isDelisted，已下架合约仍按正常行情处理

位置：[snapshot.rs:361](/home/vinci/projects/hl-trader/src/dashboard/snapshot.rs:361)。

只要名称出现在 universe 且有对应 ctx，就硬编码 listed=true，未读取 `isDelisted`。下架合约留在 metadata 时，已有价格和费率会被当作正常活跃行情，缺少方案要求的明确提示。

复现：在现有 NBIS 元数据中加入 `isDelisted:true`，`parse_asset_ctx(...).listed` 仍为 true。

修复：区分“存在元数据”“活跃/下架”“字段有效”，下架要诊断并降级行情；保留可查询的实际仓位，不抹掉账户风险。覆盖名称缺失与标记下架两种场景。

## R9 [P2] 资金费窗口及区块语义校验仍不完整

位置：[hyperliquid_pairs_service.py:393](/home/vinci/projects/stock_data/web/services/hyperliquid_pairs_service.py:393)。

当前只检查窗口时间格式和完整性布尔类型，未检查 start<end、固定 48 小时、明细在窗口内，也未拒绝 `window_complete=false` 与 `status=ok` 的组合。

复现：把有效 fixture 改为 start=2026-10-20、end=2026-10-19、complete=false、status=ok，实际校验器仍返回 ok；结果还可被写入正常缓存。

修复：验证窗口长度、顺序、明细范围及状态一致性。共用 wrapper 同步校验固定 source keys、诊断字符串类型及 ok 状态的约束；测试应使用真实 Rust fixture 加针对性的破坏，不能只验证手写 mock。

## 交付缺口与下一次验收

- stock_data `tests/fixtures/hyperliquid/` 当前未提交；需要将受控 fixture 纳入交付，并让契约测试实际加载它们。
- `docs/deployment/HYPERLIQUID_DASHBOARD.md` 仍描述旧单账户 v1，未补 v2 配置、运行命令和验证步骤。
- 修复上述项后复验，随后用用户提供的两个主网公开地址做只读对照及浏览器交互验证。不要通过真实下单构造验收数据。
- 本次只新增此验收记录，未修复业务代码，也未提交或部署。

## 整改补记：代码与离线回归

以上 R1–R9 原始发现保留不改。本轮已完成对应代码整改与离线回归；本补记不代表真实账户、浏览器或发布验收已经通过。

### 逐项修复与回归映射

| 编号 | 修复提交 | 主要回归用例 |
|---|---|---|
| R1 | hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa` | `acceptance_missing_rate_must_not_be_zero`、`market_fields_preserve_invalid_zero_negative_and_delisted_states`、`missing_rate_on_unpositioned_leg_does_not_block_daily_estimate` |
| R2 | stock_data `5cd30303267b723f57fe0724459c03e36453bbbe`、`55655277c30acf6e778dafe2a6df6efcd323acba` | `identity.test.ts` 9 项；`queries.test.ts` 跨 key、同 key、慢请求与刷新顺序 4 项；`hyperliquid-pages.test.tsx` 总览/详情身份渲染 |
| R3 | hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa` | `acceptance_failed_history_must_not_be_zero`、`funding_history_distinguishes_empty_partial_other_and_zero_sum` |
| R4 | stock_data `185cba54483a31076b05a75b367e9ee0968bd9b9`、`dddcc2ba784c0d5c4da338f3cd9aa31de99c994e`、`55655277c30acf6e778dafe2a6df6efcd323acba` | `test_degraded_block_has_full_dto_and_fixed_sources`、区块字段破坏参数化用例、页面解锁不可用/anchors 损坏渲染用例 |
| R5 | stock_data `185cba54483a31076b05a75b367e9ee0968bd9b9`、`dddcc2ba784c0d5c4da338f3cd9aa31de99c994e` | `test_flushed_output_then_sleep_obeys_deadline`、`test_both_pipes_drained_under_pressure`、无输出/超限/关闭输出后不退出/多字节分块真实子进程用例 |
| R6 | hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa` | `acceptance_single_leg_pnl_must_be_preserved`、`second_single_leg_negative_pnl_must_be_preserved`、`position_states_flat_single_same_and_filtering` |
| R7 | hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa` | `acceptance_ma_warmup_must_cover_first_display_day`、`ma_boundaries_and_warmup_gap_are_preserved`、既有缺日断线用例 |
| R8 | hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa` | `acceptance_delisted_market_must_not_be_listed`、metadata 缺失与字段无效用例 |
| R9 | stock_data `185cba54483a31076b05a75b367e9ee0968bd9b9`、`dddcc2ba784c0d5c4da338f3cd9aa31de99c994e` | `test_invalid_block_semantics_degraded`、完整历史未知收付、固定 source keys/时间、非列表与 `test_fixture_field_destruction_degrades_block` |

共享 fixture 与文档提交：stock_data `75dd67cc82860af52bbcf2a299b5cb60d86d866d`（Rust 快照/公告 fixture 及 Python 实读契约测试）、`9fda9e7ea61916f8821dcb889755d5f5b03b1dec` 与 `55655277c30acf6e778dafe2a6df6efcd323acba`（v1/v2 部署说明及完整前端回归命令）。前端页面测试复用同一组 fixture。

### 本轮复验结果

- hl-trader `862bc586b2d34ef57d115b797bb9b2e2703924aa`：`cargo fmt --check` 通过；`cargo clippy --offline --all-targets -- -D warnings` 通过；`cargo test --offline` **154 通过、0 失败、1 个既有公开频道 live smoke 忽略**。
- stock_data `55655277c30acf6e778dafe2a6df6efcd323acba`：三个 Hyperliquid Python 测试文件共 **109 通过、0 失败**；`npm run test:hl` 共 **16 通过、0 失败**；`VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build` 通过。
- 所有本轮自动测试均离线或使用本地 mock/虚构 fixture；未读取私钥、未发送订单、未重启服务。

### 仍未完成的外部验收

- 两个真实主网公开地址尚未由用户提供并配置，未做主网只读数据对照。
- 当前环境没有 Chromium/Firefox/Playwright/Puppeteer，可执行页面渲染回归已完成，但真实浏览器桌面/移动端交互与截图尚未完成。
- 未发布、未重启服务。代码与离线整改结果不能表述为真实数据验收或上线完成。

## 2026-10-19 独立复核：主要修复有效，异常类型降级仍需收尾

实际验收版本：hl-trader `63f3c14e800cdb59476064a16d08e38f0989f057`；stock_data `f23962a1fe096e539b023de0c7fdd3083f058700`。后者比上述交付清单新增一个 v1 校验与隔离文档提交。本次未修改业务代码，未发布或推送。

### 已复跑通过

- Rust：154 通过、0 失败、1 个既有 live smoke 忽略；fmt 与 Clippy（offline、all-targets、`-D warnings`）通过。
- Python：三份 Hyperliquid 测试文件 165 通过；新增提交使数量比交付清单增加 56。
- 前端：身份检查 9 项、QueryObserver/刷新顺序 4 项、页面静态渲染 3 项均通过；TypeScript 与 Vite 生产构建通过，产物仅写入 `/tmp/hl-acceptance-build-20261019`。
- 两份共享快照已被 Python 与页面测试实读；另行比较校验前后内容，两份均保持不变，没有通过静默降级来掩盖契约不兼容。
- R1/R2/R3/R5/R6/R7/R8 的本轮修复和对应回归有效；R4 原解锁崩溃与 R9 原资金费窗口复现已修复，但块级异常类型处理仍不完整。

### 尚存问题（P2）：坏区块可能导致整份快照 503

1. **v2 合约字段先做集合成员判断，缺少字符串类型检查。** `stock_data/web/services/hyperliquid_pairs_service.py:374` 与 `:455` 分别对持仓行和资金费明细的 `coin` 直接执行 set 成员判断。将共享 NBIS/CRWV fixture 中任一行 `coin` 改为 `[]`，校验器抛出 `TypeError: unhashable type: 'list'`；真实路由返回整份快照 503，其他有效区块无法展示。应先验证 coin 为字符串，再判断合约归属；错误应进入完整的单块 unavailable DTO。
2. **v1 非数组 diagnostics 被直接迭代。** `stock_data/web/services/hyperliquid_service.py:183` 对 `diagnostics=1` 或 `true` 执行列表推导，抛出 TypeError；旧看板路由同样返回整份 503。应将不合法的 diagnostics 判为该块校验失败，不遍历任意 JSON 值，也不将空对象等非法类型默认为正常空数组。

额外 HTTP 验收用例保存在本机 `/tmp/test_hl_acceptance_remaining.py`（临时文件，未提交）：v2 持仓 coin、v2 收付 coin、v1 diagnostics 共 **3 项失败**，均断言有效的其他区块应保留、接口应返回 200，实际为 503。使用本地 fixture/mock，不访问真实账户。

因此本轮结论是主要修复与现有离线回归通过，尚不能将全部异常降级验收关闭。补齐上述类型检查，并将三个 HTTP 复现纳入正式回归后再收尾。真实主网账户对照、真实浏览器桌面/移动端交互、部署身份权限与发布验收仍未执行，不在此次通过范围内。

## 2026-10-19 异常类型降级整改收尾

修复提交：stock_data `b0dae6197081ccfa41c8d42830969f586f06046c`；hl-trader 业务代码仍为 `63f3c14e800cdb59476064a16d08e38f0989f057`，本轮无需改动。

- v2 持仓与资金费收付明细先检查 `coin` 为字符串，再检查合约归属。`coin=[]` 现在只将对应区块替换为完整 unavailable DTO，接口返回 200，其他有效区块保持不变。
- v1 `diagnostics` 不是字符串数组时直接判为该区块校验失败，不再迭代任意 JSON 标量，也不再把空对象或非字符串数组成员静默转换成正常诊断；降级区块保留完整 v1 字段。
- 三个临时 HTTP 复现已迁入正式回归：`test_bad_coin_degrades_only_affected_block_over_http` 实读共享 NBIS/CRWV fixture 覆盖持仓和资金费两块，`test_v1_invalid_diagnostics_degrades_only_affected_block_over_http` 覆盖数字、布尔、空对象和非字符串数组。用例同时断言完整降级结构、诊断及其他区块不变；原始 `/tmp/test_hl_acceptance_remaining.py` 三项也已复跑通过。
- 全量离线回归：Python 三份 Hyperliquid 测试文件 **171 通过、0 失败**；Rust **154 通过、0 失败、1 个既有 live smoke 忽略**，fmt 与 Clippy 通过；前端 **16 通过、0 失败**，TypeScript 与 Vite 生产构建通过。

上述两项 P2 的代码与离线 HTTP 回归至此关闭。本轮仍未读取私钥、下单、重启服务、发布或推送；真实主网账户对照、真实浏览器桌面/移动端交互、部署身份权限与发布验收仍待完成，不能据此声明上线验收通过。
