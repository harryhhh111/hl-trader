from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_session
from ..services import hyperliquid_accounts as accounts_service
from ..services import hyperliquid_adjustments as adjustments_service
from ..services import hyperliquid_history as history_service
from ..services import hyperliquid_leverage as leverage_service
from ..services import hyperliquid_pairs as pairs_service
from ..services import hyperliquid_quotes as quotes_service
from ..services import hyperliquid_risk as risk_service
from ..services import hyperliquid_managed_adjustments as managed_adjustments_service
from ..services import hyperliquid_wallets as wallets_service
from ..services import hyperliquid_wallet_authorization as wallet_authorization
from ..services import hyperliquid_wallet_crypto as wallet_crypto
from ..services.process import HyperliquidUnavailable

router = APIRouter(dependencies=[Depends(require_session)])
logger = logging.getLogger(__name__)


def _owner(request: Request) -> int | None:
    return getattr(getattr(request.state, "session", None), "account_id", None)


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


class RiskRuleSave(BaseModel):
    account_address: str = Field(min_length=42, max_length=42)
    config_revision: str = Field(min_length=64, max_length=64)
    enabled: bool
    take_profit_usd: str | None = Field(default=None, max_length=40)
    stop_loss_usd: str | None = Field(default=None, max_length=40)

    model_config = {"extra": "forbid"}


class LeverageRequest(BaseModel):
    account_address: str = Field(min_length=42, max_length=42)
    config_revision: str = Field(min_length=64, max_length=64)
    coin: str = Field(min_length=1, max_length=64)
    leverage: int = Field(ge=1)
    margin_mode: str = Field(pattern="^(cross|isolated)$")

    model_config = {"extra": "forbid"}


class BrowserLeveragePrepare(LeverageRequest):
    expected_signer: str = Field(min_length=42, max_length=42)


class BrowserLeverageSubmit(BrowserLeveragePrepare):
    action_id: str = Field(min_length=43, max_length=43)
    signature: str = Field(min_length=132, max_length=132)


class ManagedLeverageExecute(LeverageRequest):
    request_id: str = Field(min_length=36, max_length=36)
    wallet_version: int = Field(ge=1)
    confirmation: str = Field(min_length=1, max_length=100)


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
        pairs = accounts_service.apply_active_accounts(request.app.state.settings.database_url, pairs, _owner(request))
        return {"data": {"schema_version": 2, "config_revision": revision, "pairs": pairs}}
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/accounts")
def pair_accounts(pair_id: str, request: Request) -> dict:
    try:
        return {"data": accounts_service.list_accounts(request.app.state.settings.database_url, _descriptor(pair_id), _owner(request))}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/accounts", status_code=201)
def create_pair_account(pair_id: str, body: PairAccountCreate, request: Request) -> dict:
    try:
        account = accounts_service.add_account(
            request.app.state.settings.database_url, _descriptor(pair_id), body.label, body.address, _owner(request),
        )
        return {"data": account}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.put("/hyperliquid/pairs/{pair_id}/accounts/{account_id}/activate")
