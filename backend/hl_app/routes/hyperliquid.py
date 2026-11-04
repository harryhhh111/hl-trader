from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_session
from ..services import hyperliquid_accounts as accounts_service
from ..services import hyperliquid_adjustments as adjustments_service
from ..services import hyperliquid_history as history_service
from ..services import hyperliquid_pairs as pairs_service
from ..services import hyperliquid_managed_adjustments as managed_adjustments_service
from ..services import hyperliquid_wallets as wallets_service
from ..services import hyperliquid_wallet_authorization as wallet_authorization
from ..services import hyperliquid_wallet_crypto as wallet_crypto
from ..services.process import HyperliquidUnavailable

router = APIRouter(dependencies=[Depends(require_session)])
logger = logging.getLogger(__name__)


class PairAccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=42, max_length=42)

    model_config = {"extra": "forbid"}


class AdjustmentPreview(BaseModel):
    target_notional: str = Field(min_length=1, max_length=40)
    account_address: str = Field(min_length=42, max_length=42)
    config_revision: str = Field(min_length=64, max_length=64)

    model_config = {"extra": "forbid"}


class AdjustmentExecute(AdjustmentPreview):
    confirmation: str = Field(min_length=1, max_length=40)


class BrowserAdjustmentPrepare(AdjustmentPreview):
    expected_signer: str = Field(min_length=42, max_length=42)


class BrowserSignatureItem(BaseModel):
    leg_index: int = Field(ge=0, le=1)
    signature: str = Field(min_length=132, max_length=132)

    model_config = {"extra": "forbid"}


class BrowserAdjustmentSubmit(BrowserAdjustmentPrepare):
    action_id: str = Field(min_length=41, max_length=41)
    signatures: list[BrowserSignatureItem] = Field(min_length=1, max_length=2)


class WalletChallengeCreate(BaseModel):
    account_address: str = Field(min_length=42, max_length=42)
    config_revision: str = Field(min_length=64, max_length=64)
    operation: str = Field(pattern="^(save|rotate|delete)$")

    model_config = {"extra": "forbid"}


class ManagedWalletSave(BaseModel):
    account_address: str = Field(min_length=42, max_length=42)
    config_revision: str = Field(min_length=64, max_length=64)
    payload: dict

    model_config = {"extra": "forbid"}


class ManagedWalletDelete(WalletChallengeCreate):
    challenge_id: str = Field(min_length=20, max_length=128)
    wallet_address: str = Field(min_length=42, max_length=42)
    wallet_version: int = Field(ge=1)


class ManagedAdjustmentPreview(AdjustmentPreview):
    request_id: str = Field(min_length=36, max_length=36)
    wallet_version: int = Field(ge=1)


class ManagedAdjustmentExecute(ManagedAdjustmentPreview):
    confirmation: str = Field(min_length=1, max_length=40)


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


def _descriptor(pair_id: str) -> dict:
    _revision, pairs = pairs_service.load_pairs()
    descriptor = next((item for item in pairs if item["id"] == pair_id), None)
    if descriptor is None:
        raise LookupError(f"未知或停用的 pair: {pair_id}")
    return descriptor


def _account_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, pairs_service.PairsConfigError):
        return _config_error(exc)
    if isinstance(exc, accounts_service.PairAccountError):
        return HTTPException(exc.status_code, exc.detail)
    logger.exception("hyperliquid pair account operation failed")
    return HTTPException(503, "账户配置暂不可用")


def _wallet_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (
        wallets_service.WalletError,
        wallet_crypto.WalletCryptoError,
        wallet_authorization.AuthorizationError,
        accounts_service.PairAccountError,
    )):
        return HTTPException(exc.status_code, exc.detail)
    if isinstance(exc, pairs_service.PairsConfigError):
        return _config_error(exc)
    logger.exception("hyperliquid managed wallet operation failed")
    return HTTPException(503, "托管钱包操作暂不可用")


@router.get("/hyperliquid/pairs")
def hyperliquid_pairs(request: Request) -> dict:
    try:
        revision, pairs = pairs_service.load_pairs()
        pairs = accounts_service.apply_active_accounts(request.app.state.settings.database_url, pairs)
        return {"data": {"schema_version": 2, "config_revision": revision, "pairs": pairs}}
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/accounts")
def pair_accounts(pair_id: str, request: Request) -> dict:
    try:
        return {"data": accounts_service.list_accounts(request.app.state.settings.database_url, _descriptor(pair_id))}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/accounts", status_code=201)
def create_pair_account(pair_id: str, body: PairAccountCreate, request: Request) -> dict:
    try:
        account = accounts_service.add_account(
            request.app.state.settings.database_url, _descriptor(pair_id), body.label, body.address,
        )
        return {"data": account}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.put("/hyperliquid/pairs/{pair_id}/accounts/{account_id}/activate")
