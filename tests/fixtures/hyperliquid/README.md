# Hyperliquid 共享契约样本

来源：`stock_data` 提交 `cc62434d126cdfc1dc40456f430d943f7d40d7cd` 的 `tests/fixtures/hyperliquid/`，原始序列化由 hl-trader Rust 离线 mock 快照产生。迁入后这些文件由本仓库独立拥有，Python 与 React 测试必须实读此处，不得回指源仓库。

- 时间为既有固定历史时间，不在测试中取当前时间。
- 两个源公开账户地址已替换为保留用途的虚构地址 `0x…0042`、`0x…0043`；不得在样本中写回实际公开账户。
- `v2_snapshot_hype_btc.json` 与 `v2_snapshot_nbis_crwv.json` 覆盖不同 DEX、解锁适用性和块级降级；`v2_announcements.json` 覆盖公告契约。

更新时只使用 Rust 本地 mock 产生的新 JSON，不连接真实账户。将新输出放到本目录后运行：

```sh
python3 tests/fixtures/hyperliquid/normalize.py --write
python3 tests/fixtures/hyperliquid/normalize.py --check
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests/test_hyperliquid_fixtures.py -q
cd frontend && npm run test:hl-pages
```

规范化脚本固定 UTF-8、四空格缩进和结尾换行，并拒绝非虚构账户地址。业务数值不得为通过测试而手工改成零或空值；字段破坏只发生在测试内存副本中。
