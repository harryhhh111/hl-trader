---
name: sync-company-research
description: 同步 stock-research 研究库到 hl-trader 的公司研究页面。用户要求拉取最新研究、刷新或发布 /research 列表，或排查研究库更新后网页未变化时使用；仅发布已登记的公司 HTML（含 draft），不创建研究正文，也不部署应用代码。
---

在 `/app/hl-trader` 操作。研究库默认 `~/projects/stock-research`，页面为 https://hl-trader.nfprompt.io/research 。先读取两个仓库适用的 `AGENTS.md`，流程细节见项目 `docs/deployment/RESEARCH_RELEASE_WORKFLOW.md` 的“同步研究库与线上内容”。

用户要求“更新研究页面”“同步最新研究”即授权本次拉取和内容发布；已有授权直接沿用。若用户仅询问现状、要求排查或规划，只运行只读计划，说明结果。应用部署、重启、推送、交易、密钥操作不在此流程范围。

1. 运行 `python3 deploy/sync_research_reports.py plan`，确认来源、当前分支及上游、线上挂载目录和快照。需要另一来源时在子命令前显式传 `--source /absolute/path`。不猜测另一个“最新”分支。
2. 已获内容更新授权时运行 `python3 deploy/sync_research_reports.py sync --apply`。工具会 fetch 当前上游并只允许快进，然后使用现有 Web 镜像中的依赖生成候选快照、校验所有展示文件和原件哈希及公司 HTML 覆盖率，原子替换线上 `current.json`，再从运行中的 Web 和 gateway 复验。
3. 报告实际分支/提交、公司数、HTML 数、快照与私有证据位置，以及验证限制。默认验证挂载文件、内网 gateway、健康及匿名接口鉴权；不得声称已完成公网登录后浏览器验收或财务内容核验。

遇到未提交修改、未推送提交/分支分叉、拉取失败、候选公司 HTML 被校验排除时停止，说明具体阻塞。不得 stash/reset、强制切分支、改写或补造研究索引、跳过失败继续发布；不要用 `/tmp` 脚本绕过工具。复验失败工具恢复旧指针；旧快照保留，不自动清理。

索引首选 `研究流程/index/html-index.json`；只有新路径不存在才兼容旧 `研究流程/索引/html-index.json`。每家公司仍只有一个列表入口，允许 draft/ready 的 company_research/topic/update；不展示 demo、归档或纯行业条目。

本 skill 将一次调用内的步骤自动化，不创建定时任务。后续用户提出研究页面同步请求时可自动选用；只有用户明确要求定时运行时才另行安排。
