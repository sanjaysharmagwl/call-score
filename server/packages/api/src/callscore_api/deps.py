"""FastAPI dependencies and application-state wiring for the callscore API.

Expected integrator wiring (``app.py`` is owned elsewhere)::

    from callscore_api import deps, errors
    from callscore_api.routers import runs, methodologies, meta

    app = create_app()
    errors.register_error_handlers(app)
    for router in (meta.router, methodologies.router, runs.router):
        app.include_router(router, prefix="/api")
    # serve with ``deps.lifespan`` so the job worker starts and orphaned
    # ``running`` runs are recovered on boot.

Every dependency resolves lazily from ``app.state`` and creates what is
missing, so tests can use a bare ``TestClient(create_app())`` with
``app.state.settings`` (or ``CALLSCORE_DB_PATH``) pointed at a tmp database —
no lifespan required. The lazily created engine is bootstrapped
(``callscore_store.schema.bootstrap``, idempotent) on first use.

Thread-safety: one SQLAlchemy ``Engine`` per app (engines are thread-safe and
pooled); a fresh ``Session`` per request via :func:`get_session`; one
``JobCoordinator`` per app whose CPU work runs on its own daemon thread —
never on the request/event-loop thread.
"""

import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from callscore_api.services.cache import StageCache
from callscore_api.services.jobs import JobCoordinator
from callscore_api.settings import Settings
from callscore_store import create_engine_for
from callscore_store.schema import bootstrap


def _state_lock(app: FastAPI) -> threading.Lock:
    """Per-app lock guarding lazy creation of shared state."""
    lock = getattr(app.state, "_deps_lock", None)
    if lock is None:
        lock = threading.Lock()
        app.state._deps_lock = lock
    return lock


def get_settings(request: Request) -> Settings:
    """Return the app settings, lazily loading ``Settings.from_env()``."""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = Settings.from_env()
        request.app.state.settings = settings
    return settings


def _ensure_engine(app: FastAPI, settings: Settings) -> Engine:
    """Return the app-wide engine, creating + bootstrapping it on first use."""
    engine: Engine | None = getattr(app.state, "engine", None)
    if engine is None:
        with _state_lock(app):
            engine = getattr(app.state, "engine", None)
            if engine is None:
                settings.ensure_ready()
                engine = create_engine_for(settings.db_path)
                bootstrap(engine)
                app.state.engine = engine
    return engine


def get_engine(request: Request) -> Engine:
    """Dependency: the app-wide SQLAlchemy ``Engine`` (thread-safe)."""
    return _ensure_engine(request.app, get_settings(request))


def get_session(request: Request) -> Iterator[Session]:
    """Dependency: a fresh ``Session`` per request, closed afterwards.

    Repositories accept ``Engine | Session | Connection``; most routers use
    :func:`get_engine` directly (one transaction per repo call). Use this when
    a handler needs multi-statement transactional control.
    """
    session = Session(_ensure_engine(request.app, get_settings(request)))
    try:
        yield session
    finally:
        session.close()


def get_job_coordinator(request: Request) -> JobCoordinator:
    """Dependency: the app-wide ``JobCoordinator`` singleton.

    Created lazily with the configured cache dir and lease TTL. It is NOT
    started by this dependency — background execution only happens when the
    app is served with :func:`lifespan` (or ``coordinator.start()`` is called
    explicitly). ``submit`` works either way: runs stay ``queued`` until a
    worker drains them.
    """
    coordinator: JobCoordinator | None = getattr(request.app.state, "job_coordinator", None)
    if coordinator is None:
        # Resolve the engine BEFORE taking the state lock: _ensure_engine
        # acquires the same (non-reentrant) lock and would deadlock.
        settings = get_settings(request)
        engine = _ensure_engine(request.app, settings)
        with _state_lock(request.app):
            coordinator = getattr(request.app.state, "job_coordinator", None)
            if coordinator is None:
                coordinator = JobCoordinator(
                    engine,
                    cache_dir=settings.cache_dir,
                    lease_ttl=settings.job_lease_ttl_seconds,
                    poll_interval=settings.job_poll_interval_seconds,
                )
                request.app.state.job_coordinator = coordinator
    return coordinator


def get_stage_cache(request: Request) -> StageCache:
    """Dependency: the coordinator's ``StageCache`` (never ``None`` here)."""
    coordinator = get_job_coordinator(request)
    if coordinator.cache is not None:
        return coordinator.cache
    return StageCache(get_settings(request).cache_dir)


@asynccontextmanager
async def lifespan(app: FastAPI, settings: Settings | None = None) -> AsyncIterator[None]:
    """Application lifespan: settings -> bootstrapped engine -> running coordinator.

    On startup this installs ``app.state.settings``/``engine``/
    ``job_coordinator``, bootstraps the schema and calls
    ``JobCoordinator.start()`` — which first runs
    ``recover_interrupted_runs`` so orphaned ``running`` rows become
    ``interrupted``. On shutdown the worker is stopped and the engine
    disposed. An explicit ``settings`` argument (or a pre-set
    ``app.state.settings``) wins over ``Settings.from_env()``.
    """
    resolved = settings or getattr(app.state, "settings", None) or Settings.from_env()
    app.state.settings = resolved
    resolved.ensure_ready()
    engine = create_engine_for(resolved.db_path)
    try:
        bootstrap(engine)
    except Exception:
        engine.dispose()
        raise
    coordinator = JobCoordinator(
        engine,
        cache_dir=resolved.cache_dir,
        lease_ttl=resolved.job_lease_ttl_seconds,
        poll_interval=resolved.job_poll_interval_seconds,
    )
    app.state.engine = engine
    app.state.job_coordinator = coordinator
    coordinator.start()
    try:
        yield
    finally:
        coordinator.close()
        engine.dispose()


__all__ = [
    "get_engine",
    "get_job_coordinator",
    "get_session",
    "get_settings",
    "get_stage_cache",
    "lifespan",
]
