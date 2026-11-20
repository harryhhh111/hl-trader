# 公司研究页面：核验与发布

应用核验与部署使用 `deploy/research_release.py`，研究内容同步使用 `deploy/sync_research_reports.py`，不再为每轮修改生成 `/tmp` 发布脚本。命令可从任意目录调用；默认项目根目录由脚本位置确定。依赖见对应流程。

## 查看计划和只读核验

在项目根目录运行：

```sh
make research-plan
make research-verify
make test-release-tools
```

`research-plan` 离线输出命令，不修改服务或文件。`research-verify` 读取现有容器、通过内网 gateway 检查健康、研究页面及前端资源、匿名研究接口的拒绝状态，核验当前快照白名单文件的哈希，并记录交易 CLI 哈希。公司数量、报告数量和快照标识均从当前 manifest 读取。

核验输出不含原始环境变量或凭据。其范围是内网服务与挂载文件；不代表公网真实账号的浏览器阅读验收，也不验证研究内容中的财务事实。页面交互仍需运行项目 E2E 或另行进行经授权的实际浏览器验收。

## 经授权后发布

部署、重启仍须遵守 `AGENTS.md` 的明确授权要求。以下示例仅说明用法，默认 `deploy` 也只输出计划；只有 `--apply` 执行发布。

```sh
# 默认只更新 gateway，保留 Web 和 worker
python3 deploy/research_release.py deploy --apply

# 同时修改后端时，显式选择 web 和 gateway；worker 始终保留
python3 deploy/research_release.py deploy --scope web --apply

# 可额外核对新前端产物中的文案
python3 deploy/research_release.py verify --expect-text '新页面阅读'
```

默认沿用 `.runtime/docker.env` 和 `compose.yaml`、`compose.managed-wallets.yaml`、`compose.research-reports.yaml`，不会修改这些配置。自定义项目、环境文件或 Compose 文件时，将 `--root`、`--env-file`、可重复的 `--compose-file` 放在子命令之前；指定 Compose 文件会替换默认文件列表。

流程使用锁避免本工具并发发布，检查运行配置和挂载、保留旧镜像标签、构建指定服务，通过 `up --no-deps` 切换并复验。Web 发布额外核对新旧交易 CLI 一致，并在切换前只读确认没有运行中的执行请求。它不执行迁移、密钥初始化、订单或 Git 推送，也不负责生成或切换研究快照。

每次发布使用时间、提交和随机后缀生成独立记录与回退标签，私有记录存入 `.runtime/deployments/`，包含配置指纹而非配置原文。失败会留下 `.failed.json`，不会自动回退；构建失败时可能尚未切换，切换后失败时应检查实际容器状态。按记录中的镜像标签恢复需要部署授权，Web 回退同样需核对运行中的执行请求。

## 同步研究库与线上内容

研究库更新不会自动改变网页：线上读取独立快照。只更新报告与列表时，使用 `deploy/sync_research_reports.py`，无需构建应用镜像或重启 Web、gateway、worker。

```sh
# 只读检查来源分支、上游、线上挂载及当前快照，不 fetch
make research-sync-plan

# 用户明确要求同步/更新线上研究页面后执行
make research-sync

# 自定义来源；默认 ~/projects/stock-research
python3 deploy/sync_research_reports.py --source /absolute/stock-research sync --apply
```

`sync` 未加 `--apply` 也只显示计划。脚本依赖 Python 3 标准库、Git 和现有 Docker Compose；快照构建使用正在运行的 Web 镜像中的 Python 依赖与当前工作区的消费端代码，不执行研究库脚本，不安装宿主依赖。可从任意工作目录执行，默认项目根目录由脚本位置定位。

同步固定使用来源仓库**当前分支的跟踪上游**，先 fetch，再只允许 fast-forward；不自动选另一分支。未提交修改（含未跟踪文件）、本地领先、分支分叉、脱离分支或没有上游时停止，不 stash/reset 或推送。来源首选 `研究流程/index/html-index.json`，仅在新路径不存在时兼容旧 `研究流程/索引/html-index.json`；新路径无效时不退回旧索引。

工具从运行中 Web 的只读 bind mount 找到真实快照根目录，沿用发布锁，先验证旧线上内容，然后在快照根目录内自动清理的暂存目录构建候选（本机位于 `.runtime/research-reports/`，保证与目标处于同一文件系统）。发布前检查展示文件及原件哈希、来源提交未变化、应展示的公司 HTML 没有被异常排除，拒绝空快照；demo、归档和非公司报告正常排除，draft 可展示。每家公司保留一个列表入口。

候选目录进入原有 `snapshots/` 后，只原子替换该目录内的 `current.json`，不替换 bind mount 根目录。发布后核对现有 Web 实际读取的快照、gateway 页面/资源、鉴权及三个服务的实例/镜像/配置指纹；复验失败恢复旧指针，发现指针被其他流程修改则停止并报告，不覆盖他人的发布。保留历史快照以支持旧阅读链接，不自动清理。

成功或失败记录写入 `.runtime/deployments/research-sync-*.json`，不含环境变量原文或凭据。Git 拉取失败可能已更新远端跟踪引用，但不切换线上内容；复验失败回退网页内容，不撤销已完成的来源仓库快进。验证范围同上文：不冒充公网登录浏览器或研究内容验收。

对应 skill 源码为 [sync-company-research](../../skills/sync-company-research/SKILL.md)，安装到 `${CODEX_HOME:-$HOME/.codex}/skills/sync-company-research` 后可调用：`$sync-company-research 更新线上研究页面`。它自动执行一次同步流程，不创建后台轮询或定时任务。

2026-11-20 首次同步与重复同步均成功：来源 `main` / `origin/main`，提交 `79466e96da7bd416df15c6199b6400fc3a73e23f`，快照 `s7658fcec893ae6e0a4ff5f57acdc32f0`，7 家公司、21 份 HTML。服务实例/镜像/配置及交易 CLI 哈希保持一致，21 个展示文件哈希、页面与资源、匿名接口 401 核验通过；研究库工作区保持干净。证据为 `.runtime/deployments/research-sync-20261120T053317Z-f9f17542.json` 与 `research-sync-20261120T053337Z-37fd673f.json`。公网命令行请求被 Cloudflare challenge 拦截（403），未完成公网登录后浏览器验收。发布工具 24 项与研究后端 37 项测试通过。

## 临时文件与提交

Git 分块提交使用 `git add -p` 或明确文件列表，并检查 `git diff --cached`。不再生成单次提交脚本或暂存补丁文件。

一次性探测尽量内联执行；需要文件时使用 `TemporaryDirectory` 或 `finally` 自动清理。部署证据保留在 `.runtime/deployments/`，正式验收说明保留在 `docs/`。清理仅针对本任务确认归属、没有运行引用的具体路径，不按 `/tmp` 前缀批量删除。

历史部署和验收记录见 [公司研究页面部署记录](COMPANY_RESEARCH_2026_11_17.md)。
