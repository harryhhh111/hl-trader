from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_session
from ..services import announcements, news

router = APIRouter(dependencies=[Depends(require_session)])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    return HTTPException(503, "公告资讯暂不可用")


@router.get("/watchlist/announcements")
def announcement_summary(days: int = 7) -> dict:
    try:
        return {"data": announcements.summary(days)}
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/watchlist/{item_id}/announcements")
def announcement_detail(item_id: int, days: int = 90, offset: int = 0, limit: int = 20, source: str = "all") -> dict:
    try:
        return {"data": announcements.detail(item_id, days, offset, limit, source)}
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/news/feed")
def news_feed(date: str | None = None, stock: str = "", kind: str = "all", offset: int = 0, limit: int = 200) -> dict:
    try:
        result = news.feed(date, stock, kind)
        offset = max(0, offset)
        limit = max(1, min(limit, 200))
        entries = result["entries"]
        result["total"] = len(entries)
        result["offset"] = offset
        result["limit"] = limit
        result["has_more"] = offset + limit < len(entries)
        result["entries"] = entries[offset:offset + limit]
        return {"data": result}
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/news/pinned")
def news_pinned() -> dict:
    try:
        return {"data": news.pinned()}
    except Exception as exc:
        raise _error(exc) from exc
