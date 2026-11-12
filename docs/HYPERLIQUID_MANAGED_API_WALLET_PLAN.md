# Hyperliquid 托管 API Wallet 技术方案

> 状态：已实现并通过离线、临时数据库和 Chromium 隔离验证；已于 2026-11-04 部署到本机 Docker 环境并启用托管执行。真实主网成交、流动性与异常恢复验收需单独核对，不能由部署状态替代
>
> 日期：2026-11-04
>
> 目标：为后续基金化运营提供服务端托管的 API Wallet。私钥加密后存入现有 PostgreSQL 的 `hl_app` schema，Web 在人工确认的调仓请求中解密并调用现有 Rust 执行器。保留浏览器本地签名、浏览器扩展钱包和显式 CLI 私钥文件模式。

## 1. 范围与决策

本期实现：

- 每个 pair＋交易账户配置一个托管 API Wallet。
- 私钥经浏览器混合加密传输，再由后端使用 AES-256-GCM 加密后写库。
- 数据加密密钥和传输 RSA 私钥通过独立密钥文件挂载进 Web 容器，不存入数据库。
- 调仓仍须先预览，再由操作员输入目标金额进行确认；不增加自动交易。
- 保存、轮换、删除和服务端调仓记录操作员身份及结果。
- 使用现有本机 PostgreSQL，不创建数据库或数据库容器。

本期不实现：

- 原交易账户私钥、助记词托管。
- 私钥查看、明文导出或 API 返回密文。
- 自动调仓、定时策略、跨账户批量下单。
- 多签审批、细粒度 RBAC、KMS 或独立签名微服务。
- 数据加密密钥的在线自动轮换。

同一交易账户可跨 pair 使用同一 API Wallet；本系统禁止跨交易账户复用签名地址，并永久拒绝重新导入已淘汰或已撤销的地址。Hyperliquid 按签名钱包共享 nonce 状态，pair 级数据库隔离不会改变交易所侧的钱包权限或 nonce 共享；Rust 按钱包地址持有跨 pair 写锁，串行化本机签名与提交。

## 2. 现状与参考

当前 `hyperliquid_pair_accounts` 只保存公开地址。浏览器本地签名在 `frontend/src/components/hyperliquid/position-adjustment.tsx` 中持有私钥，关闭弹窗、刷新或切换账户会清除；服务端只接收订单签名。

现有 `adjust --execute` 已具备：

- API Wallet 与原账户地址不同的强制校验。
- 按 agent 地址划分的写锁。
- 每腿目标名义上限、预览和人工确认。
- pair＋账户隔离的执行状态与回执。
- 已知失败、未完成和结果未知的区分；未知结果禁止直接重试。

参考本机 `nofx-quant`：

- `web/src/lib/crypto.ts`：AES-GCM＋RSA-OAEP 的浏览器混合加密。
- `crypto/crypto.go`：AES-GCM 数据库存储及独立密钥配置。
- `api/handler_exchange.go`：使用安全响应 DTO，避免返回秘密字段。

只复用设计，不复制以下行为：

- 加密失败或加密服务缺失时回退为明文。
- 解密失败时把原数据库值当明文继续返回。
- 默认关闭传输加密。
- 日志记录完整账户配置。
- 原账户私钥仅告警而不拒绝。

## 3. 安全边界

该方案主要防御数据库、数据库备份或只读数据库账号单独泄露。数据库中只有密文，不能离线直接恢复私钥。

以下情况不在本方案的防护能力内：

- 攻击者控制 Web 容器或取得其进程内存，同时能够读取数据加密密钥。
- 主机 root、Docker 管理权限或宿主机磁盘与密钥备份同时泄露。
- 浏览器、前端供应链或操作员设备已被恶意代码控制。
- Hyperliquid 账户本身被恶意授权或 API Wallet 未在交易所撤销。

因此，加密存储不是资金托管的全部保障。API Wallet 仍可能通过交易造成资金损失，必须保留最小权限、人工确认、额度限制、审计和撤销流程。

