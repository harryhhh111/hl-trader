# 独立 HTML 的模块导航修复

2026-11-20，修复提交 `5a81b32` 已获用户授权部署，Web 与部署后复验通过。

快照生成器原本将所有报告内链统一写成 `/research/reports/{report_id}?snapshot=...`，供网站中的隔离阅读框导航。因此从「新页面阅读」直接打开 HTML 后，点击模块也会回到网站阅读器。

修复在独立正文接口 `/api/v1/research/content/{snapshot_id}/{path}` 的响应阶段转换链接：只将该快照 manifest 中已登记报告的阅读器链接转换成 HTML 正文地址，并使用当前页面导航。保留快照标识及章节锚点，专题的返回首页链接也按同样方式转换。网站阅读器的 JSON 正文维持原有导航。

例如从 `s7658fcec893ae6e0a4ff5f57acdc32f0/SSD/SNDK/reports/main.html` 打开业务模块后，仍访问同一快照的 `SSD/SNDK/reports/business-model.html`，不会回到 `/research/reports/sndk-business-model`。响应转换支持已保留的旧快照；不修改研究库、快照内容文件、原件、manifest 或快照指针，不需要重新同步研究库。存储清单中的展示哈希仍对应磁盘快照副本，独立 HTML 响应会进行上述导航转换。

转换使用 HTML 解析器识别真实链接，只替换匹配的锚点起始标签；样式、SVG、中文、字符引用、注释及本页锚点保留原样。鉴权与原有响应头策略保持不变。

验证结果：

- 研究后端 61 项测试通过，覆盖旧快照、兼容路径、中文路径/锚点、首页与专题往返、存储副本不变及阅读器原有导航。
- 桌面/手机浏览器 28 项研究页面测试通过，包括禁止嵌入策略下独立页打开、关闭原页面、模块点击、专题刷新、返回首页、无新增标签及匿名拒绝。
- 使用真实 SNDK 旧快照在隔离 TestClient 中核验，全部报告内链保持独立 HTML，链接目标均返回 200；不代表已更新生产服务或完成公网登录后的浏览器验收。
- 浏览器验证只包含 Git 已提交基线及本次研究模块/E2E 改动，没有混入当前工作区其他功能。

部署准备使用 [Dockerfile.research-navigation](../deploy/docker/Dockerfile.research-navigation)，以运行中 Web 的固定镜像为基础，仅覆盖 `routes/research.py` 与 `services/research_display.py` 两个模块，同时保持安装目录与镜像内源文件一致。构建不会重启服务；发布前仍需按 `AGENTS.md` 获得明确授权。发布时应沿用现有环境与挂载，核对交易 CLI 哈希与运行中的执行请求，保持 gateway、worker 和研究快照不变，切换 Web 后复验。

## 部署与复验

使用 `deploy/research_release.py deploy-prepared-web --build-record ... --apply` 发布已验证镜像 `sha256:f0870d90dd5e0db5ae320010abfa32d683a0ce3597df27afe689b1cc2af3b2d4`。该模式已纳入发布工具 30 项测试；部署未重新构建工作区。Web 健康、gateway 页面及前端资源、21 个快照文件哈希、匿名接口 401 核验通过。gateway 容器/镜像保留，仅重新加载 nginx 配置；worker 实例/镜像保留。运行环境、挂载、交易 CLI 和研究快照均与发布前一致，仍为 7 家公司、21 份 HTML。切换前运行中的执行请求为 0，未执行数据库迁移或下单。

在运行中的新 Web 内，用已安装路由和实际挂载的旧 SNDK 快照执行 `deploy/verify_research_standalone.py`：11 个不同的报告/锚点目标均返回 200，链接保持独立 HTML；网站阅读器 JSON 仍保留原有导航。两个模块的安装文件哈希与已验证构建记录一致。此项使用隔离 TestClient，未完成公网真实登录浏览器验收。

证据位于 `.runtime/deployments/research-prepared-web-20261120T055948Z-beb47d85.{before,deployed}.json`、`research-standalone-build-5a81b3259cf5.json` 和 `research-standalone-live-20261120.json`。回退镜像为 `hl-app-web:before-research-prepared-20261120T055948Z-beb47d85`。
