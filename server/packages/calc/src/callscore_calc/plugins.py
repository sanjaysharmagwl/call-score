"""The plugin API: how analyses are built on top of the core calculation engine.

The core engine filters, derives, groups, aggregates, rolls up, pivots, sorts and pages. Everything
else is a plugin: a class that registers what it adds when the engine is built::

    engine = CalcEngine(catalog, plugins=[WhatIfPlugin(store=InMemoryScenarioStore())])

A plugin can add

* **functions** (:class:`FunctionDef`) usable in every formula, and **aggregates**
  (:class:`AggregateDef`) usable as a measure ``fn``;
* a **transform** (:class:`TransformDef`) that changes the dataset before the query runs. Requests
  switch it on with a block under ``extensions`` named after the transform, e.g.
  ``{"extensions": {"whatif": {"steps": [...]}}}``;
* **operations** (:class:`OperationDef`), new calls on the engine (``engine.call(name, request)``)
  built from the :class:`~callscore_calc.engine.Kernel`;
* **HTTP routes**, added to the FastAPI router by
  :func:`~callscore_calc.integrations.fastapi.create_router`.

Every computation is defined twice: once for Polars and once in plain Python for the reference
evaluator (:mod:`callscore_calc.verify`). Keep the two in agreement; ``verify()`` and property tests
compare them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal

import polars as pl
from pydantic import BaseModel

from callscore_calc.dtypes import LType

if TYPE_CHECKING:
    from callscore_calc.catalog import Dataset
    from callscore_calc.config import CalcContext, Limits
    from callscore_calc.dtypes import NumericConfig
    from callscore_calc.engine import CalcEngine, Kernel
    from callscore_calc.integrations.fastapi import RouterKit
    from callscore_calc.schema import DatasetSchema
    from callscore_calc.spec.query import DatasetRef

ENTRY_POINT_GROUP = "callscore_calc.plugins"
Row = dict[str, Any]


class PluginError(ValueError):
    """A plugin is misconfigured (e.g. two plugins register the same name)."""


# --- Functions and aggregates -----------------------------------------------------------------


@dataclass(frozen=True)
class FunctionDef:
    """A formula function, e.g. ``clip(x, lo, hi)``.

    ``typecheck`` gets the argument types and returns the result type (raise ``ValueError`` or
    ``TypeError`` to reject them). ``polars`` builds the expression from the argument
    expressions, each already cast to its own type; ``reference`` computes one value from the
    argument values (``Decimal`` for decimals, ``float``, ``int``, ``str``, ``date``...). Both
    results are cast to the declared type; non-finite floats become null. With
    ``nulls="propagate"`` (the default) a null argument makes the result null without calling
    either implementation.
    """

    name: str
    typecheck: Callable[[Sequence[LType]], LType]
    polars: Callable[..., pl.Expr]
    reference: Callable[..., Any]
    min_args: int = 1
    max_args: int | None = 1
    nulls: Literal["propagate", "pass"] = "propagate"
    description: str = ""

    def arity_text(self) -> str:
        low, high = self.min_args, self.max_args
        return f"{low}" if low == high else f"{low}+" if high is None else f"{low}-{high}"


@dataclass(frozen=True)
class AggregateDef:
    """A measure function, e.g. ``{"fn": "p95", "of": "pnl"}``.

    ``typecheck`` maps the type of ``of`` to the result type. ``polars`` aggregates one group's
    non-null values (it gets ``pl.col(...).drop_nulls()``); ``reference`` does the same for a
    non-empty list of values. A group without values is null. The aggregate runs on the rows of
    every rollup level and pivot cell, so it need not be decomposable (medians and quantiles are
    fine).
    """

    name: str
    typecheck: Callable[[LType], LType]
    polars: Callable[[pl.Expr], pl.Expr]
    reference: Callable[[list[Any]], Any]
    description: str = ""


# --- Transforms -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BindContext:
    """What a transform sees when a request is resolved (before the dataset is loaded)."""

    kernel: Kernel
    dataset: DatasetRef  # as requested; a transform may pin a version (``Bound.pinned_version``)
    ctx: CalcContext
    path: str  # JSON pointer of the transform's block, for error paths


@dataclass(frozen=True)
class PlanContext:
    """What a transform sees when it is planned against a dataset version."""

    kernel: Kernel
    dataset: Dataset
    schema: DatasetSchema  # the dataset's columns as the caller may see them
    ctx: CalcContext
    path: str
    deadline: float | None

    @property
    def numeric(self) -> NumericConfig:
        return self.kernel.config.numeric

    @property
    def limits(self) -> Limits:
        return self.kernel.config.limits

    @property
    def registry(self) -> Registry:
        return self.kernel.registry

    def collect(self, lf: pl.LazyFrame) -> pl.DataFrame:
        """Run a (small) query under the engine's concurrency limit and the request deadline."""
        return self.kernel.collect(lf, engine="in-memory", deadline=self.deadline)


