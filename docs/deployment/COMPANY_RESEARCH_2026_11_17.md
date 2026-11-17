# 公司研究 HTML 页面部署记录

2026-11-17 15:59 UTC，经用户明确授权，已更新本机 `hl-app` 的 Web 与 gateway，接入正式研究快照。页面入口为现有网站的 `/research`。

## 版本与来源

- 研究功能与验收修复提交：`a9fd9e95c4623d32576fcc4917ab18e40403aebe`。
- 构建继续使用 2026-11-15 已上线的其他工作树改动，没有将其纳入研究功能提交。部署前对比运行容器的既有后端 Python 文件，差异仅为本功能的 `api.py`、`config.py` 两个接入文件；完整工作树差异指纹记录在本地部署元数据中。
- 新 Web 镜像：`sha256:901b19763ad6e23a98895624b76f5de8bd86fcc6bb2986db450d078ca4c0872b`。
- 新 gateway 镜像：`sha256:ee37344f357f69487c4afa8c4ba4e3a638d5d5aa9f82fe52031a53ba7ba1f6e2`。
- 前端实际资源：`/assets/index-Ch5jxfyZ.js`，含公司研究导航与阅读路由。
- 展示快照：`s93b5af9e66c56961c352130ff2592aa1`，格式 v2。
- 研究库来源：`stock-research` 提交 `5a61412c9b2c260f2ea99b7e06a45d91fdba5a1d`，生成时工作树干净，未修改源报告或索引。

## 执行范围

在 `.runtime/research-reports/` 生成正式快照，并以只读方式挂载到 Web 的 `/mnt/research-reports`。运行配置沿用原有 `compose.yaml`、`compose.managed-wallets.yaml`，新增 `compose.research-reports.yaml`；秘密环境文件仅增加快照根路径，现有应用配置指纹保持一致。

执行方式：

```sh
docker compose --env-file .runtime/docker.env \
  -f compose.yaml -f compose.managed-wallets.yaml -f compose.research-reports.yaml \
  build web gateway

docker compose --env-file .runtime/docker.env \
  -f compose.yaml -f compose.managed-wallets.yaml -f compose.research-reports.yaml \
  up -d --no-deps --force-recreate --timeout 60 \
  --wait --wait-timeout 120 web gateway
```

切换前再次只读确认运行中的执行请求数量为 0。`--no-deps` 跳过 migration、数据库授权与密钥初始化；worker 的容器 ID、启动时间及镜像均保持原样。交易 CLI 与原线上文件的 SHA-256 完全一致：`06529a162201150a4007518ab3f4a366f878adc5c3b4359cbba39fc260da9109`。本次未执行数据库迁移、未发送订单、未推送远端。

## 部署后核验

- Web 为 healthy，gateway 与原 worker 正常运行；经 gateway 请求健康接口返回 `ok`。
- 会话接口保持原有 `account` 模式，没有切换登录方式。
- `/research` 返回前端入口，实际 JS 资源包含公司研究功能。
- 列表、详情及正文接口的未登录网络请求均返回 401。
- 用实际 Web 镜像和挂载路径加载 manifest，得到 3 家公司、5 篇报告；五篇 HTML 均可读取，展示哈希与 manifest 一致，演示报告排除。
- 正式来源覆盖微软三篇、AMD 一篇、QXO 一篇；draft 仍可阅读，内容核验状态保持源索引登记值。

此前代码回归为后端 296 项通过、前端单元测试及构建通过、浏览器 62 项通过和 2 项跳过；部署构建再次执行前端单元测试与打包。线上真实账号登录后的浏览器操作未另行执行，本次没有创建账号、读取用户登录凭据或变更会话；因此不把上述核验描述为生产账号浏览器验收或公司财务事实验收。

不含凭据的详细部署元数据保存在被 Git 忽略的 `.runtime/deployments/research-20261117-before.json` 与 `research-20261117-deployed.json`。

## 回退准备

旧镜像保留为 `hl-app-web:before-research-a9fd9e9` 和 `hl-app-gateway:before-research-a9fd9e9`。未执行回退；如需回退，经授权并确认无运行中的执行请求后恢复这两个镜像，再使用上述相同 Compose 文件以 `--no-deps` 重建 Web 与 gateway。worker 不需变更。旧版本不提供研究接口，正式快照文件可继续保留。

## 公网正文嵌入修复（2026-11-17 16:34 UTC）

用户在真实页面遇到 `frame-ancestors 'none'`，正文无法嵌入；原先的内网核验未覆盖公网登录后的完整阅读链路，这是上轮验收的遗漏。应用正常正文响应是 `frame-ancestors 'self'`；无凭据公网请求另外观察到 Cloudflare 403 验证页（`cf-mitigated: challenge`）。该探测不能确定登录用户命中哪条公网规则，但原来让 iframe 直接导航 HTTP 正文的方式没有适配实际公网响应。

