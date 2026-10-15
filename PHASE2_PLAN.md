# hl-trader 第二步(v2 极简交互版):pair 开仓/关仓 + status 看板

> 日期:2026-10-15
> 状态:待用户审核;本次只写方案,不实现。
> 前置:第一步(最小 CLI)已完成离线验收与测试网联调,见 MVP_PLAN.md。
> v2 变更:按用户反馈砍掉自动化层,交互收敛为「两个操作 + 一个看板」。

## 1. 交互模型(全部)

| 命令 | 作用 | 写操作 |
|---|---|---|
| `hl-trader open [--notional <usd>] [--execute]` | 开 pair:多 HYPE 永续 + 空 BTC 永续,等名义 | 默认 dry-run,加 --execute 才下单 |
| `hl-trader close [--execute]` | 关掉整个 pair(反向平掉两腿) | 同上 |
| `hl-trader status` | 看板:仓位 + 资金费 + 动态 | 否,随时可跑 |

第一阶段已有的底层命令(`order`/`cancel`/`cloid`/`positions`/`account`)原样保留,仅供调试,日常使用就是上面三个。

## 2. open

- **两腿**:多 HYPE 永续 + 空 BTC 永续,等名义 1:1;每腿名义 = min(--notional 或配置, 0.7×权益),0.7 是硬上限,配置可写小不可写大。
- **taker 立即成交**:IOC 限价单,保护带 = 买 ask+2tick / 卖 bid−2tick,保护价内未成交部分立即撤销。
- **原子性兜底**:第二腿失败(拒绝/超时/部分成交)立即对第一腿下反向 IOC 单;兜底也失败则退出码 3(结果未知),禁止自动重试。
- **数量精度**:名义÷市价,按元数据精度只舍不入(截断后名义不超过目标)。
- 成交后写状态文件 `.hl-trader/pair-state.json`(0600)。账户已有 pair 持仓时拒绝重复开仓,提示先 close。

## 3. close

- 按**账户实际持仓**平掉两腿(以 pair-state 辅助核对,状态文件丢了也能用)。
- 同样 IOC + 保护带 + 单边失败反向兜底;平完清除状态文件。
- 输出平仓回执:两腿成交名义、实现盈亏、手续费、资金费净收支。

## 4. status 看板(只读,三块)

```
== 仓位 ==  权益 1087 USDC
  HYPE 多  +12.3 (名义 500U, entry 40.21, 杠杆 0.46×, uPNL +12.4)
  BTC  空  -0.0062 (名义 500U, entry 81307, 杠杆 0.46×, uPNL -3.1)
  名义比 1.023 (漂移 2.3%)

== 资金费 ==  (每 8h 结算,多 HYPE 是净付方)
  HYPE 当前 +0.0123% (年化 ≈13.5%)  BTC 当前 +0.0011% (年化 ≈1.2%)
  按当前持仓预估:日付 ≈1.9 USDC
  近 3 次收付:...

== 动态 ==
  解锁锚点:下次 cliff 2026-11-06(22 天后),今明无 announced claim 动作
  HYPE/BTC: 0.000494, 30MA 上方 2.1%(overlay 回场状态:在场内)
  官方动态:[可机器获取则列出,否则明示"请人工查看官方 X @HyperliquidX"]
```

- 数据全部来自 Hyperliquid info 端点(免费、无需签名、随时可拉):`metaAndAssetCtxs`(当前 funding、markPx)、`fundingHistory`(历史收付)、`candleSnapshot`(HYPE/BTC 日线算 30MA)。
- 解锁日历写死在配置(创世公告 2024-11-28 的归属表),7 天内有动作才提醒。
- 官方动态:实现时探测官网/文档的机器可读更新源;拿不到就**明确标注**,不静默、不编造。
- 每次运行实时拉取,不存文件、无 daemon。

## 5. v1 有、v2 明确砍掉的东西

- **自动再平衡(cron/rebalance 命令)**:两腿名义随价格漂移,status 里显著显示漂移%,想纠正就 `close` 重开(往返 taker 成本 ≈ 本金 0.09%)。这是你拍的极简交互的直接代价,写在这里知情接受。
- **自动熔断**:人盯 status,异常自己 close。
- **intel collect 日报文件、~/.hl-trader/intel/ 目录、自动写 stock-research**。
- pair 子命令组(plan-open/rebalance 等),改为顶层三个动词。

## 6. 不变的第一阶段纪律

dry-run 默认、写操作显式 --execute;写请求禁止自动重试;超时=结果未知;秘密不进聊天/仓库/日志;主网这一步不做(上线需另行书面授权)。

## 7. 实现步骤(批准后执行)

1. `order` 加 `--tif alo|ioc`(默认 alo 不变)。
2. 新增 `src/pair.rs`:BBO 查询、IOC 下单封装、名义→数量截断、两腿 open/close + 兜底、pair-state 读写。
3. 新增 `src/intel.rs`:funding 拉取与年化、HYPE/BTC 30MA、解锁锚点、官方动态探测。
4. status 渲染(三块)。
5. 扩展 mock 测试(IOC 成交、部分成交、第二腿失败兜底、精度截断、漂移计算、状态文件读写、已有持仓拒绝重复开仓),`cargo test/clippy/fmt` 全绿。
6. 报告后请用户授权测试网联调:`open` 建 pair → `status` 核对读数 → `close` 平仓归零。

## 8. 风险与已知限制(如实写进 README)

- 无自动再平衡:单边行情漂移可能持续扩大,极端时 pair 退化近似单腿敞口,靠人看 status 发现。
- 测试网验证不了真实滑点与流动性;资金费水平以 status 实测为准(HYPE 多仓是净付方)。
- 边缘衰减:+90.8%/年是单一年份回测值,status 只解决「看得见」,不解决「边缘还在」。