## 4. 密钥、传输与防重放设计

### 4.1 密钥配置

新增两个只读密钥文件挂载：

- `HL_APP_DATA_ENCRYPTION_KEY_FILE`：Base64 编码的 32 字节随机 AES-256 密钥。
- `HL_APP_TRANSPORT_RSA_PRIVATE_KEY_FILE`：RSA-3072 PKCS#8 PEM 私钥。

同时配置非秘密的 `HL_APP_DATA_ENCRYPTION_KEY_ID`，用于标识当前存储密钥。密钥文件必须：

- 位于 `.runtime/` 或仓库外的受控位置，不进入 Git。
- 不是符号链接，权限为 0600 或更严。
- 只挂载到 Web；migrate、db-grants 和 gateway 不需要这些密钥。
- 与数据库备份分开备份；数据加密密钥丢失后，已有密文无法恢复。

启动时严格校验格式和长度。不使用字符串 SHA-256 作为低熵密钥的隐式兜底，不自动生成生产密钥，不在缺少密钥时以明文继续运行。

### 4.2 一次性 challenge

浏览器每次添加、轮换或删除前，先调用 challenge 接口。服务端使用 CSPRNG 生成至少 128 位随机 challenge，并持久化：

- `challenge_id` 及其安全哈希；明文只通过本次响应返回。
- `actor_binding`、`network`、`pair_id`、`account_address` 和操作类型。
- 当前 `wallet_version`；首次保存为 0。
- `created_at`、`expires_at`、`used_at`。

challenge 有效期 5 分钟，只能用于绑定的操作员、网络、账户和操作。每次变更会使该范围所有未使用 challenge 失效；challenge 的消费和钱包变更必须在同一数据库事务内完成。已使用、已过期、已失效或不匹配当前版本的 challenge 一律拒绝。

仅检查时间戳不构成防重放。即使旧密文仍在五分钟内，也不能再次保存、覆盖新钱包或删除后续版本。

### 4.3 浏览器到后端的传输加密

获取 RSA 公钥和 challenge 的接口均要求有效会话。`actor_binding` 由服务端生成：Cloudflare 模式绑定已验证邮箱和会话，token 模式使用会话哈希，绝不使用原始 token。

浏览器使用 Web Crypto：

1. 生成一次性 AES-256-GCM 密钥和 12 字节随机 IV。
2. 加密仅包含私钥的 JSON。
3. AAD 包含固定用途、challenge、network、pair、交易账户、当前 `wallet_version`、`actor_binding`、RSA `key_id` 和 Unix 秒时间戳。
4. 使用 RSA-OAEP/SHA-256 包装 AES 密钥。
5. 上传 `wrapped_key`、`iv`、`ciphertext`、`aad`、`key_id`、`challenge_id` 和 `ts`。

后端必须：

- 要求 HTTPS 和有效会话，继续执行现有 CSRF 约束。
- 校验 RSA `key_id`、challenge、固定用途、network、pair、账户、版本和 `actor_binding`。
- 强制校验时间戳：最多 5 分钟有效，最多允许 1 分钟未来偏差；时间戳只是辅助条件。
- 验证 AES-GCM 认证标签，限制请求体和各字段长度。
- 任何失败都返回通用错误，不返回解密细节或秘密。

这层加密用于减少 TLS 终止点、中间日志和代理错误配置造成的暴露，不替代 HTTPS，也不能防御已控制浏览器的攻击者。

### 4.4 数据库存储加密

后端在保存前重新生成随机 12 字节 nonce，使用数据加密密钥执行 AES-256-GCM。存储 AAD 固定为：

```text
hyperliquid-api-wallet:v1|<network>|<pair_id>|<account_address>|<wallet_address>|<wallet_version>
```

数据库值采用带版本的格式：

```text
ENC:v1:<data_key_id>:<base64_nonce>:<base64_ciphertext_and_tag>
```

