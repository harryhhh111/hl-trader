from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hl_app.api import create_app
from hl_app.auth import AuthError, require_session
from hl_app.services import research_reports
from hl_app.services.research_reports import (
    ResearchIndexError,
    ResearchReportError,
    ResearchSnapshotError,
    build_snapshot,
    load_snapshot,
    report_content_url,
    snapshot_content_bytes,
)

FIXTURE_LIBRARY = Path(__file__).parent / "fixtures" / "research_library"


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "stock-research"
    shutil.copytree(FIXTURE_LIBRARY, root)
    return root


@pytest.fixture
def snapshot_root(library: Path, tmp_path: Path) -> Path:
    root = tmp_path / "research-reports"
    build_snapshot(library, root)
    return root


def _write_index(library: Path, document: dict) -> None:
    path = library / "研究流程" / "索引" / "html-index.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _load_index(library: Path) -> dict:
    return json.loads((library / "研究流程" / "索引" / "html-index.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Index contract
# --------------------------------------------------------------------------


def test_index_contract_rejects_broken_documents(library: Path) -> None:
    document = _load_index(library)
    document["schema_version"] = 2
    _write_index(library, document)
    with pytest.raises(ResearchIndexError):
        build_snapshot(library, library.parent / "out")

    document = _load_index(library)
    document["reports"].append(dict(document["reports"][0]))
    _write_index(library, document)
    with pytest.raises(ResearchIndexError):
        build_snapshot(library, library.parent / "out")


def test_index_contract_rejects_unknown_and_missing_fields(library: Path) -> None:
    document = _load_index(library)
    document["reports"][0]["extra_field"] = "x"
    _write_index(library, document)
    with pytest.raises(ResearchIndexError):
        build_snapshot(library, library.parent / "out")

    document = _load_index(library)
    del document["reports"][0]["layout"]
    _write_index(library, document)
    with pytest.raises(ResearchIndexError):
        build_snapshot(library, library.parent / "out")


def test_missing_index_fails_closed(library: Path) -> None:
    (library / "研究流程" / "索引" / "html-index.json").unlink()
    with pytest.raises(ResearchIndexError):
        build_snapshot(library, library.parent / "out")


# --------------------------------------------------------------------------
# Selection and grouping
# --------------------------------------------------------------------------


def test_snapshot_selects_only_company_reports(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    entities = {company["entity_id"] for company in manifest["companies"]}
    assert entities == {"company:microsoft", "company:amd", "company:qxo", "company:topic-only", "company:ready-co"}
    report_ids = set(manifest["reports"])
    assert report_ids == {
        "msft-main", "msft-cashflow", "msft-berkshire",
        "amd-update", "qxo-main", "topic-only", "ready-co",
    }
    excluded = {item["report_id"]: item["reason"] for item in manifest["excluded"]}
    assert "implied-valuation-demo" in excluded
    assert "archived-co" in excluded
    assert "ai-chain-topic" in excluded
    assert "markdown-only" in excluded
    assert manifest["totals"] == {"companies": 5, "reports": 7}


def test_cross_directory_reports_merge_into_one_company(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    microsoft = next(item for item in manifest["companies"] if item["entity_id"] == "company:microsoft")
    assert microsoft["report_ids"] == ["msft-main", "msft-berkshire", "msft-cashflow"]
    assert microsoft["name"] == "Microsoft"
    assert microsoft["securities"] == [{"market": "US", "mic": "XNAS", "symbol": "MSFT"}]
    assert microsoft["report_count"] == 3


def test_main_report_sorts_first_then_newest_date_then_id(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    microsoft = next(item for item in manifest["companies"] if item["entity_id"] == "company:microsoft")
    # msft-main is the company_research report; among topics the newer date wins.
    assert microsoft["report_ids"][0] == "msft-main"
    assert microsoft["report_ids"][1:] == ["msft-berkshire", "msft-cashflow"]


def test_company_without_identity_keeps_report_and_uses_report_title(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    qxo = next(item for item in manifest["companies"] if item["entity_id"] == "company:qxo")
    assert qxo["name"] == "QXO公司研究"
    assert qxo["securities"] == []
    codes = {item["code"] for item in manifest["reports"]["qxo-main"]["diagnostics"]}
    assert "identity_missing" in codes


def test_topic_only_company_appears_without_main_report(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    topic_only = next(item for item in manifest["companies"] if item["entity_id"] == "company:topic-only")
    assert topic_only["report_ids"] == ["topic-only"]


def test_status_and_content_status_are_kept_apart(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    qxo = manifest["reports"]["qxo-main"]
    assert qxo["status"] == "draft"
    assert qxo["content_status"] == "partial"
    ready = manifest["reports"]["ready-co"]
    assert ready["status"] == "ready"
    assert ready["content_status"] == "verified"


def test_unknown_dates_stay_unknown(library: Path, tmp_path: Path) -> None:
    manifest = build_snapshot(library, tmp_path / "out")
    msft = manifest["reports"]["msft-main"]
    assert msft["document_updated_at"] is None
    assert msft["financial_as_of"] is None
    assert msft["market_as_of"] is None
    qxo = manifest["reports"]["qxo-main"]
    assert qxo["document_updated_at"] == "2026-11-17"
    assert qxo["financial_as_of"] == "2026-09-30"
    assert qxo["market_as_of"] == "2026-11-16"


# --------------------------------------------------------------------------
# Path and resource checks
# --------------------------------------------------------------------------


def test_registered_path_traversal_is_rejected(library: Path, tmp_path: Path) -> None:
    document = _load_index(library)
    document["reports"][0]["entry_path"] = "../outside.html"
    _write_index(library, document)
    manifest = build_snapshot(library, tmp_path / "out")
    assert "msft-main" not in manifest["reports"]
    assert any(item["report_id"] == "msft-main" for item in manifest["excluded"])


def test_symlink_entry_is_rejected(library: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.html"
    outside.write_text("<html><body>外部文件</body></html>", encoding="utf-8")
    (library / "MSFT" / "linked.html").symlink_to(outside)
    document = _load_index(library)
    document["reports"][0]["entry_path"] = "MSFT/linked.html"
    _write_index(library, document)
    manifest = build_snapshot(library, tmp_path / "out")
    assert "msft-main" not in manifest["reports"]


def test_external_display_resource_blocks_the_report(library: Path, tmp_path: Path) -> None:
    target = library / "MSFT" / "MSFT.html"
    target.write_text(
        '<html><body><img src="https://cdn.example.com/chart.png"><p>正文</p></body></html>',
        encoding="utf-8",
    )
    manifest = build_snapshot(library, tmp_path / "out")
    assert "msft-main" not in manifest["reports"]
    reason = next(item["reason"] for item in manifest["excluded"] if item["report_id"] == "msft-main")
    assert "外部展示资源" in reason


def test_local_absolute_path_blocks_the_report(library: Path, tmp_path: Path) -> None:
    target = library / "MSFT" / "MSFT.html"
    target.write_text(
        '<html><body><img src="/Users/analyst/private/chart.png"></body></html>', encoding="utf-8"
    )
    manifest = build_snapshot(library, tmp_path / "out")
    assert "msft-main" not in manifest["reports"]


def test_missing_display_resource_blocks_the_report(library: Path, tmp_path: Path) -> None:
    target = library / "MSFT" / "MSFT.html"
    target.write_text('<html><body><img src="assets/missing.png"></body></html>', encoding="utf-8")
    manifest = build_snapshot(library, tmp_path / "out")
    assert "msft-main" not in manifest["reports"]


def test_approved_asset_is_published_and_unpublished_link_is_only_explained(library: Path, tmp_path: Path) -> None:
    asset = library / "MSFT" / "assets"
    asset.mkdir()
    (asset / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    document = _load_index(library)
    document["reports"][0]["assets"] = ["MSFT/assets/chart.png"]
    _write_index(library, document)
    target = library / "MSFT" / "MSFT.html"
    target.write_text(
        '<html><body><img src="assets/chart.png"><a href="factcheck/data/inputs.json">输入数据</a></body></html>',
        encoding="utf-8",
    )
    manifest = build_snapshot(library, tmp_path / "out")
    report = manifest["reports"]["msft-main"]
    assert [asset["path"] for asset in report["assets"]] == ["MSFT/assets/chart.png"]
    assert report["unpublished_links"][0]["reference"] == "factcheck/data/inputs.json"
    assert "MSFT/assets/chart.png" in manifest["files"]
    assert "MSFT/factcheck/data/inputs.json" not in manifest["files"]


def test_broken_report_does_not_hide_other_reports(library: Path, tmp_path: Path) -> None:
    (library / "QXO" / "QXO.html").unlink()
    manifest = build_snapshot(library, tmp_path / "out")
    assert "qxo-main" not in manifest["reports"]
    assert "msft-main" in manifest["reports"]
    assert manifest["totals"]["companies"] == 4


def test_all_candidates_broken_keeps_current_pointer(library: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    build_snapshot(library, output)
    before = (output / "current.json").read_text(encoding="utf-8")
    (library / "MSFT" / "MSFT.html").unlink()
    (library / "MSFT" / "传统软件与现金流.html").unlink()
    (library / "AI_BERKSHIRE" / "微软-AI-Berkshire.html").unlink()
    (library / "AMD" / "近期股价催化.html").unlink()
    (library / "QXO" / "QXO.html").unlink()
    (library / "TOPIC" / "行业专题.html").unlink()
    (library / "READY" / "READY.html").unlink()
    with pytest.raises(ResearchReportError):
        build_snapshot(library, output)
    assert (output / "current.json").read_text(encoding="utf-8") == before


def test_valid_empty_index_produces_an_explicit_empty_snapshot(library: Path, tmp_path: Path) -> None:
    document = _load_index(library)
    document["reports"] = []
    _write_index(library, document)
    manifest = build_snapshot(library, tmp_path / "out")
    assert manifest["totals"] == {"companies": 0, "reports": 0}
    snapshot = load_snapshot(tmp_path / "out")
    assert snapshot.manifest.companies == []


def test_source_change_during_read_is_detected(library: Path, tmp_path: Path, monkeypatch) -> None:
    original_relax = research_reports._relax_directory_tree

    def mutating_relax(root: Path) -> None:
        original_relax(root)
        if Path(root).name.startswith(".staging-"):
            (library / "QXO" / "QXO.html").write_text("<html>读取期间被改动</html>", encoding="utf-8")

    monkeypatch.setattr(research_reports, "_relax_directory_tree", mutating_relax)
    with pytest.raises(ResearchReportError):
        build_snapshot(library, tmp_path / "out")
    assert not (tmp_path / "out" / "current.json").exists()


# --------------------------------------------------------------------------
# Snapshot reading and path safety
# --------------------------------------------------------------------------


def test_snapshot_content_serves_whitelisted_files(snapshot_root: Path) -> None:
    snapshot = load_snapshot(snapshot_root)
    payload, media_type = snapshot_content_bytes(snapshot_root, snapshot.snapshot_id, "MSFT/MSFT.html")
    assert "微软：软件现金流与AI资本回报" in payload.decode("utf-8")
    assert media_type == "text/html; charset=utf-8"


def test_snapshot_content_rejects_paths_outside_the_whitelist(snapshot_root: Path) -> None:
    snapshot = load_snapshot(snapshot_root)
    for candidate in (
        "MSFT/factcheck/data/inputs.json",
        "../current.json",
        "../../etc/passwd",
        "/etc/passwd",
        "MSFT/../../etc/passwd",
        "%2e%2e%2fcurrent.json",
    ):
        with pytest.raises(ResearchSnapshotError):
            snapshot_content_bytes(snapshot_root, snapshot.snapshot_id, candidate)


def test_snapshot_content_normalises_redundant_separators(snapshot_root: Path) -> None:
    snapshot = load_snapshot(snapshot_root)
    payload, _ = snapshot_content_bytes(snapshot_root, snapshot.snapshot_id, "MSFT//MSFT.html")
    assert "微软：软件现金流与AI资本回报" in payload.decode("utf-8")


def test_snapshot_content_rejects_unknown_snapshot(snapshot_root: Path) -> None:
    with pytest.raises(ResearchSnapshotError):
        snapshot_content_bytes(snapshot_root, "s" + "0" * 32, "MSFT/MSFT.html")
    with pytest.raises(ResearchSnapshotError):
        snapshot_content_bytes(snapshot_root, "../../etc", "MSFT/MSFT.html")


def test_content_url_encodes_each_path_segment() -> None:
    url = report_content_url("s" + "1" * 32, "AI_BERKSHIRE/微软-AI Berkshire.html")
    assert url == "/api/v1/research/content/s11111111111111111111111111111111/AI_BERKSHIRE/%E5%BE%AE%E8%BD%AF-AI%20Berkshire.html"


def test_old_snapshot_content_stays_available_after_a_new_snapshot(library: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    first = build_snapshot(library, output)
    first_id = first["snapshot_id"]
    (library / "QXO" / "QXO.html").write_text("<html>第二版</html>", encoding="utf-8")
    second = build_snapshot(library, output)
    assert second["snapshot_id"] != first_id
    current = load_snapshot(output)
    assert current.snapshot_id == second["snapshot_id"]
    payload, _ = snapshot_content_bytes(output, first_id, "QXO/QXO.html")
    assert "第二版" not in payload.decode("utf-8")
    assert (output / "snapshots" / first_id / "manifest.json").is_file()


def test_old_snapshot_url_still_serves_the_original_content(library: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    first = build_snapshot(library, output)
    first_id = first["snapshot_id"]
    (library / "QXO" / "QXO.html").write_text("<html>第二版</html>", encoding="utf-8")
    second = build_snapshot(library, output)
    assert second["snapshot_id"] != first_id

    client = _client(output)
    old = client.get(f"/api/v1/research/content/{first_id}/QXO/QXO.html")
    new = client.get(f"/api/v1/research/content/{second['snapshot_id']}/QXO/QXO.html")
    assert old.status_code == 200
    assert new.status_code == 200
    assert "第二版" not in old.text
    assert "第二版" in new.text


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def _client(snapshot_root: Path | None) -> TestClient:
    """A client without the application lifespan: the research routes need no database."""
    app = create_app()
    app.state.settings = SimpleNamespace(
        research_reports_path=str(snapshot_root) if snapshot_root else None,
        auth_mode="token",
    )
    app.state.initiate_shutdown = lambda: None
    app.dependency_overrides[require_session] = lambda: SimpleNamespace(account_id=1)
    return TestClient(app)


def test_companies_api_lists_companies_and_reports(snapshot_root: Path) -> None:
    response = _client(snapshot_root).get("/api/v1/research/companies")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["totals"] == {"companies": 5, "reports": 7}
    assert data["snapshot_id"]
    assert any(company["entity_id"] == "company:microsoft" for company in data["companies"])


def test_report_detail_api_returns_siblings_and_content_url(snapshot_root: Path) -> None:
    response = _client(snapshot_root).get("/api/v1/research/reports/msft-main")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["report"]["title"] == "微软：软件现金流与AI资本回报"
    assert {item["report_id"] for item in data["sibling_reports"]} == {"msft-cashflow", "msft-berkshire"}
    assert data["content_url"].startswith("/api/v1/research/content/")
    assert "MSFT/MSFT.html" in data["content_url"]
    assert data["unpublished_links"]


def test_unknown_report_is_404(snapshot_root: Path) -> None:
    assert _client(snapshot_root).get("/api/v1/research/reports/missing-report").status_code == 404


def test_content_api_sets_isolation_headers(snapshot_root: Path) -> None:
    client = _client(snapshot_root)
    snapshot_id = client.get("/api/v1/research/companies").json()["data"]["snapshot_id"]
    content = client.get(f"/api/v1/research/content/{snapshot_id}/MSFT/MSFT.html")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("text/html; charset=utf-8")
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    policy = content.headers["content-security-policy"]
    assert "sandbox" in policy
    assert "script-src 'none'" in policy
    assert "connect-src 'none'" in policy
    assert "form-action 'none'" in policy


def test_content_api_rejects_unregistered_and_escaping_paths(snapshot_root: Path) -> None:
    client = _client(snapshot_root)
    snapshot_id = client.get("/api/v1/research/companies").json()["data"]["snapshot_id"]
    assert client.get(f"/api/v1/research/content/{snapshot_id}/MSFT/factcheck/data/inputs.json").status_code == 404
    assert client.get(f"/api/v1/research/content/{snapshot_id}/..%2f..%2fcurrent.json").status_code == 404
    assert client.get(f"/api/v1/research/content/{snapshot_id}/%2Fetc%2Fpasswd").status_code == 404


def test_research_api_requires_a_session(snapshot_root: Path) -> None:
    app = create_app()
    app.state.settings = SimpleNamespace(research_reports_path=str(snapshot_root), auth_mode="token")
    app.state.initiate_shutdown = lambda: None
    app.state.web_lifecycle = SimpleNamespace(ensure_healthy=lambda: None)
    app.state.auth = SimpleNamespace(
        authenticate=lambda credential: (_ for _ in ()).throw(AuthError(401, "未登录"))
    )
    client = TestClient(app)
    assert client.get("/api/v1/research/companies").status_code == 401
    assert client.get("/api/v1/research/reports/msft-main").status_code == 401


def test_unconfigured_research_returns_503() -> None:
    response = _client(None).get("/api/v1/research/companies")
    assert response.status_code == 503
    assert "HL_APP_RESEARCH_REPORTS_PATH" in response.json()["detail"]


def test_empty_snapshot_root_returns_503(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    response = _client(tmp_path / "empty").get("/api/v1/research/companies")
    assert response.status_code == 503
    assert "研究快照不可用" in response.json()["detail"]
