# 调仓滑点与盘口深度部署记录（2026-11-19）

用户明确授权部署 `e6de848`。本次更新 Web、Rust 执行器与 gateway，行情 worker 的容器、镜像、配置及挂载保持原样。行情独立开关仍为开启，公告与 stock_data 同步仍关闭。

## 构建与版本

工作区还有其他未提交变更，因此 Rust 源码与三个 Python 调仓模块从 `e6de848` 导出构建；未提交的 Rust 看板改动没有纳入发布。Web 基于原运行镜像，仅替换以下已安装模块、同文件的打包源码副本和交易 CLI，逐文件校验其他 Python 模块保持一致：

- `routes/hyperliquid.py`
- `services/hyperliquid_adjustments.py`
- `services/hyperliquid_managed_adjustments.py`

前端保留已测试工作区中的既有页面功能，完整构建文件哈希记录在私有 build manifest 中。隔离构建使用自动清理目录，未创建 `/tmp` 一次性发布脚本。

| 产物 | 本轮版本 |
| --- | --- |
| Web 镜像 | `sha256:74784939d6bdaf7a66c819e8bc8b7b19fc9604054f75cc1a07e243d2b4ce56b7` |
| gateway 镜像 | `sha256:0cd1a5b2dd16dffff401b74295f1662b8473ec0f7751d48a5638c853bc8f01db` |
| 交易 CLI SHA-256 | `69d05b02eea5343b67e09b811e118e150d8a7dad7628951006e8c07849f0752e` |
| 前端入口资源 | `/assets/index-C8DQmLjM.js` |

原研究发布工具会拒绝交易 CLI 变化，该限制没有放宽。本轮使用独立构建流程处理经过授权的执行器升级：

```sh
python3 deploy/build_adjustment_release.py --commit e6de848 \
  --base-web-image hl-app-web:before-depth-e6de848-20261119 \
  --web-image hl-app-web:depth-e6de848 \
  --gateway-image hl-app-gateway:depth-e6de848 \
  --record .runtime/deployments/adjustment-depth-20261119-build.json
```

此命令只构建和测试，不重启服务。`Dockerfile.adjustment` 的覆盖路径对应当前 Python 3.12 镜像，升级打包方式时需重新核对。用于其他版本时须先核实改动是否仍限于这三个模块，不能直接套用覆盖清单。

验证镜像后，将两个发布镜像标记为 Compose 的 `hl-app-web:local` 和 `hl-app-gateway:local`，使用原环境文件与三个 Compose 文件切换：

```sh
docker compose --env-file .runtime/docker.env \
  -f compose.yaml -f compose.managed-wallets.yaml -f compose.research-reports.yaml \
  up -d --no-deps --no-build --force-recreate --timeout 60 \
  --wait --wait-timeout 120 web gateway
```

没有执行 migration、数据库授权、密钥初始化或远端推送，没有读取真实私钥或发送订单。

## 验证结果与范围

- 本次提交的隔离 Rust 源码 198 项测试通过、1 项公网手工检查跳过；它不包含工作区未提交的两个 Rust 测试。前端镜像构建再次通过全部单元测试和生产构建。
- 镜像内确认默认 `max_slippage_bps=5`，非法类型、零和超限参数拒绝，CLI 提供对应参数；Python 模块变动严格限定为上述三个文件。
- 切换前再次只读确认执行中的调仓为 0。切换后 Web healthy、gateway 正常，健康接口返回 ok；两者配置与挂载指纹保持一致。
- 新前端资源实际包含滑点选择与深度明细；匿名访问 Hyperliquid、关注股和研究接口均为 401。研究快照仍为原 3 家公司、5 篇报告。
- 新 CLI 对 HYPE/BTC、每腿目标 3500 USD 执行了**真实盘口只读预览**，返回 `status=preview`、无 action ID、最大滑点为 5。HYPE 预计买入 1.18、限价内可见 4141.71；BTC 预计卖出 0.00075、限价内可见 65.95662，均覆盖所需数量。这些是预览数量，不是实际成交。
- 数据库迁移版本保持 `001`–`014`；行情 worker 未重建。没有代用生产账号浏览器，也没有把只读预览描述为真实订单成交验收。

完整无凭据元数据与构建日志保存在被 Git 忽略的 `.runtime/deployments/adjustment-depth-20261119-*`。实现与前一轮工作区回归详见[实施合同第 7 节](../HYPERLIQUID_ADJUSTMENT_LIVE_QUOTES.md#7-最大滑点与订单数量深度检查2026-11-19)。

## 回退与页面使用

回退镜像保留为 `hl-app-web:before-depth-e6de848-20261119` 和 `hl-app-gateway:before-depth-e6de848-20261119`，未执行回退。若需回退，须先确认没有执行中的请求，再将旧镜像恢复到相应 Compose 标签并仅重建 Web/gateway；保留现有数据库和交易回执。后续部署、重启和回退均遵守 `AGENTS.md` 的明确授权要求。

用户刷新页面后应重新生成调仓预览。升级前的托管预览和待签名动作不能继续提交；未知结果仍按原回执核对，不能因为升级而直接重试。
