"""Runtime settings for the callscore API.

``pydantic-settings`` is not a dependency of ``callscore-api``, so settings are
a frozen dataclass populated from ``CALLSCORE_*`` environment variables.

All defaults are derived from the repository root, which is located relative
to this file (never the process cwd) — the same trick ``app.py`` uses to find
``web/dist``. Every path can be overridden via the environment::

    CALLSCORE_DB_PATH                  SQLite database file
    CALLSCORE_CACHE_DIR                stage-cache directory
    CALLSCORE_HOST / CALLSCORE_PORT    uvicorn bind address
    CALLSCORE_JOB_LEASE_TTL_SECONDS    writer-lease TTL handed to JobCoordinator
    CALLSCORE_JOB_POLL_INTERVAL_SECONDS  coordinator poll interval
    CALLSCORE_WEB_DIST                 optional override for the SPA bundle dir
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# server/packages/api/src/callscore_api/settings.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[5]

_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "db" / "callscore.db"


class SettingsError(RuntimeError):
    """Raised when settings cannot be loaded or fail validation."""


@dataclass(frozen=True)
class Settings:
    """Validated runtime configuration for the API process."""

    db_path: Path
    cache_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    job_lease_ttl_seconds: float = 300.0
    job_poll_interval_seconds: float = 0.5
    web_dist: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from ``CALLSCORE_*`` env vars and validate them.

        ``env`` defaults to ``os.environ``; pass a mapping in tests.
        """
        source = os.environ if env is None else env
        db_raw = source.get("CALLSCORE_DB_PATH")
        db_path = Path(db_raw).expanduser() if db_raw else _DEFAULT_DB_PATH
        cache_raw = source.get("CALLSCORE_CACHE_DIR")
        cache_dir = Path(cache_raw).expanduser() if cache_raw else db_path.parent / "cache"
        web_raw = source.get("CALLSCORE_WEB_DIST")
        settings = cls(
            db_path=db_path,
            cache_dir=cache_dir,
            host=source.get("CALLSCORE_HOST", "127.0.0.1"),
            port=int(source.get("CALLSCORE_PORT", "8000")),
            job_lease_ttl_seconds=float(source.get("CALLSCORE_JOB_LEASE_TTL_SECONDS", "300")),
            job_poll_interval_seconds=float(
                source.get("CALLSCORE_JOB_POLL_INTERVAL_SECONDS", "0.5")
            ),
            web_dist=Path(web_raw).expanduser() if web_raw else None,
        )
        settings.ensure_ready()
        return settings

    def ensure_ready(self) -> None:
        """Create/verify the db parent and cache dir are writable.

        Idempotent — safe to call on every dependency resolution. Raises
        :class:`SettingsError` (a ``RuntimeError``, so it surfaces as a 500,
        never a misleading 422) when a path cannot be created or written.
        """
        if not 1 <= self.port <= 65535:
            raise SettingsError(f"port out of range: {self.port}")
        if self.job_lease_ttl_seconds <= 0:
            raise SettingsError(
                f"job_lease_ttl_seconds must be positive: {self.job_lease_ttl_seconds}"
            )
        if self.job_poll_interval_seconds <= 0:
            raise SettingsError(
                f"job_poll_interval_seconds must be positive: {self.job_poll_interval_seconds}"
            )
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SettingsError(f"cannot create settings directories: {exc}") from exc
        for path, what in (
            (self.db_path.parent, "db parent directory"),
            (self.cache_dir, "cache directory"),
        ):
            if not os.access(path, os.W_OK):
                raise SettingsError(f"{what} is not writable: {path}")
        if self.db_path.exists() and not os.access(self.db_path, os.W_OK):
            raise SettingsError(f"db file is not writable: {self.db_path}")


__all__ = ["Settings", "SettingsError"]
