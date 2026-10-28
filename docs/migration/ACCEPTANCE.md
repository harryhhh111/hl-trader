# 独立应用迁移验收证据

本记录区分代码实现、离线回归、真实 DB、真实浏览器、干净环境和外部验收。未执行项目不写成通过。

## 1. 代码与离线证据

- A–F 均已完成本仓库代码、fixture 和阶段本地提交。
- Python 默认套件：137 passed，42 deselected（DB/live）；无失败。
- Rust：`cargo fmt --check` 通过；`cargo test --offline` 154 passed、0 failed、1 个既有公开来源 smoke ignored；`cargo clippy --offline --all-targets -- -D warnings` 通过。
- 前端：35 个 Node case 实际执行通过；TypeScript/Vite 生产构建通过；`npm audit` 为 0 vulnerabilities。构建有大 chunk 提示，不影响完成状态。
- Hyperliquid R1–R9 和 E1–E3 的来源→目标映射见 `SOURCE_MAP.md`。共享 fixture 实读，缺文件会失败，不跳过。

## 2. 真实 PostgreSQL 集成

各阶段使用 `initdb` 新建的本机临时实例，显式注入 `HL_APP_TEST_DATABASE_URL`，测试只清理自身 schema/角色。最新全套结果：42 passed，137 deselected，无 skip 隐藏 DB 项。

覆盖：迁移与权限、Web/worker 生命周期锁、认证 epoch/轮换/并发、三市场行情、公告 revision/多公司关联、来源失败与恢复、四进程 SEC gate、AI 失败、full 导入/幂等/回滚、baseline/activate、回退差异和参考源三方恢复。

F 的 CLI 全链路 `migrate→dry-run→apply→baseline→activate→maintenance→rollback-export` 成功。小 fixture 全链路墙钟约 0.9 秒，只证明工具路径可执行，不代表正式停写窗口；正式窗口未测量。

## 3. 真实 Chromium

`npm run test:e2e` 先构建当前 dist，启动真实 FastAPI、临时 PostgreSQL 和同源静态代理，使用本仓库 fixture 与 mock 子进程。Playwright 使用本地锁定版本和真实 Chromium。

结果：8/8 passed，桌面 1440×900、移动 390×844 各 4 项。

- 登录、pair 切换、坏解锁块、目录失败后旧读数保留；
- 关注元数据编辑、AI 草稿修改保存，页面无新增/移除/分析入口；
- 资讯筛选、置顶和来源状态，移动端无页面级横向溢出；
- 退出后受保护页面重新进入登录。

脱敏截图：

- `evidence/desktop-hyperliquid.png`
- `evidence/desktop-watchlist.png`
- `evidence/desktop-news.png`
- `evidence/mobile-hyperliquid.png`
- `evidence/mobile-watchlist.png`
- `evidence/mobile-news.png`

## 4. 干净独立环境

使用 `git ls-files -co --exclude-standard` 创建仅含目标资源的 `/tmp/hl-app-clean-G.ueynta` 副本，不含 `.git`、target、node_modules、旧仓库、旧数据库或旧凭据。重新创建 venv、安装 Python 锁依赖、editable 包和 `npm ci`，审计 0 vulnerabilities。

在该副本和新的临时 PostgreSQL 中完成：schema 初始化、mock 采集写入 1 行、Web 登录、关注列表读取。未设置旧 `PYTHONPATH`，未调用旧 API。临时目录、进程和数据库已清理。

## 5. 权限材料与数据回退

`deploy/` 仅提供单 Web、单 worker、同源 nginx 和数据库角色模板，未启用。`docs/deployment/INDEPENDENT_APP.md` 包含安装、权限、备份、停写切换和回退。真实 Web OS 身份与交易资源隔离未在生产验证。

临时源恢复演练已完成：理由/停用/revision/行情变化→回退 insert/update/delete→参考恢复→幂等/冲突拒绝。stock_data agent 的正式恢复适配和生产 schema 兼容性尚未验收，因此正式切换不就绪。

## 6. 未执行/阻碍

- 真实 Hyperliquid 公开账户、新服务器只读对照：未执行。
- US/A/HK 真实行情来源可达性：未执行。
- SEC/IR/媒体真实出口、共 NAT 协调和既有来源状态调整：未执行。
- 真实 mmx 凭据、研究目录和模型 smoke：未执行。
- 生产 OS 身份权限、HTTPS、多设备真实部署验收：未执行。
- 正式源 bundle、实际数据迁移、正式停写窗口测量、正式源恢复适配：未执行，需 stock_data agent 配合。
- 部署、重启、推送、正式发布：未执行，等待明确授权。

因此，本记录支持“代码迁移交付及本地验证完成”，不支持“全部迁移上线完成”。
