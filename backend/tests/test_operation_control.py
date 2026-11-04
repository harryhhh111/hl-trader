from __future__ import annotations

import json
import sys

import pytest

from hl_app.collectors.sources import parse_sec
from hl_app.migration import __main__ as migration_cli


@pytest.mark.parametrize("current", [True, False])
def test_sec_current_and_historical_shapes_preserve_records(current) -> None:
    columns = {
        "accessionNumber": ["0001234567-26-000001"],
        "form": ["8-K"],
        "filingDate": ["2026-10-20"],
        "primaryDocument": ["report.htm"],
    }
    payload = {"filings": {"recent": columns}} if current else columns
    records = parse_sec(json.dumps(payload).encode(), {"issuer_identifier": "0001234567"})
    assert len(records) == 1
    assert records[0].source_record_id == columns["accessionNumber"][0]
    assert records[0].published_precision == "day"
    assert records[0].url.endswith("/000123456726000001/report.htm")


@pytest.mark.parametrize("payload", [
    {}, [], {"filings": []}, {"filings": {"recent": {}}},
    {"accessionNumber": ["one"], "form": [], "filingDate": ["2026-10-20"], "primaryDocument": ["a.htm"]},
    {"accessionNumber": ["one"], "form": ["8-K"], "filingDate": ["2026-10-20"], "primaryDocument": [""]},
])
def test_sec_invalid_shape_is_an_error_instead_of_empty_success(payload) -> None:
    with pytest.raises(ValueError):
        parse_sec(json.dumps(payload).encode(), {"issuer_identifier": "0001234567"})


def test_maintenance_cli_reports_timeout_as_failure(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HL_APP_MIGRATION_DATABASE_URL", "unused-test-url")
    monkeypatch.setattr(sys, "argv", ["migration", "maintenance", "--enable"])
    monkeypatch.setattr(migration_cli, "maintenance", lambda _url: {"mode": "maintenance", "drained": False})
    assert migration_cli.main() == 1
    assert json.loads(capsys.readouterr().out)["drained"] is False
