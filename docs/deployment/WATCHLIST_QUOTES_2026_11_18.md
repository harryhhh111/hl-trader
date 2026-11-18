# 关注股行情启用记录（2026-11-18）

用户要求先开启行情同步。本次增加独立 `HL_APP_QUOTES_ENABLED` 开关并更新 worker；没有启用公告直接采集、stock_data 同步或 AI 增强。Web 和 gateway 的容器、镜像与运行配置保持原样。

## 配置与镜像

- `HL_APP_QUOTES_ENABLED=true`；`HL_APP_COLLECTORS_ENABLED=false`；`HL_APP_NEWS_SYNC_ENABLED=false`。
- 调度器开启，`HL_APP_COLLECTION_INTERVAL_SECONDS=3600`。worker 每 30 秒检查到期任务，新关注优先处理；每只证券成功后下一次尝试为一小时后。
- worker 通过 `HL_APP_WORKER_IMAGE` 固定为 `hl-app-worker:quotes-20261118-c98efae864ab`，镜像 ID 为 `sha256:5f148193557064ae7655542a690c141251910f3a982ad9626c19e0b71d6c21b6`。
- 镜像基于原运行 worker，只替换 `hl_app/workers/__init__.py` 及同文件的打包源码副本。逐文件核对已安装 Python 模块，其他模块未变化；交易 CLI SHA-256 仍为 `06529a162201150a4007518ab3f4a366f878adc5c3b4359cbba39fc260da9109`。
- 没有执行迁移、重建 Web、数据库授权、密钥初始化、订单或远端推送。

独立开关留空时沿用旧总采集开关，显式 `false` 可独立暂停行情。普通发布默认仍使用 `hl-app-web:local`，已有独立 worker 镜像配置时须保留该配置。构建 worker 时不要以 `docker compose build worker` 覆盖固定镜像，应按下面方式从旧 worker 派生。

## 构建和切换方式

下面是本轮使用的方式，任何后续部署、重启和回退仍须遵守 `AGENTS.md` 的授权要求。先检查现有 worker 的镜像、配置、挂载及交易 CLI，保留唯一回退标签，再构建新镜像；新一轮应使用自己的版本标签。

```sh
docker build --network=none -f deploy/docker/Dockerfile.worker-quotes \
  --build-arg BASE_IMAGE=hl-app-worker:before-quotes-20261118-5b669cc1be38 \
  -t hl-app-worker:quotes-20261118-c98efae864ab .

# .runtime/docker.env 中设置独立行情开关和 HL_APP_WORKER_IMAGE 后：
docker compose --env-file .runtime/docker.env \
  -f compose.yaml -f compose.managed-wallets.yaml -f compose.research-reports.yaml \
  up -d --no-deps --no-build --force-recreate --timeout 30 \
  --wait --wait-timeout 60 worker
```

`Dockerfile.worker-quotes` 的安装路径对应当前 Python 3.12 镜像；更换 Python 版本或打包路径时需同步检查。不要将其他版本源码直接叠加到旧镜像。

## 实际回补与验证

首轮在 2026-11-18 13:25 UTC 成功完成腾讯真实请求与数据库写入：

| 证券 | 状态 | 本轮写入或更新 | 最新交易日 |
| --- | --- | --- | --- |
| CRWV | success | 63 条日线 | 2026-11-17 |
| NBIS | success | 63 条日线 | 2026-11-17 |

总日线数量从 126 增至 146，其余为已有记录更新。两只证券的错误均为空，下一次尝试分别为 14:25:17 UTC 左右。按各启用关注账号调用实际列表与行情读取服务，均返回 11 月 17 日数据、`status=ok`、`stale=false`。这是数据库与读取服务核验，没有代用生产账号浏览器；已有页面可点击刷新读取新数据。

非数据库、非 live 后端回归 311 项通过、99 项排除；其中独立开关测试覆盖继承旧配置、单独开启、显式关闭、非法值拒绝及保持资讯关闭。内网 gateway 健康与原研究快照核验通过，公告、公告采集运行记录和资讯同步事件收据数量保持不变。

无凭据部署证据存于 `.runtime/deployments/quotes-20261118-{before,prepared,applying,deployed}.json`，包含模块哈希、容器指纹和实际回补结果，不保存环境变量原文。

## 暂停与回退

暂停行情可将 `HL_APP_QUOTES_ENABLED=false` 后，仅按上述方式重建 worker。需要恢复旧代码时使用回退镜像 `hl-app-worker:before-quotes-20261118-5b669cc1be38`，并恢复本轮前的开关配置。已入库的共享日线可以保留，无需删除或回退数据库。
