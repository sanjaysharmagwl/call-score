"""Read-side query layer for market, reference and derived tables.

All functions take a store ``Context`` (``Engine``, ``Connection`` or
``Session``) and return plain ``dict`` rows; routers map them onto pydantic
response models. Queries are built exclusively with SQLAlchemy expressions
(whitelisted columns, bound parameters — no string SQL), every list is
paginated with ``limit <= MAX_LIMIT``, and stable sorts always end in a
unique key.

Point-in-time conventions (decision 0003 / ``frames.py``):

* Classification/identifier "effective at ``as_of``" means the spell with the
  greatest ``from_date <= as_of`` — spell ``to_date`` is the last day the
  spell applies (inclusive), null is open-ended. When ``as_of`` is omitted the
  latest spell overall is used, so delisted securities are NEVER omitted
  (no survivorship bias): a security whose latest spell has ended is still
  returned, flagged ``is_delisted``.
* ``is_delisted`` is true when a ``delisting`` row exists, the resolved
  classification carries ``delisting_date``, or (when ``as_of`` bounds the
  query) the resolved spell's ``to_date`` precedes ``as_of``.

Chart downsampling: when a resampled price series exceeds
``PRICE_DOWNSAMPLE_TARGET`` points it is reduced with *bucket extrema* — the
series is split into equal-sized buckets and, per bucket, the rows with the
minimum and maximum ``close`` are kept (plus the first and last row), then
re-sorted by date. This preserves the global min/max close exactly, which is
what a price chart needs; it is NOT LTTB (no visual-area optimization) and is
deliberately simple and deterministic.

Provenance: market-data responses carry ``provenance`` distinguishing the
source-data revision (``source_revision`` rows + latest ``generation_manifest``
checksum) from calculation ``run_id``s, which only scope derived tables.
"""

import csv
import io
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import Connection, Engine, Select, and_, func, literal, or_, select
from sqlalchemy.orm import Session

from callscore_core.enums import RatingAction, RunStatus
from callscore_core.frames import HORIZON_TO_EXIT
from callscore_store.db import create_engine_for
from callscore_store.models import (
    Analyst,
    AnalystComposite,
    AnalystCoverage,
    CalcRun,
    ConsensusDaily,
    Delisting,
    GenerationManifest,
    IntervalContribution,
    IntervalReturn,
    MethodologyVersion,
    RatingEvent,
    RatingInterval,
    Sector,
    Security,
    SecurityClassification,
    SecurityDaily,
    SecurityIdentifier,
    SourceRevision,
    TargetOutcome,
    TradingSession,
)
from callscore_store.repositories.base import Context
from callscore_store.repositories.runs import get_calc_run

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# Price series longer than this are bucket-extrema downsampled before paging.
PRICE_DOWNSAMPLE_TARGET = 2000

# Interval resampling whitelist for /prices.
PRICE_INTERVALS = ("1d", "1w", "1M")

# ``/api/changes`` type filter whitelist: every rating action plus run
# lifecycle pseudo-types and the ``correction`` alias.
RUN_CHANGE_TYPES = ("run_created", "run_succeeded", "run_failed", "run_interrupted")
CHANGE_TYPES = frozenset({a.value for a in RatingAction} | set(RUN_CHANGE_TYPES) | {"correction"})

_REPO_ROOT = Path(__file__).resolve().parents[6]
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "db" / "callscore.db"


# --------------------------------------------------------------------------- #
# Engine dependency (temporary home until callscore_api.deps lands — T13)
# --------------------------------------------------------------------------- #


def resolve_db_path() -> Path:
    """DB path from ``CALLSCORE_DB`` env var, else repo ``data/db/callscore.db``."""
    raw = os.environ.get("CALLSCORE_DB")
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_DB_PATH


@lru_cache(maxsize=8)
def _engine_for(path_str: str) -> Engine:
    return create_engine_for(path_str)


def get_engine(request: Request) -> Engine:
    """FastAPI dependency delegating to ``callscore_api.deps.get_engine``.

    Kept as a module-level alias so routers can ``Depends(mq.get_engine)``;
    the shared app engine (``app.state.engine``, bootstrapped, honoring
    ``Settings.db_path``) is the single source of truth.
    """
    from callscore_api import deps

    return deps.get_engine(request)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _all(ctx: Context, stmt: Any) -> list[dict[str, Any]]:
    """Execute ``stmt`` and return rows as dicts (works for all Context kinds)."""
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
    if isinstance(ctx, Session | Connection):
        return [dict(r) for r in ctx.execute(stmt).mappings()]
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def _scalar(ctx: Context, stmt: Any) -> Any:
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()
    if isinstance(ctx, Session | Connection):
        return ctx.execute(stmt).scalar_one_or_none()
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def clamp_limit(limit: int) -> int:
    """Enforce the global page-size cap (defense in depth; routers also validate)."""
    return max(1, min(int(limit), MAX_LIMIT))


def clamp_offset(offset: int) -> int:
    return max(0, int(offset))


def _latest_classification_subquery(as_of: date | None) -> Any:
    """Derived table ``(security_id, mf)`` = max ``from_date <= as_of`` per security.

    ``to_date`` is deliberately NOT consulted here: the latest spell that had
    started by ``as_of`` is the security's classification at that date even if
    the spell (and the security) has since ended — that is what keeps delisted
    names visible. ``as_of=None`` means "latest spell overall".
    """
    cls = SecurityClassification.__table__
    stmt = select(cls.c.security_id, func.max(cls.c.from_date).label("mf")).group_by(
        cls.c.security_id
    )
    if as_of is not None:
        stmt = stmt.where(cls.c.from_date <= as_of)
    return stmt.subquery()


