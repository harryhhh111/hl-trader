# 公司研究 HTML 功能独立验收

首次验收日期：2026-11-17，首次结论为不通过。下文保留当时的问题、位置和复现证据；最新状态：R1–R6 已在工作树修复并完成本地复验，结果见文末修复记录。尚未部署或切换运行服务的快照。

首次验收只检查实现、执行测试、在临时目录生成样本和记录验收结果；当时未修改功能代码，未修改 stock-research 或 stock_data，未部署、重启或推送。本文是独立复验结果，优先于开发方案第九节对本次运行范围的描述；不代表重新核验公司研究内容和财务事实。

## 已通过的范围

- `make test-backend`：280 passed、99 deselected；有测试依赖弃用和 pytest 缓存权限提示，不影响该次测试结果。
- `frontend` 中 `npm run test:unit && npm run build`：通过，包括研究页面单元测试、TypeScript 检查和生产构建。
- `make test-e2e`：50 passed、2 skipped，包含研究功能在 desktop/mobile 下的 10 项现有测试。默认 token 场景的跳过项不算通过其他认证模式验收。
- 真实研究库只读生成临时快照：3 家公司、5 篇报告，跨目录微软报告合并，QXO 无 YAML 仍展示，演示报告排除，符合当前用户筛选要求。
- 现有匿名访问、文件白名单与基本越界测试通过。

**现有测试通过不能证明图片已加载或生产挂载可用。** 本轮 E2E 日志已出现 iframe 图片 401，而相关用例仍然通过。

## 必须修复的问题

### R1 / P1：生成快照无法由默认 Web 容器用户读取

位置：`backend/hl_app/services/research_reports.py:1280`、`:1299`；关联 `Dockerfile:35` 和 `compose.research-reports.yaml`。

快照从 `tempfile.mkdtemp()` 创建的 staging 目录直接重命名，保留了目录权限 `0700`。主机执行 CLI 的用户与 Web 镜像的 UID 10001 不一致时，容器不能进入该快照目录。只读挂载不会改变其权限。

本轮实际生成的目标目录权限：

```text
700 app app snapshots/sab846b4ff0cb1fd0b80d421c186dc55b
```

用 `hl-app-web:local`、`--user 10001`、只读挂载该生成产物并调用 `load_snapshot()`，实际得到：

```text
PermissionError: [Errno 13] Permission denied:
'/reports/snapshots/sab846b4ff0cb1fd0b80d421c186dc55b/manifest.json'
```

该异常也不在路由捕获的 `ResearchSnapshotError` 内，因此不能保证向用户返回约定的 503。

修复要求：明确生成产物的服务用户/组与目录、文件权限；在发布指针之前验证目标 Web 用户可以遍历目录和读取清单/正文。增加不同生成用户与运行用户的只读挂载验收，并把读取权限异常转换为约定的服务不可用响应，不能以 root 运行 Web 绕过。

### R2 / P1：sandbox 内获准图片无法沿用当前 Cookie 认证

位置：`frontend/src/pages/research-report-page.tsx:161`，关联正文响应的 CSP sandbox、现有 SameSite=Lax 会话 Cookie 和 `require_session`。

真实浏览器中顶层页面发起 iframe 正文请求时有会话，返回 200；iframe 内图片请求没有会话，返回 401。本轮现有 E2E 输出已出现：

```text
GET /api/v1/research/content/<snapshot>/ORCL/reports/main.html 200 OK
GET /api/v1/research/content/<snapshot>/ORCL/assets/chart.png 401 Unauthorized
```

独立 Chromium 复现沿用代码中的完整 CSP、空 sandbox 和 SameSite=Lax Cookie，得到 `image.complete=true`、`image.naturalWidth=0`。服务端记录正文 `hasCookie=true`、图片 `hasCookie=false`。

现有用例仅对 `<img>` 使用 `toBeVisible()`，该断言在破图时仍然通过；通过 `page.request` 单独拿图片则走顶层 Cookie，不能证明 iframe 加载成功。同类相对报告导航也必须真实点击验证，不能用外层报告切换代替。

修复要求：实现与静态隔离和逐资源授权兼容的交付方式，例如有明确变换记录的展示副本内嵌获准依赖，或经过完整验证的独立内容来源认证方案；保留源 HTML。不应以开放整个研究库、取消资源鉴权或直接增加 allow-same-origin 解决。浏览器断言至少包含图片 `naturalWidth > 0`、真实响应状态和 iframe 中实际报告链接导航。

