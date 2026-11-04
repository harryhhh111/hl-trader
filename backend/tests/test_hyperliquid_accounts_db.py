from __future__ import annotations

import pytest

from hl_app.db import migrate
from hl_app.services.hyperliquid_accounts import (
    PairAccountError,
    activate_account,
    add_account,
    apply_active_accounts,
    list_accounts,
)

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER

pytestmark = pytest.mark.db
SECOND = "0x1111111111111111111111111111111111111111"
THIRD = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def ready(postgres_namespace: PostgresNamespace):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    return postgres_namespace


def descriptor(address=ADDRESS_LOWER):
    return {"id": "nbis-crwv", "account_address": address}


def test_add_multiple_switch_and_preserve_static_default(ready) -> None:
    initial = list_accounts(ready.runtime_url, descriptor())["accounts"]
    assert len(initial) == 1
    assert initial[0]["source"] == "config" and initial[0]["is_active"] is True

    added = add_account(ready.runtime_url, descriptor(), "账户二", SECOND.upper())
    assert added["address"] == SECOND and added["is_active"] is True
    accounts = list_accounts(ready.runtime_url, descriptor())["accounts"]
    assert [(item["label"], item["address"], item["is_active"]) for item in accounts] == [
        ("默认账户", ADDRESS_LOWER, False),
        ("账户二", SECOND, True),
    ]
    assert apply_active_accounts(ready.runtime_url, [descriptor()])[0]["account_address"] == SECOND

    add_account(ready.runtime_url, descriptor(), "账户三", THIRD)
    activated = activate_account(ready.runtime_url, descriptor(), added["id"])
    assert activated["id"] == added["id"] and activated["is_active"] is True
    accounts = list_accounts(ready.runtime_url, descriptor())["accounts"]
    assert [item["is_active"] for item in accounts] == [False, True, False]
    assert apply_active_accounts(ready.runtime_url, [descriptor()])[0]["account_address"] == SECOND


def test_accounts_are_isolated_by_pair_and_duplicates_are_rejected(ready) -> None:
    add_account(ready.runtime_url, descriptor(), "NBIS 账户", SECOND)
    other = {"id": "hype-btc", "account_address": None}
    assert list_accounts(ready.runtime_url, other)["accounts"] == []
    assert apply_active_accounts(ready.runtime_url, [descriptor(), other]) == [descriptor(SECOND), other]
    with pytest.raises(PairAccountError, match="已添加") as exc:
        add_account(ready.runtime_url, descriptor(), "重复", SECOND.upper())
    assert exc.value.status_code == 409
    with pytest.raises(PairAccountError, match="不属于"):
        activate_account(ready.runtime_url, other, 1)


def test_empty_configuration_allows_first_account(ready) -> None:
    empty = descriptor(None)
    assert list_accounts(ready.runtime_url, empty)["accounts"] == []
    added = add_account(ready.runtime_url, empty, "第一个", SECOND)
    assert added["is_active"] is True
    assert apply_active_accounts(ready.runtime_url, [empty])[0]["account_address"] == SECOND
