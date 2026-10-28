from __future__ import annotations

import hashlib
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

ROOT = Path(__file__).resolve().parents[2]
PG_DIR = Path(sys.argv[1])
PG_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 55440
ADMIN = f"postgresql://postgres@/postgres?host={PG_DIR}&port={PG_PORT}"
SCHEMA_URL = ADMIN + "&options=-csearch_path%3De2e"
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
print(json.dumps({"database_url": SCHEMA_URL, "trader": str(trader), "pairs": str(config_path), "mmx": str(mmx), "research": str(research), "token": TOKEN}))
