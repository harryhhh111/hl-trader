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
        return cls(database_url=database_url("runtime", source), host=host, port=port)
