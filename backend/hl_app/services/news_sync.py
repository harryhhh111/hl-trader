from __future__ import annotations

import json
import os
import time
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import AwareDatetime, BaseModel, Field, StrictBool, StrictInt, ValidationError

from ..migration.service import business_task
from . import news_sync_store as store

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PAGE_LIMIT = 200
MAX_PAGES_PER_RUN = 100


class NewsSyncError(RuntimeError):
    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class NewsSourceEvent(BaseModel):
    model_config = {"extra": "forbid"}

    sync_seq: StrictInt = Field(gt=0)
    event_kind: Literal["source"]
    source_id: StrictInt = Field(ge=0)
    market: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    platform: str | None
    issuer_identifier: str
    entry_url: str = Field(min_length=1)
    allowed_domain: str = Field(min_length=1)
    source_timezone: str | None
    parser_version: StrictInt = Field(ge=1)
    interval_minutes: StrictInt | None = Field(default=None, gt=0)
    enabled: StrictBool
    source_status: str = Field(min_length=1)
    status_reason: str | None
    effective_interval_minutes: StrictInt | None = Field(default=None, gt=0)
    last_attempt_at: AwareDatetime | None
    last_attempt_status: str | None
    last_success_at: AwareDatetime | None
    next_due_at: AwareDatetime | None
    checked_through: AwareDatetime | None
    last_error_class: str | None
    open_gaps: StrictInt = Field(ge=0)
    earliest_gap_start: AwareDatetime | None


class NewsRelation(BaseModel):
    model_config = {"extra": "forbid"}

    market: str = Field(min_length=1)
    stock_code: str = Field(min_length=1)
    relation_reason: str | None


class NewsItemEvent(BaseModel):
    model_config = {"extra": "forbid"}

    sync_seq: StrictInt = Field(gt=0)
    event_kind: Literal["announcement", "relation", "llm"]
    market: str = Field(min_length=1)
    source_id: StrictInt = Field(ge=0)
    source_kind: str = Field(min_length=1)
    platform: str | None
    issuer_identifier: str
    entry_url: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    revision: StrictInt = Field(ge=1)
    title: str = Field(min_length=1)
    form_type: str | None
    original_url: str = Field(min_length=1)
    excerpt: str | None
    content_hash: str | None
    content_nature: str | None
    media_source: str | None
    is_backfill: StrictBool
    published_at: AwareDatetime | None
    published_at_precision: str = Field(min_length=1)
    source_published_raw: str | None
    source_timezone: str | None
    first_observed_at: AwareDatetime
    last_seen_at: AwareDatetime
    title_zh: str | None
    summary_zh: str | None
    importance: str | None
    source_status: str = Field(min_length=1)
    status_reason: str | None
    parser_version: StrictInt = Field(ge=1)
    relations: list[NewsRelation]


class NewsSyncResponse(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal[1]
    source_instance: str = Field(min_length=1)
    snapshot_at: AwareDatetime
    next_cursor: str = Field(min_length=1)
    has_more: StrictBool
    sources: list[NewsSourceEvent]
    items: list[NewsItemEvent]


def parse_response(payload: bytes) -> NewsSyncResponse:
    try:
        decoded = json.loads(payload)
        return NewsSyncResponse.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise NewsSyncError("上游同步响应不符合严格 DTO 合同") from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NewsSyncClient:
    def __init__(self, base_url: str, source_instance: str, token: str, timeout: int = 20, opener=None) -> None:
        parts = urlsplit(base_url)
        if (
            parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise NewsSyncError("HL_APP_STOCK_DATA_BASE_URL 必须是不带凭据、查询或路径的 HTTPS origin")
        if not source_instance.strip():
            raise NewsSyncError("HL_APP_STOCK_DATA_SOURCE_INSTANCE 未配置")
        if len(token) < 24:
            raise NewsSyncError("HL_APP_STOCK_DATA_READ_TOKEN 未配置或长度不足")
        self.base_url = base_url.rstrip("/")
        self.source_instance = source_instance
        self.token = token
        self.timeout = timeout
        self.opener = opener or build_opener(_NoRedirect())

    @classmethod
    def from_env(cls) -> NewsSyncClient:
        return cls(
            os.environ.get("HL_APP_STOCK_DATA_BASE_URL", ""),
            os.environ.get("HL_APP_STOCK_DATA_SOURCE_INSTANCE", ""),
            os.environ.get("HL_APP_STOCK_DATA_READ_TOKEN", ""),
        )

    def fetch(self, cursor: str | None) -> NewsSyncResponse:
        query = {"limit": str(PAGE_LIMIT)}
        if cursor is not None:
            query["cursor"] = cursor
        url = f"{self.base_url}/api/v1/integrations/hl-trader/news/sync?{urlencode(query)}"
        request = Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json",
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise NewsSyncError(f"上游同步请求被拒绝或不可用（HTTP {exc.code}）", exc.code) from exc
        except Exception as exc:
            raise NewsSyncError("上游同步网络请求失败") from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise NewsSyncError("上游同步响应超过 2 MiB 上限")
        return parse_response(payload)


def sync_position(url: str) -> dict:
    with store.connect(url, cursor_factory=store.RealDictCursor) as conn:
        with conn.cursor() as cursor:
            state = store._state(cursor)
            return {
                "source_instance": state["source_instance"], "next_cursor": state["next_cursor"],
                "bootstrap_status": state["bootstrap_status"],
            }


def run(url: str, bootstrap: bool = False, client: NewsSyncClient | None = None) -> dict:
    client = client or NewsSyncClient.from_env()
    store.mark_attempt(url)
    pages = []
    try:
        if bootstrap:
            return _run_pages(url, client, bootstrap, pages, None)
        with business_task(url) as check_active:
            return _run_pages(url, client, bootstrap, pages, check_active)
    except Exception as exc:
        store.mark_error(url, exc)
        raise


def _run_pages(url: str, client: NewsSyncClient, bootstrap: bool, pages: list, check_active) -> dict:
    for _index in range(MAX_PAGES_PER_RUN):
        if check_active is not None:
            check_active()
        position = sync_position(url)
        if position["source_instance"] is not None and position["source_instance"] != client.source_instance:
            raise NewsSyncError("本地绑定的 source_instance 与配置不一致")
        if bootstrap and position["bootstrap_status"] == "completed":
            raise NewsSyncError("bootstrap 已完成，不能重复初始化")
        if not bootstrap and position["bootstrap_status"] != "completed":
            raise NewsSyncError("请先完成 bootstrap 并独立激活")
        response = client.fetch(position["next_cursor"])
        result = store.apply_page(url, response, client.source_instance, bootstrap)
        pages.append(result)
        if check_active is not None:
            check_active()
        if not response.has_more:
            break
        time.sleep(1)
    return {
        "kind": "news_sync", "mode": "bootstrap" if bootstrap else "incremental",
        "pages": len(pages), "events": sum(page["events"] for page in pages),
        "replayed": sum(page["replayed"] for page in pages),
        "budget_exhausted": bool(pages and pages[-1]["has_more"]),
        "status": pages[-1]["bootstrap_status"] if pages else "unchanged",
    }
