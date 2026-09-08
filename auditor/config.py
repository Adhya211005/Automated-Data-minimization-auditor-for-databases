"""Configuration for the auditor.

Right now this is just the *target database* connection profile - the database
the auditor scans. It is deliberately separate from the Phase 0 seeder's
``seed-data/.env`` (that role can write; this one must not).

Precedence: explicit kwargs > environment variables > ``auditor/.env`` file
> built-in defaults (which point at the local Phase 0 seed DB).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_ENV_FILE = _PKG_DIR / ".env"

_ENV_PREFIX = "AUDIT_DB_"
_DEFAULTS = {
    "DIALECT": "postgresql",
    "HOST": "localhost",
    "PORT": "5432",
    "USER": "dma_auditor_ro",
    "PASSWORD": "dma_auditor_ro_pw",
    "NAME": "dma_auditor",
    "SCHEMA": "public",
}


def _load_env_file(path: Path = _ENV_FILE) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file (no overwrite)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class TargetConfig:
    """Where the target database lives and how to reach it (read-only)."""

    host: str = _DEFAULTS["HOST"]
    port: int = int(_DEFAULTS["PORT"])
    user: str = _DEFAULTS["USER"]
    password: str = field(default=_DEFAULTS["PASSWORD"], repr=False)
    dbname: str = _DEFAULTS["NAME"]
    dialect: str = _DEFAULTS["DIALECT"]  # "postgresql" | "mysql"
    schema: str = _DEFAULTS["SCHEMA"]

    @classmethod
    def from_env(cls, **overrides) -> "TargetConfig":
        _load_env_file()

        def pick(suffix: str) -> str:
            return os.environ.get(_ENV_PREFIX + suffix, _DEFAULTS[suffix])

        values = dict(
            host=pick("HOST"),
            port=int(pick("PORT")),
            user=pick("USER"),
            password=pick("PASSWORD"),
            dbname=pick("NAME"),
            dialect=pick("DIALECT"),
            schema=pick("SCHEMA"),
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    @property
    def driver(self) -> str:
        return {
            "postgresql": "postgresql+psycopg",
            "mysql": "mysql+pymysql",
        }.get(self.dialect, self.dialect)

    def sqlalchemy_url(self) -> str:
        from sqlalchemy.engine import URL

        return URL.create(
            self.driver,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.dbname,
        ).render_as_string(hide_password=False)

    def safe_repr(self) -> str:
        return f"{self.dialect}://{self.user}@{self.host}:{self.port}/{self.dbname} (schema={self.schema})"
