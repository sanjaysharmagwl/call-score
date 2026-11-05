"""Schemas for the backtest endpoints (``/api/backtests``)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from callscore_api.schemas.common import PaginationMeta


class BacktestCreateRequest(BaseModel):
    """Body of ``POST /api/backtests``.

    ``spec`` is validated server-side as ``callscore_engine.backtest.
    BacktestSpec`` (422 on any violation — including the dynamic-selection
    requirement that ``top_n``/``top_quartile`` set
    ``score_lookback_sessions``). The optional top-level ``start_date`` /
    ``end_date`` / ``benchmark_id`` override the same-named spec fields.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=500)
    spec: dict[str, Any]
    start_date: date | None = None
    end_date: date | None = None
    benchmark_id: str | None = None


class BacktestSubmitResponse(BaseModel):
    """``202`` response for ``POST /api/backtests``."""

    backtest_id: str
    status: str


class BacktestErrorInfo(BaseModel):
    """Parsed ``config_json.error`` written on failure."""

    type: str
    message: str


class BacktestSummary(BaseModel):
    """List-row view of a ``backtest``."""

    backtest_id: str
    name: str | None = None
    status: str
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    benchmark_id: str | None = None
    error: BacktestErrorInfo | None = None


class BacktestDetail(BacktestSummary):
    """Full single-backtest view: validated spec + result artifacts."""

    spec: dict[str, Any] | None = None
    monthly_returns: list[dict[str, Any]] | None = None
    attribution: list[dict[str, Any]] | None = None


class BacktestListResponse(BaseModel):
    data: list[BacktestSummary]
    pagination: PaginationMeta


class BacktestNavRow(BaseModel):
    session_date: date
    nav: float
    benchmark_nav: float
    active_return: float | None = None
    gross_exposure: float
    turnover: float
    cost: float
    drawdown: float


class BacktestNavResponse(BaseModel):
    data: list[BacktestNavRow]
    interval: str
    downsampled: bool
    source_rows: int
    total: int


class BacktestStatRow(BaseModel):
    stat_code: str
    value: float | None = None
    std_err: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    detail_json: str | None = None


class BacktestStatsResponse(BaseModel):
    data: list[BacktestStatRow]


class BacktestTradeRow(BaseModel):
    trade_seq: int
    session_date: date
    security_id: str
    side: str
    quantity: float
    price: Decimal | float
    notional: Decimal | float | None = None
    fee: Decimal | float | None = None
    realized_pnl: Decimal | float | None = None


class BacktestTradesResponse(BaseModel):
    data: list[BacktestTradeRow]
    pagination: PaginationMeta
    truncated: bool = False
    """True when the export cap prevented returning all rows."""


class BacktestMonthlyRow(BaseModel):
    year: int
    month: int
    month_return: float
    nav_end: float


class BacktestMonthlyResponse(BaseModel):
    data: list[BacktestMonthlyRow]


class BacktestAttributionRow(BaseModel):
    dimension: str
    group_key: str
    contribution: float


class BacktestAttributionResponse(BaseModel):
    data: list[BacktestAttributionRow]


__all__ = [
    "BacktestAttributionResponse",
    "BacktestAttributionRow",
    "BacktestCreateRequest",
    "BacktestDetail",
    "BacktestErrorInfo",
    "BacktestListResponse",
    "BacktestMonthlyResponse",
    "BacktestMonthlyRow",
    "BacktestNavResponse",
    "BacktestNavRow",
    "BacktestStatRow",
    "BacktestStatsResponse",
    "BacktestSubmitResponse",
    "BacktestSummary",
    "BacktestTradeRow",
    "BacktestTradesResponse",
]