解密时要求版本、`data_key_id`、nonce 长度和 AAD 全部匹配。解密失败、未知版本或缺少密钥均为硬错误，不返回数据库原值，不尝试明文兼容。

## 5. 数据模型

新增 `008_hyperliquid_api_wallets.sql`。所有身份字段都包含 `network`；网络变化不会复用旧网络的钱包、challenge 或执行请求。

### 5.1 `hyperliquid_api_wallets`

每个 network＋pair＋交易账户保留一条版本化记录：

- `network TEXT`、`pair_id TEXT`、`account_address CHAR(42)`
- `wallet_address CHAR(42)`：当前或最后使用的公开地址
- `wallet_version BIGINT`：首次保存为 1，轮换和删除都递增
- `status TEXT`：`active` / `deleted`
- `encrypted_private_key TEXT NULL`：删除后清空
- `encryption_version SMALLINT`、`data_key_id TEXT`
- `authorization_master CHAR(42) NULL`、`authorization_checked_at TIMESTAMPTZ NULL`
- `created_by`、`updated_by`、`created_at`、`updated_at`
- 主键：`(network, pair_id, account_address)`
- 约束：地址均为小写、合法、非零，且 `wallet_address <> account_address`
- migration `011` 新增 `hyperliquid_api_wallet_account_bindings`，以 `(network, wallet_address)` 绑定唯一交易账户：同账户可跨 pair 复用，跨交易账户由绑定主键和保存校验共同拒绝

采用软删除以保留版本递增和审计身份；`deleted` 记录没有私钥密文，查询时视为未配置。该表不依赖账户数据库 ID，使 JSON 默认账户也可配置。账户或网络变化是另一项配置，不自动复制私钥。

### 5.2 `hyperliquid_api_wallet_challenges`

保存服务端一次性 challenge 的哈希、操作员绑定、network／pair／account、操作类型、签发时 `wallet_version`、到期和消费时间。`challenge_id` 全局唯一；消费使用原子条件更新，并和钱包变更处于同一事务。

### 5.3 `hyperliquid_api_wallet_retired_addresses`

以 `(network, wallet_address)` 为主键记录首次使用、淘汰时间和来源账户。轮换或删除时把旧地址永久写入该表。之后即使用户仍持有旧私钥，也不能重新导入；防止 Agent nonce 状态被剪枝后复用地址造成重放风险。

### 5.4 `hyperliquid_execution_requests`

持久化人工调仓请求：

- `request_id`：浏览器生成的随机 UUID，全局唯一
- `actor`、`network`、`pair_id`、`account_address`
- `config_revision`、规范化 `target_notional`、`request_payload_hash`
- `wallet_address`、`wallet_version`
- `status`：`previewed` / `running` / `succeeded` / `incomplete` / `failed` / `unknown`
- `action_id TEXT NULL`、安全的结果摘要、错误分类和时间戳

同一 `request_id` 的请求正文哈希必须一致，否则返回 409。同一 network＋pair＋账户至多一个 `running`／`unknown` 请求；`previewed` 只代表可核对的意图，不占用执行权。执行意图不包含私钥或密文。

### 5.5 `hyperliquid_api_wallet_events`

追加式审计记录：

- `id BIGSERIAL PRIMARY KEY`
- `event_type TEXT`：`save`、`rotate`、`delete`、`execute_requested`、`execute_finished`
- `network`、`pair_id`、`account_address`、`wallet_address`、`wallet_version`
- `actor TEXT`：Cloudflare 邮箱；token 模式为不可逆会话标识
- `request_id`、`action_id`、`outcome` 等可空公开字段
- `created_at TIMESTAMPTZ`

不保存私钥、密文、签名、秘密文件描述符内容或完整请求正文。运行账号只有 `SELECT, INSERT`，迁移后显式撤销该表的 `UPDATE, DELETE`。

## 6. 后端接口与服务

新增独立服务模块负责传输解密、地址与授权校验、存储加密和秘密生命周期，避免把私钥逻辑混入账户公开信息服务。

### 6.1 查询配置状态与签发 challenge