### R3 / P2：获准独立 CSS 文件仍被 CSP 拦截

位置：`backend/hl_app/routes/research.py:22`。

当前 `style-src 'unsafe-inline'` 只允许内嵌样式，未允许获准的样式资源。快照即使收录 `<link rel="stylesheet" href="style.css">` 与对应 asset，浏览器也不会加载它。

独立 Chromium 复现得到明确 CSP 错误：

```text
Loading the stylesheet ... violates ... style-src 'unsafe-inline'
```

预期由 CSS 设置为 `rgb(1, 2, 3)` 的文字仍为 `rgb(0, 0, 0)`。这与 R2 是两项独立故障；仅修好 Cookie 不会恢复样式。

修复要求：按最终资源交付方式调整 CSP，仍限制到获准内容；用有独立 CSS 的报告校验实际 computed style。若选择静态内嵌方案，应检查 CSS 的递归资源依赖，而非只把文件复制进快照。

### R4 / P2：仅更新公司身份信息时，网站继续使用旧快照

位置：`backend/hl_app/services/research_reports.py:1252`、`:1299`；关联 `load_identities()`。

快照 ID 包含索引、正文、assets 和 Git 状态，却没有包含实际消费的 research.yaml 内容。身份文件也没有加入来源变更跟踪。对非 Git 来源，或已处于 dirty 状态且 HEAD/索引/正文未变的工作树，仅修改公司名、别名、证券后再次生成，会命中旧快照目录并删除新的 staging。

使用 fixture 连续生成，第二次只把 Microsoft 改为 Microsoft Updated，实际结果：

```text
same_snapshot_id = true
build_snapshot 返回名称 = Microsoft Updated
load_snapshot 实际读取名称 = Microsoft
```

修复要求：将消费到的身份信息/其来源字节纳入快照指纹与读取一致性检查，确保生成返回值、磁盘 manifest 和 Web 读取结果相同。增加连续身份更新、缺失后新增身份文件的回归用例。

### R5 / P2：索引已登记正文哈希不符时仍展示新正文

位置：`backend/hl_app/services/research_reports.py:1085`。

当前只追加 `content_hash_mismatch` warning，并继续接收报告。把某报告的登记哈希设为 64 个 0，快照仍接收该报告，且阅读页没有展示这条诊断。正文已变更时，网站仍使用原先的日期和核验标签，破坏索引与正文版本的对应关系。

修复要求：已登记的非空哈希必须匹配实际正文，否则排除该报告并给出明确诊断；若无任何有效候选则保留原指针。继续允许用户授权的 draft 且哈希为 null 的场景，并只记录实际哈希。补充非空哈希不符和 ready 无哈希的消费契约测试，不能把「允许 draft」等同于忽略非空登记哈希。

### R6 / P2：未发布必需资源会漏过报告可用性检查

位置：`backend/hl_app/services/research_reports.py:1042`、`:1060`。

两种情况均已复现：

1. HTML 使用 `<img src="assets/missing.png">`，该路径列入 unpublished_links。解析器在检查 display 类型之前提前 continue，报告被接收，但图不存在。
2. HTML 使用 `<img src="chart.png">`，文件在源库存在但未列入 assets。解析器仅给 link_not_published warning，报告被接收，但快照文件白名单没有该图，访问必然失败。

修复要求：必需展示资源必须在实际可交付文件集合中；unpublished_links 只能说明附件/来源超链接，不能豁免 display 依赖。检查应覆盖获准 CSS/SVG 等文件的递归依赖、内联 style、srcset 与锚点/查询串的正确 URL 解析。增加独立失败样本，确保这些报告不会被当作可阅读报告接收。

## 其他已确认缺口

- 索引 redirects 仅被解析，未写入快照或由内容路由解析。独立样本登记 `MSFT/old.html → msft-main` 后，快照中没有对应兼容路径；按原方案访问会是 404。当前真实索引 redirects 为空，因此尚未影响上述五篇报告，但不能称兼容路径支持已完成。
- 跨报告链接中的 fragment/query 未通过 URL 拆解，`传统软件与现金流.html#cash` 被当作文件名并误报不存在；展示中已知资源缺口可能失真。
- 规划文档仍同时包含「命令当前尚未实现」等规划期描述和「全部实现」记录，修复后应统一，并将浏览器测试通过范围写准确。

