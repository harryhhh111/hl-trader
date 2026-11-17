from __future__ import annotations

import hashlib
import struct
import zlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import yaml

from hl_app.auth import initialize
from hl_app.db import migrate
from hl_app.services.research_reports import build_snapshot

ROOT = Path(__file__).resolve().parents[2]
PG_DIR = Path(sys.argv[1])
PG_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 55440
ADMIN = os.environ.get("HL_APP_E2E_ADMIN_URL", f"postgresql://postgres@/postgres?host={PG_DIR}&port={PG_PORT}")
SCHEMA_URL = ADMIN + ("&" if "?" in ADMIN else "?") + "options=-csearch_path%3De2e"
TOKEN = "a" * 64

with psycopg2.connect(ADMIN) as conn:
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute("CREATE SCHEMA e2e")
migrate(SCHEMA_URL)
initialize(SCHEMA_URL, TOKEN)
now = datetime.now(timezone.utc).isoformat()
with psycopg2.connect(SCHEMA_URL) as conn:
    with conn.cursor() as cursor:
        cursor.execute("UPDATE operation_control SET mode = 'active'")
        rows = [
            ("US", "ORCL", "Oracle", "USD", "ORCL.N", ".N", "软件"),
            ("CN_A", "000001", "A 示例", "CNY", "sz000001", None, None),
            ("CN_HK", "00001", "HK 示例", "HKD", "hk00001", None, None),
        ]
        for market, code, name, currency, symbol, suffix, industry in rows:
            cursor.execute(
                "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix, industry, cik) VALUES (%s, %s, %s, %s, 'tencent', %s, %s, %s, %s) RETURNING id",
                (market, code, name, currency, symbol, suffix, industry, "0000001234" if market == "US" else None),
            )
            security_id = cursor.fetchone()[0]
            cursor.execute("INSERT INTO watchlist_items (security_id, group_name, note, research_url) VALUES (%s, %s, %s, %s)", (security_id, "初始分组", "初始理由", "https://example.com/research"))
        cursor.execute(
            "INSERT INTO daily_quotes (market, stock_code, trade_date, close, source, currency) VALUES ('US', 'ORCL', '2026-10-21', 100, 'fixture', 'USD'), ('US', 'ORCL', '2026-10-22', 102, 'fixture', 'USD')"
        )
        cursor.execute(
            "INSERT INTO watchlist_announcement_source (source_id, market, source_kind, platform, issuer_identifier, allowed_domain, entry_url, enabled, source_status) VALUES (1, 'US', 'media', 'fixture', '', 'example.com', 'https://example.com/feed', true, 'enabled')"
        )
        cursor.execute(
            """
            INSERT INTO watchlist_announcement (
                id, market, issuer_identifier, source_id, source_record_id, title,
                form_type, original_url, published_at, published_at_precision,
                source_published_raw, source_timezone, first_observed_at,
                last_seen_at, content_hash, content_nature, media_source,
                revision, is_backfill, title_zh, summary_zh, importance
            ) VALUES (1, 'US', '', 1, 'story-1', 'Shared story - Reuters', 'media_report',
                'https://example.com/story-1', %s, 'second', %s, 'UTC', %s, %s, 'one',
                'unclassified', 'Reuters', 1, false, '共享资讯', '中文摘要', 'high')
            """,
            (now, now, now, now),
        )
        cursor.execute(
            "INSERT INTO watchlist_announcement_security (announcement_id, market, stock_code, relation_reason) VALUES (1, 'US', 'ORCL', 'ticker ORCL')"
        )

