from __future__ import annotations

import pytest

from hl_app.services import hyperliquid_wallet_authorization as authorization

ACCOUNT = "0x" + "7" * 40
MASTER = "0x" + "8" * 40
WALLET = "0x" + "1" * 40


def test_agent_must_belong_to_master_account(monkeypatch) -> None:
    roles = {
        WALLET: {"role": "agent", "data": {"user": ACCOUNT}},
        ACCOUNT: {"role": "user"},
    }
    monkeypatch.setattr(authorization, "user_role", lambda network, address: roles[address])
    assert authorization.verify_authorization("mainnet", ACCOUNT, WALLET) == ACCOUNT
    roles[WALLET] = {"role": "agent", "data": {"user": MASTER}}
    with pytest.raises(authorization.AuthorizationError, match="未获得"):
        authorization.verify_authorization("mainnet", ACCOUNT, WALLET)


def test_subaccount_uses_agent_master(monkeypatch) -> None:
    roles = {
        WALLET: {"role": "agent", "data": {"user": MASTER}},
        ACCOUNT: {"role": "subAccount", "data": {"master": MASTER}},
    }
    monkeypatch.setattr(authorization, "user_role", lambda network, address: roles[address])
    assert authorization.verify_authorization("mainnet", ACCOUNT, WALLET) == MASTER
    roles[ACCOUNT] = {"role": "subAccount", "data": {"master": ACCOUNT}}
    with pytest.raises(authorization.AuthorizationError, match="未获得"):
        authorization.verify_authorization("mainnet", ACCOUNT, WALLET)


@pytest.mark.parametrize("role", ["missing", "user", "vault", "subAccount"])
def test_non_agent_or_unavailable_authorization_fails_closed(monkeypatch, role) -> None:
    monkeypatch.setattr(authorization, "user_role", lambda network, address: {"role": role})
    with pytest.raises(authorization.AuthorizationError):
        authorization.verify_authorization("mainnet", ACCOUNT, WALLET)
