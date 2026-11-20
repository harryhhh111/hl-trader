"""Immutable display snapshots of company research HTML reports.

The only source of truth is the research library index
``研究流程/index/html-index.json`` (with a legacy path fallback). This module
owns the consumer side contract: validate the index, select the reports allowed to
show, verify every registered path and display resource, and copy the accepted
files into a content addressed snapshot.

The web layer never walks the research library: it only reads the generated
snapshot through :func:`load_snapshot`. Nothing here executes research scripts,
contacts data vendors or runs git pull.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote

import yaml

from .research_display import DisplayError, StaticDisplay, DISPLAY_REVISION, local_reference, srcset_items


class ResearchReportError(RuntimeError):
    """Any failure that must keep the current snapshot pointer untouched."""


class ResearchIndexError(ResearchReportError):
    """The source index is missing, unreadable or violates the schema."""


class ResearchSnapshotError(ResearchReportError):
    """The generated snapshot is missing, unreadable or incomplete."""


class ResearchSnapshotUnavailable(ResearchSnapshotError):
    """The snapshot exists but the service user cannot read it."""


INDEX_RELATIVE_PATH = "研究流程/index/html-index.json"
LEGACY_INDEX_RELATIVE_PATH = "研究流程/索引/html-index.json"
SNAPSHOT_SCHEMA_VERSION = 2
SOURCE_INDEX_SCHEMA_VERSION = 1

ALLOWED_REPORT_TYPES = frozenset({"company_research", "topic", "update"})
ALLOWED_STATUSES = frozenset({"draft", "ready"})
ALLOWED_CONTENT_STATUSES = frozenset({"unreviewed", "partial", "verified", "synthetic"})
ALLOWED_LAYOUTS = frozenset({"standard", "legacy"})

REPORT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMPANY_ENTITY_PATTERN = re.compile(r"^company:[a-z0-9]+(?:-[a-z0-9]+)*$")
ENTITY_PATTERN = re.compile(r"^(company|topic|protocol|token|other):[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MARKETS: dict[str, tuple[str, frozenset[str]]] = {
    "US": ("US", frozenset({"XNYS", "XNAS"})),
    "HK": ("CN_HK", frozenset({"XHKG"})),
    "CN": ("CN_A", frozenset({"XSHG", "XSHE", "XBSE"})),
}
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")

MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_HTML_BYTES = 16 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_IDENTITY_BYTES = 256 * 1024
MAX_ASSETS_PER_REPORT = 64
SNAPSHOT_DIR_MODE = 0o755
SNAPSHOT_FILE_MODE = 0o644
MAX_REPORTS = 5000
MAX_IDENTITY_DEPTH = 3

EXCLUDED_SOURCE_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "参考库", "归档", ".github", ".idea"}
)

MEDIA_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/plain; charset=utf-8",
    ".mjs": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".json": "application/json; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}

# Elements whose resource reference must resolve locally for the page to render.
DISPLAY_RESOURCE_TAGS = frozenset({"img", "link", "script", "iframe", "embed", "object", "video", "audio", "source", "track", "input"})
# Attributes that point at a sub resource.
RESOURCE_ATTRIBUTES = frozenset({"src", "data", "poster", "srcset"})
# Attributes that point at a hyperlink.
HYPERLINK_ATTRIBUTES = frozenset({"href"})
# Tags whose href is a stylesheet / display dependency rather than a citation.
STYLESHEET_REL = frozenset({"stylesheet", "preload"})


def media_type_for(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return MEDIA_TYPES.get(suffix, "application/octet-stream")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_clean_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ResearchIndexError(message)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        _require(key not in result, f'重复 JSON 键：{key}')
        result[key] = value
    return result


# --------------------------------------------------------------------------
# Index contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportEntry:
    report_id: str
    title: str
    entity_id: str | None
    project_dir: str
    entry_path: str
    report_type: str
    status: str
    content_status: str
    document_updated_at: str | None
    financial_as_of: str | None
    market_as_of: str | None
    layout: str
    assets: tuple[str, ...]
    unpublished_links: dict[str, str]
    content_sha256: str | None

    @property
    def directory(self) -> str:
        return str(PurePosixPath(self.entry_path).parent)


@dataclass(frozen=True)
class SourceIndex:
    schema_version: int
    entries: tuple[ReportEntry, ...]
    redirects: tuple[dict[str, str], ...]
    index_path: str
    index_sha256: str


def _parse_date(value: Any, field_name: str, report_id: str) -> str | None:
    if value is None:
        return None
    _require(isinstance(value, str), f"{report_id}: {field_name} 必须是字符串或 null")
    _require(bool(DATE_PATTERN.match(value)), f"{report_id}: {field_name} 必须是 YYYY-MM-DD 日期")
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise ResearchIndexError(f'{report_id}: {field_name} 不是有效日期') from exc
    return value


def _parse_report_entry(raw: Any, position: int) -> ReportEntry:
    _require(isinstance(raw, dict), f"reports[{position}] 必须是对象")
    report_id = raw.get("report_id")
    _require(
        isinstance(report_id, str) and bool(REPORT_ID_PATTERN.match(report_id)),
        f"reports[{position}].report_id 必须是稳定的小写短横线 ID",
    )
    required = {
        "report_id", "title", "entity_id", "project_dir", "entry_path", "report_type",
        "status", "content_status", "document_updated_at", "financial_as_of",
        "market_as_of", "layout", "assets", "unpublished_links", "content_sha256",
    }
    missing = required - set(raw)
    _require(not missing, f"{report_id}: 缺少字段 {sorted(missing)}")
    unknown = set(raw) - required
    _require(not unknown, f"{report_id}: 包含未知字段 {sorted(unknown)}")

    title = raw["title"]
    _require(_is_clean_string(title), f"{report_id}: title 无效")
    entity_id = raw["entity_id"]
    _require(
        entity_id is None or (isinstance(entity_id, str) and bool(ENTITY_PATTERN.match(entity_id))),
        f"{report_id}: entity_id 无效",
    )
    _require(_is_clean_string(raw["project_dir"]), f"{report_id}: project_dir 无效")
    _require(_is_clean_string(raw["entry_path"]), f"{report_id}: entry_path 无效")
    _require(raw["report_type"] in ALLOWED_REPORT_TYPES | {"demo"}, f"{report_id}: 未知 report_type")
    _require(raw["status"] in {"draft", "ready", "archived"}, f"{report_id}: 未知 status")
    _require(raw["content_status"] in ALLOWED_CONTENT_STATUSES, f"{report_id}: 未知 content_status")
    _require(raw["layout"] in ALLOWED_LAYOUTS, f"{report_id}: 未知 layout")
    assets = raw["assets"]
    _require(isinstance(assets, list) and all(_is_clean_string(item) for item in assets), f"{report_id}: assets 无效")
    _require(len(set(assets)) == len(assets), f"{report_id}: assets 存在重复路径")
    _require(len(assets) <= MAX_ASSETS_PER_REPORT, f"{report_id}: assets 数量超限")
    unpublished = raw["unpublished_links"]
    _require(
        isinstance(unpublished, dict)
        and all(_is_clean_string(key) and _is_clean_string(value) for key, value in unpublished.items()),
        f"{report_id}: unpublished_links 无效",
    )
    content_sha256 = raw["content_sha256"]
    _require(
        content_sha256 is None or (isinstance(content_sha256, str) and bool(SHA256_PATTERN.match(content_sha256))),
        f"{report_id}: content_sha256 无效",
    )
    _require(raw['status'] != 'ready' or content_sha256 is not None,
             f'{report_id}: ready 报告必须登记正文哈希')
    return ReportEntry(
        report_id=report_id,
        title=title,
        entity_id=entity_id,
        project_dir=raw["project_dir"],
        entry_path=raw["entry_path"],
        report_type=raw["report_type"],
        status=raw["status"],
        content_status=raw["content_status"],
        document_updated_at=_parse_date(raw["document_updated_at"], "document_updated_at", report_id),
        financial_as_of=_parse_date(raw["financial_as_of"], "financial_as_of", report_id),
        market_as_of=_parse_date(raw["market_as_of"], "market_as_of", report_id),
        layout=raw["layout"],
        assets=tuple(assets),
        unpublished_links=dict(unpublished),
        content_sha256=content_sha256,
    )


def load_index(source_root: Path) -> SourceIndex:
    """Read and validate the research library index. Never executes source code."""
    relative_path = INDEX_RELATIVE_PATH
    # A malformed or inaccessible canonical index must fail closed, never
    # silently publish stale data from the legacy location.
    if not (source_root / relative_path).exists() and not (source_root / relative_path).is_symlink():
        relative_path = LEGACY_INDEX_RELATIVE_PATH
    index_path = source_root / relative_path
    if not index_path.is_file():
        raise ResearchIndexError(f"研究索引不存在：{INDEX_RELATIVE_PATH}")
    size = index_path.stat().st_size
    if size > MAX_INDEX_BYTES:
        raise ResearchIndexError("研究索引超过读取大小限制")
    raw = index_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchIndexError(f"研究索引不是有效 JSON：{exc}") from exc
    _require(isinstance(document, dict), "研究索引必须是对象")
    _require(
        type(document.get("schema_version")) is int and document.get("schema_version") == SOURCE_INDEX_SCHEMA_VERSION,
        f"仅支持 schema_version: {SOURCE_INDEX_SCHEMA_VERSION}",
    )
    _require(set(document) <= {"schema_version", "reports", "redirects"}, "研究索引包含未知字段")
    reports = document.get("reports")
    _require(isinstance(reports, list), "reports 必须是列表")
    _require(len(reports) <= MAX_REPORTS, "研究索引条目数量超限")
    entries = tuple(_parse_report_entry(item, position) for position, item in enumerate(reports))
    seen: set[str] = set()
    for entry in entries:
        _require(entry.report_id not in seen, f"重复 report_id：{entry.report_id}")
        seen.add(entry.report_id)
    redirects = document.get("redirects")
    _require(isinstance(redirects, list), "redirects 必须是列表")
    parsed_redirects: list[dict[str, str]] = []
    redirect_paths: set[str] = set()
    for position, item in enumerate(redirects):
        _require(isinstance(item, dict), f"redirects[{position}] 必须是对象")
        _require(set(item) == {'from', 'report_id'}, f'redirects[{position}] 字段无效')
        source = item.get("from")
        target = item.get("report_id")
        _require(_is_clean_string(source), f"redirects[{position}].from 无效")
        _require(
            isinstance(target, str) and bool(REPORT_ID_PATTERN.match(target)),
            f"redirects[{position}].report_id 无效",
        )
        normalized = _safe_relative_path(source)
        _require(normalized is not None and normalized not in redirect_paths,
                 f'redirects[{position}] 路径越界或重复')
        _require(normalized not in {entry.entry_path for entry in entries},
                 f'redirects[{position}] 与报告路径冲突')
        _require(target in seen, f'redirects[{position}] 目标不存在')
        redirect_paths.add(normalized)
        parsed_redirects.append({"from": normalized, "report_id": target})
    return SourceIndex(
        schema_version=SOURCE_INDEX_SCHEMA_VERSION,
        entries=entries,
        redirects=tuple(parsed_redirects),
        index_path=relative_path,
        index_sha256=_sha256_bytes(raw),
    )


# --------------------------------------------------------------------------
# Optional company identity (research.yaml)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityReference:
    market: str
    mic: str
    symbol: str

    def to_dict(self) -> dict[str, str]:
        return {"market": self.market, "mic": self.mic, "symbol": self.symbol}


@dataclass(frozen=True)
class CompanyIdentity:
    entity_id: str
    name: str | None
    aliases: tuple[str, ...]
    securities: tuple[SecurityReference, ...]
    source_path: str


class _IdentityLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            _require(isinstance(key, str), "YAML 键必须是字符串")
            _require(key not in result, f"重复 YAML 键：{key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _iter_identity_files(source_root: Path) -> Iterable[Path]:
    stack: list[tuple[Path, int]] = [(source_root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth + 1 <= MAX_IDENTITY_DEPTH and entry.name not in EXCLUDED_SOURCE_DIRS:
                    stack.append((entry, depth + 1))
                continue
            if entry.name == "research.yaml":
                yield entry


ENTITY_ID_SCAN_PATTERN = re.compile(r"^entity_id\s*:\s*['\"]?(?P<value>[^'\"\n#]+)", re.MULTILINE)


def _peek_entity_id(text: str) -> str | None:
    """Cheap pre-scan so identity files of unrelated companies are never parsed."""
    match = ENTITY_ID_SCAN_PATTERN.search(text)
    if match is None:
        return None
    return match.group("value").strip()


def load_identities(
    source_root: Path, wanted: Iterable[str] | None = None, tracker: "_SourceTracker | None" = None
) -> tuple[dict[str, CompanyIdentity], list[dict[str, str]], list[str]]:
    """Read optional ``research.yaml`` entries for company identity only.

    The file is an optional identity hint: a missing or invalid entry keeps the
    report readable and only records a diagnostic. Markdown section extraction
    from the research library is intentionally not used here.
    """
    wanted_ids = set(wanted) if wanted is not None else None
    identities: dict[str, CompanyIdentity] = {}
    diagnostics: list[dict[str, str]] = []
    consumed: list[str] = []
    for path in _iter_identity_files(source_root):
        relative = path.relative_to(source_root).as_posix()
        try:
            if tracker is not None:
                raw = tracker.read(relative, MAX_IDENTITY_BYTES)
            else:
                size = path.stat().st_size
                if size > MAX_IDENTITY_BYTES:
                    raise ResearchIndexError("research.yaml 超过读取大小限制")
                raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (ResearchReportError, OSError, UnicodeError) as exc:
            diagnostics.append({"path": relative, "message": f"research.yaml 不可读取：{exc}"})
            continue
        peeked = _peek_entity_id(text)
        if wanted_ids is not None and peeked not in wanted_ids:
            # Identity of an unrelated entity is not a diagnostic for this feature.
            continue
        try:
            if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
                raise ResearchIndexError("research.yaml 不支持 YAML 别名")
            data = yaml.load(text, Loader=_IdentityLoader)
            if not isinstance(data, dict):
                raise ResearchIndexError("research.yaml 必须是映射")
            entity_id = data.get("entity_id")
            if not isinstance(entity_id, str) or not COMPANY_ENTITY_PATTERN.match(entity_id):
                raise ResearchIndexError("research.yaml 的 entity_id 不是公司主体")
            if wanted_ids is not None and entity_id not in wanted_ids:
                continue
            if entity_id in identities:
                raise ResearchIndexError(f"重复主体入口：{entity_id}")
            name = data.get("name")
            if name is not None and not _is_clean_string(name):
                raise ResearchIndexError("research.yaml 的 name 无效")
            raw_aliases = data.get("aliases") or []
            if not isinstance(raw_aliases, list) or not all(_is_clean_string(item) for item in raw_aliases):
                raise ResearchIndexError("research.yaml 的 aliases 无效")
            raw_securities = data.get("securities") or []
            if not isinstance(raw_securities, list):
                raise ResearchIndexError("research.yaml 的 securities 无效")
            securities: list[SecurityReference] = []
            seen: set[tuple[str, str]] = set()
            for item in raw_securities:
                if not isinstance(item, dict):
                    raise ResearchIndexError("research.yaml 的 securities 条目无效")
                market = item.get("market")
                mic = item.get("mic")
                symbol = item.get("symbol")
                if not all(_is_clean_string(value) for value in (market, mic, symbol)):
                    raise ResearchIndexError("research.yaml 的证券字段无效")
                if market not in MARKETS or mic not in MARKETS[market][1]:
                    raise ResearchIndexError("research.yaml 的市场或 MIC 无效")
                if not SYMBOL_PATTERN.match(symbol):
                    raise ResearchIndexError("research.yaml 的股票代码无效")
                identity = (mic, symbol.upper())
                if identity in seen:
                    raise ResearchIndexError("research.yaml 内重复证券")
                seen.add(identity)
                securities.append(SecurityReference(market=market, mic=mic, symbol=symbol))
        except (ResearchIndexError, yaml.YAMLError, UnicodeError, ValueError, TypeError) as exc:
            diagnostics.append({"path": relative, "message": str(exc)})
            continue
        identities[entity_id] = CompanyIdentity(
            entity_id=entity_id,
            name=name,
            aliases=tuple(raw_aliases),
            securities=tuple(securities),
            source_path=relative,
        )
        consumed.append(relative)
    return identities, diagnostics, consumed


# --------------------------------------------------------------------------
# HTML resource inspection
# --------------------------------------------------------------------------


CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(?P<value>[^'\")]+)\1\s*\)", re.IGNORECASE)


def srcset_candidates(value: str) -> list[str]:
    return [url for url, _ in srcset_items(value)]


@dataclass(frozen=True)
class HtmlReference:
    kind: str  # "display" | "hyperlink"
    tag: str
    attribute: str
    value: str
    label: str = ""


@dataclass(frozen=True)
class _ScanResult:
    references: tuple[HtmlReference, ...]
    has_script: bool
    has_form: bool


class _ResourceScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[HtmlReference] = []
        self.has_script = False
        self.has_form = False
        self._style_depth = 0
        self._style_text: list[str] = []
        self._style_blocks: list[str] = []
        self._link_depth = 0
        self._link_text: list[str] = []
        self._link_label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "script":
            self.has_script = True
        if tag == "form":
            self.has_form = True
        if tag == "style":
            self._style_depth += 1
        if tag == "a" and "href" in attributes:
            # Anchor references are emitted when the element closes so the link
            # text can be attached; start tags only open the capture.
            self._link_depth += 1
            self._link_text = []
            self._link_label = attributes["href"]
        for attribute, value in attributes.items():
            if not value:
                continue
            if attribute == "srcset":
                # A srcset holds several candidates and each one is a dependency.
                for candidate in srcset_candidates(value):
                    self.references.append(HtmlReference("display", tag, "srcset", candidate))
            elif attribute in RESOURCE_ATTRIBUTES:
                self.references.append(HtmlReference("display", tag, attribute, value))
            elif attribute == "style":
                for match in CSS_URL_PATTERN.finditer(value):
                    self.references.append(HtmlReference("display", tag, "style-url", match.group("value").strip()))
            elif attribute in HYPERLINK_ATTRIBUTES and tag != "a":
                kind = "display" if tag == "link" and attributes.get("rel", "").lower() in STYLESHEET_REL else "hyperlink"
                self.references.append(HtmlReference(kind, tag, attribute, value))

    def close(self) -> None:
        super().close()
        while self._link_depth:
            self._link_depth -= 1
            label = " ".join(part.strip() for part in self._link_text if part.strip())
            self.references.append(HtmlReference("hyperlink", "a", "href", self._link_label, label))
            self._link_text = []
            self._link_label = ""

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "style":
            self._style_depth = max(0, self._style_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
            self._style_blocks.append("\n".join(self._style_text))
            self._style_text = []
        if tag == "a" and self._link_depth:
            self._link_depth -= 1
            label = " ".join(part.strip() for part in self._link_text if part.strip())
            self.references.append(HtmlReference("hyperlink", "a", "href", self._link_label, label))
            self._link_text = []
            self._link_label = ""

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self._style_text.append(data)
        if self._link_depth:
            self._link_text.append(data)

    @property
    def style_text(self) -> str:
        return "\n".join(self._style_blocks)


def _scan_html(text: str) -> _ScanResult:
    scanner = _ResourceScanner()
    try:
        scanner.feed(text)
        scanner.close()
    except Exception as exc:  # pragma: no cover - defensive, malformed markup
        raise ResearchReportError(f"HTML 解析失败：{exc}") from exc
    references = list(scanner.references)
    for match in CSS_URL_PATTERN.finditer(scanner.style_text):
        references.append(HtmlReference("display", "style", "url", match.group("value").strip()))
    return _ScanResult(tuple(references), scanner.has_script, scanner.has_form)


# --------------------------------------------------------------------------
# Display copy
#
# The reading frame is sandboxed and therefore has an opaque origin. A request
# issued by such a frame is cross site, so a SameSite=Lax session cookie is not
# attached to it and every sub resource would answer 401. The delivered copy is
# therefore self contained: approved images and styles are embedded as data
# URIs and the document needs no further request. The original HTML stays in
# the snapshot untouched and every transformation is recorded.
# --------------------------------------------------------------------------


@dataclass
class DisplayDependency:
    reference: str
    path: str
    kind: str
    media_type: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "path": self.path,
            "kind": self.kind,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }


@dataclass
class DisplayContext:
    """Everything the display copy builder needs to resolve a reference."""

    base_directory: str
    asset_paths: set[str]
    report_entries: dict[str, str]
    payloads: dict[str, bytes]
    media_types: dict[str, str]

    def deliverable(self, path: str) -> tuple[bytes, str] | None:
        if path not in self.asset_paths:
            return None
        payload = self.payloads.get(path)
        if payload is None:
            return None
        return payload, self.media_types.get(path) or media_type_for(path)


def _resolve_reference(value: str, directory: str) -> str | None:
    try:
        return local_reference(value, directory + '/__entry__.html')[0]
    except (DisplayError, ValueError):
        return None


def build_display_copy(
    html_text: str, context: DisplayContext,
) -> tuple[str, list[DisplayDependency], list[str]]:
    """Render only approved resources, including recursive CSS/SVG dependencies."""
    def read(path: str) -> bytes:
        delivered = context.deliverable(path)
        if delivered is None:
            raise DisplayError(f'必需展示资源不可读取：{path}')
        return delivered[0]

    renderer = StaticDisplay(read, context.asset_paths, context.report_entries,
                             '__RESEARCH_SNAPSHOT_ID__')
    try:
        payload = renderer.render(html_text, context.base_directory + '/__entry__.html')
    except (DisplayError, UnicodeError, ValueError) as exc:
        return '', [], [str(exc)]
    dependencies = [DisplayDependency(
        reference=path, path=path, kind='embedded',
        media_type=context.media_types.get(path) or media_type_for(path),
        sha256=_sha256_bytes(context.payloads[path]),
    ) for path in sorted(renderer.used)]
    return payload.decode('utf-8'), dependencies, []


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------


def _safe_relative_path(value: str) -> str | None:
    """Normalise a registered relative path. Returns None when unusable."""
    normalized = _normalize_relative_key(value)
    if normalized is None:
        return None
    parts: list[str] = []
    for part in normalized.split("/"):
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _normalize_relative_key(value: str) -> str | None:
    """Normalise a path but keep leading ``..`` so report relative keys stay comparable."""
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return None
    if value.startswith("file:") or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                parts.append("..")
                continue
            if parts[-1] == "..":
                parts.append("..")
                continue
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _resolve_source_file(source_root: Path, relative: str) -> Path:
    """Resolve a registered path inside the source root, rejecting symlinks."""
    normalized = _safe_relative_path(relative)
    if normalized is None:
        raise ResearchReportError(f"非法登记路径：{relative}")
    candidate = source_root / normalized
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise ResearchReportError(f"登记文件不可读取：{relative}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ResearchReportError(f"登记文件不是普通文件：{relative}")
    resolved = candidate.resolve()
    root = source_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ResearchReportError(f"登记路径越界：{relative}")
    return candidate


def _relative_to(source_root: Path, path: Path) -> str:
    return path.relative_to(source_root).as_posix()


# --------------------------------------------------------------------------
# Snapshot model
# --------------------------------------------------------------------------


@dataclass
class Diagnostic:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class SnapshotAsset:
    path: str
    sha256: str
    size: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
        }


@dataclass
class SnapshotReport:
    report_id: str
    entity_id: str
    title: str
    report_type: str
    status: str
    content_status: str
    document_updated_at: str | None
    financial_as_of: str | None
    market_as_of: str | None
    entry_path: str
    content_sha256: str
    registered_content_sha256: str | None
    display_sha256: str = ""
    display_size: int = 0
    display_dependencies: list[dict[str, str]] = field(default_factory=list)
    assets: list[SnapshotAsset] = field(default_factory=list)
    unpublished_links: list[dict[str, str]] = field(default_factory=list)
    external_links: list[dict[str, str]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    has_script: bool = False
    has_form: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "entity_id": self.entity_id,
            "title": self.title,
            "report_type": self.report_type,
            "status": self.status,
            "content_status": self.content_status,
            "document_updated_at": self.document_updated_at,
            "financial_as_of": self.financial_as_of,
            "market_as_of": self.market_as_of,
            "entry_path": self.entry_path,
            "content_sha256": self.content_sha256,
            "registered_content_sha256": self.registered_content_sha256,
            "display_sha256": self.display_sha256,
            "display_size": self.display_size,
            "display_dependencies": list(self.display_dependencies),
            "assets": [asset.to_dict() for asset in self.assets],
            "unpublished_links": list(self.unpublished_links),
            "external_links": list(self.external_links),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "has_script": self.has_script,
            "has_form": self.has_form,
        }


@dataclass
class SnapshotCompany:
    entity_id: str
    name: str
    aliases: list[str]
    securities: list[SecurityReference]
    report_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "securities": [item.to_dict() for item in self.securities],
            "report_count": len(self.report_ids),
            "report_ids": list(self.report_ids),
        }


@dataclass
class SnapshotManifest:
    snapshot_id: str
    generated_at: str
    source: dict[str, Any]
    totals: dict[str, int]
    companies: list[SnapshotCompany]
    reports: dict[str, SnapshotReport]
    files: dict[str, dict[str, Any]]
    originals: dict[str, dict[str, Any]]
    redirects: list[dict[str, str]]
    excluded: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "source": dict(self.source),
            "totals": dict(self.totals),
            "companies": [item.to_dict() for item in self.companies],
            "reports": {key: value.to_dict() for key, value in self.reports.items()},
            "files": dict(self.files),
            "originals": dict(self.originals),
            "redirects": list(self.redirects),
            "excluded": list(self.excluded),
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "SnapshotManifest":
        reports: dict[str, SnapshotReport] = {}
        for report_id, raw in document.get("reports", {}).items():
            reports[report_id] = SnapshotReport(
                report_id=raw["report_id"],
                entity_id=raw["entity_id"],
                title=raw["title"],
                report_type=raw["report_type"],
                status=raw["status"],
                content_status=raw["content_status"],
                document_updated_at=raw.get("document_updated_at"),
                financial_as_of=raw.get("financial_as_of"),
                market_as_of=raw.get("market_as_of"),
                entry_path=raw["entry_path"],
                content_sha256=raw.get("content_sha256", ""),
                registered_content_sha256=raw.get("registered_content_sha256"),
                display_sha256=raw.get("display_sha256", ""),
                display_size=int(raw.get("display_size", 0)),
                display_dependencies=list(raw.get("display_dependencies", [])),
                assets=[SnapshotAsset(**item) for item in raw.get("assets", [])],
                unpublished_links=list(raw.get("unpublished_links", [])),
                external_links=list(raw.get("external_links", [])),
                diagnostics=[Diagnostic(**item) for item in raw.get("diagnostics", [])],
                has_script=bool(raw.get("has_script")),
                has_form=bool(raw.get("has_form")),
            )
        companies = [
            SnapshotCompany(
                entity_id=item["entity_id"],
                name=item["name"],
                aliases=list(item.get("aliases", [])),
                securities=[SecurityReference(**security) for security in item.get("securities", [])],
                report_ids=list(item.get("report_ids", [])),
            )
            for item in document.get("companies", [])
        ]
        return cls(
            snapshot_id=document["snapshot_id"],
            generated_at=document.get("generated_at", ""),
            source=dict(document.get("source", {})),
            totals=dict(document.get("totals", {})),
            companies=companies,
            reports=reports,
            files=dict(document.get("files", {})),
            originals=dict(document.get("originals", {})),
            redirects=list(document.get("redirects", [])),
            excluded=list(document.get("excluded", [])),
        )


# --------------------------------------------------------------------------
# Snapshot generation
# --------------------------------------------------------------------------


@dataclass
class _SourceStamp:
    relative: str
    size: int
    mtime_ns: int
    sha256: str


class _SourceTracker:
    """Records the exact bytes read from the source so mixing versions is caught."""

    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root
        self.stamps: dict[str, _SourceStamp] = {}
        self.payloads: dict[str, bytes] = {}

    def read(self, relative: str, limit: int, keep: bool = False) -> bytes:
        path = _resolve_source_file(self.source_root, relative)
        info = path.lstat()
        if info.st_size > limit:
            raise ResearchReportError(f"登记文件超过读取大小限制：{relative}")
        payload = path.read_bytes()
        if len(payload) > limit:
            raise ResearchReportError(f"登记文件超过读取大小限制：{relative}")
        self.stamps[relative] = _SourceStamp(
            relative=relative,
            size=len(payload),
            mtime_ns=path.stat().st_mtime_ns,
            sha256=_sha256_bytes(payload),
        )
        if keep:
            self.payloads[relative] = payload
        return payload

    def verify_unchanged(self) -> None:
        for relative, stamp in self.stamps.items():
            path = self.source_root / relative
            try:
                info = path.lstat()
            except OSError as exc:
                raise ResearchReportError(f"读取期间来源文件消失：{relative}") from exc
            if info.st_size != stamp.size or info.st_mtime_ns != stamp.mtime_ns:
                raise ResearchReportError(f"读取期间来源文件发生变化：{relative}")
            if _sha256_file(path) != stamp.sha256:
                raise ResearchReportError(f"读取期间来源文件发生变化：{relative}")


def _git_provenance(source_root: Path) -> dict[str, Any]:
    """Best effort commit / dirty marker. HEAD never impersonates the working tree."""
    provenance: dict[str, Any] = {"name": source_root.name, "commit": None, "dirty": None}
    try:
        top = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=15, check=True,
        ).stdout.decode().strip()
    except (OSError, subprocess.SubprocessError):
        return provenance
    if Path(top).resolve() != source_root.resolve():
        return provenance
    try:
        commit = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(source_root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, timeout=15, check=True,
        ).stdout.decode().strip()
        status = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(source_root), "status", "--porcelain"],
            capture_output=True, timeout=30, check=True,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return provenance
    changed = [line for line in status.splitlines() if line.strip()]
    provenance["commit"] = commit
    provenance["dirty"] = bool(changed)
    provenance["dirty_file_count"] = len(changed)
    return provenance


def _select_candidates(index: SourceIndex) -> tuple[list[ReportEntry], list[dict[str, str]]]:
    candidates: list[ReportEntry] = []
    excluded: list[dict[str, str]] = []
    for entry in index.entries:
        if entry.report_type not in ALLOWED_REPORT_TYPES:
            excluded.append({"report_id": entry.report_id, "reason": f"报告类型 {entry.report_type} 不进入公司研究列表"})
            continue
        if entry.status not in ALLOWED_STATUSES:
            excluded.append({"report_id": entry.report_id, "reason": f"交付状态 {entry.status} 不进入公司研究列表"})
            continue
        if entry.entity_id is None or not COMPANY_ENTITY_PATTERN.match(entry.entity_id):
            excluded.append({"report_id": entry.report_id, "reason": "未登记公司主体"})
            continue
        candidates.append(entry)
    return candidates, excluded


def _report_sort_key(entry: SnapshotReport) -> tuple[int, int, int, str]:
    is_main = 0 if entry.report_type == "company_research" else 1
    date_value = 0
    if entry.document_updated_at:
        date_value = int(entry.document_updated_at.replace("-", ""))
    unknown = 0 if entry.document_updated_at else 1
    return (is_main, unknown, -date_value, entry.report_id)


@dataclass
class _Candidate:
    """One candidate report after the entry local checks passed."""

    entry: ReportEntry
    scan: _ScanResult
    diagnostics: list[Diagnostic]
    assets: list[SnapshotAsset]
    asset_paths: dict[str, str]
    content_sha256: str
    html_text: str = ""
    payloads: dict[str, bytes] = field(default_factory=dict)


def _prepare_candidate(
    entry: ReportEntry, tracker: _SourceTracker
) -> tuple[_Candidate | None, str | None]:
    """Read and scan one candidate. Only entry local problems are decided here."""
    diagnostics: list[Diagnostic] = []
    if PurePosixPath(entry.entry_path).suffix.lower() not in {".html", ".htm"}:
        return None, f"登记入口不是 HTML 文件：{entry.entry_path}"
    try:
        html_bytes = tracker.read(entry.entry_path, MAX_HTML_BYTES, keep=True)
    except ResearchReportError as exc:
        return None, str(exc)
    try:
        html_text = html_bytes.decode("utf-8")
    except UnicodeError:
        return None, f"报告正文不是有效 UTF-8：{entry.entry_path}"
    try:
        scan = _scan_html(html_text)
    except (DisplayError, ValueError) as exc:
        return None, f'报告资源格式无效：{exc}'
    content_sha256 = tracker.stamps[entry.entry_path].sha256

    # A registered hash binds the published body to the reviewed version. A
    # mismatch means the body changed after registration and must not be shown
    # with the registered dates and verification labels.
    if entry.content_sha256 and entry.content_sha256 != content_sha256:
        return None, (
            f"正文实际哈希与索引登记哈希不一致，已排除该报告：{entry.entry_path}"
        )

    if scan.has_script:
        diagnostics.append(Diagnostic("script_present", "info", "报告包含脚本；静态阅读模式不执行脚本。"))
    if scan.has_form:
        diagnostics.append(Diagnostic("form_present", "warning", "报告包含表单；阅读模式已禁止提交。"))

    # Approved assets are the only non-entry files that may be published.
    assets: list[SnapshotAsset] = []
    asset_paths: dict[str, str] = {}
    for raw_asset in entry.assets:
        normalized = _safe_relative_path(raw_asset)
        if normalized is None:
            diagnostics.append(Diagnostic("asset_path_invalid", "warning", f"忽略非法资源路径：{raw_asset}"))
            continue
        if normalized != raw_asset:
            diagnostics.append(Diagnostic("asset_path_normalized", "warning", f"资源路径已规范化：{raw_asset}"))
        try:
            payload = tracker.read(normalized, MAX_ASSET_BYTES, keep=True)
        except ResearchReportError as exc:
            diagnostics.append(Diagnostic("asset_unreadable", "warning", str(exc)))
            continue
        asset_paths[normalized] = _sha256_bytes(payload)
        assets.append(
            SnapshotAsset(
                path=normalized,
                sha256=asset_paths[normalized],
                size=len(payload),
                media_type=media_type_for(normalized),
            )
        )

    candidate = _Candidate(
        entry=entry,
        scan=scan,
        diagnostics=diagnostics,
        assets=assets,
        asset_paths=asset_paths,
        content_sha256=content_sha256,
        html_text=html_text,
        payloads={path: tracker.payloads[path] for path in asset_paths if path in tracker.payloads},
    )
    return candidate, None


def _resolve_candidate(
    candidate: _Candidate,
    source_root: Path,
    approved_entries: set[str],
    report_entries: dict[str, str],
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[Diagnostic],
    str | None,
    str | None,
    list[DisplayDependency],
]:
    """Resolve the references of one candidate against the accepted report set."""
    entry = candidate.entry
    diagnostics: list[Diagnostic] = []
    unpublished: list[dict[str, str]] = []
    external_links: list[dict[str, str]] = []
    blocking: str | None = None

    base_directory = entry.directory
    unpublished_lookup: dict[str, str] = {}
    for raw_key, reason in entry.unpublished_links.items():
        key = _normalize_relative_key(unquote(raw_key))
        if key is None:
            continue
        unpublished_lookup[key] = reason
        resolved_key = _safe_relative_path(
            f"{base_directory}/{key}" if base_directory not in {".", ""} else key
        )
        if resolved_key is not None:
            unpublished_lookup.setdefault(resolved_key, reason)
    unpublished_seen: set[str] = set()
    not_published_seen: set[str] = set()

    for reference in candidate.scan.references:
        raw_value = reference.value.strip()
        if not raw_value:
            continue
        if raw_value.startswith("#"):
            continue
        lowered = raw_value.lower()
        if lowered.startswith("data:"):
            continue
        if lowered.startswith("file:") or re.match(r"^[A-Za-z]:[\\/]", raw_value):
            blocking = blocking or f"报告引用本机绝对路径：{raw_value}"
            diagnostics.append(Diagnostic("local_path_reference", "error", f"报告引用本机绝对路径：{raw_value}"))
            continue
        if raw_value.startswith("/"):
            blocking = blocking or f"报告引用根路径绝对地址：{raw_value}"
            diagnostics.append(Diagnostic("absolute_path_reference", "error", f"报告引用根路径绝对地址：{raw_value}"))
            continue
        if lowered.startswith(("http://", "https://", "//")):
            if reference.kind == "display":
                blocking = blocking or f"报告依赖外部展示资源：{raw_value}"
                diagnostics.append(
                    Diagnostic("external_display_resource", "error", f"报告依赖外部展示资源，已阻止发布：{raw_value}")
                )
            else:
                link = {"url": raw_value, "label": reference.label}
                if link not in external_links:
                    external_links.append(link)
            continue
        if ":" in raw_value.split("/")[0]:
            blocking = blocking or f"报告引用不支持的协议：{raw_value}"
            diagnostics.append(Diagnostic("unsupported_scheme", "error", f"报告引用不支持的协议：{raw_value}"))
            continue

        resolved = _resolve_reference(raw_value, base_directory)
        if resolved is None:
            diagnostics.append(Diagnostic("reference_out_of_tree", "warning", f"报告引用越界路径：{raw_value}"))
            continue
        matched_reason = unpublished_lookup.get(resolved) or unpublished_lookup.get(
            _normalize_relative_key(unquote(raw_value)) or ""
        )
        if reference.kind == "display":
            # A display dependency must be part of the delivered set. An
            # unpublished or unregistered file cannot stand in for it.
            if resolved in candidate.asset_paths:
                continue
            if matched_reason is not None:
                blocking = blocking or f"必需展示资源未发布：{raw_value}"
                diagnostics.append(
                    Diagnostic("display_resource_unpublished", "error", f"必需展示资源未发布，已阻止发布：{raw_value}")
                )
                continue
            blocking = blocking or f"必需展示资源未登记：{raw_value}"
            diagnostics.append(
                Diagnostic("display_resource_missing", "error", f"必需展示资源未登记，已阻止发布：{raw_value}")
            )
            continue
        if matched_reason is not None:
            if raw_value not in unpublished_seen:
                unpublished_seen.add(raw_value)
                unpublished.append({"path": resolved, "reference": raw_value, "reason": matched_reason})
            continue
        if resolved in candidate.asset_paths or resolved in approved_entries:
            continue
        try:
            _resolve_source_file(source_root, resolved)
        except ResearchReportError:
            if raw_value not in not_published_seen:
                not_published_seen.add(raw_value)
                diagnostics.append(Diagnostic("link_target_missing", "warning", f"正文链接目标不存在：{raw_value}"))
            continue
        if raw_value not in not_published_seen:
            not_published_seen.add(raw_value)
            diagnostics.append(
                Diagnostic("link_not_published", "warning", f"正文链接未纳入发布范围：{raw_value}")
            )

    # Recursive CSS and SVG dependencies are validated by the same renderer
    # that builds the display copy, so validation and delivery cannot diverge.
    context = DisplayContext(
        base_directory=base_directory,
        asset_paths=set(candidate.asset_paths),
        report_entries=report_entries,
        payloads=candidate.payloads,
        media_types={asset.path: asset.media_type for asset in candidate.assets},
    )
    display_html, dependencies, missing = build_display_copy(candidate.html_text, context)
    if missing:
        blocking = blocking or f"必需展示资源无法交付：{missing[0]}"
        diagnostics.append(
            Diagnostic("display_resource_undeliverable", "error", f"必需展示资源无法交付，已阻止发布：{missing[0]}")
        )
    return unpublished, external_links, diagnostics, blocking, display_html, dependencies


def _make_report(
    candidate: _Candidate,
    unpublished: list[dict[str, str]],
    external_links: list[dict[str, str]],
    reference_diagnostics: list[Diagnostic],
    identities: dict[str, CompanyIdentity],
    display_html: str,
    dependencies: list[DisplayDependency],
) -> SnapshotReport:
    entry = candidate.entry
    diagnostics = list(candidate.diagnostics) + list(reference_diagnostics)
    if unpublished:
        diagnostics.append(
            Diagnostic("unpublished_links", "info", f"报告包含 {len(unpublished)} 项未发布资料，正文保留原链接但不提供下载。")
        )
    if external_links:
        diagnostics.append(
            Diagnostic("external_links", "info", f"报告包含 {len(external_links)} 个外部来源链接，由外层受控入口打开。")
        )
    if entry.content_sha256 is None:
        diagnostics.append(
            Diagnostic("content_hash_absent", "info", "索引未登记正文哈希；快照记录实际哈希，不回填源索引。")
        )
    if entry.entity_id not in identities:
        diagnostics.append(Diagnostic("identity_missing", "info", "未找到主体身份入口；展示名称回退为报告标题。"))
    display_bytes = display_html.encode("utf-8")
    return SnapshotReport(
        report_id=entry.report_id,
        entity_id=entry.entity_id,
        title=entry.title,
        report_type=entry.report_type,
        status=entry.status,
        content_status=entry.content_status,
        document_updated_at=entry.document_updated_at,
        financial_as_of=entry.financial_as_of,
        market_as_of=entry.market_as_of,
        entry_path=entry.entry_path,
        content_sha256=candidate.content_sha256,
        registered_content_sha256=entry.content_sha256,
        display_sha256=_sha256_bytes(display_bytes),
        display_size=len(display_bytes),
        display_dependencies=[item.to_dict() for item in dependencies],
        assets=candidate.assets,
        unpublished_links=unpublished,
        external_links=external_links,
        diagnostics=diagnostics,
        has_script=candidate.scan.has_script,
        has_form=candidate.scan.has_form,
    )


def _build_companies(
    reports: list[SnapshotReport],
    identities: dict[str, CompanyIdentity],
) -> list[SnapshotCompany]:
    grouped: dict[str, list[SnapshotReport]] = {}
    for report in reports:
        grouped.setdefault(report.entity_id, []).append(report)
    companies: list[SnapshotCompany] = []
    for entity_id, members in grouped.items():
        ordered = sorted(members, key=_report_sort_key)
        identity = identities.get(entity_id)
        name = identity.name if identity and identity.name else ordered[0].title
        aliases = list(identity.aliases) if identity else []
        securities = list(identity.securities) if identity else []
        companies.append(
            SnapshotCompany(
                entity_id=entity_id,
                name=name,
                aliases=aliases,
                securities=securities,
                report_ids=[report.report_id for report in ordered],
            )
        )
    companies.sort(key=lambda item: (item.name, item.entity_id))
    return companies


def build_snapshot(source_root: Path, output_root: Path) -> dict[str, Any]:
    """Generate an immutable snapshot from the research library.

    The current snapshot pointer is only switched when the whole snapshot is
    complete and verified. Any failure leaves the previous pointer untouched.
    """
    source_root = Path(source_root).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    if not source_root.is_dir():
        raise ResearchReportError("研究库目录不存在")
    output_root.mkdir(parents=True, exist_ok=True)
    snapshots_root = output_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    _relax_directory(output_root)
    _relax_directory(snapshots_root)

    index = load_index(source_root)
    tracker = _SourceTracker(source_root)
    tracker.read(index.index_path, MAX_INDEX_BYTES)
    if tracker.stamps[index.index_path].sha256 != index.index_sha256:
        raise ResearchIndexError("研究索引在读取期间发生变化")
    candidates, excluded = _select_candidates(index)
    identities, identity_diagnostics, identity_files = load_identities(
        source_root, {entry.entity_id for entry in candidates if entry.entity_id}, tracker
    )
    for item in identity_diagnostics:
        excluded.append({"report_id": "", "reason": f"主体身份入口无效：{item['path']}：{item['message']}"})

    prepared: list[_Candidate] = []
    for entry in candidates:
        candidate, reason = _prepare_candidate(entry, tracker)
        if candidate is None:
            excluded.append({"report_id": entry.report_id, "reason": reason or "报告不可用"})
            continue
        prepared.append(candidate)

    # References are resolved against the reports that survive the checks. A
    # report that is blocked may itself be linked from another report, so the
    # accepted set is refined until it stops changing.
    blocked: set[str] = set()
    resolutions: dict[str, tuple] = {}
    for _ in range(len(prepared) + 1):
        approved_entries = {
            candidate.entry.entry_path
            for candidate in prepared
            if candidate.entry.report_id not in blocked
        }
        report_entries = {
            candidate.entry.entry_path: candidate.entry.report_id
            for candidate in prepared
            if candidate.entry.report_id not in blocked
        }
        for redirect in index.redirects:
            if redirect['report_id'] in report_entries.values():
                alias = _safe_relative_path(redirect['from'])
                if alias:
                    report_entries.setdefault(alias, redirect['report_id'])
        resolutions = {}
        newly_blocked: set[str] = set()
        for candidate in prepared:
            unresolved, external_links, diagnostics, blocking, display_html, dependencies = _resolve_candidate(
                candidate, source_root, approved_entries, report_entries
            )
            resolutions[candidate.entry.report_id] = (
                unresolved, external_links, diagnostics, display_html, dependencies
            )
            if blocking is not None:
                newly_blocked.add(candidate.entry.report_id)
        if newly_blocked == blocked:
            blocked = newly_blocked
            break
        blocked = newly_blocked

    accepted: list[SnapshotReport] = []
    display_copies: dict[str, bytes] = {}
    for candidate in prepared:
        report_id = candidate.entry.report_id
        unresolved, external_links, diagnostics, display_html, dependencies = resolutions.get(
            report_id, ([], [], [], "", [])
        )
        if report_id in blocked:
            reason = next(
                (item.message for item in diagnostics if item.severity == "error"),
                "报告依赖未通过检查的资源",
            )
            excluded.append({"report_id": report_id, "reason": reason})
            continue
        report = _make_report(
            candidate, unresolved, external_links, diagnostics, identities, display_html, dependencies
        )
        accepted.append(report)
        display_copies[candidate.entry.entry_path] = display_html.encode("utf-8")

    if candidates and not accepted:
        raise ResearchReportError("候选报告全部不可用，保留当前快照指针不变")

    companies = _build_companies(accepted, identities)

    # Hash everything that will be copied so the snapshot id is content addressed.
    file_records: dict[str, dict[str, Any]] = {}
    original_records: dict[str, dict[str, Any]] = {}
    copy_plan: list[tuple[str, str]] = []  # (source relative, served relative)
    for report in accepted:
        copy_plan.append((report.entry_path, report.entry_path))
        for asset in report.assets:
            copy_plan.append((asset.path, asset.path))
    for source_relative, served_relative in copy_plan:
        if source_relative in display_copies:
            payload = display_copies[source_relative]
            file_records[served_relative] = {
                "sha256": _sha256_bytes(payload),
                "size": len(payload),
                "media_type": media_type_for(served_relative),
            }
            original = tracker.payloads.get(source_relative)
            if original is not None:
                original_records[served_relative] = {
                    "sha256": _sha256_bytes(original),
                    "size": len(original),
                }
            continue
        stamp = tracker.stamps.get(source_relative)
        if stamp is None:
            raise ResearchReportError(f"快照缺少已读取文件：{source_relative}")
        file_records[served_relative] = {
            "sha256": stamp.sha256,
            "size": stamp.size,
            "media_type": media_type_for(served_relative),
        }

    index_stamp = tracker.stamps.get(index.index_path)
    if index_stamp is None:
        raise ResearchReportError("研究索引未被读取")
    if index_stamp.sha256 != index.index_sha256:
        raise ResearchIndexError("研究索引在读取期间发生变化")

    provenance = _git_provenance(source_root)
    digest = hashlib.sha256()
    digest.update(f"schema={SNAPSHOT_SCHEMA_VERSION};display={DISPLAY_REVISION}\n".encode())
    digest.update(f"index={index.index_sha256}\n".encode())
    digest.update(f"commit={provenance.get('commit')}\n".encode())
    digest.update(f"dirty={provenance.get('dirty')}\n".encode())
    for relative in sorted(file_records):
        digest.update(f"{relative}\0{file_records[relative]['sha256']}\n".encode())
    for relative in sorted(original_records):
        digest.update(f"original\0{relative}\0{original_records[relative]['sha256']}\n".encode())
    # The consumed identity files are part of the snapshot: a company name or
    # security change must produce a new snapshot instead of reusing the old one.
    for relative in sorted(identity_files):
        stamp = tracker.stamps.get(relative)
        if stamp is None:
            continue
        digest.update(f"identity\0{relative}\0{stamp.sha256}\n".encode())
    snapshot_id = "s" + digest.hexdigest()[:32]

    # The digest binds renderer inputs and template bytes. Bind links to that
    # exact snapshot, then record hashes of the actual delivered copies.
    for report in accepted:
        payload = display_copies[report.entry_path].replace(
            b'__RESEARCH_SNAPSHOT_ID__', snapshot_id.encode('ascii'))
        display_copies[report.entry_path] = payload
        report.display_sha256 = _sha256_bytes(payload)
        report.display_size = len(payload)
        file_records[report.entry_path]['sha256'] = report.display_sha256
        file_records[report.entry_path]['size'] = report.display_size

    redirects = _resolve_redirects(index, {report.report_id for report in accepted})
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        generated_at=_utc_now(),
        source={
            "name": provenance.get("name"),
            "commit": provenance.get("commit"),
            "dirty": provenance.get("dirty"),
            "dirty_file_count": provenance.get("dirty_file_count"),
            "index_path": index.index_path,
            "index_sha256": index.index_sha256,
            "identity_files": sorted(identity_files),
            "display_revision": DISPLAY_REVISION,
        },
        totals={"companies": len(companies), "reports": len(accepted)},
        companies=companies,
        reports={report.report_id: report for report in accepted},
        files=file_records,
        originals=original_records,
        redirects=redirects,
        excluded=excluded,
    )

    target_root = snapshots_root / snapshot_id
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(snapshots_root)))
    try:
        content_root = staging / "content"
        content_root.mkdir(parents=True, exist_ok=True)
        for source_relative, served_relative in copy_plan:
            destination = content_root / served_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = display_copies.get(source_relative)
            if payload is None:
                payload = (source_root / source_relative).read_bytes()
            destination.write_bytes(payload)
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ResearchReportError(f"复制结果不是普通文件：{served_relative}")
            if _sha256_file(destination) != file_records[served_relative]["sha256"]:
                raise ResearchReportError(f"复制后哈希不一致：{served_relative}")
            os.chmod(destination, SNAPSHOT_FILE_MODE)
        originals_root = staging / "originals"
        for served_relative, record in original_records.items():
            destination = originals_root / served_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_relative = served_relative
            payload = tracker.payloads.get(source_relative)
            if payload is None:
                continue
            destination.write_bytes(payload)
            os.chmod(destination, SNAPSHOT_FILE_MODE)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(manifest_path, SNAPSHOT_FILE_MODE)
        _relax_directory_tree(staging)
        tracker.verify_unchanged()
        if target_root.exists():
            # Reuse the actual manifest, including its original generation date.
            existing = _read_manifest(output_root, snapshot_id)
            _relax_directory_tree(target_root)
            manifest = existing
            shutil.rmtree(staging)
        else:
            os.replace(staging, target_root)
            staging = None  # type: ignore[assignment]
            _relax_directory_tree(target_root)
    finally:
        if staging is not None and Path(staging).exists():
            shutil.rmtree(staging, ignore_errors=True)

    pointer = output_root / "current.json"
    pointer_payload = json.dumps({"snapshot_id": snapshot_id, "generated_at": manifest.generated_at}, ensure_ascii=False)
    temporary_pointer = output_root / f".current-{os.getpid()}.json"
    temporary_pointer.write_text(pointer_payload, encoding="utf-8")
    os.chmod(temporary_pointer, SNAPSHOT_FILE_MODE)
    os.replace(temporary_pointer, pointer)
    return manifest.to_dict()


def _relax_directory(path: Path) -> None:
    """Make a directory traversable for the web service user."""
    try:
        os.chmod(path, SNAPSHOT_DIR_MODE)
    except OSError as exc:
        raise ResearchReportError("无法设置快照目录权限") from exc


def _relax_directory_tree(root: Path) -> None:
    for directory, _, _ in os.walk(root):
        _relax_directory(Path(directory))


def _resolve_redirects(index: SourceIndex, accepted: set[str]) -> list[dict[str, str]]:
    """Keep the registered compatibility paths that point at a shown report."""
    resolved: list[dict[str, str]] = []
    for item in index.redirects:
        if item["report_id"] not in accepted:
            continue
        source = _safe_relative_path(item["from"])
        if source is None:
            continue
        resolved.append({"from": source, "report_id": item["report_id"]})
    resolved.sort(key=lambda item: item["from"])
    return resolved


# --------------------------------------------------------------------------
# Snapshot reading (web side)
# --------------------------------------------------------------------------

SNAPSHOT_ID_PATTERN = re.compile(r"^s[0-9a-f]{8,64}$")


@dataclass
class LoadedSnapshot:
    root: Path
    snapshot_id: str
    manifest: SnapshotManifest


def _read_manifest(root: Path, snapshot_id: str) -> SnapshotManifest:
    manifest_path = root / "snapshots" / snapshot_id / "manifest.json"
    try:
        exists = manifest_path.is_file()
    except OSError as exc:
        raise ResearchSnapshotUnavailable(f"快照清单不可读取（{exc}）") from exc
    if not exists:
        raise ResearchSnapshotError("快照不存在")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        raise ResearchSnapshotUnavailable(f"快照清单不可读取（{exc}）") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchSnapshotError(f"快照清单损坏（{exc}）") from exc
    if not isinstance(document, dict) or document.get('schema_version') != SNAPSHOT_SCHEMA_VERSION:
        raise ResearchSnapshotError('快照格式不兼容，请重新生成快照')
    try:
        manifest = SnapshotManifest.from_dict(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchSnapshotError(f"快照清单字段错误（{exc}）") from exc
    if manifest.snapshot_id != snapshot_id:
        raise ResearchSnapshotError("快照标识与清单不一致")
    return manifest


def load_snapshot(root: Path, snapshot_id: str | None = None) -> LoadedSnapshot:
    """Load the current snapshot. Raises when the feature is not usable."""
    root = Path(root).expanduser()
    if snapshot_id is not None:
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise ResearchSnapshotError('快照标识无效')
        return LoadedSnapshot(root=root, snapshot_id=snapshot_id, manifest=_read_manifest(root, snapshot_id))
    pointer = root / "current.json"
    try:
        exists = pointer.is_file()
    except OSError as exc:
        raise ResearchSnapshotUnavailable(f"研究快照不可用：快照指针不可读取（{exc}）") from exc
    if not exists:
        raise ResearchSnapshotError("研究快照不可用：未找到当前快照指针")
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except PermissionError as exc:
        raise ResearchSnapshotUnavailable(f"研究快照不可用：快照指针不可读取（{exc}）") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchSnapshotError(f"研究快照不可用：快照指针损坏（{exc}）") from exc
    snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_PATTERN.match(snapshot_id):
        raise ResearchSnapshotError("研究快照不可用：快照标识无效")
    return LoadedSnapshot(root=root, snapshot_id=snapshot_id, manifest=_read_manifest(root, snapshot_id))


def snapshot_content_bytes(root: Path, snapshot_id: str, registered_path: str) -> tuple[bytes, str]:
    """Return the bytes of a whitelisted file inside one snapshot.

    Any retained snapshot can be served, so a report URL keeps pointing at the
    content it was opened with even after a newer snapshot becomes current.
    """
    root = Path(root).expanduser()
    if not SNAPSHOT_ID_PATTERN.match(snapshot_id or ""):
        raise ResearchSnapshotError("快照标识无效")
    manifest = _read_manifest(root, snapshot_id)
    normalized = _safe_relative_path(registered_path)
    if normalized is None:
        raise ResearchSnapshotError("资源路径无效")
    record = manifest.files.get(normalized)
    if record is None:
        # A registered compatibility path resolves to the report it points at.
        target_report = next(
            (item["report_id"] for item in manifest.redirects if item["from"] == normalized), None
        )
        if target_report is None or target_report not in manifest.reports:
            raise ResearchSnapshotError("资源不在快照白名单内")
        normalized = manifest.reports[target_report].entry_path
        record = manifest.files.get(normalized)
        if record is None:
            raise ResearchSnapshotError("资源不在快照白名单内")
    content_root = (root / "snapshots" / snapshot_id / "content").resolve()
    target = content_root / normalized
    try:
        info = target.lstat()
    except PermissionError as exc:
        raise ResearchSnapshotUnavailable(f"资源文件不可读取（{exc}）") from exc
    except OSError as exc:
        raise ResearchSnapshotError("资源文件不可读取") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ResearchSnapshotError("资源文件不是普通文件")
    resolved = target.resolve()
    if resolved != content_root and content_root not in resolved.parents:
        raise ResearchSnapshotError("资源路径越界")
    try:
        payload = target.read_bytes()
    except PermissionError as exc:
        raise ResearchSnapshotUnavailable(f"资源文件不可读取（{exc}）") from exc
    if _sha256_bytes(payload) != record.get("sha256"):
        raise ResearchSnapshotError("资源文件与快照哈希不一致")
    return payload, str(record.get("media_type") or media_type_for(normalized))


def report_content_url(snapshot_id: str, registered_path: str) -> str:
    """Build the backend owned content URL, encoding every path segment."""
    encoded = "/".join(quote(part, safe="") for part in registered_path.split("/"))
    return f"/api/v1/research/content/{quote(snapshot_id, safe='')}/{encoded}"


def report_summary(report: SnapshotReport) -> dict[str, Any]:
    """Metadata that is safe to expose for both the list and the detail view."""
    return {
        "report_id": report.report_id,
        "entity_id": report.entity_id,
        "title": report.title,
        "report_type": report.report_type,
        "status": report.status,
        "content_status": report.content_status,
        "document_updated_at": report.document_updated_at,
        "financial_as_of": report.financial_as_of,
        "market_as_of": report.market_as_of,
    }


def company_payload(snapshot: LoadedSnapshot, company: SnapshotCompany) -> dict[str, Any]:
    reports = [snapshot.manifest.reports[report_id] for report_id in company.report_ids]
    return {
        "entity_id": company.entity_id,
        "name": company.name,
        "aliases": list(company.aliases),
        "securities": [item.to_dict() for item in company.securities],
        "report_count": len(reports),
        "reports": [report_summary(report) for report in reports],
    }


def company_list_payload(snapshot: LoadedSnapshot) -> dict[str, Any]:
    manifest = snapshot.manifest
    return {
        "snapshot_id": manifest.snapshot_id,
        "generated_at": manifest.generated_at,
        "totals": {
            "companies": len(manifest.companies),
            "reports": len(manifest.reports),
        },
        "companies": [company_payload(snapshot, company) for company in manifest.companies],
        "diagnostics": {
            "excluded_count": len(manifest.excluded),
            "excluded": list(manifest.excluded),
            "source": {
                "name": manifest.source.get("name"),
                "commit": manifest.source.get("commit"),
                "dirty": manifest.source.get("dirty"),
            },
        },
    }


def report_detail_payload(snapshot: LoadedSnapshot, report_id: str) -> dict[str, Any] | None:
    manifest = snapshot.manifest
    report = manifest.reports.get(report_id)
    if report is None:
        return None
    company = next((item for item in manifest.companies if item.entity_id == report.entity_id), None)
    siblings = [
        report_summary(manifest.reports[item])
        for item in (company.report_ids if company else [])
        if item != report_id
    ]
    return {
        "snapshot_id": manifest.snapshot_id,
        "generated_at": manifest.generated_at,
        "report": report_summary(report),
        "company": {
            "entity_id": report.entity_id,
            "name": company.name if company else report.title,
            "aliases": list(company.aliases) if company else [],
            "securities": [item.to_dict() for item in company.securities] if company else [],
        },
        "sibling_reports": siblings,
        "content_url": report_content_url(manifest.snapshot_id, report.entry_path),
        "unpublished_links": list(report.unpublished_links),
        "external_links": list(report.external_links),
        "diagnostics": [item.to_dict() for item in report.diagnostics],
        "assets": [asset.to_dict() for asset in report.assets],
        "display": {
            "sha256": report.display_sha256,
            "size": report.display_size,
            "dependencies": list(report.display_dependencies),
        },
    }
