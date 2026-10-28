from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..auth import SessionInfo, require_session
from ..services import watchlist as service

router = APIRouter(dependencies=[Depends(require_session)])


class ItemDetails(BaseModel):
    group_name: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=2000)
    research_url: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0, ge=-100000, le=100000)

    model_config = {"extra": "forbid"}

    @field_validator("research_url")
    @classmethod
    def valid_research_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("研究链接必须是安全的 HTTP(S) URL")
        return value


class QuoteRequest(BaseModel):
    market: Literal["US", "CN_HK", "CN_A"]
    codes: list[str] = Field(min_length=1, max_length=50)

    @field_validator("codes")
    @classmethod
    def valid_codes(cls, codes: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9.\-]{1,20}", code) for code in codes):
            raise ValueError("证券代码格式无效")
        return codes


@router.get("/watchlist")
def get_items(request: Request) -> dict:
    try:
        return {"data": service.list_items(request.app.state.settings.database_url)}
    except Exception as exc:
        raise HTTPException(503, "关注列表暂不可用") from exc


@router.patch("/watchlist/{item_id}")
def update_item(item_id: int, body: ItemDetails, request: Request, _session: SessionInfo = Depends(require_session)) -> dict:
    try:
        return {"data": service.update_item(request.app.state.settings.database_url, item_id, body.model_dump())}
    except service.WatchlistError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/watchlist/quotes")
def quotes(body: QuoteRequest, request: Request) -> dict:
    try:
        return {"data": service.quote_batch(request.app.state.settings.database_url, body.market, body.codes)}
    except service.WatchlistError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise HTTPException(503, "行情读取暂不可用") from exc