修复提交 `78d7986`：报告详情经原有鉴权 JSON 请求返回 `document_html`，浏览器生成本地 Blob 文档并交给受限 iframe。正文携带静态 CSP，iframe 继续禁止脚本与同源权限；报告的样式、图片、同快照链接及章节锚点保留。左侧“未发布资料”模块移除，后端仍保留登记与文件白名单。直接正文接口继续鉴权，不改变 Cloudflare 配置。

本轮后端 302 项通过、前端研究单元测试及构建通过、浏览器 64 项通过和 2 项跳过。新增桌面/手机回归模拟全站 `frame-ancestors 'none'`、正文路径返回 403 验证页，验证实际内容显示与章节跳转。部署前在新镜像内验证 JSON 正文契约和静态 CSP；部署后确认实际 JS 使用 `document_html` / `createObjectURL`，且不含已移除的模块文案。

- Web 镜像：`sha256:0d47de6b81e93c59e0fb9aefdf46718cd8dc7176a42baff153095e6b810f55f2`。
- gateway 镜像：`sha256:28d03fbf19dff2a61345086c6f8aa1003e2edbf77614182da8ff5bb61978130c`。
- 实际前端资源：`/assets/index-mdIkVxIq.js`。
- 快照沿用 `s93b5af9e66c56961c352130ff2592aa1`，仍为 3 家公司、5 篇报告；应用配置、worker 与交易 CLI 保持一致，未执行迁移或发送订单。
- 回退标签：`hl-app-web:before-research-csp-78d7986`、`hl-app-gateway:before-research-csp-78d7986`。
- 详细无凭据元数据：`.runtime/deployments/research-csp-20261117-before.json` 与 `research-csp-20261117-deployed.json`。

公网真实账号的登录会话没有代用；本次区分线上资源/服务核验与针对已报告策略的浏览器模拟，不把后者称为生产账号实测。修复提交未推送远端。

## 新页面阅读与侧栏简化（2026-11-17 16:49 UTC）

按用户要求移除“外部来源”模块，正文旁新增“新页面阅读”按钮。新标签页直接加载带鉴权的原快照 HTML，使用浏览器整页滚动，不含 iframe；默认内嵌预览保留。代码提交 `ec03c66`。

本轮仅构建并更新 gateway，Web 与 worker 的容器 ID 和镜像均保持不变，原报告快照与鉴权沿用。新 gateway 镜像为 `sha256:29af23283447c72cc159606684046ab2d0c661722df6841d4c3d2be99e9f6d37`，实际前端资源 `/assets/index-By1q0K_7.js`；部署后检查健康为 ok，新按钮文案存在，已删除的两项资料模块文案不在前端资源中，匿名研究请求仍为 401。

前端单元测试、类型检查及构建通过，浏览器 66 项通过、2 项跳过。新增桌面/手机用例在 `frame-ancestors 'none'` 和 `X-Frame-Options: DENY` 下真实打开独立标签页，验证正文、样式、图片、无 opener、关闭原页后刷新、原生折叠及匿名 401。线上真实账号浏览器未代行，不把模拟与资源核验称为生产账号操作。

本地元数据为 `.runtime/deployments/research-newpage-20261117-before.json` 与 `research-newpage-20261117-deployed.json`；回退 gateway 标签为 `hl-app-gateway:before-research-newpage-ec03c66`。未执行数据库迁移或交易，未推送远端。

## 公司单入口与紧凑列表（2026-11-17 17:07 UTC）

研究列表改为每家公司一行、一个入口，主报告优先；只有专题或更新的公司也保留入口。同公司的其他报告在阅读页切换，搜索专题不改变主入口。列表不再展开所有报告标题、数量及别名，未收录说明默认折叠。代码提交 `6e88553`。

前端单元测试、类型检查、构建通过，浏览器 68 项通过、2 项跳过。桌面/手机实际验证单公司单链接、行高不超过 80px、无整体横向溢出、专题搜索保留主报告以及阅读页切换与返回流程。三篇报告选择主入口和专题独有公司的回退规则另由单元测试覆盖。

本次仅更新 gateway：镜像 `sha256:b349fd1bb103f058906b4b29747ca8f45af16ab1e8f76e9a9d7b3ccc98256e4f`，实际资源 `/assets/index-FjNj_xE1.js`。部署后经 gateway 健康检查为 ok，新公司列表资源已生效，匿名研究请求仍为 401；Web、worker 容器和镜像均保持原样，原研究快照不变。公网真实账号浏览器未代行，不将上述模拟与资源检查称为该项实测。

本地部署记录为 `.runtime/deployments/research-company-entry-20261117-before.json` 与 `research-company-entry-20261117-deployed.json`，回退标签 `hl-app-gateway:before-research-company-entry-6e88553`。未执行数据库迁移或交易，未推送远端。

## 后续工具整理

重复核验和发布步骤已整理为仓库正式工具，使用方式见 [研究页面核验与发布流程](RESEARCH_RELEASE_WORKFLOW.md)。原部署记录和回退镜像继续保留；此次整理只执行工具测试与只读核验，不重新部署服务。任务结束时的一次性文件清理清单记录在 `.runtime/maintenance/`。