```text
GET  /api/v1/hyperliquid/pairs/{pair_id}/api-wallet?account_address=0x...
POST /api/v1/hyperliquid/pairs/{pair_id}/api-wallet/challenge
```

状态接口只返回 `configured`、`network`、公开地址、`wallet_version`、授权核对时间、活动执行请求和时间戳。不返回私钥、密文或任何密钥材料。

challenge 接口正文包含 `account_address`、`config_revision` 和操作类型。服务端核对当前账户后签发第 4.2 节的一次性 challenge，同时返回对应 RSA 公钥及 `key_id`。

### 6.2 实时验证真实授权关系

保存和每次服务端执行前都必须通过 pair 配置选择的固定官方 mainnet／testnet `/info` 查询，不允许任意 URL：

1. 查询 `userRole(agent_address)`，要求 `role=agent` 且 `data.user` 存在。
2. 普通账户要求 `data.user == account_address`。
3. 子账户先查询 `userRole(account_address)`，要求 `role=subAccount`，再要求 Agent 的 `data.user == subAccount.data.master`。
4. `missing`、其他角色、归属不符、响应无法识别、超时或网络错误均失败关闭，不保存、不发送订单。
5. vault 或其他新型账户关系须另行定义和验收，本期不猜测授权语义。

数据库中的 `authorization_master` 和 `authorization_checked_at` 只是审计观测，不作为跳过实时校验的依据。校验通过后再被外部撤销属于交易所执行阶段的正常竞态，由交易所拒绝和现有结果协议处理。

### 6.3 保存或轮换

```text
PUT /api/v1/hyperliquid/pairs/{pair_id}/api-wallet
```

正文包含 `account_address`、`config_revision`、一次性 challenge 和第 4.3 节的加密载荷。后端流程：

1. 重新读取 pair 配置和当前账户，拒绝过期配置或非当前账户。
2. 验证并解密传输载荷。
3. 使用 Linux `memfd_create` 创建匿名内存文件，写入后通过继承文件描述符传给子进程；不创建文件系统路径。
4. 调用离线 Rust 子命令派生 API Wallet 地址；命令行只传 fd 编号，私钥不得出现在参数、stdout 或 stderr。
5. 父进程关闭自己的 fd，子进程退出后内核回收最后的 fd；`finally` 用于正常清理，但不是唯一的安全保障。
6. 拒绝原账户私钥、零私钥、非法 secp256k1 密钥、其他账户正在使用的地址及已淘汰地址。
7. 按第 6.2 节实时核对授权归属和网络。
8. 获取该 network＋pair＋账户的数据库事务锁，重新检查版本和活动执行请求；无记录时也使用同一 advisory lock 覆盖首次写入。
9. 原子消费 challenge，upsert 新密文并递增 `wallet_version`；旧地址写入淘汰表，审计和变更同事务提交。

Rust 新增离线 `api-wallet-address --secret-fd N --json` 入口，只输出 schema 版本和派生地址，不读取 pair 配置、不访问网络、不获取交易锁。显式 CLI 的 `--secret-file` 保留，但托管 Web 不使用文件回退。

### 6.4 删除

删除使用一次性 challenge，并携带当前 `wallet_version` 和待删除 API Wallet 地址。后端在同一账户锁和事务内核对版本、确认没有 `running`／`unknown` 执行请求，然后递增版本、清空密文、标记 `deleted`、淘汰地址并写审计。

响应必须明确：删除数据库密文不会撤销 Hyperliquid 授权，也无法撤回已经发送的动作；操作员仍需在 Hyperliquid 官方界面撤销对应 Agent。

## 7. 服务端调仓链路

托管模式在预览时由浏览器生成随机 `request_id`，预览请求额外携带 `request_id` 和 `wallet_version`。后端持久化 `previewed` 请求、规范化参数、预览结果哈希和操作员。目标、账户、配置或钱包变化必须生成新的预览和 request_id。

