# 公司研究页面：核验与发布

重复操作统一使用 `deploy/research_release.py`，不再为每轮修改生成 `/tmp` 发布脚本。仅依赖 Python 3 标准库及现有 Docker Compose。命令可从任意目录调用；默认项目根目录由脚本位置确定。

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

## 临时文件与提交

Git 分块提交使用 `git add -p` 或明确文件列表，并检查 `git diff --cached`。不再生成单次提交脚本或暂存补丁文件。

一次性探测尽量内联执行；需要文件时使用 `TemporaryDirectory` 或 `finally` 自动清理。部署证据保留在 `.runtime/deployments/`，正式验收说明保留在 `docs/`。清理仅针对本任务确认归属、没有运行引用的具体路径，不按 `/tmp` 前缀批量删除。

历史部署和验收记录见 [公司研究页面部署记录](COMPANY_RESEARCH_2026_11_17.md)。