config = {
    "config_version": 1,
    "pairs": [
        {"id": "hype-btc", "label": "HYPE / BTC", "enabled": True, "network": "mainnet", "dex": "", "coins": ["HYPE", "BTC"], "account_address": "0x0000000000000000000000000000000000000042", "unlock_anchors": []},
        {"id": "nbis-crwv", "label": "NBIS / CRWV", "enabled": True, "network": "mainnet", "dex": "xyz", "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": "0x0000000000000000000000000000000000000043", "unlock_anchors": None},
    ],
}
work = PG_DIR / "work"
work.mkdir(exist_ok=True)
config_path = work / "pairs.json"
raw = json.dumps(config).encode()
config_path.write_bytes(raw)
revision = hashlib.sha256(raw).hexdigest()
trader = work / "fake_trader.py"
trader.write_text(
    "#!/usr/bin/env python3\n"
    "import json, sys\n"
    f"revision = {revision!r}\n"
    "pair = sys.argv[sys.argv.index('--pair') + 1] if '--pair' in sys.argv else None\n"
    "if pair is None:\n"
    "    print(json.dumps({'schema_version': 2, 'queried_at': '2026-10-22T12:00:00Z', 'block': {'status': 'ok', 'queried_at': '2026-10-22T12:00:00Z', 'source_queries': {'general': {'status': 'ok', 'queried_at': '2026-10-22T12:00:00Z'}, 'api': {'status': 'ok', 'queried_at': '2026-10-22T12:00:00Z'}}, 'diagnostics': [], 'items': []}}))\n"
    "    raise SystemExit(0)\n"
    f"path = {str(ROOT / 'tests/fixtures/hyperliquid/v2_snapshot_hype_btc.json')!r} if pair == 'hype-btc' else {str(ROOT / 'tests/fixtures/hyperliquid/v2_snapshot_nbis_crwv.json')!r}\n"
    "data = json.load(open(path))\n"
    "data['config_revision'] = revision\n"
    "if 'adjust-preflight' in sys.argv:\n"
    "    from datetime import datetime, timezone\n"
    "    address = sys.argv[sys.argv.index('--address') + 1]\n"
    "    print(json.dumps({'schema_version': 1, 'pair_id': pair, 'network': 'mainnet', 'config_revision': revision, 'account_address': address, 'queried_at': datetime.now(timezone.utc).isoformat(), 'policy_max_leverage': 3, 'ready_for_increase': True, 'legs': [{'coin': coin, 'margin_mode': 'cross', 'leverage': 3, 'market_max_leverage': 5, 'cross_available': True, 'ready_for_increase': True, 'diagnostics': []} for coin in data['pair']['coins']]}))\n"
    "    raise SystemExit(0)\n"
    "if pair == 'hype-btc': data['blocks']['unlocks']['anchors'] = {}\n"
    "print(json.dumps(data))\n",
    encoding="utf-8",
)
trader.chmod(0o755)
mmx = work / "mmx"
mmx.write_text("#!/bin/sh\nprintf '%s' '{\"group\":\"AI 云\",\"note\":\"研究草稿\"}'\n")
mmx.chmod(0o755)
research = work / "research"
research.mkdir()
subprocess.run(["git", "init", "-q", str(research)], check=True)
subprocess.run(["git", "-C", str(research), "config", "user.email", "fixture@example.invalid"], check=True)
subprocess.run(["git", "-C", str(research), "config", "user.name", "Fixture"], check=True)
(research / "ORCL").mkdir()
(research / "ORCL" / "主文档.md").write_text("# Report\n## 观点\nOCI 增速与 RPO 是核心变量。\n", encoding="utf-8")
entry = {
    "schema_version": 2,
    "entity_id": "company:oracle",
    "kind": "company",
    "name": "Oracle",
    "securities": [{"market": "US", "mic": "XNAS", "symbol": "ORCL"}],
    "primary_document": "主文档.md",
    "documents": [],
    "related_entities": [],
    "sections": {"thesis": {"path": "主文档.md", "heading_path": ["Report", "观点"]}, "risks": None, "metrics": None, "open_questions": None, "research_metadata": None},
}
(research / "ORCL" / "research.yaml").write_text(yaml.safe_dump(entry, allow_unicode=True), encoding="utf-8")
subprocess.run(["git", "-C", str(research), "add", "."], check=True)
subprocess.run(["git", "-C", str(research), "commit", "-qm", "fixture"], check=True)

# --- 公司研究 HTML 快照（只读阅读，不执行研究脚本） ---
library = work / "research-library"
(library / "研究流程" / "索引").mkdir(parents=True)
(library / "ORCL" / "reports").mkdir(parents=True)
(library / "ORCL" / "assets").mkdir(parents=True)
(library / "ORCL" / "evidence").mkdir(parents=True)
(library / "ORCL" / "research.yaml").write_text(
    yaml.safe_dump(
        {
            "schema_version": 2,
            "entity_id": "company:oracle",
            "kind": "company",
            "name": "Oracle",
            "aliases": ["ORCL", "甲骨文"],
            "securities": [{"market": "US", "mic": "XNAS", "symbol": "ORCL"}],
            "primary_document": "主文档.md",
            "documents": [],
            "related_entities": [],
            "sections": {"thesis": None, "risks": None, "metrics": None, "open_questions": None, "research_metadata": None},
        },
        allow_unicode=True,
    ),
    encoding="utf-8",
)
(library / "ORCL" / "evidence" / "data.json").write_text('{"未发布": true}', encoding="utf-8")
def png_chunk(kind, payload):
    return struct.pack('!I', len(payload)) + kind + payload + struct.pack('!I', zlib.crc32(kind + payload))

PNG = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 6, 0, 0, 0))
       + png_chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\xff')) + png_chunk(b'IEND', b''))
(library / 'ORCL' / 'assets' / 'report.css').write_text(
    '@import "nested.css"; .fixture-styled{color:rgb(1,2,3)}', encoding='utf-8')