```text
POST /api/v1/hyperliquid/pairs/{pair_id}/adjustments/managed-execute
GET  /api/v1/hyperliquid/pairs/{pair_id}/adjustments/managed-executions/{request_id}
```

执行正文包含：

- `request_id`
- `target_notional`
- `account_address`
- `config_revision`
- `wallet_version`
- 与目标金额完全一致的 `confirmation`

执行协议：

1. 要求 `HL_APP_TRADING_ENABLED=true`、完整加密配置和绝对路径执行状态目录。
2. 获取该 network＋pair＋账户的数据库事务锁，核对 request_id、预览参数哈希、操作员、当前配置和钱包版本。
3. 同一 request_id 且正文相同：终态返回原保存结果，`running` 返回 202 和原状态，`unknown` 返回原未知结果；不得启动新执行器。相同 request_id 但参数不同返回 409。
4. 有运行中或未知请求时拒绝新执行。轮换和删除同样检查活动请求；`previewed` 可被新版本作废，但不能以旧版本执行。
5. 原子地把匹配的 `previewed` 请求推进为 `running`，写 `execute_requested` 后提交，再解密私钥。没有配置则拒绝，不回退到其他钱包或全局私钥。
6. 按第 6.2 节重新核对实时授权；使用 memfd 和继承 fd 调用 `adjust --execute --secret-fd N --state-dir ... --request-id <uuid>`。
7. Rust 在发送任何订单前，把 request_id、action ID、cloid、两腿意图及配置身份写入现有持久执行状态。这样 Web 或 Python 崩溃后仍能由 request_id 找到回执并核对交易所订单。
8. 完成后持久化原执行报告、action ID 和结果分类，写 `execute_finished` 并清理运行时缓存。确认尚未发送的授权失败、参数错误或执行器拒绝记为 `failed`；已可能发送但无法确认时才记为 `unknown`。父进程关闭 memfd；子进程结束或强杀后由内核回收剩余 fd。
9. 请求超时、断线或进程崩溃时状态为 `unknown`，不自动重试、不自动补偿。前端或操作员用原 request_id 查询；Web 重启只把未完成请求标记为未知，不自动恢复发送。
10. 结果未知时继续阻止该账户的新执行、轮换和删除，直到通过独立核对流程确认订单、成交和实际仓位后显式解除。核对接口只读交易所，不重新发送订单。

文件签名模式和托管模式分别判断就绪条件：托管模式不要求 `HL_APP_AGENT_SECRET_FILE`，旧文件模式不要求数据库钱包。两者都必须由 `HL_APP_TRADING_ENABLED` 显式开启。

现有 `browser-prepare`、`browser-submit` 和浏览器本地私钥模式保持兼容。服务端模式不经过浏览器签名接口，也不把托管私钥发送到浏览器。

## 8. 前端交互

在 pair 详情页的“账户配置”中增加托管钱包入口：

- 未配置：显示“未配置托管 API Wallet”和“添加”按钮。
- 已配置：显示 network、脱敏公开地址、版本、授权核对时间和“更换／删除”按钮。
- 添加、轮换和删除前都获取一次性 challenge；按钮操作不复用旧 challenge。
- 添加或更换弹窗只接受新私钥，不回填旧值；展示禁止输入原账户私钥和已撤销地址的提示。
- 保存中禁止重复提交；成功后清空输入框和组件内私钥状态。
- 删除弹窗要求确认 API Wallet 地址和版本，并说明数据库删除不等于交易所撤销。

调仓弹窗提供两种明确选择：

- `使用已托管 API Wallet`：展示服务器端公开地址及版本，确认目标金额后执行。
- `浏览器签名`：保留当前一次性本地私钥和扩展钱包流程。

账户、pair、网络、配置或钱包版本变化时清除旧预览和 request_id。执行请求超时后保留 request_id 并轮询原状态，不生成新请求；刷新页面后可通过该账户的活动请求恢复进度展示。结果未知时显示 request_id、action ID（若已产生）和“禁止直接重试”。

## 9. Docker 与部署变更

