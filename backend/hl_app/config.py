from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(RuntimeError):
    pass


_DATABASE_VARIABLES = {
    "runtime": "HL_APP_DATABASE_URL",
    "migration": "HL_APP_MIGRATION_DATABASE_URL",
    "test": "HL_APP_TEST_DATABASE_URL",
}


def database_url(purpose: str, environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    variable = _DATABASE_VARIABLES[purpose]
    value = source.get(variable, "").strip()
    if not value:
        raise ConfigError(f"{variable} is required")
    return value


def optional_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    value = source.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_token: str | None
    auth_mode: str
    cloudflare_team_domain: str | None
    cloudflare_access_aud: str | None
    session_ttl_seconds: int
    secure_cookie: bool
    trusted_origins: tuple[str, ...]
    host: str
    port: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        host = source.get("HL_APP_HOST", "127.0.0.1")
        raw_port = source.get("HL_APP_PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigError("HL_APP_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("HL_APP_PORT must be between 1 and 65535")
        auth_mode = source.get("HL_APP_AUTH_MODE", "token").strip().lower()
        if auth_mode not in {"token", "cloudflare_access"}:
            raise ConfigError("HL_APP_AUTH_MODE must be token or cloudflare_access")
        auth_token = optional_value("HL_APP_AUTH_TOKEN", source)
        if auth_mode == "token" and auth_token is None:
            raise ConfigError("HL_APP_AUTH_TOKEN is required in token mode")
        team_domain = optional_value("HL_APP_CF_TEAM_DOMAIN", source)
        access_aud = optional_value("HL_APP_CF_ACCESS_AUD", source)
        if auth_mode == "cloudflare_access":
            from urllib.parse import urlsplit

            if team_domain is None:
                raise ConfigError("HL_APP_CF_TEAM_DOMAIN is required in cloudflare_access mode")
            parsed_team = urlsplit(team_domain.rstrip("/"))
            if (
                parsed_team.scheme != "https"
                or not parsed_team.hostname
                or not parsed_team.hostname.endswith(".cloudflareaccess.com")
                or parsed_team.username
                or parsed_team.password
                or parsed_team.port
                or parsed_team.path
                or parsed_team.query
                or parsed_team.fragment
            ):
                raise ConfigError("HL_APP_CF_TEAM_DOMAIN must be https://<team>.cloudflareaccess.com")
            team_domain = team_domain.rstrip("/")
            if access_aud is None:
                raise ConfigError("HL_APP_CF_ACCESS_AUD is required in cloudflare_access mode")
            if len(access_aud) != 64 or any(c not in "0123456789abcdefABCDEF" for c in access_aud):
                raise ConfigError("HL_APP_CF_ACCESS_AUD must be the 64-character application AUD tag")
            access_aud = access_aud.lower()
        raw_ttl = source.get("HL_APP_SESSION_TTL_SECONDS", "604800")
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise ConfigError("HL_APP_SESSION_TTL_SECONDS must be an integer") from exc
        if not 300 <= ttl <= 2592000:
            raise ConfigError("HL_APP_SESSION_TTL_SECONDS must be between 300 and 2592000")
        raw_secure = source.get("HL_APP_SECURE_COOKIE", "true").lower()
        if raw_secure not in {"true", "false"}:
            raise ConfigError("HL_APP_SECURE_COOKIE must be true or false")
        secure = raw_secure == "true"
        origins = tuple(
            origin.strip().rstrip("/")
            for origin in source.get(
                "HL_APP_TRUSTED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
            ).split(",")
            if origin.strip()
        )
        if not origins:
            raise ConfigError("HL_APP_TRUSTED_ORIGINS is required")
        if not secure:
            from urllib.parse import urlsplit

            if any(urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"} for origin in origins):
                raise ConfigError("HL_APP_SECURE_COOKIE=false is only valid for localhost origins")
        return cls(
            database_url=database_url("runtime", source),
            auth_token=auth_token,
            auth_mode=auth_mode,
            cloudflare_team_domain=team_domain,
            cloudflare_access_aud=access_aud,
            session_ttl_seconds=ttl,
            secure_cookie=secure,
            trusted_origins=origins,
            host=host,
            port=port,
        )
