# 多 Pair 的保证金模式与账户边界

核对日期：2026-11-11。本文解释账户组织和风险边界，不授权修改保证金、划转资金或交易；具体合约支持的模式以交易所实时配置为准。

## Cross 与 isolated 隔离什么

Cross 让同一保证金池中的 cross 仓位共享抵押品，提高资金使用效率，也使一组仓位的亏损可能影响其他仓位。Isolated 将保证金限定在单个合约仓位；另一腿的浮盈不会自动成为该 isolated 仓位的保证金。一腿独立被清算后，pair 可能留下单腿敞口。因此，逐仓不能直接实现“每个 pair 独立承担风险”。依据：[官方 Margining](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining)。

Cross 也不保证两腿可以完全抵消风险，或会同时平仓。不同合约仍有各自价格、流动性和维持保证金要求。

## 跨 DEX 的共享范围

保证金模式还要结合账户抽象模式理解：

| 账户模式 | Cross 保证金共享范围 |
| --- | --- |
| Manual / Standard | 每个 DEX 分别管理保证金，cross 只在同一 DEX 内共享 |
| Unified account | 同一抵押资产的余额支持对应的跨 DEX cross 仓位；不能把原生 DEX 与 `xyz` 自动当成独立资金池 |
| Portfolio margin | 按支持资产与组合规则计算，不能使用普通单 DEX 的余额口径推算整体强平风险 |

部分 HIP-3 市场使用 `noCross` 或 `strictIsolated`，不能选择 cross。不能从一个合约可用 cross 推断所有合约都支持。依据：[账户抽象模式](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)、[HIP-3 Margin Modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining#hip-3-margin-modes)。

## 多 Pair 的组织方式

- 如果目标是让各个策略分别承担风险，且账户具备子账户使用权限，可考虑每个 pair 分配独立子账户和资金；合约允许时，在该子账户内用 cross 管理两腿。这是账户组织建议，不是交易所提供的 pair 专属保证金类型，也不能隔离主账户授权被滥用的风险。
- 如果多个 pair 共用一个交易地址，cross 更便于共享保证金，但必须按整个共享池评估风险；页面上分别设置止损不会隔离抵押品。
- 如果选择 isolated，应逐腿管理保证金并处理单腿先被清算的风险，不能仅以两腿合计浮盈为正就认为安全。
- 同一网站登录账号下添加多个公开地址，不会自动创建 Hyperliquid 子账户或隔离链上资金。是否隔离取决于真实交易地址和交易所账户关系。

多个 pair 若共享同一合约，本应用会按账户中该合约的实际仓位读取和计算；当前没有将同一合约仓位按策略拆分归属的账本。不要把不同 pair 页面误认为互不关联的独立仓位。

## 当前应用能提供的观察

看板显示交易所返回的每腿 `cross` / `isolated`、当前杠杆倍数和市场上限。对于 Unified / Portfolio 等不能套用单 DEX 权益的账户模式，Rust 看板会保留可核对的仓位并降级权益字段；这不等于已经实现完整的统一账户保证金率或组合强平风险监控。

用户可通过每腿的“调杠杆”入口显式选择 `cross` / `isolated` 和正整数倍数，使用托管或浏览器 API Wallet 签名。执行器校验市场上限、账户与仓位前提，提交后重新读取实际杠杆；该操作只作用于所选合约，不改变另一腿，也不会附带下单。结果未知时禁止直接重试。

当前 Pair 风控使用两腿未实现盈亏阈值，并由用户人工确认平仓；它不是账户强平保护，也不会自动追加保证金或切换保证金模式。