## 修复后的复验顺序

1. 跨 UID、只读挂载验证列表和正文可读。
2. 浏览器真实读取获准图片、独立样式、同快照相对报告链接，检查资源响应与内容结果；退出后仍保持访问限制。
3. 身份信息更新、登记正文哈希不符、未发布必需资源和旧路径兼容各自加入有失败判定的回归用例。
4. 重跑现有离线、前端和浏览器测试，再用真实库生成独立临时快照确认实际公司/报告集合。正式接入运行服务另外按授权执行。

上述问题修复前，不建议把现有「已实现、测试通过」作为整体验收通过或可直接上线的依据。

---

## 修复与复验（2026-11-17，开发方回复）

本节由开发方在验收复验后追加，记录针对 R1–R6 与其他已确认缺口的修复动作和复验证据；上文第一节的复验结论保留原样，不被本节改写。

### R1 / P1：快照读取权限

- 修复：快照 staging 目录与最终目录显式设为 `0755`，快照内文件显式设为 `0644`（`_relax_directory` / `_relax_directory_tree`）；读取端把权限异常映射为 `ResearchSnapshotUnavailable`，路由返回 503 而不是 500。
- 复验：真实研究库生成的快照逐项检查 `permission mismatches: []`；`docker run --user 10001:10001` 只读挂载后 `load_snapshot()` 返回 3 家公司 / 5 篇报告，`snapshot_content_bytes()` 可取回正文。

### R2 / P1：获准图片与 Cookie

- 修复：改为「展示副本」交付方式（`backend/hl_app/services/research_display.py`）。获准图片以 data URI 内联进展示副本，原始 HTML 原样保留在快照 `originals/`；变换写入 manifest（`display_sha256`、`display_dependencies`、`display_revision`）。未开放 `allow-same-origin`，未取消资源鉴权，未开放整个研究库。
- 复验：浏览器断言 `naturalWidth > 0`、`naturalHeight > 0`、`currentSrc` 为 `data:`，且隔离区内所有 `/research/content/` 请求均为 200；图片不再依赖 frame 内凭据请求。

### R3 / P2：独立 CSS 与 CSP

- 修复：独立样式表转为内联 `<style>`；CSP 保持脚本/连接/表单禁用，样式与内联图形按展示副本实际需要放开（`style-src 'unsafe-inline'`、`img-src data:`）。CSS 的递归依赖（`@import` → `url(...)`）在生成时一并解析与内联。
- 复验：浏览器 computed style 为 `rgb(1, 2, 3)`；嵌套样式的 `url(chart.png)` 解析为 data URI；断网后已加载正文与图形仍可读。

### R4 / P2：身份信息未进快照指纹

- 修复：实际消费的 `research.yaml` 字节纳入快照指纹与来源变更跟踪，生成返回值、磁盘 manifest 与 Web 读取结果一致。
- 复验：只改公司名后重建，`snapshot_id` 变化且读取到新名称；回归用例覆盖连续身份更新及读取期间身份内容变化；身份文件由缺失变为有效时也参与新的快照指纹。

### R5 / P2：登记哈希不符仍展示

- 修复：索引登记的非空正文哈希必须匹配实际正文，否则排除该报告并记录诊断；`draft` 且哈希为 null 仍允许，只记录实际哈希不回填源索引。
- 复验：`backend/tests/test_research_acceptance.py` 覆盖非空哈希不符与 ready 无哈希两种消费契约。

### R6 / P2：未发布必需资源漏检

- 修复：必需展示资源必须属于实际可交付文件集合，`unpublished_links` 不再作为豁免；检查覆盖 `srcset`、内联 style、CSS 递归依赖与锚点/查询串拆分。
- 复验：独立失败样本不会被当作可阅读报告接收。

### 其他已确认缺口

- redirects 写入 manifest，内容路由按白名单解析到目标报告，旧路径 URL 与目标报告同权限。
- 跨报告链接的 fragment/query 按 URL 拆解，不再被当作文件名。
- 文档口径已同步：开发方案第九节、README 与本页一致。

### 修复后的验证（2026-11-17 工作树）

