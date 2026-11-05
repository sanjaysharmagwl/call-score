"""Repository layer: bulk ingest, run bookkeeping, leases, derived persistence."""

from callscore_store.repositories.backtests import (
    get_backtest,
    insert_backtest,
    insert_backtest_results,
    update_backtest_status,
)
from callscore_store.repositories.base import (
    Context,
    iter_rows,
    normalize_rows,
    write_csv,
)
from callscore_store.repositories.derived import STAGE_TABLES, publish_run_results
from callscore_store.repositories.events import DuplicateRowError, insert_rating_events
from callscore_store.repositories.jobs import (
    acquire_writer_lease,
    get_writer_lease,
    heartbeat_writer_lease,
    release_writer_lease,
)
from callscore_store.repositories.market import insert_market
from callscore_store.repositories.reference import insert_reference, known_scale_ids
from callscore_store.repositories.runs import (
    RunTransitionError,
    create_calc_run,
    get_calc_run,
    list_calc_runs,
    register_methodology_version,
    update_run_progress,
    update_run_status,
)

__all__ = [
    "STAGE_TABLES",
    "Context",
    "DuplicateRowError",
    "RunTransitionError",
    "acquire_writer_lease",
    "create_calc_run",
    "get_backtest",
    "get_calc_run",
    "get_writer_lease",
    "heartbeat_writer_lease",
    "insert_backtest",
    "insert_backtest_results",
    "insert_market",
    "insert_rating_events",
    "insert_reference",
    "iter_rows",
    "known_scale_ids",
    "list_calc_runs",
    "normalize_rows",
    "publish_run_results",
    "register_methodology_version",
    "release_writer_lease",
    "update_backtest_status",
    "update_run_progress",
    "update_run_status",
    "write_csv",
]
