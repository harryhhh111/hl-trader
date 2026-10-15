# hl-trader

Hyperliquid **测试网**最小命令行工具（MVP 第一步，方案见 `MVP_PLAN.md`）。

闭环能力：查行情 → 显式提交 ALO 限价单 → 按 cloid 查真实状态 → 按 oid 撤单。

**默认 dry-run**：不加 `--execute` 时，不加载私钥、不生成签名、不发送任何写请求。

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

```sh
hl-trader market --coin BTC
hl-trader account --address 0x<账户地址>
hl-trader order --coin BTC --side buy --price 60000 --size 0.001 --cloid 0x<32位hex>          # 预览
hl-trader order --coin BTC --side buy --price 60000 --size 0.001 --cloid 0x<32位hex> --execute # 签名提交
hl-trader status --address 0x<账户地址> --cloid 0x<32位hex>
hl-trader cancel --coin BTC --oid 12345          # 预览
hl-trader cancel --coin BTC --oid 12345 --execute # 签名撤单
```

首版只支持测试网标准 BTC 永续合约；端点硬编码为 `https://api.hyperliquid-testnet.xyz`，
不支持主网、不允许任意 URL 覆盖、不跟随重定向。

## 语义与退出码

- HTTP 200 或顶层 `ok` 不等于成交；真实订单 ID、成交量、价格只来自交易所响应/查询。
- 退出码：`0` 成功；`2` 输入/配置/校验错误；`1` 明确的失败（拒单、无权限等）；
  `3` **结果未知**（超时、连接中断、响应格式无法识别）。退出码 3 时禁止直接重试下单，
  先运行 `status --cloid` 人工核对。写请求永不自动重试。
- `cloid` 幂等：相同 cloid 不会自动再次提交；本地回执（`.hl-trader/`）仅用于防误重发，
  不能替代交易所状态核对。
- 只撤 `--oid` 指定的订单；不批量撤单、不自动平仓；撤单失败/已成交会明确报告，
  不声称已经消除敞口。

## 离线测试

```sh
cargo test
```

测试全部基于本地 mock HTTP 服务器，不访问主网/测试网，不需要任何真实私钥。

## 许可与来源

- 本项目自身许可证：**待确认**（见 `MVP_PLAN.md` §2，未默认指定 MIT/AGPL）。
- 协议签名与类型复用社区 Rust SDK [`infinitefield/hypersdk`](https://github.com/infinitefield/hypersdk)
  （MPL-2.0，官方文档收录）。第三方许可明细见 `THIRD_PARTY.md`。