class TransformPlan(ABC):
    """A validated transform, ready to run. The engine caches it (see :class:`Bound`).

    ``env`` is the column types after the transform; ``canonical`` is its JSON identity (part of
    every result fingerprint), a list of ``{"kind": ...}`` entries.
    """

    env: dict[str, LType]
    canonical: list[Any]

    @abstractmethod
    def apply(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        """The Polars implementation."""

    @abstractmethod
    def apply_reference(self, rows: list[Row]) -> list[Row]:
        """The pure-Python implementation (used by :func:`callscore_calc.verify.verify`)."""

    def meta(self) -> dict[str, Any]:
        """JSON data reported in ``ResultMeta.extensions[<transform>]``."""
        return {}

    def explain(self) -> dict[str, Any]:
        """JSON data reported by ``engine.explain`` under ``extensions.<transform>``."""
        return {}

    def size(self) -> int:
        """Approximate bytes, for the plan cache."""
        return 1024


class Bound(ABC):
    """A transform block resolved for one request (cheap: no dataset access yet).

    The engine caches the plan under ``identity`` (plus the dataset version, the caller's
    context and the transforms before it), so ``identity`` must capture everything ``plan``
    depends on.
    """

    pinned_version: str | None = None  # the dataset version this transform requires, if any

    @property
    @abstractmethod
    def identity(self) -> Any: ...

    @abstractmethod
    def plan(self, env: Mapping[str, LType], frame: pl.LazyFrame, pc: PlanContext) -> TransformPlan:
        """Validate against the incoming columns ``env`` (``frame`` is the incoming data)."""


@dataclass(frozen=True)
class TransformDef:
    """A dataset transform, enabled per request by ``extensions[name]``.

    ``model`` parses the block; ``bind`` resolves it (e.g. loads a saved scenario). Transforms
    run in the order plugins were registered, after the caller's row filter and column
    restrictions and before the query.
    """

    name: str
    model: type[BaseModel]
    bind: Callable[[Any, BindContext], Bound]
    description: str = ""


# --- Operations and routes --------------------------------------------------------------------


@dataclass(frozen=True)
class OperationDef:
    """A new engine call: ``engine.call(name, request, ctx)`` parses ``request`` with
    ``request_model`` and returns ``run(kernel, parsed, ctx)``.

    ``run`` is responsible for taking an executor slot (``with kernel.slot():``) around its
    computation; it must not call ``engine.run`` while holding one.
    """

    name: str
    request_model: type[BaseModel]
    run: Callable[[Kernel, Any, CalcContext], Any]
    description: str = ""


RoutesHook = Callable[[Any, "RouterKit"], None]
"""``hook(router, kit)``: add routes to the FastAPI ``APIRouter`` (see ``RouterKit``)."""


# --- Registry and plugins ---------------------------------------------------------------------


@dataclass
class Registry:
    """Everything the engine's plugins registered. Names are unique across plugins."""

    functions: dict[str, FunctionDef] = field(default_factory=dict)
    aggregates: dict[str, AggregateDef] = field(default_factory=dict)
    transforms: dict[str, TransformDef] = field(default_factory=dict)
    operations: dict[str, OperationDef] = field(default_factory=dict)
    routes: list[RoutesHook] = field(default_factory=list)
    owners: dict[tuple[str, str], str] = field(default_factory=dict)
    current: str = "core"

    def add_function(self, fdef: FunctionDef) -> None:
        from callscore_calc.spec.expr import FUNC_ARITY, FUNC_NAME_PATTERN

        self._check_name("function", fdef.name, FUNC_NAME_PATTERN, reserved=FUNC_ARITY)
        if fdef.min_args < 0 or (fdef.max_args is not None and fdef.max_args < fdef.min_args):
            raise PluginError(f"function {fdef.name}: invalid argument counts")
        self.functions[fdef.name] = fdef

    def add_aggregate(self, adef: AggregateDef) -> None:
        from callscore_calc.spec.query import AGG_NAME_PATTERN, BUILTIN_AGGS

        self._check_name("aggregate", adef.name, AGG_NAME_PATTERN, reserved=BUILTIN_AGGS)
        self.aggregates[adef.name] = adef

    def add_transform(self, tdef: TransformDef) -> None:
        self._check_name("transform", tdef.name, r"^[a-z][a-z0-9_]*$", reserved=())
        self.transforms[tdef.name] = tdef

    def add_operation(self, odef: OperationDef) -> None:
        reserved = ("run", "compare", "explain", "distinct", "schema")
        self._check_name("operation", odef.name, r"^[a-z][a-z0-9_.]*$", reserved=reserved)
        self.operations[odef.name] = odef

    def add_routes(self, hook: RoutesHook) -> None:
        self.routes.append(hook)

    def _check_name(self, kind: str, name: str, pattern: str, *, reserved: Any) -> None:
        import re

        if not re.match(pattern, name):
            raise PluginError(f"{self.current}: invalid {kind} name {name!r} (need {pattern})")
        if name in reserved:
            raise PluginError(f"{self.current}: {kind} {name!r} is built in")
        owner = self.owners.get((kind, name))
        if owner is not None:
            raise PluginError(f"{self.current}: {kind} {name!r} is already registered by {owner}")
        self.owners[(kind, name)] = self.current


class Plugin:
    """Base class for plugins. Subclasses set ``name`` and ``version`` and override
    :meth:`register`; :meth:`attach` gives them the engine they were installed in.

    ``version`` is folded into cache keys, so bump it whenever results would change.
    """

    name: str = ""
    version: str = "0"

    def register(self, registry: Registry) -> None:
        raise NotImplementedError

    def attach(self, engine: CalcEngine) -> None:  # noqa: B027 - optional hook
        """Called once, after every plugin registered."""


def discover_plugins(group: str = ENTRY_POINT_GROUP) -> list[Plugin]:
    """Instantiate every installed plugin advertised under the ``callscore_calc.plugins`` entry
    point group (each with no arguments), sorted by entry-point name."""
    found = []
    for ep in sorted(entry_points(group=group), key=lambda e: e.name):
        factory = ep.load()
        plugin = factory()
        if not isinstance(plugin, Plugin):
            raise PluginError(f"entry point {ep.name} did not produce a Plugin")
        found.append(plugin)
    return found


def build_registry(plugins: Sequence[Plugin]) -> Registry:
    registry = Registry()
    seen: set[str] = set()
    for plugin in plugins:
        if not plugin.name:
            raise PluginError(f"{type(plugin).__name__} has no name")
        if plugin.name in seen:
            raise PluginError(f"plugin {plugin.name} is installed twice")
        seen.add(plugin.name)
        registry.current = plugin.name
        plugin.register(registry)
    registry.current = "core"
    return registry
