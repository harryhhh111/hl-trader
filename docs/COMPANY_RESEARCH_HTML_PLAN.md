# 公司研究 HTML 阅读功能开发方案

状态：2026-11-17，功能已实现，经用户授权已部署 Web、gateway 并接入正式研究快照。部署范围、来源与核验边界见[部署记录](deployment/COMPANY_RESEARCH_2026_11_17.md)。本文保留实现与验收过程，第 9.1/9.2 节为部署前记录，最新部署状态见 9.3。

2026-11-17 第二轮：按[独立验收复验](COMPANY_RESEARCH_HTML_ACCEPTANCE.md)提出的 R1–R6 与其他已确认缺口完成修复并复验，修复范围与证据见第九节。

用户已确认：在 hl-trader 新增公司研究页面，只展示已有 HTML 报告的公司；`draft` 也允许内部阅读。该规则覆盖研究库接入说明中默认仅展示 `ready` 的建议，不改写源索引状态。

## 1. 第一版交付

网站主导航增加「公司研究」，沿用现有 React、FastAPI 和登录体系。

| 页面 | 路径 | 行为 |
| --- | --- | --- |
| 公司列表 | `/research` | 按公司分组，展示其 HTML 报告，支持公司/报告名称搜索 |
| 报告阅读 | `/research/reports/:reportId` | 显示报告信息、同公司其他报告、返回入口和独立 HTML 阅读区域 |

公司列表中每组显示公司名称、可用证券信息、报告数量与报告列表。报告显示标题、主报告/专题/更新类型、草稿/可阅读状态、核验状态和已登记的日期。只有一篇专题或局部更新的公司也应出现，不要求先有完整主报告。

阅读页保留原 HTML 的文字、表格、图形、目录和原生折叠内容。页面外层显示文档更新日、财务截止日、行情截止日及内容核验状态，不展示未发布资料清单；未知日期显示「未登记」，不使用文件修改时间或快照生成时间替代。

手机端先展示报告信息和切换入口，再展示正文；桌面端为正文提供足够阅读宽度。列表搜索作用于公司名称、明确登记的证券代码和报告标题，不承诺 HTML 全文检索。搜索参数保留在 URL 中，阅读后返回保留搜索条件。

第一版聚焦目录和静态阅读。在线编辑、关注池联动、全文检索、报告下载包、报告脚本增强与浏览器打印增强留待后续。

## 2. 展示规则与主体身份

唯一报告来源为研究库 `研究流程/索引/html-index.json`。不把扫描到的全部 HTML 当作研究报告，不另建可手工维护的报告数据库。

纳入条件同时满足：

- `entity_id` 明确属于 `company:`。
- `report_type` 为 `company_research`、`topic` 或 `update`。
- `status` 为 `draft` 或 `ready`。
- 登记入口为真实、可读取的 HTML 普通文件，并通过本功能的路径和资源检查。

排除 `demo`、`archived`、非公司主题、未登记 HTML、模板及原始财报网页。公司仅在至少有一篇可用报告时出现。

以 `entity_id` 合并同公司报告，包括跨目录报告。主报告优先展示，其余按登记更新日倒序排列，未知日期置后，最后以稳定 ID 排序；不通过文件名或目录顺序猜测主报告。

公司名称、别名和证券可从同一源快照中的 `research.yaml` 补充，但该文件是可选身份信息，不是报告正文或可展示性的前置条件。只做有大小限制的安全 YAML 读取和相关身份字段校验，不调用现有 Markdown 章节提取器；缺失或无效时保留报告，附内部诊断。无名称时使用该公司已登记报告标题作为展示名称，无证券时省略代码，不根据目录名伪造 market/mic/symbol。

`status` 与 `content_status` 分开展示：草稿可以阅读；`partial` 显示「有研究缺口」；`unreviewed` 显示「尚未核验」。页面不会因成功加载 HTML 自动将报告标成已核验，也不修改研究库登记内容。

## 3. 本次观察到的真实样本

