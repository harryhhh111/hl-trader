from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .announcements import AnnouncementRecord


def _hash(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    query = [(key, value) for key, value in parse_qsl(parsed.query) if not key.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", urlencode(query), ""))


def _record(source: dict, stable: str, title: str, url: str, raw: str, published: datetime | None, precision: str, fields: dict) -> AnnouncementRecord:
    return AnnouncementRecord(
        source_record_id=stable,
        form_type="press_release",
        title=title,
        url=url,
        published_at=published,
        published_precision=precision,
        published_raw=raw,
        source_timezone=source.get("source_timezone") or "UTC",
        content_hash=_hash(fields),
    )


def _nebius(body: bytes, source: dict) -> list[AnnouncementRecord]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body.decode("utf-8", "replace"), re.S)
    if not match:
        raise ValueError("nebius 页面缺少 __NEXT_DATA__")
    data = json.loads(match.group(1))

    def cache(value):
        if isinstance(value, dict):
            if any(re.match(r"^\w+:\{", key) for key in value):
                return value
            for child in value.values():
                found = cache(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cache(child)
                if found is not None:
                    return found
        return None

    entries = cache(data)
    if entries is None:
        if isinstance(data, dict) and "props" in data:
            return []
        raise ValueError("nebius __NEXT_DATA__ 缺少归一化缓存")
    records = []
    for key, value in entries.items():
        if not key.startswith("press_releases:"):
            continue
        slug, title, day = value.get("name"), value.get("title"), value.get("date")
        if not slug or not title or not day:
            raise ValueError("nebius 条目缺必需字段")
        raw = str(value.get("publication_datetime") or "")
        published = None
        precision = "day"
        if raw:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            precision = "second"
        records.append(_record(source, str(slug), str(title), f"https://nebius.com/newsroom/{slug}", raw or str(day), published, precision, {"slug": slug, "title": title, "date": day, "publication_datetime": raw}))
    return sorted(records, key=lambda record: record.source_record_id)


def _q4(body: bytes, source: dict) -> list[AnnouncementRecord]:
    data = json.loads(body.decode("utf-8", "replace"))
    rows = data.get("GetPressReleaseListResult")
    if not isinstance(rows, list):
        raise ValueError("Q4 列表缺少 GetPressReleaseListResult")
    records = []
    for row in rows:
        item_id, title = row.get("PressReleaseId"), row.get("Title")
        if not item_id or not title:
            raise ValueError("Q4 条目缺 PressReleaseId/Title")
        raw = str(row.get("PressReleaseDate") or "")
        published = None
        match = re.fullmatch(r"/Date\((\d+)\)/", raw)
        if match:
            published = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        elif raw:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            published = parsed if parsed.tzinfo is not None else None
        precision = "second" if published is not None else ("day" if raw else "unknown")
        link = row.get("LinkToDetailPage") or ""
        url = link if link.startswith("http") else normalize_url(f"{source['entry_url'].rstrip('/')}/{link.lstrip('/')}") if link else source["entry_url"]
        records.append(_record(source, str(item_id), str(title), url, raw, published, precision, {"id": item_id, "title": title, "link": url, "date": raw}))
    return records


def parse_ir(body: bytes, source: dict) -> list[AnnouncementRecord]:
    platform = source.get("platform")
    if platform == "nebius_newsroom":
        return _nebius(body, source)
    if platform == "q4_press_release":
        return _q4(body, source)
    raise ValueError(f"未配置的 IR 平台：{platform}")