(library / 'ORCL' / 'assets' / 'nested.css').write_text(
    '#style-proof{background-image:url(chart.png)}', encoding='utf-8')
(library / "ORCL" / "assets" / "chart.png").write_bytes(PNG)
(library / "ORCL" / "reports" / "main.html").write_text(
    """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>甲骨文：OCI 与 RPO</title>
<link rel="stylesheet" href="../assets/report.css">
<style>body{font-family:system-ui,sans-serif;margin:1.5rem;line-height:1.7}
table{border-collapse:collapse}th,td{border:1px solid #bbb;padding:.3rem .6rem}</style>
</head><body>
<h1 id="top">甲骨文：OCI 与 RPO</h1>
<p id="style-proof" class="fixture-styled">样式加载确认</p>
<nav><ul><li><a href="#thesis">核心判断</a></li><li><a href="#metrics">跟踪指标</a></li></ul></nav>
<h2 id="thesis">核心判断</h2>
<p>OCI 增速与 RPO 是核心变量，RPO 转化率仍待观察。</p>
<table><tr><th>财年</th><th>营业收入</th></tr><tr><td>2024</td><td>52,961</td></tr><tr><td>2025</td><td>57,419</td></tr></table>
<img src="../assets/chart.png" alt="收入趋势图" width="120" height="80">
<h2 id="metrics">跟踪指标</h2>
<details><summary>展开查看口径</summary><p>口径为美元百万。</p></details>
<h2 id="sources">来源</h2>
<ul>
<li><a href="https://example.com/oracle-source">甲骨文投资者关系</a></li>
<li><a href="../evidence/data.json">未发布证据数据</a></li>
<li><a href="topic.html?ref=fixture#contracts">同公司专题</a></li>
</ul>
<form action="/api/v1/session/logout" method="post"><button type="submit">不应可提交</button></form>
<script>fetch('/api/v1/session').then(function(r){return r.text()}).then(function(t){document.title='已泄漏:'+t});</script>
</body></html>
""",
    encoding="utf-8",
)
(library / "ORCL" / "reports" / "topic.html").write_text(
    """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>甲骨文：云合同结构</title></head>
<body><h1 id="contracts">甲骨文：云合同结构</h1><p>剩余履约义务与合同期限结构。</p></body></html>
""",
    encoding="utf-8",
)
html_index = {
    "schema_version": 1,
    "reports": [
        {
            "report_id": "orcl-main",
            "title": "甲骨文：OCI 与 RPO",
            "entity_id": "company:oracle",
            "project_dir": "ORCL",
            "entry_path": "ORCL/reports/main.html",
            "report_type": "company_research",
            "status": "draft",
            "content_status": "unreviewed",
            "document_updated_at": "2026-11-17",
            "financial_as_of": "2026-08-31",
            "market_as_of": "2026-11-14",
            "layout": "legacy",
            "assets": ["ORCL/assets/chart.png", "ORCL/assets/report.css", "ORCL/assets/nested.css"],
            "unpublished_links": {
                "ORCL/evidence/data.json": "网站附件发布范围尚未审核；本轮仅登记报告，不自动公开证据。"
            },
            "content_sha256": None,
        },
        {
            "report_id": "orcl-topic",
            "title": "甲骨文：云合同结构",
            "entity_id": "company:oracle",
            "project_dir": "ORCL",
            "entry_path": "ORCL/reports/topic.html",
            "report_type": "topic",
            "status": "draft",
            "content_status": "unreviewed",
            "document_updated_at": None,
            "financial_as_of": None,
            "market_as_of": None,
            "layout": "legacy",
            "assets": [],
            "unpublished_links": {},
            "content_sha256": None,
        },
        {
            "report_id": "orcl-demo",
            "title": "演示报告",
            "entity_id": None,
            "project_dir": "ORCL",
            "entry_path": "ORCL/reports/topic.html",
            "report_type": "demo",
            "status": "ready",
            "content_status": "synthetic",
            "document_updated_at": None,
            "financial_as_of": None,
            "market_as_of": None,
            "layout": "legacy",
            "assets": [],
            "unpublished_links": {},
            "content_sha256": None,
        },
    ],
    "redirects": [],
}
for report in html_index['reports']:
    if report['status'] == 'ready':
        report['content_sha256'] = hashlib.sha256((library / report['entry_path']).read_bytes()).hexdigest()
(library / "研究流程" / "索引" / "html-index.json").write_text(
    json.dumps(html_index, ensure_ascii=False, indent=2), encoding="utf-8"
)
research_snapshot = work / "research-snapshot"
build_snapshot(library, research_snapshot)
print(json.dumps({
    "database_url": SCHEMA_URL, "trader": str(trader), "pairs": str(config_path), "mmx": str(mmx),
    "research": str(research), "research_snapshot": str(research_snapshot), "token": TOKEN,
}))