以下仅是 2026-11-17 工作树观察，不是固定业务清单或已发布版本：

| 公司主体 | 当前报告 | 状态 |
| --- | --- | --- |
| `company:microsoft` | 微软主报告、传统软件与现金流、AI Berkshire 研究试验 | 3 篇 draft / unreviewed，分布在 MSFT 与 AI_BERKSHIRE 目录 |
| `company:amd` | AMD 近期股价催化 | 1 篇 draft / unreviewed |
| `company:qxo` | QXO 公司研究 | 1 篇 draft / partial；无 research.yaml |

索引另有一篇 `ready` 演示报告，必须排除。当前所有公司报告的 `assets` 都为空，且有显式 `unpublished_links`。不得因此公开整个 factcheck 目录。

## 4. 读取与更新方式

采用「源索引 → 本地生成不可变展示快照 → Web 只读提供」的方式。浏览器请求不遍历研究库、不执行研究脚本、不访问数据商，也不执行 Git pull。

本仓库已提供快照命令，参数形式如下：

```sh
.venv/bin/python -m hl_app.research_reports snapshot \
  --source /absolute/path/to/stock-research \
  --output /absolute/path/to/research-reports
```

生成步骤：

1. 校验源索引 Schema 版本、必需字段、字段类型、重复键/ID、报告状态和路径。使用本仓库维护的消费端契约，不导入或运行源仓库工具代码。
2. 选择本方案允许展示的公司报告，读取入口与显式 `assets`，保留原有相对目录。读取可选主体身份信息。
3. 校验链接及显示资源，生成每篇报告的可用性和未发布链接诊断。一个报告损坏不应隐藏其他有效报告；损坏报告从可阅读集合排除，在快照诊断中明确记录。全局索引损坏或有候选但全部失效时不切换当前快照；合法的零候选索引可产生明确的空快照。
4. 对实际复制的索引、HTML、获准资源与身份信息计算哈希，生成派生 manifest 和快照 ID；来源记录区分已提交树与含工作树改动的快照，不把 HEAD 冒充当前正文版本。索引已登记正文哈希时核对一致；draft 哈希为 null 时记录实际哈希，但不回填源索引。
5. 在临时目录完成复制与验证，并检查读取期间来源文件是否变化；检测到变化则失败并要求重试，避免混合版本。完成后原子切换本地当前快照指针。

派生 manifest 只记录该次实际展示的文件、哈希、身份和诊断，是可重建的缓存，不是第二份人工清单。旧快照保留，正文 URL 绑定快照 ID，已打开的报告不随新快照替换正文。Web 按当前快照读取列表，不要求重启；可显式切回已有有效快照，不自动清理历史快照。

Web 新增 `HL_APP_RESEARCH_REPORTS_PATH`，指向生成产物根目录；与现有用于 AI 读取 Markdown 的 `HL_APP_RESEARCH_REPO_PATH` 分开。未配置时仅研究接口返回 503 和明确提示，其他页面正常运行；空目录或无当前快照属于不可用，不伪装成「没有研究」。有效空索引才显示空列表。

Docker 准备独立 Compose override 示例，将快照根目录只读挂载到 Web；不要求生产容器读取整份研究仓库，也不把研究内容打进前端包。快照生成在容器外完成，完整快照根目录挂载保证旧 URL 仍可访问。更新报告后重新生成并显式同步快照即可，无需重建前端。

本轮规划和后续本地实现不包含部署、重启或推送；操作运行中服务的内容指针属于上线步骤，仍须用户明确授权。

## 5. 接口约定

