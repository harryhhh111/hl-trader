# 独立应用迁移执行记录

## 固定基线

- 工作目录：`/home/vinci/projects/hl-trader`
- 开发分支：`codex/independent-app-migration`
- hl-trader 起始 HEAD：`960f6d7f106559531421f4b31c431ea5420cc926`（`main`）
- stock_data 固定 HEAD：`cc62434d126cdfc1dc40456f430d943f7d40d7cd`（`main`），与合同编写值一致，无需重定来源版本
- hl-trader 起始未提交内容：用户已修改 `README.md`；未跟踪 `docs/INDEPENDENT_APP_MIGRATION_EXECUTION_PLAN.md`。实施时保留，不 reset 或覆盖
- stock_data 起始未提交内容：边界方案、新闻正文方案、证据目录及 `scripts/verify_news_body_gate0.py`；均保持只读，不属于本次复制来源
- 当前进程未设置 `STOCK_MARKETS`；未读取源 `.env`。合同已确认源环境为 `STOCK_MARKETS=US`，不在源环境执行 A/HK 同步、DDL 或迁移

## A：基线与骨架

状态：代码与验证完成，待阶段提交。

已落地：独立 Python 包、三类 DSN 无回退、SQL migration、operation control 默认 maintenance、单 Web advisory lifecycle lock、FastAPI app factory、React/Vite 基础、同源 v1 client、来源清单及测试骨架。

工作目录除前端命令外均为仓库根；前端命令在 `frontend/`。起始 HEAD 见上文，实际验证结果：

- `python3 -m venv .venv && .venv/bin/python -m pip install -e "backend[test]"`：安装成功；`pip freeze --exclude-editable` 生成 23 项精确锁定依赖。
- `.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q`：9 passed，3 deselected。
- 在本机 `initdb` 新建的 `/tmp/hl-app-pg-A.XA3YDb` 临时实例上，显式注入 `HL_APP_TEST_DATABASE_URL` 后运行 DB 套件：3 passed，9 deselected；覆盖空 schema 初始化/重复迁移、运行账号无 DDL/control 写权限、第二 Web 生命周期锁拒绝。另以 `HL_APP_MIGRATION_DATABASE_URL` 实际运行 `python -m hl_app.db migrate` 两次，首次应用 `001_operation_control`，重复执行无变更。
- `npm ci && npm run test:unit && npm run build`：3 个 client case 实际执行并通过，TypeScript/Vite 构建成功。因初始依赖审计发现漏洞，升级 Vite、React Router 和 esbuild 后 `npm audit` 为 0 vulnerabilities。
- `cargo fmt --check`：通过；`cargo test --offline`：154 passed、0 failed、1 ignored（既有公开来源 smoke）；`cargo clippy --offline --all-targets -- -D warnings`：通过。
- 验证中遇到主机磁盘 ENOSPC。仅清理可重新下载的包/浏览器缓存并重建 Rust `target`；Rust 编译使用 `CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0` 降低中间产物，未减少或跳过任何测试。旧 `/tmp` 数据库备份和 stock_data 数据未改动。

未完成：A 阶段以外的业务页面、认证和采集；真实外部来源、生产数据库、部署和真实采集均未执行。阶段提交号在后续进展记录中补充。