- `compose.managed-wallets.yaml` 为 Web 增加两个只读密钥文件挂载及对应 `_FILE` 配置；基础 `compose.yaml` 不持有这些密钥。
- 默认示例不包含真实密钥；`.runtime/` 继续由 Git 忽略。
- Web 保持 `read_only: true`。托管私钥通过 Linux memfd 和继承 fd 传递，不在 `/tmp`、命名卷或镜像中创建秘密文件。
- 生产托管模式要求 Linux memfd 可用；不可用时拒绝保存和执行，不回退到磁盘文件。
- gateway、migrate 和 db-grants 不获得交易私钥或数据加密密钥。
- 迁移使用现有 `HL_APP_MIGRATION_DATABASE_URL`，运行使用现有 runtime 账号；不创建数据库。
- `deploy/docker/grant-runtime.sql` 保持新钱包表所需的运行权限，并限制审计表为追加式。
- README、独立部署手册和调仓使用说明同步更新安全边界、授权核对、请求恢复与撤销步骤。

部署时需要单独授权生成生产密钥、执行迁移和重建容器。真实私钥保存及真实调仓不作为自动部署步骤。

## 10. 测试与验收

### 10.1 加密与防重放测试

- 保存后能用正确密钥和包含 network／版本的 AAD 解密，地址一致。
- 密文不含明文私钥，且相同私钥两次保存产生不同密文。
- 错误密钥、错误 AAD、篡改 nonce／密文／标签全部失败。
- 缺密钥、弱密钥、未知版本、非法 RSA 载荷、过期时间戳全部失败。
- 同一 challenge 并发或顺序重放最多成功一次；旧密文不能恢复旧钱包版本。
- 其他操作员、网络、pair、账户、操作类型或钱包版本的 challenge 被拒绝。
- 不存在任何明文回退路径；日志、异常和 API 响应不包含私钥或密文。

### 10.2 后端、授权与数据库测试

- 默认配置账户和数据库账户均可保存、查询、轮换和软删除。
- mainnet／testnet、pair、账户和钱包地址严格隔离；跨账户复用及淘汰地址重新导入被拒绝。
- mock `userRole` 覆盖普通账户、子账户、错误 master、非 agent、missing、超时和非法响应；未通过时不写库、不下单。
- 原账户私钥、零私钥和非法私钥被拒绝。
- 未配置托管钱包时执行被拒绝，不使用 `HL_APP_AGENT_SECRET_FILE` 兜底。
- `HL_APP_TRADING_ENABLED=false` 时托管执行被拒绝。
- 并行保存、轮换、删除和执行验证同一账户锁及版本条件；运行中变更返回 409。
- 同一 request_id 重试只返回原状态，不新增执行器或订单；不同参数复用 request_id 返回 409。
- 模拟崩溃发生在发送前、发送中和响应后时，都能由 request_id 找到执行意图；重启不自动发送。
- 模拟执行器验证秘密只通过 memfd fd 传递；正常退出、异常和 SIGKILL 后无文件系统秘密残留，fd 不泄漏到无关子进程。
- 审计只含允许的公开字段，runtime 无法更新或删除审计。
- 数据库测试只使用显式一次性测试库。

### 10.3 前端与 E2E

- 添加、轮换、删除、错误提示和调仓方式切换。
- 私钥字段成功后清空，页面状态及后续请求不再携带私钥。
- 账户或版本切换后旧预览、challenge 和 request_id 不能提交。
- 请求超时后沿用原 request_id 查询，不出现第二个下单请求。
- 服务端模式不回传私钥，浏览器模式行为不回归。
- 桌面与移动布局、加载中和结果未知状态可用。

### 10.4 标准回归与发布验收

按项目既有方式执行 Rust、Python、前端 unit/build/E2E。离线 mock 通过不代表主网授权、流动性或成交验收通过。

发布前另外核对：