def _is_delisted_flag(
    *,
    delisting_exists: bool,
    classification_delisting_date: date | None,
    classification_to_date: date | None,
    as_of: date | None,
) -> bool:
    """Delisted = terminal record, explicit delisting date, or an ended spell.

    With ``as_of=None`` any ended latest spell counts (the security has no
    current classification). With ``as_of`` set, only spells that ended before
    ``as_of`` count — a spell covering ``as_of`` is active by definition.
    """
    if delisting_exists or classification_delisting_date is not None:
        return True
    if classification_to_date is None:
        return False
    if as_of is None:
        return True
    return classification_to_date < as_of


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _json_value(value: Any) -> Any:
    """Make a scalar JSON/CSV-safe (dates ISO, Decimal as string, bool lower)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def rows_to_csv(rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> str:
    """Serialize row dicts to CSV text (header + ``\\n`` newlines, UTF-8 safe)."""
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_cell(v) for k, v in row.items()})
    return buf.getvalue()


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def csv_response(csv_text: str, filename: str) -> Response:
    """HTTP response for ``?format=csv`` exports."""
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Provenance (source data vs calculation runs)
# --------------------------------------------------------------------------- #


def provenance(ctx: Context) -> dict[str, Any]:
    """Source-data provenance: revisions + latest generation manifest.

    This describes the *source* tables only; derived (run-scoped) responses
    add their own ``run_id``/``source_fingerprint`` alongside it.
    """
    revisions = _all(
        ctx,
        select(
            SourceRevision.source_id, SourceRevision.revision, SourceRevision.captured_at
        ).order_by(SourceRevision.source_id, SourceRevision.revision),
    )
    manifest = _all(
        ctx,
        select(
            GenerationManifest.id,
            GenerationManifest.seed,
            GenerationManifest.profile,
            GenerationManifest.table_checksums_json,
            GenerationManifest.created_at,
        )
        .order_by(GenerationManifest.id.desc())
        .limit(1),
    )
    return {
        "source_revisions": revisions,
        "generation_manifest": manifest[0] if manifest else None,
    }


def run_provenance(ctx: Context, run: Mapping[str, Any]) -> dict[str, Any]:
    """Calculation provenance for one ``calc_run`` row."""
    methodology_fingerprint: str | None = None
    mv_id = run.get("methodology_version_id")
    if mv_id is not None:
        row = _all(
            ctx,
            select(MethodologyVersion.fingerprint).where(MethodologyVersion.id == int(mv_id)),
        )
        if row:
            methodology_fingerprint = str(row[0]["fingerprint"])
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "as_of_date": run.get("as_of_date"),
        "knowledge_cutoff": run.get("knowledge_cutoff"),
        "methodology_version_id": mv_id,
        "methodology_fingerprint": methodology_fingerprint,
        "source_fingerprint": run.get("source_fingerprint"),
        "algorithm_version": run.get("algorithm_version"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


def get_run_or_404(ctx: Context, run_id: str) -> dict[str, Any]:
    """Fetch a calc_run or raise ``KeyError`` (routers map to 404)."""
    run = get_calc_run(ctx, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    return run


# --------------------------------------------------------------------------- #
# Securities
# --------------------------------------------------------------------------- #


def _identifier_exists(as_of: date | None, like: str) -> Any:
    """Correlated EXISTS: security has an identifier matching ``like``.

    With ``as_of`` the identifier spell must cover that date; without it any
    historical identifier matches (search is not point-in-time by default).
    """
    conds = [
        SecurityIdentifier.security_id == Security.security_id,
        SecurityIdentifier.value.like(like),
    ]
    if as_of is not None:
        conds += [
            SecurityIdentifier.from_date <= as_of,
            or_(
                SecurityIdentifier.to_date.is_(None),
                SecurityIdentifier.to_date >= as_of,
            ),
        ]
    return select(literal(1)).select_from(SecurityIdentifier.__table__).where(and_(*conds)).exists()


def _security_list_stmt(
    *,
    q: str | None,
    sector: str | None,
    cap_bucket: str | None,
    as_of: date | None,
    count_only: bool,
) -> Select[Any]:
    latest = _latest_classification_subquery(as_of)
    cls = SecurityClassification.__table__
    join_cond = and_(cls.c.security_id == latest.c.security_id, cls.c.from_date == latest.c.mf)

    stmt: Select[Any]
    if count_only:
        stmt = (
            select(func.count())
            .select_from(Security.__table__)
            .join(latest, latest.c.security_id == Security.security_id)
            .join(cls, join_cond)
        )
    else:
        stmt = (
            select(
                Security.security_id,
                Security.name,
                cls.c.sector_id,
                Sector.name.label("sector_name"),
                cls.c.region,
                cls.c.currency,
                cls.c.exchange_id,
                cls.c.cap_bucket,
                cls.c.listing_date,
                cls.c.delisting_date.label("classification_delisting_date"),
                cls.c.from_date.label("classification_from_date"),
                cls.c.to_date.label("classification_to_date"),
            )
            .select_from(Security.__table__)
            .join(latest, latest.c.security_id == Security.security_id)
            .join(cls, join_cond)
            .outerjoin(Sector.__table__, Sector.sector_id == cls.c.sector_id)
        )

    wheres = []
    if q:
        like = f"%{q}%"
        wheres.append(
            or_(
                Security.name.like(like),
                Security.security_id.like(like),
                _identifier_exists(as_of, like),
            )
        )
    if sector is not None:
        wheres.append(cls.c.sector_id == sector)
    if cap_bucket is not None:
        wheres.append(cls.c.cap_bucket == cap_bucket)
    if wheres:
        stmt = stmt.where(and_(*wheres))
    if not count_only:
        # Stable sort: name then unique id tie-break.
        stmt = stmt.order_by(Security.name.asc(), Security.security_id.asc())
    return stmt


def _tickers_for(ctx: Context, security_ids: Sequence[str], as_of: date | None) -> dict[str, str]:
    """Best ``ticker`` identifier per security (covers ``as_of`` preferred)."""
    if not security_ids:
        return {}
    rows = _all(
        ctx,
        select(
            SecurityIdentifier.security_id,
            SecurityIdentifier.value,
            SecurityIdentifier.from_date,
            SecurityIdentifier.to_date,
        )
        .where(
            SecurityIdentifier.security_id.in_(list(security_ids)),
            SecurityIdentifier.id_type == "ticker",
        )
        .order_by(
            SecurityIdentifier.security_id.asc(),
            SecurityIdentifier.from_date.desc(),
        ),
    )
    # Rows arrive from_date-desc per security. A spell covering ``as_of``
    # beats any non-covering one; the first covering row is the latest such
    # spell, and the first row overall is the latest historical fallback.
    out: dict[str, str] = {}
    covered: set[str] = set()
    for row in rows:
        sid = str(row["security_id"])
        if sid in covered:
            continue
        covers = as_of is None or (
            row["from_date"] <= as_of and (row["to_date"] is None or row["to_date"] >= as_of)
        )
        if covers:
            out[sid] = str(row["value"])
            covered.add(sid)
        elif sid not in out:
            out[sid] = str(row["value"])
    return out


def _delisted_ids(ctx: Context, security_ids: Sequence[str]) -> set[str]:
    if not security_ids:
        return set()
    rows = _all(
        ctx,
        select(Delisting.security_id).where(Delisting.security_id.in_(list(security_ids))),
    )
    return {str(r["security_id"]) for r in rows}


def search_securities(
    ctx: Context,
    *,
    q: str | None = None,
    sector: str | None = None,
    cap_bucket: str | None = None,
    as_of: date | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Search/list securities with PIT classification. Returns ``(rows, total)``.

    Delisted securities are always eligible (latest spell wins); ``is_delisted``
    is computed per row. ``q`` matches name, security_id and identifier values.
    """
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    total = int(
        _scalar(
            ctx,
            _security_list_stmt(
                q=q, sector=sector, cap_bucket=cap_bucket, as_of=as_of, count_only=True
            ),
        )
        or 0
    )
    rows = _all(
        ctx,
        _security_list_stmt(
            q=q, sector=sector, cap_bucket=cap_bucket, as_of=as_of, count_only=False
        )
        .limit(limit)
        .offset(offset),
    )
    ids = [str(r["security_id"]) for r in rows]
    tickers = _tickers_for(ctx, ids, as_of)
    delisted = _delisted_ids(ctx, ids)
    for row in rows:
        sid = str(row["security_id"])
        row["ticker"] = tickers.get(sid)
        row["is_delisted"] = _is_delisted_flag(
            delisting_exists=sid in delisted,
            classification_delisting_date=row.get("classification_delisting_date"),
            classification_to_date=row.get("classification_to_date"),
            as_of=as_of,
        )
    return rows, total


