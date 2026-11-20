# 账户权益修复部署记录（2026-11-20）

用户明确授权更新已验证的 Web 执行器并重载 gateway。2026-11-20 08:10 UTC 已完成切换与复验。

## 修复与镜像

11 月 19 日按 `e6de848` 发布的执行器覆盖了未提交的账户权益修复，旧 CLI 缺少 `account.source_queries.portfolio`，与运行后端的完整来源键合同不一致，导致权益区块降级。本次使用源码收尾提交 `72f3d36`、`7b18dae` 的预构建镜像，恢复 Unified / Portfolio 账户总权益读取。

- Web 镜像：`hl-app-web:equity-closeout-7b18dae`，ID `sha256:87595dae9fef150b5387ffa8bffb830a8bb82d4f81757bb1a144578e3d9010b7`。
- CLI SHA-256：`cba86b2d40f47527511d3106cd7927a64fb3c8b073da98fff8f693c0b6e7d2ad`。
- 构建记录：`.runtime/deployments/equity-closeout-20261120-build.json`；候选镜像核验：同目录 `equity-closeout-20261120-verified.json`。
- 候选与原 Web 的 70 个已安装 Python/SQL 文件哈希一致；Rust 相对已上线 `e6de848` 只改变两个看板文件。隔离提交验证及修正缓存后的标准入口均为 Rust 200 项通过、1 项既有公网手工检查忽略，完整源码验证见[交付基线](RELEASE_BASELINE_2026_11_20.md)。

## 切换范围与保护

发布期间持有现有研究发布锁；使用 `deploy/research_release.py` 中的配置、服务指纹、只读执行请求和研究快照核验组件。普通研究发布要求 CLI 不变的限制保持原样；本次按已授权的执行器升级单独核对新旧 CLI 哈希。

切换前确认基础 Web、gateway、worker 与候选验证时的实例、镜像、配置、挂载指纹一致，研究快照未变化；再次只读检查执行中的托管请求为 0。保留旧镜像后，将已验证候选按镜像 ID 标记为 Compose 的 `hl-app-web:local`，仅重建 Web：

```sh
docker compose --env-file .runtime/docker.env \
  -f compose.yaml -f compose.managed-wallets.yaml -f compose.research-reports.yaml \
  up -d --no-deps --no-build --force-recreate --timeout 60 \
  --wait --wait-timeout 120 web
```

Web healthy 后，在原 gateway 容器内重新加载 nginx 配置以解析新 Web 地址。gateway 容器和镜像、worker 容器和镜像均保留；所有服务的环境与挂载指纹一致。没有执行数据库迁移、密钥初始化、真实下单或 Git 推送。

## 部署后复验

- 新 Web 和 CLI 哈希与已验证候选一致，健康接口与内网 gateway 正常。
- 前端入口资源及哈希保持一致，研究快照仍为 `s1cee4a3ba5dcbc6b149e3a12798a688a`，7 家公司、27 份 HTML，27 个展示文件哈希核验通过；匿名研究请求均为 401。
- 在新 Web 内，使用已安装服务和 CLI 读取现有活跃公开账户，再由已安装后端严格校验快照；数据库连接仅只读，输出不记录地址或权益金额。

| Pair | 账户模式 | 权益区块 | 权益范围 | portfolio 来源 | 持仓区块 |
| --- | --- | --- | --- | --- | --- |
| HYPE/BTC | unifiedAccount | ok，权益非空 | account | ok | ok |
| NBIS/CRWV | unifiedAccount | ok，权益非空 | account | ok | ok |

两组均包含 `mode/balances/portfolio`，无权益诊断。此项证明运行 CLI 与消费端合同一致且真实只读账户查询成功，不替代公网真实登录浏览器操作、真实交易或 portfolioMargin 模式的单独来源验收。

## 证据与回退

发布前、切换阶段及发布后记录：`.runtime/deployments/equity-closeout-20261120T080959Z-c01dcd3d.{before,applying,deployed}.json`。真实公开账户只读复验：同目录 `equity-closeout-20261120-live-accounts.json`。

原 Web 的回退标签为 `hl-app-web:before-equity-closeout-20261120T080959Z-c01dcd3d`，保留旧 CLI 与 Python 模块；未执行回退。后续回退仍须获得明确授权，先确认没有执行中的请求，沿用现有配置与卷，仅恢复 Web 并重载 gateway，之后复验研究快照及账户块状态。旧版本缺少权益修复，回退后应如实标记该区块的已知降级。
