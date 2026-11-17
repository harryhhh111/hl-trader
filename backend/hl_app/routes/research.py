"""Read only API for the generated research report snapshot.

Every route requires a logged in session. The browser never walks the research
library: it only reads the immutable snapshot produced by
``python -m hl_app.research_reports snapshot``.
"""
from __future__ import annotations

from pathlib import Path
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..auth import require_session
from ..services import research_reports
from ..services.research_display import with_document_policy

router = APIRouter(dependencies=[Depends(require_session)])
logger = logging.getLogger(__name__)

# Static reading mode: no script, no network, no form, no plugin, opaque origin.
# The frame may only open a link in the top level window after the reader
# clicked it, so a link to another report reaches the outer application.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src data:; "
    "media-src data:; "
    "script-src 'none'; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "sandbox allow-top-navigation-by-user-activation; "
    "frame-ancestors 'self'"
)
DOCUMENT_SECURITY_POLICY = '; '.join(
    directive.strip() for directive in CONTENT_SECURITY_POLICY.split(';')
    if not directive.strip().startswith(('sandbox', 'frame-ancestors'))
)


def _configured_path(request: Request) -> str:
    configured = request.app.state.settings.research_reports_path
    if not configured:
        raise HTTPException(503, "研究功能未配置：未设置 HL_APP_RESEARCH_REPORTS_PATH")
    return configured


def _snapshot(request: Request, snapshot_id: str | None = None) -> research_reports.LoadedSnapshot:
    configured = _configured_path(request)
    try:
        return research_reports.load_snapshot(Path(configured), snapshot_id)
    except research_reports.ResearchSnapshotError as exc:
        logger.warning('Research snapshot unavailable: %s', exc)
        raise HTTPException(503, "研究快照不可用，请检查配置、版本及读取权限") from exc


@router.get("/research/companies")
def list_companies(request: Request, response: Response) -> dict:
    response.headers['Cache-Control'] = 'private, no-store'
    snapshot = _snapshot(request)
    return {"data": research_reports.company_list_payload(snapshot)}


@router.get("/research/reports/{report_id}")
def report_detail(report_id: str, request: Request, response: Response, snapshot: str | None = None) -> dict:
    response.headers['Cache-Control'] = 'private, no-store'
    loaded = _snapshot(request, snapshot)
    payload = research_reports.report_detail_payload(loaded, report_id)
    if payload is None:
        raise HTTPException(404, "报告不存在或不在当前快照内")
    try:
        entry = loaded.manifest.reports[report_id]
        document, _ = research_reports.snapshot_content_bytes(loaded.root, loaded.snapshot_id, entry.entry_path)
    except research_reports.ResearchSnapshotUnavailable as exc:
        logger.warning('Research document unavailable: %s', exc)
        raise HTTPException(503, '研究正文暂时不可读取') from exc
    except research_reports.ResearchSnapshotError as exc:
        raise HTTPException(404, '研究正文不存在或校验失败') from exc
    # A normal authenticated JSON request traverses the same public access
    # layer as the rest of the app. The reading frame does not navigate to an
    # HTTP URL that an edge can replace with an unframeable verification page.
    payload['document_html'] = with_document_policy(document.decode('utf-8'), DOCUMENT_SECURITY_POLICY)
    return {"data": payload}


@router.get("/research/content/{snapshot_id}/{registered_path:path}")
def report_content(snapshot_id: str, registered_path: str, request: Request) -> Response:
    configured = _configured_path(request)
    try:
        # Any retained snapshot stays readable, so an opened report keeps its content.
        payload, media_type = research_reports.snapshot_content_bytes(
            Path(configured), snapshot_id, registered_path
        )
    except research_reports.ResearchSnapshotUnavailable as exc:
        logger.warning('Research content unavailable: %s', exc)
        raise HTTPException(503, "研究快照不可读取，请检查服务读取权限") from exc
    except research_reports.ResearchSnapshotError as exc:
        raise HTTPException(404, f"资源不可用：{exc}") from exc
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Referrer-Policy": "no-referrer",
        },
    )