def security_detail(ctx: Context, security_id: str, *, as_of: date | None = None) -> dict[str, Any]:
    """Full detail for one security; raises ``KeyError`` when unknown."""
    sec = _all(ctx, select(Security).where(Security.security_id == security_id))
    if not sec:
        raise KeyError(f"security {security_id!r} not found")

    cls_rows = _all(
        ctx,
        select(SecurityClassification)
        .where(SecurityClassification.security_id == security_id)
        .order_by(SecurityClassification.from_date.asc()),
    )
    id_rows = _all(
        ctx,
        select(SecurityIdentifier)
        .where(SecurityIdentifier.security_id == security_id)
        .order_by(
            SecurityIdentifier.id_type.asc(),
            SecurityIdentifier.from_date.desc(),
        ),
    )
    delist = _all(ctx, select(Delisting).where(Delisting.security_id == security_id))

    # Latest classification at/before as_of (same rule as the list endpoint).
    latest = None
    candidates = [r for r in cls_rows if as_of is None or r["from_date"] <= as_of]
    if candidates:
        latest = max(candidates, key=lambda r: r["from_date"])

    # Coverage: distinct analysts whose coverage spell covers as_of; when
    # as_of is None (or none cover it) count all coverage spells.
    cov_stmt = select(func.count(func.distinct(AnalystCoverage.analyst_id))).where(
        AnalystCoverage.security_id == security_id
    )
    if as_of is not None:
        cov_stmt = cov_stmt.where(
            AnalystCoverage.from_date <= as_of,
            or_(AnalystCoverage.to_date.is_(None), AnalystCoverage.to_date >= as_of),
        )
    n_cov = int(_scalar(ctx, cov_stmt) or 0)
    if n_cov == 0 and as_of is not None:
        # No spell covers as_of — fall back to all coverage on the security.
        n_cov = int(
            _scalar(
                ctx,
                select(func.count(func.distinct(AnalystCoverage.analyst_id))).where(
                    AnalystCoverage.security_id == security_id
                ),
            )
            or 0
        )

    price_stats = _all(
        ctx,
        select(
            func.count().label("n_rows"),
            func.min(SecurityDaily.session_date).label("first_session"),
            func.max(SecurityDaily.session_date).label("last_session"),
        ).where(SecurityDaily.security_id == security_id),
    )[0]
    n_events = int(
        _scalar(
            ctx,
            select(func.count())
            .select_from(RatingEvent.__table__)
            .where(RatingEvent.security_id == security_id),
        )
        or 0
    )

    sector_name = None
    if latest is not None:
        sector_row = _all(
            ctx,
            select(Sector.name).where(Sector.sector_id == latest["sector_id"]),
        )
        sector_name = str(sector_row[0]["name"]) if sector_row else None

    is_delisted = _is_delisted_flag(
        delisting_exists=bool(delist),
        classification_delisting_date=(latest["delisting_date"] if latest is not None else None),
        classification_to_date=(latest["to_date"] if latest is not None else None),
        as_of=as_of,
    )

    return {
        "security": sec[0],
        "latest_classification": latest,
        "sector_name": sector_name,
        "is_delisted": is_delisted,
        "delisting": delist[0] if delist else None,
        "identifiers": id_rows,
        "classification_spells": cls_rows,
        "coverage_analysts": n_cov,
        "price_coverage": price_stats,
        "rating_event_count": n_events,
    }


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def _resample_interval(rows: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Resample daily bars to ``1w`` (ISO week) or ``1M`` (calendar month).

    Aggregation: open=first, high=max, low=min, close=last, adj_factor=last,
    cash_dividend=sum (per-share dividends add within the bucket), tr_index=last,
    is_halted=any. ``1d`` returns rows unchanged.
    """
    if interval == "1d" or len(rows) <= 1:
        return rows

    def _key(d: date) -> tuple[int, int]:
        if interval == "1w":
            iso = d.isocalendar()
            return (iso[0], iso[1])
        return (d.year, d.month)

    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    order: list[tuple[int, int]] = []
    for row in rows:
        key = _key(row["session_date"])
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    out: list[dict[str, Any]] = []
    for key in order:
        group = buckets[key]
        first, last = group[0], group[-1]
        dividends = [r["cash_dividend"] for r in group if r["cash_dividend"] is not None]
        out.append(
            {
                "session_date": last["session_date"],
                "period_start": first["session_date"],
                "open": first["open"],
                "high": max(float(r["high"]) for r in group),
                "low": min(float(r["low"]) for r in group),
                "close": last["close"],
                "adj_factor": last["adj_factor"],
                "cash_dividend": sum(dividends) if dividends else None,
                "tr_index": last["tr_index"],
                "is_halted": any(bool(r["is_halted"]) for r in group),
                "n_sessions": len(group),
            }
        )
    return out


def _bucket_extrema(rows: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Downsample ``rows`` (date-sorted) to <= ``target`` keeping bucket extrema.

    Per equal-sized bucket the min-close and max-close rows survive, plus the
    first and last row of the whole series; survivors are re-sorted by date.
    Global min/max ``close`` are therefore preserved exactly.
    """
    n = len(rows)
    if n <= target or target < 2:
        return rows
    bucket_size = -(-n // target)  # ceil
    keep: set[int] = {0, n - 1}
    for start in range(0, n, bucket_size):
        bucket = rows[start : start + bucket_size]
        closes = [float(r["close"]) for r in bucket]
        keep.add(start + closes.index(min(closes)))
        keep.add(start + closes.index(max(closes)))
    return [rows[i] for i in sorted(keep)]


def security_prices(
    ctx: Context,
    security_id: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    adjusted: bool = True,
    interval: str = "1d",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """OHLC/adjustment series for one security; raises ``KeyError`` if unknown.

    Returns ``{"rows", "total", "downsampled", "source_rows", "interval"}``.
    ``adjusted=True`` adds ``adj_open/adj_high/adj_low/adj_close`` =
    ``price * adj_factor`` (the split-continuous basis from ``frames.py``).
    """
    if interval not in PRICE_INTERVALS:
        raise ValueError(f"unknown interval {interval!r}; allowed: {list(PRICE_INTERVALS)}")
    # Existence check (also gives a clean 404 before querying prices).
    security_detail(ctx, security_id)

    conds = [SecurityDaily.security_id == security_id]
    if from_date is not None:
        conds.append(SecurityDaily.session_date >= from_date)
    if to_date is not None:
        conds.append(SecurityDaily.session_date <= to_date)
    raw = _all(
        ctx,
        select(SecurityDaily).where(and_(*conds)).order_by(SecurityDaily.session_date.asc()),
    )
    source_rows = len(raw)

    rows: list[dict[str, Any]] = []
    for r in raw:
        row: dict[str, Any] = {
            "session_date": r["session_date"],
            "open": _to_float(r["open"]),
            "high": _to_float(r["high"]),
            "low": _to_float(r["low"]),
            "close": _to_float(r["close"]),
            "adj_factor": r["adj_factor"],
            "cash_dividend": _to_float(r["cash_dividend"]),
            "tr_index": r["tr_index"],
            "is_halted": bool(r["is_halted"]),
        }
        if adjusted:
            adj = float(r["adj_factor"])
            row["adj_open"] = row["open"] * adj if row["open"] is not None else None
            row["adj_high"] = row["high"] * adj if row["high"] is not None else None
            row["adj_low"] = row["low"] * adj if row["low"] is not None else None
            row["adj_close"] = row["close"] * adj if row["close"] is not None else None
        rows.append(row)

    series = _resample_interval(rows, interval)
    downsampled = False
    if len(series) > PRICE_DOWNSAMPLE_TARGET:
        series = _bucket_extrema(series, PRICE_DOWNSAMPLE_TARGET)
        downsampled = True

    total = len(series)
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    page = series[offset : offset + limit]
    return {
        "rows": page,
        "total": total,
        "downsampled": downsampled,
        "source_rows": source_rows,
        "interval": interval,
    }


# --------------------------------------------------------------------------- #
# Ratings / consensus / performance for one security
# --------------------------------------------------------------------------- #


def _date_conds(column: Any, from_date: date | None, to_date: date | None) -> list[Any]:
    conds: list[Any] = []
    if from_date is not None:
        conds.append(func.date(column) >= from_date)
    if to_date is not None:
        conds.append(func.date(column) <= to_date)
    return conds


def security_ratings(
    ctx: Context,
    security_id: str,
    *,
    run_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Rating events for a security, optionally joined to run interval outcomes.

    With ``run_id`` each row gains ``interval_id``, entry/exit sessions,
    ``exit_reason``, ``is_investable``, ``excess_return``/``stock_return``
    (``to_exit`` horizon) and ``contribution`` — all ``None`` when the event
    produced no interval in that run.
    """
    security_detail(ctx, security_id)  # 404 on unknown security
    if run_id is not None:
        get_run_or_404(ctx, run_id)

    ev = RatingEvent.__table__
    stmt = (
        select(
            ev.c.event_id,
            ev.c.analyst_id,
            Analyst.name.label("analyst_name"),
            ev.c.firm_id,
            ev.c.security_id,
            ev.c.action_type,
            ev.c.native_rating,
            ev.c.canonical_rating,
            ev.c.prior_canonical_rating,
            ev.c.announced_ts,
            ev.c.effective_ts,
            ev.c.knowledge_ts,
            ev.c.is_correction,
            ev.c.price_target,
            ev.c.pt_currency,
            ev.c.pt_horizon_months,
            ev.c.expiry_date,
            ev.c.scale_id,
            ev.c.source,
        )
        .select_from(ev)
        .outerjoin(Analyst.__table__, Analyst.analyst_id == ev.c.analyst_id)
        .where(ev.c.security_id == security_id)
    )
    wheres = _date_conds(ev.c.announced_ts, from_date, to_date)
    if wheres:
        stmt = stmt.where(and_(*wheres))

    interval_cols: list[Any] = []
    if run_id is not None:
        ri = RatingInterval.__table__
        ret = IntervalReturn.__table__
        con = IntervalContribution.__table__
        stmt = (
            stmt.outerjoin(
                ri,
                and_(ri.c.event_id == ev.c.event_id, ri.c.run_id == run_id),
            )
            .outerjoin(
                ret,
                and_(
                    ret.c.run_id == ri.c.run_id,
                    ret.c.interval_id == ri.c.interval_id,
                    ret.c.horizon == HORIZON_TO_EXIT,
                ),
            )
            .outerjoin(
                con,
                and_(
                    con.c.run_id == ri.c.run_id,
                    con.c.interval_id == ri.c.interval_id,
                    con.c.horizon == HORIZON_TO_EXIT,
                ),
            )
        )
        interval_cols = [
            ri.c.interval_id,
            ri.c.entry_session,
            ri.c.entry_point,
            ri.c.exit_session,
            ri.c.exit_point,
            ri.c.exit_reason,
            ri.c.is_investable,
            ret.c.stock_return,
            ret.c.benchmark_return,
            ret.c.excess_return,
            ret.c.benchmark_id,
            con.c.position_weight,
            con.c.contribution,
        ]
        stmt = stmt.add_columns(*interval_cols)

    count_stmt = select(func.count()).select_from(ev).where(ev.c.security_id == security_id)
    if wheres:
        count_stmt = count_stmt.where(and_(*wheres))
    total = int(_scalar(ctx, count_stmt) or 0)

    stmt = stmt.order_by(ev.c.announced_ts.asc(), ev.c.event_id.asc())
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    rows = _all(ctx, stmt.limit(limit).offset(offset))

    for row in rows:
        row["price_target"] = _to_float(row.get("price_target"))
        for key in ("stock_return", "benchmark_return", "excess_return", "contribution"):
            if key in row:
                row[key] = _to_float(row[key])
    return rows, total


def security_consensus(
    ctx: Context,
    security_id: str,
    *,
    run_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict[str, Any]:
    """``consensus_daily`` series plus run-scoped ``target_outcome`` rows.

    ``target_outcomes`` is populated only when ``run_id`` is given (the table
    is run-scoped); it joins through ``rating_event`` for this security.
    """
    security_detail(ctx, security_id)
    if run_id is not None:
        get_run_or_404(ctx, run_id)

    cd = ConsensusDaily.__table__
    conds = [cd.c.security_id == security_id]
    if from_date is not None:
        conds.append(cd.c.session_date >= from_date)
    if to_date is not None:
        conds.append(cd.c.session_date <= to_date)
    consensus = _all(
        ctx,
        select(cd).where(and_(*conds)).order_by(cd.c.session_date.asc()),
    )
    for row in consensus:
        row["target_price"] = _to_float(row.get("target_price"))

    outcomes: list[dict[str, Any]] = []
    if run_id is not None:
        to_ = TargetOutcome.__table__
        ev = RatingEvent.__table__
        rows = _all(
            ctx,
            select(
                to_.c.event_id,
                ev.c.analyst_id,
                ev.c.canonical_rating,
                ev.c.action_type,
                ev.c.announced_ts,
                to_.c.entry_session,
                to_.c.horizon_end_session,
                to_.c.entry_price,
                to_.c.target_price,
                to_.c.implied_return,
                to_.c.realized_return,
                to_.c.hit,
                to_.c.is_matured,
            )
            .select_from(to_)
            .join(ev, ev.c.event_id == to_.c.event_id)
            .where(and_(to_.c.run_id == run_id, ev.c.security_id == security_id))
            .order_by(to_.c.entry_session.asc(), to_.c.event_id.asc()),
        )
        outcomes = rows

    return {"consensus": consensus, "target_outcomes": outcomes}


def security_performance(ctx: Context, security_id: str, run_id: str) -> dict[str, Any]:
    """Aggregate interval stats for one security within a run.

    ``n_ratings`` counts intervals (one per rating event), ``n_investable``
    those with a weight, ``mean_excess_return``/``hit_rate`` use the
    ``to_exit`` horizon, ``mean_contribution`` the ``to_exit`` contribution,
    ``target_hit_rate`` matured ``target_outcome.hit`` rows. ``by_horizon``
    breaks returns down per fixed horizon. All values reconcile with the
    stored derived rows.
    """
    security_detail(ctx, security_id)
    run = get_run_or_404(ctx, run_id)

    ri = RatingInterval.__table__
    intervals = _all(
        ctx,
        select(
            ri.c.interval_id,
            ri.c.event_id,
            ri.c.analyst_id,
            ri.c.canonical_rating,
            ri.c.action_type,
            ri.c.announced_ts,
            ri.c.is_investable,
            ri.c.exit_reason,
        )
        .where(and_(ri.c.run_id == run_id, ri.c.security_id == security_id))
        .order_by(ri.c.announced_ts.asc(), ri.c.interval_id.asc()),
    )
    interval_ids = [r["interval_id"] for r in intervals]

    ret = IntervalReturn.__table__
    returns: list[dict[str, Any]] = []
    if interval_ids:
        returns = _all(
            ctx,
            select(ret)
            .where(and_(ret.c.run_id == run_id, ret.c.interval_id.in_(interval_ids)))
            .order_by(ret.c.interval_id.asc(), ret.c.horizon.asc()),
        )

    con = IntervalContribution.__table__
    contributions: list[dict[str, Any]] = []
    if interval_ids:
        contributions = _all(
            ctx,
            select(con)
            .where(
                and_(
                    con.c.run_id == run_id,
                    con.c.interval_id.in_(interval_ids),
                    con.c.horizon == HORIZON_TO_EXIT,
                )
            )
            .order_by(con.c.interval_id.asc()),
        )

    to_ = TargetOutcome.__table__
    event_ids = [r["event_id"] for r in intervals]
    outcomes: list[dict[str, Any]] = []
    if event_ids:
        outcomes = _all(
            ctx,
            select(to_)
            .where(and_(to_.c.run_id == run_id, to_.c.event_id.in_(event_ids)))
            .order_by(to_.c.event_id.asc()),
        )

    to_exit = {r["interval_id"]: r for r in returns if r["horizon"] == HORIZON_TO_EXIT}
    contrib = {r["interval_id"]: r for r in contributions}

    excess: list[float] = []
    for i in intervals:
        if i["interval_id"] in to_exit:
            v = _to_float(to_exit[i["interval_id"]]["excess_return"])
            if v is not None:
                excess.append(v)
    contribs: list[float] = []
    for i in intervals:
        if i["interval_id"] in contrib:
            v = _to_float(contrib[i["interval_id"]]["contribution"])
            if v is not None:
                contribs.append(v)
    hits = [bool(o["hit"]) for o in outcomes if o["hit"] is not None]

    by_horizon: dict[str, dict[str, Any]] = {}
    for r in returns:
        h = str(r["horizon"])
        bucket = by_horizon.setdefault(
            h,
            {
                "horizon": h,
                "n": 0,
                "stock_return": {"sum": 0.0, "n": 0},
                "benchmark_return": {"sum": 0.0, "n": 0},
                "excess_return": {"sum": 0.0, "n": 0},
            },
        )
        bucket["n"] += 1
        for metric in ("stock_return", "benchmark_return", "excess_return"):
            v = _to_float(r[metric])
            if v is not None:
                bucket[metric]["sum"] += v
                bucket[metric]["n"] += 1
    horizon_rows = []
    for h in sorted(by_horizon):
        bucket = by_horizon[h]
        horizon_rows.append(
            {
                "horizon": h,
                "n": bucket["n"],
                **{
                    metric: (
                        bucket[metric]["sum"] / bucket[metric]["n"] if bucket[metric]["n"] else None
                    )
                    for metric in ("stock_return", "benchmark_return", "excess_return")
                },
                **{
                    f"{metric}_n": bucket[metric]["n"]
                    for metric in ("stock_return", "benchmark_return", "excess_return")
                },
            }
        )

    return {
        "run": run_provenance(ctx, run),
        "security_id": security_id,
        "n_ratings": len(intervals),
        "n_investable": sum(1 for i in intervals if i["is_investable"]),
        "mean_excess_return": (sum(excess) / len(excess)) if excess else None,
        "hit_rate": (sum(1 for v in excess if v > 0) / len(excess)) if excess else None,
        "mean_contribution": (sum(contribs) / len(contribs)) if contribs else None,
        "target_hit_rate": (sum(1 for h in hits if h) / len(hits)) if hits else None,
        "n_target_outcomes": len(outcomes),
        "by_horizon": horizon_rows,
    }


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

_SCORE_BUCKETS = 10


def _latest_run(ctx: Context) -> dict[str, Any] | None:
    """Most recent succeeded run (by ``finished_at``), else most recent run."""
    rows = _all(
        ctx,
        select(CalcRun)
        .where(CalcRun.status == RunStatus.SUCCEEDED.value)
        .order_by(CalcRun.finished_at.desc())
        .limit(1),
    )
    if rows:
        return rows[0]
    rows = _all(
        ctx,
        select(CalcRun).order_by(CalcRun.as_of_date.desc(), CalcRun.run_id.desc()).limit(1),
    )
    return rows[0] if rows else None


def _score_distribution(
    scores: Sequence[float], buckets: int = _SCORE_BUCKETS
) -> list[dict[str, Any]]:
    """Equal-width histogram over observed min..max; deterministic labels."""
    vals = sorted(v for v in scores if v is not None)
    if not vals:
        return []
    lo, hi = vals[0], vals[-1]
    if hi == lo:
        return [{"bucket": f"{lo:.6f}", "low": lo, "high": hi, "count": len(vals)}]
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in vals:
        idx = min(int((v - lo) / width), buckets - 1)
        counts[idx] += 1
    return [
        {
            "bucket": f"{lo + i * width:.6f}-{lo + (i + 1) * width:.6f}",
            "low": lo + i * width,
            "high": lo + (i + 1) * width,
            "count": counts[i],
        }
        for i in range(buckets)
    ]


def overview(ctx: Context, run_id: str | None = None) -> dict[str, Any]:
    """Dashboard aggregates; every count is a direct SQL aggregate.

    With ``run_id=None`` the latest succeeded run is used (falling back to the
    latest run of any status); analyst/score sections are empty when the
    resolved run has no composite rows.
    """
    run = get_run_or_404(ctx, run_id) if run_id is not None else _latest_run(ctx)

    analysts_block: dict[str, Any] = {
        "n_analysts": 0,
        "n_ranked": 0,
        "n_scored": 0,
        "score_distribution": [],
        "top": [],
        "bottom": [],
    }
    if run is not None:
        rid = str(run["run_id"])
        comp = _all(
            ctx,
            select(AnalystComposite)
            .where(AnalystComposite.run_id == rid)
            .order_by(AnalystComposite.analyst_id.asc()),
        )
        scores = [c["composite_score"] for c in comp if c["composite_score"] is not None]
        ranked = [c for c in comp if c["is_ranked"]]
        scored_sorted = sorted(
            (c for c in comp if c["composite_score"] is not None),
            key=lambda c: (-(c["composite_score"] or 0.0), str(c["analyst_id"])),
        )
        analysts_block = {
            "n_analysts": len(comp),
            "n_ranked": len(ranked),
            "n_scored": len(scores),
            "score_distribution": _score_distribution([float(s) for s in scores]),
            "top": scored_sorted[:5],
            "bottom": list(reversed(scored_sorted[-5:])),
        }

    run_counts = _all(
        ctx,
        select(CalcRun.status, func.count().label("n"))
        .group_by(CalcRun.status)
        .order_by(CalcRun.status.asc()),
    )

    sess = _all(
        ctx,
        select(
            func.count().label("n_sessions"),
            func.min(TradingSession.session_date).label("first_session"),
            func.max(TradingSession.session_date).label("last_session"),
            func.count(func.distinct(TradingSession.exchange_id)).label("n_exchanges"),
        ),
    )[0]
    n_securities = int(_scalar(ctx, select(func.count()).select_from(Security.__table__)) or 0)
    n_delisted_securities = int(
        _scalar(ctx, select(func.count()).select_from(Delisting.__table__)) or 0
    )
    n_events = int(_scalar(ctx, select(func.count()).select_from(RatingEvent.__table__)) or 0)
    latest_event = _all(
        ctx,
        select(func.max(RatingEvent.announced_ts).label("latest_event_ts")),
    )[0]["latest_event_ts"]
    n_consensus = int(_scalar(ctx, select(func.count()).select_from(ConsensusDaily.__table__)) or 0)
    latest_consensus = _all(
        ctx,
        select(func.max(ConsensusDaily.session_date).label("d")).select_from(
            ConsensusDaily.__table__
        ),
    )[0]["d"]
    n_price_rows = int(_scalar(ctx, select(func.count()).select_from(SecurityDaily.__table__)) or 0)

    return {
        "run": run_provenance(ctx, run) if run is not None else None,
        "analysts": analysts_block,
        "run_counts_by_status": {str(r["status"]): int(r["n"]) for r in run_counts},
        "data_coverage": {
            "n_sessions": sess["n_sessions"],
            "first_session": sess["first_session"],
            "last_session": sess["last_session"],
            "n_exchanges": sess["n_exchanges"],
            "n_securities": n_securities,
            "n_delisted_securities": n_delisted_securities,
            "n_rating_events": n_events,
            "latest_event_ts": latest_event,
            "n_consensus_rows": n_consensus,
            "latest_consensus_date": latest_consensus,
            "n_security_daily_rows": n_price_rows,
        },
        "provenance": provenance(ctx),
    }


# --------------------------------------------------------------------------- #
# Sectors
# --------------------------------------------------------------------------- #


def _sector_security_counts(ctx: Context, as_of: date | None) -> dict[str, dict[str, int]]:
    """Per-sector security/analyst/delisted counts via latest classification."""
    latest = _latest_classification_subquery(as_of)
    cls = SecurityClassification.__table__
    join_cond = and_(cls.c.security_id == latest.c.security_id, cls.c.from_date == latest.c.mf)

    sec_rows = _all(
        ctx,
        select(cls.c.sector_id, func.count(func.distinct(cls.c.security_id)).label("n"))
        .select_from(cls)
        .join(latest, join_cond)
        .group_by(cls.c.sector_id),
    )
    counts = {
        str(r["sector_id"]): {
            "security_count": int(r["n"]),
            "analyst_count": 0,
            "delisted_count": 0,
        }
        for r in sec_rows
    }

    # Analysts covering each sector's securities (any coverage spell).
    analyst_rows = _all(
        ctx,
        select(cls.c.sector_id, func.count(func.distinct(AnalystCoverage.analyst_id)).label("n"))
        .select_from(cls)
        .join(latest, join_cond)
        .join(
            AnalystCoverage.__table__,
            AnalystCoverage.security_id == cls.c.security_id,
        )
        .group_by(cls.c.sector_id),
    )
    for r in analyst_rows:
        key = str(r["sector_id"])
        if key in counts:
            counts[key]["analyst_count"] = int(r["n"])

    delist_rows = _all(
        ctx,
        select(cls.c.sector_id, func.count(func.distinct(Delisting.security_id)).label("n"))
        .select_from(cls)
        .join(latest, join_cond)
        .join(Delisting.__table__, Delisting.security_id == cls.c.security_id)
        .group_by(cls.c.sector_id),
    )
    for r in delist_rows:
        key = str(r["sector_id"])
        if key in counts:
            counts[key]["delisted_count"] = int(r["n"])
    return counts


def list_sectors(ctx: Context, *, as_of: date | None = None) -> list[dict[str, Any]]:
    """All sectors with security/analyst/delisted counts (PIT via ``as_of``)."""
    sectors = _all(
        ctx,
        select(Sector.sector_id, Sector.name, Sector.parent_sector_id).order_by(
            Sector.name.asc(), Sector.sector_id.asc()
        ),
    )
    counts = _sector_security_counts(ctx, as_of)
    out = []
    for s in sectors:
        c = counts.get(str(s["sector_id"]), {})
        out.append(
            {
                **s,
                "security_count": c.get("security_count", 0),
                "analyst_count": c.get("analyst_count", 0),
                "delisted_count": c.get("delisted_count", 0),
            }
        )
    return out


def sector_performance(ctx: Context, sector_id: str, run_id: str) -> dict[str, Any]:
    """Sector aggregates within a run.

    "Analysts in sector" = analysts with an ``analyst_coverage`` spell on a
    security whose latest classification (as of the run's ``as_of_date``)
    belongs to ``sector_id``; the composite stats are taken from that run's
    ``analyst_composite`` rows only. ``coverage`` reports how many sector
    securities have price data.
    """
    sector = _all(ctx, select(Sector).where(Sector.sector_id == sector_id))
    if not sector:
        raise KeyError(f"sector {sector_id!r} not found")
    run = get_run_or_404(ctx, run_id)
    as_of = run["as_of_date"]
    if not isinstance(as_of, date):
        raise TypeError(f"calc_run.as_of_date has unexpected type {type(as_of)}")

    latest = _latest_classification_subquery(as_of)
    cls = SecurityClassification.__table__
    join_cond = and_(cls.c.security_id == latest.c.security_id, cls.c.from_date == latest.c.mf)
    sec_rows = _all(
        ctx,
        select(cls.c.security_id, cls.c.delisting_date)
        .select_from(cls)
        .join(latest, join_cond)
        .where(cls.c.sector_id == sector_id),
    )
    sec_ids = [str(r["security_id"]) for r in sec_rows]

    n_delisted = 0
    if sec_ids:
        dl = _delisted_ids(ctx, sec_ids)
        n_delisted = len(dl) + sum(
            1
            for r in sec_rows
            if str(r["security_id"]) not in dl and r["delisting_date"] is not None
        )

    n_with_prices = 0
    if sec_ids:
        n_with_prices = int(
            _scalar(
                ctx,
                select(func.count(func.distinct(SecurityDaily.security_id))).where(
                    SecurityDaily.security_id.in_(sec_ids)
                ),
            )
            or 0
        )

    # Analysts covering sector securities, restricted to this run's composites.
    comp_rows: list[dict[str, Any]] = []
    if sec_ids:
        comp_rows = _all(
            ctx,
            select(AnalystComposite)
            .where(
                and_(
                    AnalystComposite.run_id == run_id,
                    AnalystComposite.analyst_id.in_(
                        select(func.distinct(AnalystCoverage.analyst_id))
                        .select_from(AnalystCoverage.__table__)
                        .where(AnalystCoverage.security_id.in_(sec_ids))
                    ),
                )
            )
            .order_by(AnalystComposite.rank_overall.is_(None), AnalystComposite.rank_overall),
        )
    scores = [float(c["composite_score"]) for c in comp_rows if c["composite_score"] is not None]

    return {
        "sector": sector[0],
        "run": run_provenance(ctx, run),
        "n_securities": len(sec_ids),
        "n_delisted": n_delisted,
        "n_with_prices": n_with_prices,
        "n_analysts": len(comp_rows),
        "n_ranked": sum(1 for c in comp_rows if c["is_ranked"]),
        "mean_composite_score": (sum(scores) / len(scores)) if scores else None,
        "composite_score_distribution": _score_distribution(scores, buckets=5),
    }


# --------------------------------------------------------------------------- #
# Changes / audit log
# --------------------------------------------------------------------------- #


def _validate_change_type(change_type: str | None) -> str | None:
    if change_type is None:
        return None
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"unknown change type {change_type!r}; allowed: {sorted(CHANGE_TYPES)}")
    return change_type


def list_changes(
    ctx: Context,
    *,
    run_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    change_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Audit/change log over rating events and run lifecycle.

    Rating rows carry ``type`` = ``action_type`` (initiation/upgrade/.../drop),
    ``old_rating``/``new_rating`` = prior->canonical, and ``is_correction``.
    ``type='correction'`` filters to correction events only. Run rows carry
    ``type`` in ``run_created|run_succeeded|run_failed|run_interrupted``.

    ``run_id`` scopes BOTH sources: rating events must have produced an
    interval in that run, and only that run's lifecycle rows are returned.
    ``calc_run`` has no ``created_at`` column, so ``run_created`` is stamped
    with ``started_at`` (documented approximation — see handoff).
    """
    change_type = _validate_change_type(change_type)
    if run_id is not None:
        get_run_or_404(ctx, run_id)

    rating_rows: list[dict[str, Any]] = []
    rating_total = 0
    want_ratings = (
        change_type is None
        or change_type == "correction"
        or change_type in {a.value for a in RatingAction}
    )
    if want_ratings:
        ev = RatingEvent.__table__
        stmt = (
            select(
                ev.c.event_id,
                ev.c.analyst_id,
                Analyst.name.label("analyst_name"),
                ev.c.security_id,
                Security.name.label("security_name"),
                ev.c.action_type,
                ev.c.canonical_rating,
                ev.c.prior_canonical_rating,
                ev.c.announced_ts,
                ev.c.is_correction,
                ev.c.firm_id,
            )
            .select_from(ev)
            .outerjoin(Analyst.__table__, Analyst.analyst_id == ev.c.analyst_id)
            .outerjoin(Security.__table__, Security.security_id == ev.c.security_id)
            .where(ev.c.security_id.is_not(None))
        )
        wheres = _date_conds(ev.c.announced_ts, from_date, to_date)
        if change_type == "correction":
            wheres.append(ev.c.is_correction.is_(True))
        elif change_type is not None:
            wheres.append(ev.c.action_type == change_type)
        if run_id is not None:
            ri = RatingInterval.__table__
            wheres.append(
                select(literal(1))
                .select_from(ri)
                .where(and_(ri.c.run_id == run_id, ri.c.event_id == ev.c.event_id))
                .exists()
            )
        count_stmt = select(func.count()).select_from(ev)
        if wheres:
            stmt = stmt.where(and_(*wheres))
            count_stmt = count_stmt.where(and_(*wheres))
        rating_total = int(_scalar(ctx, count_stmt) or 0)
        rating_rows = _all(ctx, stmt.order_by(ev.c.announced_ts.asc(), ev.c.event_id.asc()))

    run_rows: list[dict[str, Any]] = []
    run_total = 0
    want_runs = change_type is None or change_type in RUN_CHANGE_TYPES
    if want_runs:
        cr = CalcRun.__table__
        run_conds = []
        if run_id is not None:
            run_conds.append(cr.c.run_id == run_id)
        all_runs = _all(
            ctx,
            select(CalcRun).where(and_(*run_conds)) if run_conds else select(CalcRun),
        )
        from_d = datetime.combine(from_date, time.min, tzinfo=UTC) if from_date else None
        to_d_excl = (
            datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC) if to_date else None
        )

        def _in_window(ts: Any) -> bool:
            if ts is None:
                return False
            if from_d is not None and ts < from_d:
                return False
            return not (to_d_excl is not None and ts >= to_d_excl)

        for r in all_runs:
            rid = str(r["run_id"])
            status = str(r["status"])
            candidates: list[tuple[str, Any]] = [("run_created", r["started_at"])]
            if status == RunStatus.SUCCEEDED.value:
                candidates.append(("run_succeeded", r["finished_at"]))
            elif status == RunStatus.FAILED.value:
                candidates.append(("run_failed", r["finished_at"]))
            elif status == RunStatus.INTERRUPTED.value:
                candidates.append(("run_interrupted", r["finished_at"]))
            for ctype, ts in candidates:
                if change_type is not None and ctype != change_type:
                    continue
                if not _in_window(ts):
                    continue
                run_total += 1
                run_rows.append(
                    {
                        "change_id": f"{rid}:{ctype}",
                        "kind": "run",
                        "type": ctype,
                        "ts": ts,
                        "run_id": rid,
                        "status": status,
                        "detail": r.get("error_json"),
                    }
                )

    merged: list[dict[str, Any]] = []
    for r in rating_rows:
        merged.append(
            {
                "change_id": str(r["event_id"]),
                "kind": "rating",
                "type": str(r["action_type"]),
                "ts": r["announced_ts"],
                "run_id": run_id,
                "analyst_id": r["analyst_id"],
                "analyst_name": r.get("analyst_name"),
                "firm_id": r.get("firm_id"),
                "security_id": r["security_id"],
                "security_name": r.get("security_name"),
                "old_rating": r.get("prior_canonical_rating"),
                "new_rating": r["canonical_rating"],
                "is_correction": bool(r["is_correction"]),
            }
        )
    merged.extend(run_rows)
    # Stable sort: newest first; ``change_id`` breaks ties deterministically.
    merged.sort(
        key=lambda r: (
            r["ts"] is not None,
            r["ts"] or datetime.min.replace(tzinfo=UTC),
            str(r["change_id"]),
        ),
        reverse=True,
    )

    total = rating_total + run_total
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    return merged[offset : offset + limit], total


__all__ = [
    "CHANGE_TYPES",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PRICE_DOWNSAMPLE_TARGET",
    "PRICE_INTERVALS",
    "RUN_CHANGE_TYPES",
    "clamp_limit",
    "clamp_offset",
    "csv_response",
    "get_engine",
    "get_run_or_404",
    "list_changes",
    "list_sectors",
    "overview",
    "provenance",
    "resolve_db_path",
    "rows_to_csv",
    "run_provenance",
    "search_securities",
    "sector_performance",
    "security_consensus",
    "security_detail",
    "security_performance",
    "security_prices",
    "security_ratings",
]