| 项目 | 结果 |
| --- | --- |
| `make test-backend` | 296 passed、99 deselected |
| `make test-frontend` | 单元测试 + `tsc` + 生产构建通过 |
| `make test-e2e` | 62 passed、2 skipped；研究相关 22 项全部通过（1440px 与 390px） |
| 真实研究库只读快照 | 3 家公司、5 篇报告；演示报告被排除；正文文本与原文逐字一致（仅移除一段不执行的内联脚本）；目录/文件权限全部 `0755`/`0644`；跨 UID 10001 只读可读 |

### 仍未完成

- 未部署、未重启运行中服务、未推送，未切换任何生产快照指针；生产只读挂载仅提供 `compose.research-reports.yaml` 示例，未在运行中的 Compose 环境执行。
- 因此本节只说明代码层修复与本地复验结果，不等于整体上线验收通过。

## 正式部署补记（2026-11-17）

上述原验收与修复复验结论保留。用户随后明确授权部署，现已更新 Web 与 gateway，并接入 3 家公司、5 篇报告的正式 v2 快照；worker 保持运行。健康、实际前端资源、未登录 401、挂载报告哈希和现有应用配置保持情况均已核对。本次未登录真实账号操作浏览器，不扩充原代码/本地复验的结论范围。版本、镜像、来源、执行命令及回退准备见[部署记录](deployment/COMPANY_RESEARCH_2026_11_17.md)。

## 公网正文故障补充复验（2026-11-17）

用户报告公网 `frame-ancestors 'none'` 阻止正文嵌入，证明前一轮内网核验没有覆盖公网登录后的完整阅读链路。应用正文设置的是 `frame-ancestors 'self'`；无凭据公网探测返回 Cloudflare 403，带 `cf-mitigated: challenge`，不能据此确定登录用户命中的具体规则，也不能把该验证页当作原始报告响应。

修复：详情 JSON 在原有鉴权请求中携带静态正文，浏览器生成 Blob 阅读文档；正文内嵌 CSP，iframe 继续禁止脚本与同源权限。移除左侧“未发布资料”模块，保留后台索引与文件白名单。跨报告地址转换为本站完整地址，保留快照及章节片段；Blob 在卸载时撤销。

本轮后端 302 项通过、研究前端单元测试和构建通过、浏览器 64 项通过/2 项跳过。新增回归模拟公网全站禁止 HTTP 页面嵌入、正文地址返回 403 验证页，实际验证正文、图片、递归样式和同快照章节跳转，且无 frame-ancestors 报错。公网真实账号登录后的浏览器操作尚未代行，模拟通过不表述为该项实测通过。

本轮修复 `78d7986` 已更新线上 Web 与 gateway，当前资源为 `/assets/index-mdIkVxIq.js`；服务健康，正文 JSON 契约与已移除模块的实际资源均已检查，worker 和原配置保持不变。公网账号浏览器仍未代行，见[后续部署记录](deployment/COMPANY_RESEARCH_2026_11_17.md)。

## 新页面阅读复验（2026-11-17）

阅读侧栏移除“外部来源”，新增“新页面阅读”链接。默认内嵌预览保留；新标签直接加载已登记快照 HTML，独立于原页面及 Blob 生命周期。桌面/手机实际打开并确认无 iframe、图片成功解码、样式生效、无 opener；关闭原页面后刷新仍为 200，原生折叠可用。模拟禁止嵌入的 CSP 与 X-Frame-Options 不影响该顶层页面。匿名请求仍为 401。

本轮前端单元测试、类型检查及构建通过；浏览器 66 项通过、2 项跳过。没有后端或数据库变更，未重复运行后端测试；结果不表述为生产账号浏览器操作。

## 公司单入口列表复验（2026-11-17）

公司列表改为紧凑行，每家公司只有一个链接，优先主报告，无主报告时回退专题/更新。单元用例覆盖同公司三篇报告、主报告非数组首项、专题独有公司；搜索专题保留完整公司及主入口，不再过滤掉主报告。阅读页继续提供其他报告切换。

前端单元测试、类型检查和构建通过；浏览器 68 项通过、2 项跳过。实际桌面和手机验证单公司单链接、行高不超过 80px、无横向溢出，以及专题搜索→主报告→专题→返回列表完整流程。没有后端/数据库/源库改动；本轮未重复运行后端测试。