def activate_pair_account(pair_id: str, account_id: int, request: Request) -> dict:
    try:
        account = accounts_service.activate_account(
            request.app.state.settings.database_url, _descriptor(pair_id), account_id, _owner(request),
        )
        return {"data": account}
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/api-wallet")
def get_managed_wallet(pair_id: str, account_address: str, request: Request) -> dict:
    try:
        return {"data": wallets_service.get_wallet(
            request.app.state.settings.database_url, pair_id, account_address, _owner(request)
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


def _risk_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (risk_service.RiskError, adjustments_service.AdjustmentError, accounts_service.PairAccountError)):
        return HTTPException(exc.status_code, exc.detail)
    if isinstance(exc, pairs_service.PairsConfigError):
        return _config_error(exc)
    logger.exception("hyperliquid pair risk operation failed")
    return HTTPException(503, "Pair 止盈止损暂不可用")


def _record_risk_result(request: Request, pair_id: str, account_address: str, report: dict) -> None:
    try:
        risk_service.record_close_result(
            request.app.state.settings.database_url, pair_id, account_address, _owner(request), report,
        )
    except Exception:
        logger.exception("pair risk close result could not be recorded")


@router.get("/hyperliquid/pairs/{pair_id}/risk")
def get_pair_risk(pair_id: str, account_address: str, request: Request) -> dict:
    try:
        return {"data": risk_service.get_rule(
            request.app.state.settings.database_url, pair_id, account_address, _owner(request),
        )}
    except Exception as exc:
        raise _risk_error(exc) from exc


@router.put("/hyperliquid/pairs/{pair_id}/risk")
def save_pair_risk(pair_id: str, body: RiskRuleSave, request: Request) -> dict:
    try:
        return {"data": risk_service.save_rule(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.enabled, body.take_profit_usd, body.stop_loss_usd,
            _owner(request),
        )}
    except Exception as exc:
        raise _risk_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/leverage/browser-prepare")
def prepare_browser_leverage(pair_id: str, body: BrowserLeveragePrepare, request: Request) -> dict:
    try:
        return {"data": leverage_service.prepare_browser(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.coin, body.leverage, body.margin_mode,
            body.expected_signer, _owner(request),
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/leverage/browser-submit")
def submit_browser_leverage(pair_id: str, body: BrowserLeverageSubmit, request: Request) -> dict:
    try:
        return {"data": leverage_service.submit_browser(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.coin, body.leverage, body.margin_mode,
            body.expected_signer, body.action_id, body.signature, _owner(request),
        )}
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.post("/hyperliquid/pairs/{pair_id}/leverage/managed-execute")
def execute_managed_leverage(pair_id: str, body: ManagedLeverageExecute, request: Request) -> dict:
    try:
        result, status = leverage_service.execute_managed(
            request.app.state.settings.database_url, pair_id, body.account_address,
            body.config_revision, body.coin, body.leverage, body.margin_mode,
            body.confirmation, body.request_id, body.wallet_version, request.state.session,
        )
        return JSONResponse({"data": result}, status_code=status)
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/adjustment-quote")
def adjustment_quote(pair_id: str, config_revision: str) -> dict:
    try:
        return {"data": quotes_service.get_adjustment_quote(pair_id, config_revision)}
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except pairs_service.PairsConfigError as exc:
        raise _config_error(exc) from exc
    except Exception as exc:
        logger.exception("hyperliquid adjustment quote failed")
        raise HTTPException(503, "盘口读取暂不可用") from exc


@router.post("/hyperliquid/pairs/{pair_id}/adjustments/preview")
def preview_pair_adjustment(pair_id: str, body: AdjustmentPreview, request: Request) -> dict:
    try:
        report = adjustments_service.preview(
            request.app.state.settings.database_url,
            pair_id,
            body.account_address,
            body.config_revision,
            body.target_notional,
            _owner(request),
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
        _record_risk_result(request, pair_id, body.account_address, result)
        return JSONResponse({"data": result}, status_code=status)
    except adjustments_service.AdjustmentError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise _wallet_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/adjustments/managed-executions/{request_id}")
def get_managed_adjustment(pair_id: str, request_id: str, request: Request) -> dict:
    try:
        result = managed_adjustments_service.get_managed_execution(
            request.app.state.settings.database_url, pair_id, request_id, _owner(request)
        )
        _record_risk_result(request, pair_id, result["account_address"], result)
        return {"data": result}
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
            _owner(request),
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
            _owner(request),
        )
        _record_risk_result(request, pair_id, body.account_address, report)
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
            _owner(request),
        )
        _record_risk_result(request, pair_id, body.account_address, report)
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
            request.app.state.settings.database_url, _descriptor(pair_id), account_address, _owner(request),
        )}
    except history_service.TradeHistoryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except Exception as exc:
        raise _account_error(exc) from exc


@router.get("/hyperliquid/pairs/{pair_id}/dashboard")
def pair_dashboard(pair_id: str, request: Request) -> dict:
    try:
        return {"data": pairs_service.get_pair_dashboard(pair_id, request.app.state.settings.database_url, _owner(request))}
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
