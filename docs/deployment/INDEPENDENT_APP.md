# hl-app 独立运行与切换手册

本文只提供部署准备，不执行部署、重启、生产迁移或真实采集。真实地址、数据库、token、研究资料和公开 pair 配置由用户提供，不写入仓库。

## 1. 组件与权限

- Rust CLI：原交易功能保留；Web 仅执行固定 v2 dashboard 参数数组。
- 单 FastAPI 进程：持有固定 Web advisory lock，不建表、不启动采集。禁止多 workers、滚动重叠和容器副本。
- 单采集/增强 worker：常驻与 `--once` 共用独立固定锁，默认关闭。
- PostgreSQL：`HL_APP_DATABASE_URL` 仅运行账号；`HL_APP_MIGRATION_DATABASE_URL` 仅管理进程；`HL_APP_TEST_DATABASE_URL` 仅一次性测试。三者互不回退，Web/worker 不注入迁移账号。

Web 应使用与交易不同的 OS 身份，无 root、交易组或切换身份能力，不继承签名环境变量。交易配置、状态、回执、锁、源码、二进制及可替换父目录对 Web 不可写。只读 pair JSON 和二进制由受控身份管理。生产权限必须用假秘密和临时交易资源验证，不能仅凭命令参数测试宣称隔离通过。

`deploy/database.example.sql` 是角色示例，执行前先审查权限和默认 privileges。运行账号只有必要业务读写，不能 DDL，也不能修改认证、迁移或 operation control。

## 2. 安装

```sh
cd /opt/hl-trader
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e backend
cd frontend
npm ci
npm run build
cd ..
```

要求 Python 3.11+、Node.js 22+、PostgreSQL 和 Rust 构建产物。`.env.example` 只含占位；从秘密环境文件注入，不加载旧 `.env`，不把 DSN/token 放命令行或报告。

## 3. 初始化与启动

先执行有序 migration，再初始化高熵 token（base64url 至少 32 随机字节）：

```sh
.venv/bin/python -m hl_app.db migrate
.venv/bin/python -m hl_app.auth initialize
.venv/bin/python -m hl_app
.venv/bin/python -m hl_app.workers --dry-run
```

管理进程注入 `HL_APP_MIGRATION_DATABASE_URL` 和 `HL_APP_AUTH_TOKEN`；Web 仅注入运行 DSN。轮换使用 `auth rotate`，不能只换环境变量。旧配置进程会拒绝读取数据。

Web 配置 `HL_APP_TRADER_BIN`、`HL_APP_PAIRS_CONFIG`、可信 Origin、Secure cookie。只有 localhost 开发可显式关闭 Secure。生产 HTTPS 下保持 Secure。

worker 默认 `HL_APP_COLLECTORS_ENABLED=false`、`HL_APP_SCHEDULER_ENABLED=false`。真实出口、来源可达性和维护窗口确认后才可启用：

```sh
.venv/bin/python -m hl_app.workers --once --kind quotes
.venv/bin/python -m hl_app.workers --once --kind announcements
.venv/bin/python -m hl_app.workers --run
```

`deploy/hl-app-web.service.example`、`hl-app-worker.service.example` 和 `nginx.conf.example` 仅为模板，不启用。代理只暴露一个同源站点：`/api/v1` 到后端，其余 SPA fallback 到 dist。

## 4. 标准验证

```sh
cargo fmt --check
cargo test --offline
cargo clippy --offline --all-targets -- -D warnings
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m 'not db and not live' -q
# 仅显式一次性测试库：
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -m db -q
cd frontend
npm ci
npm run test:unit
npm run build
npm run test:e2e
npm audit
```

E2E 使用真实 Chromium 和本地临时 PostgreSQL，桌面 1440×900、移动 390×844。真实来源/模型不属于默认回归。

## 5. 预演与停写测量

先在独立预演库完成 full 导入、基线、激活、业务变化、回退导出和源侧恢复。正式源 bundle 和源侧恢复适配由 stock_data agent 负责，未验收前不能切换。

```sh
.venv/bin/python -m hl_app.migration inspect --bundle /path/to/full
.venv/bin/python -m hl_app.migration import --bundle /path/to/full --dry-run
.venv/bin/python -m hl_app.migration import --bundle /path/to/full --apply
.venv/bin/python -m hl_app.migration export --purpose baseline --output /path/to/baseline
.venv/bin/python -m hl_app.migration activate --baseline /path/to/baseline
.venv/bin/python -m hl_app.migration maintenance --enable
.venv/bin/python -m hl_app.migration rollback-export --baseline /path/to/baseline --output /path/to/rollback
```

分别测量停写后导出、传输、导入和核对耗时；若窗口不可接受，停止准备，不擅自改增量合并。小 fixture 的本地结果不代表生产窗口。

## 6. 正式切换

1. 用户确认环境、范围、备份、截止点和窗口；源侧操作交对应 agent。正式恢复适配须已验收。
2. 正式目标业务实体为空且 maintenance，预演数据不得复制。源/目标完整备份和版本清单可用，真实采集关闭。
3. 停旧侧本迁移模块的采集和编辑，排空任务，导出一致性 full；无关财务任务继续。
4. inspect→dry-run→apply 到正式空库，核对自然键、规范值、revision、关联和 checkpoint/gap/recovery。
5. 导出并注册 baseline，保留源冻结 full。权限、页面、真实来源和回退证据满足后，用户授权 activate，再开对应任务和入口。
6. 旧入口保留回退，但不得继续产生分歧写入。未正式切换时新侧保持 maintenance；不得双侧活跃写入。

## 7. 回退

1. 获授权后目标 maintenance，停采集/编辑，排空并完整备份；旧侧仍冻结。
2. 对 cutover baseline 执行 rollback-export，保留完整 snapshot、before/after delta 和报告并交 stock_data agent。缺基线或未排空则停止。
3. 源侧备份后按三方规则恢复：before 才改、after 幂等、其他冲突。checkpoint 保守恢复并保留未证明 gap，禁止最新时间戳覆盖。
4. 所有变化恢复、复用或经用户明确处置后，核对逻辑键、revision/关联、停用和 gap，再恢复旧侧入口/写入。新侧保持停止。
5. 不得只切前端或覆盖交易账本、私钥和锁文件。正式源恢复未验收时不得宣布回退可用。

## 8. 备份与检查

备份包含 PostgreSQL 一致性快照、源冻结 full、注册 baseline、目标 rollback snapshot 和版本清单。迁移包是私有数据，权限 0700，不提交 Git。恢复检查必须比较自然键和值，不能只看行数或 updated_at。
