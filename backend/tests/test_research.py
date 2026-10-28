from __future__ import annotations

import subprocess

import pytest
import yaml

from hl_app.services.research_reader import ResearchReadError, read_research


def init_repo(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    entry = root / "ORCL"
    entry.mkdir()
    (entry / "主文档.md").write_text("# Report\n## 观点\nOCI 增速与 RPO 是核心变量。\n", encoding="utf-8")
    data = {
        "schema_version": 2,
        "entity_id": "company:oracle",
        "kind": "company",
        "name": "Oracle",
        "securities": [{"market": "US", "mic": "XNAS", "symbol": "ORCL"}],
        "primary_document": "主文档.md",
        "documents": [],
        "related_entities": [],
        "sections": {
            "thesis": {"path": "主文档.md", "heading_path": ["Report", "观点"]},
            "risks": None,
            "metrics": None,
            "open_questions": None,
            "research_metadata": None,
        },
    }
    (entry / "research.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)


def test_research_reads_only_committed_entry(tmp_path) -> None:
    init_repo(tmp_path)
    (tmp_path / "ORCL" / "主文档.md").write_text("未提交内容")
    docs, text, diagnostic = read_research(tmp_path, "US", "ORCL")
    assert docs == ["ORCL/主文档.md"]
    assert "OCI 增速" in text and "未提交" not in text
    assert diagnostic["status"] == "matched"
    assert read_research(tmp_path, "US", "UNKNOWN")[2]["status"] == "no_entry"


def test_missing_or_broken_research_fails_closed(tmp_path) -> None:
    with pytest.raises(ResearchReadError):
        read_research(tmp_path / "missing", "US", "ORCL")
    init_repo(tmp_path)
    (tmp_path / "ORCL" / "research.yaml").write_text("schema_version: 999")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "broken"], check=True)
    with pytest.raises(ResearchReadError):
        read_research(tmp_path, "US", "ORCL")
