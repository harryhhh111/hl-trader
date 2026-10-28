from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_session
from ..services import ai

router = APIRouter(dependencies=[Depends(require_session)])


class SuggestRequest(BaseModel):
    market: Literal["US", "CN_HK", "CN_A"]
    stock_code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    stock_name: str = Field(min_length=1, max_length=200)


@router.post("/watchlist/suggest")
def suggest(body: SuggestRequest, request: Request) -> dict:
    try:
        return {"data": ai.suggest_fields(request.app.state.settings.database_url, body.market, body.stock_code, body.stock_name)}
    except ai.AIError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "研究读取或 AI 草稿生成失败") from exc
