from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import hyperliquid_accounts as accounts_service

NETWORK_INFO_URLS = {
    "mainnet": "https://api.hyperliquid.xyz/info",
    "testnet": "https://api.hyperliquid-testnet.xyz/info",
}


class AuthorizationError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def user_role(network: str, address: str) -> dict:
    endpoint = NETWORK_INFO_URLS.get(network)
    if endpoint is None:
        raise AuthorizationError(422, "Hyperliquid 网络非法")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"type": "userRole", "user": address}).encode("ascii"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=8) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise AuthorizationError(503, "Hyperliquid 授权查询不可用")
            raw = response.read(16385)
    except AuthorizationError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise AuthorizationError(503, "Hyperliquid 授权查询不可用") from exc
    if len(raw) > 16384:
        raise AuthorizationError(503, "Hyperliquid 授权响应非法")
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthorizationError(503, "Hyperliquid 授权响应非法") from exc
    if not isinstance(result, dict) or not isinstance(result.get("role"), str):
        raise AuthorizationError(503, "Hyperliquid 授权响应非法")
    return result


def verify_authorization(network: str, account_address: str, wallet_address: str) -> str:
    try:
        agent = user_role(network, wallet_address)
        agent_data = agent.get("data")
        master = agent_data.get("user") if isinstance(agent_data, dict) else None
        if agent.get("role") != "agent" or not isinstance(master, str):
            raise AuthorizationError(422, "该地址不是有效的 Hyperliquid API Wallet")
        master = accounts_service.normalize_address(master)
        account = user_role(network, account_address)
        if account.get("role") == "subAccount":
            data = account.get("data")
            account_master = data.get("master") if isinstance(data, dict) else None
            if not isinstance(account_master, str):
                raise AuthorizationError(503, "Hyperliquid 子账户归属响应非法")
            expected_master = accounts_service.normalize_address(account_master)
        elif account.get("role") == "user":
            expected_master = account_address
        else:
            raise AuthorizationError(422, "当前账户类型不支持托管 API Wallet")
        if master != expected_master:
            raise AuthorizationError(422, "API Wallet 未获得当前交易账户授权")
        return master
    except accounts_service.PairAccountError as exc:
        raise AuthorizationError(503, "Hyperliquid 授权响应地址非法") from exc