def activate_pair_account(pair_id: str, account_id: int, request: Request) -> dict:
    try:
        account = accounts_service.activate_account(
            request.app.state.settings.database_url, _descriptor(pair_id), account_id,
        )
        return {"data": account}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/api-wallet")
def get_managed_wallet(pair_id: str, account_address: str, request: Request) -> dict:
    try:
        return {"data": wallets_service.get_wallet(
            request.app.state.settings.database_url, pair_id, account_address
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/api-wallet/challenge")
def create_wallet_challenge(pair_id: str, body: WalletChallengeCreate, request: Request) -> dict:
    try:
        return {"data": wallets_service.issue_challenge(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.operation, request.state.session,
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.put("/hyperliquid/pairs/{pair_id}/api-wallet")
def save_managed_wallet(pair_id: str, body: ManagedWalletSave, request: Request) -> dict:
    try:
        return {"data": wallets_service.save_wallet(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.payload, request.state.session,
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/api-wallet/delete")
def delete_managed_wallet(pair_id: str, body: ManagedWalletDelete, request: Request) -> dict:
    try:
        return {"data": wallets_service.delete_wallet(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.challenge_id, body.wallet_address,
            body.wallet_version, request.state.session,
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/preview")
def preview_pair_adjustment(pair_id: str, body: AdjustmentPreview, request: Request) -> dict:
    try:
        report = adjustments_service.preview(
            request.app.state.settings.database_url,
            pair_id,
            body.account_address,
            body.config_revision,
            body.target_notional,
        )
        return {"data": report}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (pairs_service.PairsConfigError, accounts_service.PairAccountError) as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        logger.exception("hyperliquid adjustment preview failed")
        raise HTTPException(503, "调仓预览暂不可用") from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/managed-preview")
def preview_managed_adjustment(pair_id: str, body: ManagedAdjustmentPreview, request: Request) -> dict:
    try:
        return {"data": managed_adjustments_service.preview_managed(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.target_notional, body.request_id,
            body.wallet_version, request.state.session,
        )}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/managed-execute")
def execute_managed_adjustment(pair_id: str, body: ManagedAdjustmentExecute, request: Request) -> dict:
    try:
        result, status = managed_adjustments_service.execute_managed(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.target_notional, body.confirmation,
            body.request_id, body.wallet_version, request.state.session,
        )
        return JSONResponse({"data": result}, status_code=status)
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/adjustments/managed-executions/{request_id}")
def get_managed_adjustment(pair_id: str, request_id: str, request: Request) -> dict:
    try:
        return {"data": managed_adjustments_service.get_managed_execution(
            request.app.state.settings.database_url, pair_id, request_id
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/browser-prepare")
def prepare_browser_adjustment(pair_id: str, body: BrowserAdjustmentPrepare, request: Request) -> dict:
    try:
        report = adjustments_service.prepare_browser(
            request.app.state.settings.database_url,
            pair_id,
            body.account_address,
            body.config_revision,
            body.target_notional,
            body.expected_signer,
        )
        return {"data": report}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (pairs_service.PairsConfigError, accounts_service.PairAccountError) as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        logger.exception("hyperliquid browser adjustment preparation failed")
        raise HTTPException(503, "钱包签名准备暂不可用") from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/browser-submit")
def submit_browser_adjustment(pair_id: str, body: BrowserAdjustmentSubmit, request: Request) -> dict:
    try:
        report = adjustments_service.submit_browser(
            request.app.state.settings.database_url,
            pair_id,
            body.account_address,
            body.config_revision,
            body.target_notional,
            body.expected_signer,
            body.action_id,
            [item.model_dump() for item in body.signatures],
        )
        return {"data": report}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (pairs_service.PairsConfigError, accounts_service.PairAccountError) as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        logger.exception("hyperliquid browser adjustment submission failed")
        raise HTTPException(503, "签名提交异常；如已提交订单，请先核对结果，禁止直接重试") from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/execute")
def execute_pair_adjustment(pair_id: str, body: AdjustmentExecute, request: Request) -> dict:
    try:
        report = adjustments_service.execute(
            request.app.state.settings.database_url,
            pair_id,
            body.account_address,
            body.config_revision,
            body.target_notional,
            body.confirmation,
        )
        return {"data": report}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (pairs_service.PairsConfigError, accounts_service.PairAccountError) as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        logger.exception("hyperliquid adjustment execution failed")
        raise HTTPException(503, "调仓执行异常；如已提交订单，请先核对结果，禁止直接重试") from exc


@router.get("/hyperliquid/pairs/{pair_id}/trade-history")
def pair_trade_history(pair_id: str, account_address: str, request: Request) -> dict:
    try:
        return {"data": history_service.list_history(
            request.app.state.settings.database_url, _descriptor(pair_id), account_address,
        )}
    except history_service.TradeHistoryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/dashboard")
def pair_dashboard(pair_id: str, request: Request) -> dict:
    try:
        return {"data": pairs_service.get_pair_dashboard(pair_id, request.app.state.settings.database_url)}
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