所有路由使用现有 `/api/v1/` 前缀，直接复用 Nginx、Vite 及 E2E 的 API 代理，避免内容路由落入 SPA 兜底。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/research/companies` | 返回当前快照的公司及报告元数据，包含总数、快照 ID 与同步诊断摘要 |
| `GET /api/v1/research/reports/{report_id}` | 返回当前快照中的报告元数据、同公司报告、正文 URL 与未发布资料说明 |
| `GET /api/v1/research/content/{snapshot_id}/{registered_path:path}` | 返回该快照白名单内的 HTML 或获准资源，保留相对资源和跨报告链接语义 |

列表和详情沿用 `{ "data": ... }` 包装；正文直接返回 `text/html; charset=utf-8`，不包装 JSON，不转 Markdown，不执行模板语法。正文 URL 由后端逐段编码生成。

元数据至少包含 `report_id`、`entity_id`、标题、报告类型、`status`、`content_status`、三个登记日期、快照 ID、正文 URL 和已知缺口。公司可选字段包括名称、别名与显式证券信息；不向浏览器暴露源库绝对路径。

已登记兼容路径仅通过索引 redirects 解析到允许展示的报告，重定向目标同样鉴权；redirects 写入快照清单并由内容路由解析，旧路径 URL 与目标报告共用同一套权限与白名单。`unpublished_links` 只提供说明，不构成文件读取白名单。

错误语义：未登录 401；不允许访问的报告/资源或不存在文件 404；研究功能未配置或当前索引不可用 503。后端日志保留具体诊断，用户错误不泄露本机路径。列表查询成功后正文文件损坏应报告真实错误，不回退网站首页。

## 6. 登录范围与内容隔离

第一版为登录用户共享的内部研究库：token、account、Cloudflare Access 模式均沿用现有鉴权；不按关注关系筛选公司，不增加每用户私有报告配置。account 模式下所有可登录账号都能阅读，若未来需要限制特定账号，再增加明确的研究访问权限。

列表、详情、直接 HTML 和每个资源请求都执行 `require_session`。仅保护前端路由不算完成，退出后也不能继续通过正文 URL 请求资料。

阅读采用 `iframe sandbox="allow-top-navigation-by-user-activation"`、`referrerpolicy="no-referrer"`。不将完整报告注入 React DOM，不默认运行脚本或开放同源能力；不开放 `allow-same-origin`。正文响应同时设置 CSP sandbox 和静态资源限制，覆盖直接打开正文 URL 的场景；限制脚本、连接、表单和对象，仅允许实际需要的内嵌样式/图形及获准资源，配置允许本站嵌入的 frame-ancestors。

报告详情 API 在 JSON 中返回 `document_html`；外层页面通过正常鉴权请求读取后，以 Blob 文档装载隔离 iframe，不再将公网正文 URL 作为 frame 导航目标。报告正文插入可用于本地文档的静态 CSP（禁脚本、连接、表单、子框架及 base），iframe 保持无脚本、无同源权限；直接正文 URL 继续保留原有 HTTP 响应头和鉴权。Blob 地址随报告切换/卸载撤销，读取或初始化失败显示明确错误。

获准图片、CSS 与递归依赖内联，原始 HTML 保留在 `originals/`，变换由 manifest 记录。跨报告链接解析为本站完整阅读路由，绑定快照并保留章节片段；只有用户点击才能导航外层页面。未发布资料保留在后端登记与白名单规则中，阅读页不展示“未发布资料”模块。来源链接文字仍保留，外部来源由外层受控入口提供。

报告及资源使用 `Cache-Control: private, no-store`、正确 MIME 和 `X-Content-Type-Options: nosniff`。路径归一化后必须同时在快照目录和文件白名单内；拒绝目录浏览、绝对路径、路径穿越、双重编码绕过、符号链接越界和任意 URL 代理。

原生目录锚点和 details 折叠应能静态阅读。报告打印按钮、目录高亮等脚本增强第一版不保证工作，不添加放开脚本的开关。外部来源在受控的外层入口处理，校验协议并使用安全新窗口属性；原正文保留，不能静默删除来源。原文中的相对 HTML 链接通过同快照路由解析，未获准附件返回明确不可用，外层列明原因。

资源检查覆盖 HTML src/href、CSS url、iframe/object、脚本取数线索、本机绝对路径与 file URL。必需展示资源缺失不能以 unpublished_links 豁免；获准资源的 MIME 和大小有明确限制，脚本证据只可作文本查阅，不执行。外部必需展示资源不默认联网加载，记录并阻止该报告进入可阅读集合，直到形成明确处理方案。

## 7. 实现分片与文件范围

| 顺序 | 产出 | 预计文件 |
| --- | --- | --- |
| 1 | 索引消费契约、筛选/公司归组、路径资源检查、快照生成 CLI | `backend/hl_app/services/research_reports.py`、`backend/hl_app/research_reports.py`、独立后端测试与小型 fixture |
| 2 | 鉴权接口、配置与静态响应 | `backend/hl_app/routes/research.py`、`api.py`、`config.py`、`.env.example`、API 测试 |
| 3 | 公司列表、搜索和阅读页 | `frontend/src/pages/research-page.tsx`、`research-report-page.tsx`、`lib/types/research.ts`、`lib/api/research.ts`、`App.tsx` |
| 4 | 浏览器验收、运行说明与容器挂载示例 | 新研究 E2E 测试与 fixture、现有 E2E seed/run 的必要扩展、独立部署 override 示例、README 和部署说明 |

现有 `research_reader.py` 继续服务原有 AI 的 Markdown 摘录，不扩展为 HTML 阅读器。此功能不需要新增业务数据库表或采集 worker。

工作树当前有关注股、资讯、Hyperliquid 等未提交修改。开发以现有工作树为基线，优先新增独立文件；共享文件只增加必要接入，不重置、代提交或覆盖现有改动。全部实现位于 hl-trader，不修改 stock_data 代码或 stock-research 的报告与索引。

## 8. 验收条件

功能：

- fixture 覆盖只有 Markdown、仅专题、主报告加专题、跨目录同公司、无 research.yaml、draft、ready、archived、demo 与非公司主题。
- 当前源样本在通过实际检查后，应得到 3 个公司、5 篇报告；微软跨目录合并，QXO 缺 YAML 不被遗漏，演示报告不出现。数量来自该次样本，不写死在代码中。
- 搜索、直接报告深链接、同公司切换、返回保留条件、空列表、未配置、部分报告损坏及服务错误分别有明确界面。
- 读到的 HTML、表格和数值与对应快照文件一致；未知日期保持未知；两种状态及 QXO 的 partial 缺口可见。
- 新快照生成成功后列表可刷新，旧正文 URL 仍指向旧内容；失败不切换指针，dirty 来源和实际哈希可追溯。

隔离与浏览器：

- 列表、详情、正文、相对资源及兼容链接均测试匿名访问；未登记、越界、编码绕过与符号链接均不能读取文件。
- 桌面 1440px 与手机 390px 实际进入 iframe，验证中文、图形、目录锚点、折叠区和滚动；外层页面不整体横向溢出。
- 带获准相对 CSS/图片和同公司报告链接的 fixture 验证路由；实测 sandbox 下资源请求与现有 Cookie 鉴权的组合，失败时处理资源交付方案，不靠开放 allow-same-origin 解决。
- 恶意脚本、表单提交及报告对应用接口的脚本访问被阻止；直接打开正文 URL 仍受响应头隔离。
- 未发布附件说明可见，访问不返回 SPA 首页；已批准外部来源通过受控入口打开。关闭网络后已加载的正文与内嵌图形仍可读。

验证命令按实际改动执行：新增后端离线测试、前端单元测试及构建、研究相关 E2E；共享接入完成后运行 `make test-backend` 与 `make test-frontend`，E2E 使用现有 `make test-e2e` 固定容器。若本地依赖已齐全，也可按 README 运行等效本机命令。无数据库结构或 Rust 改动时不因本功能新增数据库迁移或扩大 Rust 验证范围。

上述验收条件已在 2026-11-17 的实现轮与随后的验收修复轮中执行，结果见第九节与[独立验收复验](COMPANY_RESEARCH_HTML_ACCEPTANCE.md)。后续交付分别报告代码验证、真实研究样本接入和部署状态，不能把其中任意一项替代另外两项。

## 9. 实施记录（2026-11-17）

实现全部位于 hl-trader 工作树，未修改 stock_data 代码，也未修改 stock-research 的报告与索引。

| 分片 | 产出文件 | 状态 |
| --- | --- | --- |
| 1 | `backend/hl_app/services/research_reports.py`、`backend/hl_app/services/research_display.py`、`backend/hl_app/research_reports.py`、`backend/tests/test_research_reports.py`、`backend/tests/test_research_acceptance.py`、`backend/tests/fixtures/research_library/` | 已实现 |
| 2 | `backend/hl_app/routes/research.py`、`backend/hl_app/api.py`、`backend/hl_app/config.py`、`.env.example` | 已实现 |
| 3 | `frontend/src/pages/research-page.tsx`、`research-report-page.tsx`、`frontend/src/lib/types/research.ts`、`frontend/src/lib/api/research.ts`、`frontend/src/App.tsx` | 已实现 |
| 4 | `frontend/e2e/tests/research.spec.mjs`、`frontend/e2e/tests/research-assets.spec.mjs`、`frontend/e2e/seed.py`、`frontend/e2e/run.mjs`、`compose.research-reports.yaml`、README | 已实现 |

### 9.1 第一轮实现（2026-11-17）

代码验证（2026-11-17 工作树）：

- 后端离线测试 `backend/tests/test_research_reports.py` 35 项通过；`make test-backend` 全量 280 项通过。
- 前端单元测试 `frontend/src/pages/research-page.test.tsx` 9 项通过；`make test-frontend`（单元 + `tsc` + 构建）通过。
- 研究 E2E `frontend/e2e/tests/research.spec.mjs` 在 1440px 与 390px 下 10 项全部通过，覆盖列表分组与搜索、阅读页与同公司切换、iframe 内中文/表格/目录锚点/原生折叠/获准相对图片、匿名 401、未登记与越界 404、正文响应头隔离与报告脚本被阻止。

真实研究样本接入（2026-11-17 工作树观察）：

- 以 `/app/projects/stock-research` 为源执行 `python -m hl_app.research_reports snapshot`，得到 3 家公司、5 篇报告：微软跨 `MSFT` 与 `AI_BERKSHIRE` 合并为 3 篇，AMD 1 篇，QXO 1 篇且无 `research.yaml` 仍出现并显示「有研究缺口」；`ready` 演示报告被排除。
- 5 篇报告的 `assets` 均为空，索引登记的 `unpublished_links` 全部识别为未发布资料，报告正文哈希与索引登记值一致（QXO）或按 draft 规则只记录实际哈希（其余 4 篇）。

### 9.2 验收修复轮（2026-11-17）

[独立验收复验](COMPANY_RESEARCH_HTML_ACCEPTANCE.md)提出 R1–R6 与其他已确认缺口。逐条修复与复验结果：

| 问题 | 修复 | 复验证据 |
| --- | --- | --- |
| R1 快照无法由 Web 容器用户读取 | 快照目录显式 `0755`、文件显式 `0644`；读取权限异常映射为 `ResearchSnapshotUnavailable`，路由返回 503 | 真实快照 `permission mismatches: []`；`docker run --user 10001` 只读挂载后 `load_snapshot()` 与正文读取成功 |
| R2 获准图片无法沿用 Cookie | 展示副本把获准图片内联为 data URI，不再依赖 frame 内凭据请求 | E2E 断言 `naturalWidth > 0`、`currentSrc` 为 `data:`、隔离区内无 401 |
| R3 获准独立 CSS 被 CSP 拦截 | 独立样式表转为内联 `<style>`，CSP 放开 `style-src 'unsafe-inline'`、`img-src data:`；CSS 递归依赖一并内联 | E2E 断言 computed style 为 `rgb(1, 2, 3)`，递归 `url(chart.png)` 解析为 data URI |
| R4 仅更新身份信息仍用旧快照 | 实际消费的 `research.yaml` 字节纳入快照指纹与来源变更跟踪 | 改名后 `snapshot_id` 变化，`load_snapshot` 读到新名称 |
| R5 登记正文哈希不符仍展示 | 非空登记哈希不匹配即排除该报告并给出诊断；`draft` 且哈希为 null 仍允许，只记录实际哈希 | `backend/tests/test_research_acceptance.py` 覆盖 |
| R6 未发布必需资源漏过检查 | 必需展示资源必须属于可交付集合；`unpublished_links` 不再豁免；覆盖 `srcset`、内联 style、CSS 递归依赖、锚点与查询串拆分 | `backend/tests/test_research_acceptance.py` 覆盖 |
| 缺口：redirects 未落地 | redirects 写入 manifest，内容路由按白名单解析到目标报告 | 快照 `redirects` 字段与路由解析已实现 |
| 缺口：fragment/query 未拆解 | 统一按 URL 拆解路径、查询串与片段，查询串不再被当作文件名 | `local_reference()` 与回归用例 |
| 缺口：文档口径不一致 | 本节与 README、验收文档同步为实际状态 | 当前工作树 |

修复后的验证（2026-11-17 工作树）：

- `make test-backend`：296 passed、99 deselected。
- `make test-frontend`：单元测试 + `tsc` + 生产构建通过。
- `make test-e2e`：62 passed、2 skipped；其中研究相关 `research.spec.mjs` 与 `research-assets.spec.mjs` 共 22 项（1440px 与 390px 各一轮）全部通过，覆盖图片真实解码、CSP 与递归样式、隔离区内真实点击同公司链接并完成导航、断网后已加载正文仍可读、表单与脚本被阻止、退出后正文与附件仍为 401。
- 真实研究库只读生成独立快照：3 家公司、5 篇报告，`implied-valuation-demo` 被排除并记录原因；5 篇报告展示副本正文与原文逐字一致（`MSFT/MSFT.html` 仅移除一段不执行的 `<script>` 源码，其余正文文本完全相同）；快照目录与文件权限全部为 `0755`/`0644`；跨 UID 10001 只读挂载可读清单与正文。

未完成且需单独授权的部分：

- 未生成或切换任何运行中服务的快照指针，未重建容器、未推送。
- 生产容器只读挂载快照根目录的 override 示例已提供（`compose.research-reports.yaml`），但未在运行中的 Compose 环境执行。
- 报告内打印按钮、目录高亮等脚本增强在静态阅读模式下不工作，这是第一版的既定边界，不是缺陷。

### 9.3 正式部署（2026-11-17）

经用户授权，以研究功能提交 `a9fd9e9` 部署 Web 与 gateway，正式挂载 v2 快照 `s93b5af9e66c56961c352130ff2592aa1`，覆盖 3 家公司、5 篇报告。Web 健康、现有账号认证模式保留，未登录的研究列表、详情和正文请求均返回 401；运行中镜像已逐篇核对正文展示哈希。worker 未重启、交易 CLI 文件哈希未变，未执行数据库迁移或发送订单。线上真实账号浏览器操作未另行验收；完整证据和回退镜像见[部署记录](deployment/COMPANY_RESEARCH_2026_11_17.md)。

### 9.4 公网嵌入故障修复（2026-11-17）

用户反馈公网正文因 `frame-ancestors 'none'` 被拦截。应用正文响应原本为 `frame-ancestors 'self'`；无凭据公网探测收到 Cloudflare 403 验证页，不能用内网 gateway 验证代替公网登录后的 frame 实测。本轮改为鉴权 JSON 传输正文并在浏览器内生成受限 Blob 文档，同时移除“未发布资料”侧栏。直接正文接口的权限和静态响应头保持有效，未更改 Cloudflare 规则。

回归：后端 302 项通过，前端研究单元测试与构建通过，浏览器 64 项通过、2 项跳过。新增桌面/手机用例模拟全站 `frame-ancestors 'none'`，并将 HTTP 正文路径替换为 403 验证页；确认不会请求该 frame 路径，正文、图形、样式、同快照链接与锚点仍可读。生产账号浏览器不在该模拟结论内；后续上线结果单独补记。
