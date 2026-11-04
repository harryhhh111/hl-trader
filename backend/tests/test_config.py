from __future__ import annotations

import pytest

from hl_app.config import ConfigError, Settings, database_url


@pytest.mark.parametrize(
    ("purpose", "variable"),
    [
        ("runtime", "HL_APP_DATABASE_URL"),
        ("migration", "HL_APP_MIGRATION_DATABASE_URL"),
        ("test", "HL_APP_TEST_DATABASE_URL"),
    ],
)
def test_database_urls_do_not_fall_back(purpose: str, variable: str) -> None:
    other = {
        "HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl",
        "HL_APP_MIGRATION_DATABASE_URL": "postgresql://migration.invalid/hl",
        "HL_APP_TEST_DATABASE_URL": "postgresql://test.invalid/hl",
    }
    other.pop(variable)
    with pytest.raises(ConfigError, match=variable):
        database_url(purpose, other)
    assert variable not in str(other)


AUTH = "A" * 43


def test_runtime_settings_ignore_legacy_environment() -> None:
    with pytest.raises(ConfigError, match="HL_APP_DATABASE_URL"):
        Settings.from_env(
            {
                "HL_APP_AUTH_TOKEN": AUTH,
                "DATABASE_URL": "postgresql://legacy.invalid/hl",
                "STOCK_DATABASE_URL": "postgresql://legacy.invalid/stock",
                "STOCK_MARKETS": "US",
            }
        )


def test_runtime_settings_require_auth_token() -> None:
    with pytest.raises(ConfigError, match="HL_APP_AUTH_TOKEN"):
        Settings.from_env({"HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl"})


def test_runtime_settings_read_explicit_host_and_port() -> None:
    settings = Settings.from_env(
        {
            "HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl",
            "HL_APP_AUTH_TOKEN": AUTH,
            "HL_APP_HOST": "127.0.0.2",
            "HL_APP_PORT": "9001",
        }
    )
    assert settings.host == "127.0.0.2"
    assert settings.port == 9001


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_port_is_rejected(port: str) -> None:
    with pytest.raises(ConfigError, match="HL_APP_PORT"):
        Settings.from_env(
            {
                "HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl",
                "HL_APP_AUTH_TOKEN": AUTH,
                "HL_APP_PORT": port,
            }
        )


def test_auth_mode_defaults_to_token_and_cloudflare_does_not_need_token() -> None:
    token_settings = Settings.from_env(
        {"HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl", "HL_APP_AUTH_TOKEN": AUTH}
    )
    assert token_settings.auth_mode == "token"
    settings = Settings.from_env(
        {
            "HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl",
            "HL_APP_AUTH_MODE": "cloudflare_access",
            "HL_APP_CF_TEAM_DOMAIN": "https://team.cloudflareaccess.com",
            "HL_APP_CF_ACCESS_AUD": "A" * 64,
        }
    )
    assert settings.auth_mode == "cloudflare_access"
    assert settings.auth_token is None
    assert settings.cloudflare_access_aud == "a" * 64


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HL_APP_CF_TEAM_DOMAIN", "http://team.cloudflareaccess.com"),
        ("HL_APP_CF_TEAM_DOMAIN", "https://example.com"),
        ("HL_APP_CF_TEAM_DOMAIN", "https://team.cloudflareaccess.com/path"),
        ("HL_APP_CF_ACCESS_AUD", "not-an-aud"),
    ],
)
def test_invalid_cloudflare_configuration_is_rejected(name: str, value: str) -> None:
    environ = {
        "HL_APP_DATABASE_URL": "postgresql://runtime.invalid/hl",
        "HL_APP_AUTH_MODE": "cloudflare_access",
        "HL_APP_CF_TEAM_DOMAIN": "https://team.cloudflareaccess.com",
        "HL_APP_CF_ACCESS_AUD": "a" * 64,
        name: value,
    }
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(environ)
