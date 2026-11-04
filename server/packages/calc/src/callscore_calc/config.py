"""Limits and per-request context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from callscore_calc.spec.canonical import fingerprint
from callscore_calc.spec.expr import Node


@dataclass(frozen=True)
class Limits:
    """Hard caps that keep a single request from exhausting a pod. Exceeding one is a 413."""

    max_expr_depth: int = 64
    max_expr_nodes: int = 5000
    max_measures: int = 200
    max_group_by: int = 32
    max_page_size: int = 50_000
    max_unpaged_rows: int = 100_000
    max_groups: int = 2_000_000
    max_pivot_columns: int = 2_000


@dataclass(frozen=True)
class CalcContext:
    """Who is asking, and what they may see. Supplied by the host service per request.

    ``row_filter`` (a formula or expression) is applied to the dataset before anything else, and
    ``allowed_columns`` hides every other non-key column, so neither can be bypassed by a query.
    ``principal`` identifies the caller to authorization hooks and plugins (e.g. as the author
    of scenario changes).
    """

    principal: str | None = None
    row_filter: str | Node | None = None
    allowed_columns: frozenset[str] | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def row_filter_node(self) -> Node | None:
        if self.row_filter is None or not isinstance(self.row_filter, str):
            return self.row_filter
        from callscore_calc.spec.formula import parse_formula

        return parse_formula(self.row_filter)

    def digest(self) -> str:
        """Fingerprint of everything in the context that changes results (for cache keys)."""
        columns = None if self.allowed_columns is None else sorted(self.allowed_columns)
        return fingerprint({"row_filter": self.row_filter_node(), "columns": columns})
