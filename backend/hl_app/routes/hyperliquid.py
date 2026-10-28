from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_session
from ..services import hyperliquid_pairs as pairs_service
from ..services.process import HyperliquidUnavailable

router = APIRouter(dependencies=[Depends(require_session)])
logger = logging.getLogger(__name__)


def _config_error(exc: pairs_service.PairsConfigError) -> HTTPException:
    if exc.public:
        return HTTPException(503, exc.public)
    logger.warning("hyperliquid pairs config invalid: %s", exc)
    return HTTPException(503, "Hyperliquid 多 pair 配置不可用")


def _unavailable(exc: HyperliquidUnavailable, fallback: str) -> HTTPException:
    if exc.public:
        return HTTPException(503, exc.public)
    logger.warning("hyperliquid collection unavailable: %s", exc)
    return HTTPException(503, fallback)


@router.get("/hyperliquid/pairs")
def hyperliquid_pairs() -> dict:
    try:
        revision, pairs = pairs_service.load_pairs()
        return {"data": {"schema_version": 2, "config_revision": revision, "pairs": pairs}}
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/dashboard")
def pair_dashboard(pair_id: str) -> dict:
    try:
        return {"data": pairs_service.get_pair_dashboard(pair_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc
    except HyperliquidUnavailable as exc:
        raise _unavailable(exc, "Hyperliquid pair 看板暂不可用") from exc
    except Exception as exc:
        logger.exception("hyperliquid pair dashboard failed")
        raise HTTPException(503, "Hyperliquid pair 看板暂不可用") from exc


@router.get("/hyperliquid/announcements")
def hyperliquid_announcements() -> dict:
    try:
        return {"data": pairs_service.get_announcements()}
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc
    except HyperliquidUnavailable as exc:
        raise _unavailable(exc, "Hyperliquid 公告暂不可用") from exc
    except Exception as exc:
        logger.exception("hyperliquid announcements failed")
        raise HTTPException(503, "Hyperliquid 公告暂不可用") from exc
