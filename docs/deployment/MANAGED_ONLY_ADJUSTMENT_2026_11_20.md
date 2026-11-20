# 调仓弹框仅使用托管钱包：部署记录

2026-11-20 经用户明确授权，将 `3b828b6` 的前端改动部署到 gateway。调仓弹框直接使用托管 API Wallet，不再提供本地私钥或扩展钱包入口；金额确认、未配置提示及原请求恢复/轮询保留。

使用现有正式流程发布：

```sh
python3 deploy/research_release.py deploy --scope frontend --apply \
  --expect-text '签名方式：托管 API Wallet' --expect-text '托管钱包读取失败'
```

发布前已有前端单元测试、构建和 Chromium 桌面/手机回归 76 项通过、2 项跳过；本轮镜像构建再次通过单元测试与构建。已核对弹框源码与提交一致，部署后经真实 gateway 读取到新页面资源与预期文案。

- gateway 镜像：`sha256:f6ef5bed141b450691c6959ef3e02621fb733207481c573d8a1e24f3c6cbdebe`。
- 页面资源：`/assets/index-GeR44wPA.js`，SHA-256 为 `57809ec475bb81c4c3b8f025642b9b141ce3ff8d7243916c509cbaef29ea0e23`。
- 核验完成时间：2026-11-20 02:47:51 UTC；健康接口正常，匿名研究接口仍为 401，研究快照保持原样。
- Web、worker 的容器、镜像、配置与挂载均保持一致。交易 CLI 仍为 `69d05b02eea5343b67e09b811e118e150d8a7dad7628951006e8c07849f0752e`。
- worker 独立行情开关仍为 true，公告总采集及 stock_data 同步开关仍为 false。

本轮没有执行迁移、密钥初始化、真实下单或远端推送，也没有代用生产账号浏览器操作。部署核验范围为服务状态与内网实际资源，交互证据来自上述隔离浏览器回归。

私有部署记录为 `.runtime/deployments/research-20261120T024728Z-3b828b681287-06a6ae58.{before,deployed}.json`，另有 `managed-only-20261120-summary.json` 与发布日志。旧 gateway 镜像保留在 `hl-app-gateway:before-research-20261120T024728Z-3b828b681287-06a6ae58`，未执行回退。后续重建或回退仍须遵守 `AGENTS.md` 的部署授权要求。