- 数据加密密钥和 RSA 私钥文件权限、挂载范围及独立备份。
- 新表中实际保存的是 `ENC:v1:` 密文，测试私钥不出现在数据库其他字段、日志或回执中。
- 使用真实公开地址和官方 `/info` 做只读授权对照；只有经用户另行授权后才导入真实私钥。
- Web 健康、现有浏览器签名、账户切换和只读看板均正常。
- 回退只需关闭托管执行并重建 Web；保留表和密钥，不删除未知状态的执行记录。

首次真实钱包导入和小额主网调仓必须由用户另行明确授权，并单独记录验收结果。

## 11. 密钥轮换、泄露响应与备份恢复

本期提供离线管理命令和操作流程，不提供公网在线轮换接口。生产使用前必须用测试密钥和一次性数据库完成演练。

### 11.1 例行数据加密密钥轮换

1. 暂停托管保存和执行，等待已有请求到达可核对的终态；保留 Web 只读能力。
2. 备份数据库和当前密钥文件，生成独立的新随机 DEK 及新 `data_key_id`。
3. 离线命令同时读取旧、新 DEK，先 dry-run 解密并校验所有 active 密文及 AAD；任一记录失败则停止，不写数据库。
4. 在一个数据库事务内逐条解密并用新 DEK、新随机 nonce 重加密，更新 `data_key_id`。任何失败整体回滚，禁止部分记录静默跳过。
5. 切换 Web 到新 DEK 配置并重启，验证读取、授权检查和模拟执行；旧 DEK 从 Web 移除。
6. 旧 DEK 按既定保留期离线保存，用于解密轮换前的旧备份；保留期结束且旧备份全部淘汰后再安全销毁。

离线命令不记录明文，不通过 CLI 参数传密钥，不使用 runtime Web API。轮换期间不修改 API Wallet 地址和 `wallet_version`，存储 AAD 保持不变。

### 11.2 旧密钥或私钥疑似泄露

1. 立即关闭托管执行并阻止新请求，核对所有 `running`／`unknown` 动作。
2. 如果旧 DEK 与数据库密文可能同时泄露，应假设对应 API Wallet 私钥已泄露；仅重加密数据库不足以补救。
3. 在 Hyperliquid 撤销相关 Agent，为每个账户生成全新地址的 API Wallet，禁止复用旧地址。
4. 使用新 DEK 保存新钱包，重新验证授权；旧地址写入淘汰表，旧 DEK 按取证和旧备份需要隔离保管。

### 11.3 旧备份恢复

1. 在隔离恢复环境还原备份，不覆盖当前生产表。
2. 根据每条密文的 `data_key_id` 选择对应离线旧 DEK；缺少密钥时明确标记不可恢复，不尝试明文读取。
3. 校验 network、账户、地址、版本和 AES-GCM 完整性，再决定恢复到当前环境。
4. 恢复进入当前生产前重加密为当前 DEK，并重新做实时授权校验；已淘汰或已撤销地址不得恢复为 active。
5. 恢复后核对待处理执行意图、交易所订单和仓位，禁止把旧备份中的 `running` 状态当成可重发请求。

RSA 传输密钥可独立轮换：替换密钥对并更新 `key_id`，浏览器重新获取公钥和 challenge，旧未使用 challenge 失效；不影响数据库密文。所有轮换和恢复校验失败均失败关闭，不降级、不部分提交。

## 12. 后续基金化演进

本期接口和审计字段为后续能力保留扩展点，但不提前引入复杂基础设施。进入多人运营或自动化阶段前，应另行评估：

- 操作员、复核员、管理员等 RBAC 和双人审批。
- 按基金账户配置风险限额和允许动作。
- AWS KMS 或其他 KMS 管理数据加密密钥。
- 独立签名服务，使公网 Web 不再持有解密能力。
- 完整、防篡改的订单意图、审批、签名和成交审计链。
- API Wallet 定期轮换、撤销演练及密钥泄露应急响应。

## 参考

- [Hyperliquid：Nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)
- [Hyperliquid：Info endpoint／userRole](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [OWASP：Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html#key-lifetimes-and-rotation)
