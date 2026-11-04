"""The calculation engine: resolve the dataset and plugin transforms, plan, execute, cache.

Typical embedding::

    catalog = Catalog()
    catalog.register_frame("holdings", df, key_columns=["security_id"])
    engine = CalcEngine(catalog, plugins=[...])
    result = engine.run({"dataset": "holdings", "query": {"group_by": ["sector"], ...}})

:class:`CalcEngine` is the public face; :class:`Kernel` holds the machinery (executor, caches,
view resolution, query execution) and is what plugin operations build on.

Every method is synchronous and CPU-bound; call it from a worker thread (FastAPI does this for
plain ``def`` endpoints).
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version as package_version
from typing import Any, TypeVar, overload

import polars as pl
from pydantic import BaseModel, ValidationError

from callscore_calc.cache import ResultCache, frame_size
from callscore_calc.catalog import ROW_INDEX, Dataset, DatasetCatalog
from callscore_calc.compile.compare import compare_frame, plan_compare
from callscore_calc.compile.exprs import compile_expr
from callscore_calc.compile.logical import LEVEL, LogicalQuery, SortSpec, plan_query
from callscore_calc.compile.query import (
    TOTAL,
    Finished,
    agg_frame,
    count_frame,
    domain_frame,
    finish_aggregate,
    leaf_frame,
    rows_frame,
    sort_frame,
    totals_frame,
)
from callscore_calc.compile.validate import check_predicate
from callscore_calc.config import CalcContext, Limits
from callscore_calc.dtypes import LType, NumericConfig
from callscore_calc.errors import CalcError, LimitExceeded, SpecError, VersionConflict, join_path
from callscore_calc.exec import EngineName, Executor
from callscore_calc.plugins import (
    BindContext,
    Bound,
    OperationDef,
    PlanContext,
    Plugin,
    Registry,
    TransformPlan,
    build_registry,
)
from callscore_calc.result import CalcResult, ColumnInfo, ResultMeta
from callscore_calc.schema import DatasetSchema
from callscore_calc.spec.canonical import fingerprint, upgrade
from callscore_calc.spec.query import (
    CalcRequest,
    CompareRequest,
    DatasetRef,
    Options,
    Page,
    Query,
)

M = TypeVar("M", bound=BaseModel)
P = TypeVar("P", bound=Plugin)
Authorizer = Callable[[CalcContext, str, Any], None]
ResultHook = Callable[[ResultMeta, CalcContext], None]

_FORMULA_CODES = {"formula_syntax", "formula_too_long", "formula_too_complex"}


@dataclass(frozen=True)
class EngineConfig:
    """Engine-wide settings.

    ``authorize(ctx, action, resource)`` is called by plugins before protected actions (the
    what-if plugin uses ``scenario.read``, ``scenario.write``, ``scenario.create`` and
    ``scenario.delete`` with the scenario as the resource) and should raise
    :class:`~callscore_calc.errors.Forbidden` to refuse. ``on_result(meta, ctx)`` sees every
    result, e.g. for an audit log.
    """

    numeric: NumericConfig = NumericConfig()
    limits: Limits = Limits()
    max_concurrent: int = 2
    queue_timeout_s: float = 10.0
    default_timeout_s: float | None = 60.0
    cache_bytes: int = 256 * 1024 * 1024
    authorize: Authorizer | None = None
    on_result: ResultHook | None = None


@dataclass(frozen=True)
class AppliedTransform:
    name: str
    identity: Any
    plan: TransformPlan


@dataclass
class View:
    """A dataset version as one caller sees it, after the requested plugin transforms."""

    dataset: Dataset
    schema: DatasetSchema
    base: pl.LazyFrame  # the dataset after the caller's row filter and column restrictions
    frame: pl.LazyFrame  # ... and after the transforms
    env: dict[str, LType]
    transforms: tuple[AppliedTransform, ...]
    steps_fingerprint: str

    def identity(self) -> dict[str, Any]:
        return {
            "dataset": [self.dataset.id, self.dataset.version],
            "steps": self.steps_fingerprint,
        }

    def transform(self, name: str) -> TransformPlan | None:
        return next((t.plan for t in self.transforms if t.name == name), None)

    @property
    def canonical(self) -> list[Any]:
        return [entry for t in self.transforms for entry in t.plan.canonical]

    def meta(self) -> dict[str, Any]:
        return {t.name: t.plan.meta() for t in self.transforms}


def validation_error(exc: ValidationError, prefix: str = "") -> SpecError:
    """Turn a pydantic error into a :class:`SpecError` with a JSON-pointer path."""
    errors = exc.errors(include_url=False)
    first = errors[0]
    path = join_path(prefix, *first["loc"]) if first["loc"] else prefix or None
    code = first["type"] if first["type"] in _FORMULA_CODES else "invalid_request"
    message = str(first["msg"]).removeprefix("Value error, ")
    detail = {
        "errors": [
            {"path": join_path(prefix, *e["loc"]), "message": e["msg"], "type": e["type"]}
            for e in errors[:20]
        ]
    }
    return SpecError(message, code=code, path=path, detail=detail)


class Kernel:
    """The engine's machinery, shared by the built-in calls and by plugin operations.

    A typical operation::

        def run(kernel, req, ctx):
            with kernel.slot():
                deadline = kernel.deadline(req.options)
                view = kernel.resolve_view(req.dataset, req.extensions, ctx, deadline=deadline)
                return kernel.query(view, req.query, req.options, ctx, deadline=deadline)
    """

    def __init__(
        self,
        catalog: DatasetCatalog,
        config: EngineConfig,
        plugins: Sequence[Plugin],
    ) -> None:
        self.catalog = catalog
        self.config = config
        self.plugins = tuple(plugins)
        self.registry: Registry = build_registry(self.plugins)
        self._executor = Executor(
            max_concurrent=config.max_concurrent, queue_timeout_s=config.queue_timeout_s
        )
        self.cache: ResultCache[Any] = ResultCache(config.cache_bytes)
        self.plans: ResultCache[Any] = ResultCache(64 * 1024 * 1024)
        self.versions = {
            "callscore-calc": package_version("callscore-calc"),
            "polars": pl.__version__,
            **{f"plugin:{p.name}": p.version for p in self.plugins},
        }
        self.salt = fingerprint(
            {"versions": self.versions, "numeric": dataclasses.asdict(config.numeric)}
        )

    # --- Plumbing ---------------------------------------------------------------------------

    def parse(self, model: type[M], request: M | Mapping[str, Any], path: str = "") -> M:
        """Validate a request; versioned requests (``spec_version``) are upgraded first."""
        if isinstance(request, model):
            return request
        if not isinstance(request, Mapping):
            raise SpecError(f"expected a {model.__name__} or a JSON object", code="invalid_request")
        raw = dict(request)
        if "spec_version" in model.model_fields:
            raw = upgrade(raw)
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise validation_error(exc, path) from None

    def authorize(self, ctx: CalcContext, action: str, resource: Any) -> None:
        if self.config.authorize is not None:
            self.config.authorize(ctx, action, resource)

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one of the engine's concurrent execution slots (queues, then 503)."""
        with self._executor.slot():
            yield

    def deadline(self, options: Options) -> float | None:
        timeout = options.timeout_s or self.config.default_timeout_s
        return None if timeout is None else time.monotonic() + timeout

    def engine_name(self, options: Options, dataset: Dataset) -> EngineName:
        if options.engine != "auto":
            return options.engine
        return "streaming" if dataset.kind == "scan" else "in-memory"

    def collect(
        self, lf: pl.LazyFrame, *, engine: EngineName = "in-memory", deadline: float | None
    ) -> pl.DataFrame:
        return self._executor.collect(lf, engine=engine, deadline=deadline)

    # --- Resolution -------------------------------------------------------------------------

    def resolve_view(
        self,
        dataset: DatasetRef,
        extensions: Mapping[str, Any],
        ctx: CalcContext,
        *,
        deadline: float | None,
        path: str = "/extensions",
    ) -> View:
        """Load the dataset version and apply the transforms named in ``extensions``."""
        for name in extensions:
            if name not in self.registry.transforms:
                raise SpecError(
                    f"unknown extension: {name}",
                    code="unknown_extension",
                    path=join_path(path, name),
                    detail={"available": sorted(self.registry.transforms)},
                )
        bound: list[tuple[str, Bound]] = []
        for name, tdef in self.registry.transforms.items():
            if name not in extensions:
                continue
            block_path = join_path(path, name)
            block = extensions[name]
            if not isinstance(block, tdef.model):
                block = self.parse(tdef.model, block if block is not None else {}, block_path)
            bound.append((name, tdef.bind(block, BindContext(self, dataset, ctx, block_path))))
        pins = {b.pinned_version for _, b in bound if b.pinned_version is not None}
        if len(pins) > 1:
            raise VersionConflict(
                f"the extensions pin different dataset versions: {sorted(pins)}", path=path
            )
        version = next(iter(pins)) if pins else dataset.version
        return self.plan_view(
            self.catalog.get(dataset.id, version), bound, ctx, deadline=deadline, path=path
        )

    def plan_view(
        self,
        dataset: Dataset,
        bound: Sequence[tuple[str, Bound]],
        ctx: CalcContext,
        *,
        deadline: float | None,
        path: str = "/extensions",
    ) -> View:
        """Apply already-bound transforms to a dataset version (plans are cached)."""
        cfg = self.config.numeric
        schema = dataset.schema.restrict(ctx.allowed_columns)
        base = dataset.lazy()
        if ctx.allowed_columns is not None:
            visible = [c.name for c in schema.columns]
            base = base.select(visible + ([ROW_INDEX] if dataset.has_row_index else []))
        row_filter = ctx.row_filter_node()
        if row_filter is not None:
            typed = check_predicate(
                row_filter,
                schema.ltypes(),
                cfg,
                "/context/row_filter",
                functions=self.registry.functions,
            )
            base = base.filter(compile_expr(typed))
        env = schema.ltypes()
        frame = base
        applied: list[AppliedTransform] = []
        for name, b in bound:
            identity = b.identity
            key = "plan:" + fingerprint(
                {
                    "dataset": [dataset.id, dataset.version],
                    "transform": name,
                    "identity": identity,
                    "upstream": [[t.name, t.identity] for t in applied],
                    "ctx": ctx.digest(),
                    "salt": self.salt,
                }
            )
            plan = self.plans.get(key)
            if plan is None:
                pc = PlanContext(self, dataset, schema, ctx, join_path(path, name), deadline)
                plan = b.plan(env, frame, pc)
                self.plans.put(key, plan, plan.size())
            frame = plan.apply(frame)
            env = dict(plan.env)
            applied.append(AppliedTransform(name, identity, plan))
        canonical = [entry for t in applied for entry in t.plan.canonical]
        return View(
            dataset=dataset,
            schema=schema,
            base=base,
            frame=frame,
            env=env,
            transforms=tuple(applied),
            steps_fingerprint=fingerprint(canonical),
        )

    def plan_query(self, query: Query, view: View, path: str = "/query") -> LogicalQuery:
        return plan_query(
            query,
            view.env,
            self.config.numeric,
            self.config.limits,
            key_columns=view.dataset.key_columns,
            path=path,
            registry=self.registry,
        )

    # --- Execution --------------------------------------------------------------------------

    def query(
        self,
        view: View,
        query: Query,
        options: Options,
        ctx: CalcContext,
        *,
        deadline: float | None,
        timings: dict[str, float] | None = None,
    ) -> CalcResult:
        """Plan and run ``query`` on a resolved view (paged, cached, with metadata)."""
        timings = {} if timings is None else timings
        mark = time.perf_counter()
        logical = self.plan_query(query, view)
        timings["plan"] = timings.get("plan", 0.0) + _ms(mark)
        engine = self.engine_name(options, view.dataset)
        identity = self.identity(view.identity(), query, options, ctx)
        mark = time.perf_counter()
        if logical.aggregated:
            key = self.cache_key(identity, drop_page=True)
            finished, cached = self.aggregate(view, logical, key, engine, deadline, options)
            frame = _page(finished.frame, query.page)
            total, columns, fields = finished.total_rows, finished.columns, finished.pivot_fields
        else:
            key = self.cache_key(identity, drop_page=False)
            frame, total, cached = self.leaf(view, logical, key, engine, deadline)
            columns, fields = logical.output, None
        timings["execute"] = _ms(mark)
        stage_rows = None
        if options.audit:
            stage_rows = self.stage_rows(view, logical, total, engine, deadline)
        meta = self.meta(
            identity, view, frame, total, query.page, columns, fields, engine, cached, timings
        )
        if stage_rows is not None:
            meta = meta.model_copy(update={"stage_rows": stage_rows})
        return CalcResult(frame, meta)

    def aggregate(
        self,
        view: View,
        logical: LogicalQuery,
        key: str,
        engine: EngineName,
        deadline: float | None,
        options: Options,
    ) -> tuple[Finished, bool]:
        """Aggregate a view, through the result cache. Returns (result, was it cached)."""
        hit = self.cache.get(key)
        if hit is not None:
            return hit, True
        finished = self.evaluate_aggregate(
            view.frame,
            logical,
            engine=engine,
            deadline=deadline,
            deterministic=options.deterministic,
        )
        self.cache.put(key, finished, frame_size(finished.frame))
        return finished, False

    def evaluate_aggregate(
        self,
        frame: pl.LazyFrame,
        logical: LogicalQuery,
        *,
        engine: EngineName,
        deadline: float | None,
        deterministic: bool,
    ) -> Finished:
        """Aggregate, pivot and sort ``frame`` (no paging, no cache)."""
        collect = self._executor.collect
        agg = collect(
            agg_frame(frame, logical, deterministic=deterministic), engine=engine, deadline=deadline
        )
        domain = totals = None
        if logical.pivot is not None:
            if logical.pivot.domain is None:
                domain = collect(domain_frame(frame, logical), engine=engine, deadline=deadline)
            if logical.pivot.totals:
                totals = collect(
                    totals_frame(frame, logical, deterministic=deterministic),
                    engine=engine,
                    deadline=deadline,
                )
        return finish_aggregate(agg, logical, self.config.limits, domain=domain, totals=totals)

    def leaf(
        self,
        view: View,
        logical: LogicalQuery,
        key: str,
        engine: EngineName,
        deadline: float | None,
    ) -> tuple[pl.DataFrame, int, bool]:
        """Rows of a view (one page), through the cache. Returns (rows, total, was it cached)."""
        hit = self.cache.get(key)
        if hit is not None:
            return hit[0], hit[1], True
        limits = self.config.limits
        df = self._executor.collect(
            leaf_frame(view.frame, logical, limits), engine=engine, deadline=deadline
        )
        page = logical.page
        if page is None and df.height > limits.max_unpaged_rows:
            raise _too_many_rows(limits)
        if df.height:
            total = int(df[TOTAL][0])
        elif page is None or page.offset == 0:
            total = 0
        else:
            counted = self._executor.collect(
                count_frame(view.frame, logical), engine=engine, deadline=deadline
            )
            total = int(counted[TOTAL][0])
        frame = df.drop(TOTAL)
        self.cache.put(key, (frame, total), frame_size(frame))
        return frame, total, False

    def stage_rows(
        self,
        view: View,
        logical: LogicalQuery,
        total: int,
        engine: EngineName,
        deadline: float | None,
    ) -> dict[str, int]:
        collect = self._executor.collect
        base = collect(view.base.select(pl.len()), engine=engine, deadline=deadline).item()
        filtered = collect(count_frame(view.frame, logical), engine=engine, deadline=deadline)
        return {"dataset": int(base), "filtered": int(filtered[TOTAL][0]), "result": total}

    # --- Identity and metadata --------------------------------------------------------------

    def identity(
        self, side: Mapping[str, Any], query: Query, options: Options, ctx: CalcContext
    ) -> dict[str, Any]:
        """What a result depends on; its fingerprint is the result's fingerprint."""
        return {
            "spec_version": 1,  # the identity format, not the request format
            "side": dict(side),
            "query": query,
            "deterministic": options.deterministic,
            "numeric": dataclasses.asdict(self.config.numeric),
            "context": ctx.digest(),
        }

    def cache_key(self, identity: Mapping[str, Any], *, drop_page: bool) -> str:
        data = dict(identity)
        if drop_page:
            query = data["query"]
            assert isinstance(query, Query)
            data["query"] = query.model_copy(update={"page": None})
        return hashlib.sha256((fingerprint(data) + self.salt).encode()).hexdigest()

    def meta(
        self,
        identity: Mapping[str, Any],
        view: View,
        frame: pl.DataFrame,
        total: int,
        page: Page | None,
        columns: Mapping[str, LType],
        fields: list[str] | None,
        engine: str,
        cached: bool,
        timings: dict[str, float],
    ) -> ResultMeta:
        return ResultMeta(
            fingerprint=fingerprint(identity),
            dataset=DatasetRef(id=view.dataset.id, version=view.dataset.version),
            total_rows=total,
            offset=page.offset if page else 0,
            rows=frame.height,
            columns=[ColumnInfo.of(n, t) for n, t in columns.items()],
            pivot_fields=fields,
            engine=engine,
            cached=cached,
            timings_ms=timings,
            extensions=view.meta(),
            versions=self.versions,
        )

    def finish(self, result: CalcResult, ctx: CalcContext) -> CalcResult:
        """Hand a result to ``EngineConfig.on_result``; every call should end here."""
        if self.config.on_result is not None:
            self.config.on_result(result.meta, ctx)
        return result


class CalcEngine:
    """Evaluate calculation requests over a :class:`~callscore_calc.catalog.DatasetCatalog`.

    ``plugins`` add functions, aggregates, transforms, operations and routes (see
    :mod:`callscore_calc.plugins`); ``discover_plugins()`` finds the installed ones.
    """

    def __init__(
        self,
        catalog: DatasetCatalog,
        config: EngineConfig | None = None,
        plugins: Sequence[Plugin] = (),
    ) -> None:
        self.kernel = Kernel(catalog, config or EngineConfig(), plugins)
        for plugin in self.kernel.plugins:
            plugin.attach(self)

    @property
    def catalog(self) -> DatasetCatalog:
        return self.kernel.catalog

    @property
    def config(self) -> EngineConfig:
        return self.kernel.config

    @property
    def cache(self) -> ResultCache[Any]:
        return self.kernel.cache

    @property
    def registry(self) -> Registry:
        return self.kernel.registry

    @property
    def versions(self) -> dict[str, str]:
        return self.kernel.versions

    @overload
    def plugin(self, key: str) -> Plugin: ...
    @overload
    def plugin(self, key: type[P]) -> P: ...
    def plugin(self, key: str | type[Plugin]) -> Plugin:
        """An installed plugin, by name or by class."""
        for p in self.kernel.plugins:
            if (isinstance(key, str) and p.name == key) or (
                isinstance(key, type) and isinstance(p, key)
            ):
                return p
        name = key if isinstance(key, str) else key.__name__
        raise CalcError(f"plugin {name} is not installed", code="plugin_not_installed")

    # --- Public API -------------------------------------------------------------------------

    def authorize(self, ctx: CalcContext, action: str, resource: Any) -> None:
        self.kernel.authorize(ctx, action, resource)

    def parse(self, model: type[M], request: M | Mapping[str, Any]) -> M:
        return self.kernel.parse(model, request)

    def schema(
        self, dataset_id: str, version: str | None = None, *, ctx: CalcContext | None = None
    ) -> DatasetSchema:
        """The dataset's columns as ``ctx`` may see them."""
        ctx = ctx or CalcContext()
        return self.catalog.get(dataset_id, version).schema.restrict(ctx.allowed_columns)

    def run(
        self, request: CalcRequest | Mapping[str, Any], ctx: CalcContext | None = None
    ) -> CalcResult:
        """Evaluate a request and return the (paged) result with its metadata."""
        k = self.kernel
        started = time.perf_counter()
        req = k.parse(CalcRequest, request)
        ctx = ctx or CalcContext()
        timings: dict[str, float] = {}
        with k.slot():
            timings["queue"] = _ms(started)
            deadline = k.deadline(req.options)
            mark = time.perf_counter()
            view = k.resolve_view(req.dataset, req.extensions, ctx, deadline=deadline)
            timings["plan"] = _ms(mark)
            result = k.query(view, req.query, req.options, ctx, deadline=deadline, timings=timings)
        timings["total"] = _ms(started)
        return k.finish(result, ctx)

    def compare(
        self, request: CompareRequest | Mapping[str, Any], ctx: CalcContext | None = None
    ) -> CalcResult:
        """Run a query on two sides and join them with deltas (see :class:`CompareRequest`)."""
        k = self.kernel
        started = time.perf_counter()
        req = k.parse(CompareRequest, request)
        ctx = ctx or CalcContext()
        timings: dict[str, float] = {}
        core = req.query.model_copy(update={"sort": (), "page": None})
        base_ref = DatasetRef(id=req.dataset.id, version=req.base.version or req.dataset.version)
        with k.slot():
            timings["queue"] = _ms(started)
            deadline = k.deadline(req.options)
            target = k.resolve_view(req.dataset, req.extensions, ctx, deadline=deadline)
            base = k.resolve_view(
                base_ref, req.base.extensions, ctx, deadline=deadline, path="/base/extensions"
            )
            lt, lb = k.plan_query(core, target), k.plan_query(core, base)
            engine = k.engine_name(req.options, target.dataset)
            identity = k.identity(
                {"compare": [target.identity(), base.identity()]}, req.query, req.options, ctx
            )
            mark = time.perf_counter()
            if lt.aggregated:
                keys = tuple(lt.group_by) + ((LEVEL,) if lt.rollup else ())
                plan = plan_compare(keys, lt.output, lb.output, lt.value_names, k.config.numeric)
                key = k.cache_key(identity, drop_page=True)
                hit = k.cache.get(key)
                cached = hit is not None
                if hit is None:
                    kt = k.cache_key(
                        k.identity(target.identity(), core, req.options, ctx), drop_page=True
                    )
                    kb = k.cache_key(
                        k.identity(base.identity(), core, req.options, ctx), drop_page=True
                    )
                    ft, _ = k.aggregate(target, lt, kt, engine, deadline, req.options)
                    fb, _ = k.aggregate(base, lb, kb, engine, deadline, req.options)
                    joined = compare_frame(ft.frame.lazy(), fb.frame.lazy(), plan).collect()
                    view = _with_sort(lt, req.query.sort, set(plan.output))
                    hit = sort_frame(joined, view)
                    k.cache.put(key, hit, frame_size(hit))
                total = hit.height
                frame = _page(hit, req.query.page)
                columns = plan.output
            else:
                keys = target.dataset.key_columns
                if not keys:
                    raise SpecError(
                        "comparing rows needs a dataset with key columns", code="no_key_columns"
                    )
                values = tuple(c for c in lt.select if c not in keys)
                plan = plan_compare(
                    keys,
                    lt.output | {c: lt.row_env[c] for c in keys},
                    lb.output | {c: lb.row_env[c] for c in keys},
                    values,
                    k.config.numeric,
                )
                view = _with_sort(lt, req.query.sort, set(plan.output))
                lf = compare_frame(
                    rows_frame(target.frame, lt), rows_frame(base.frame, lb), plan
                ).with_columns(pl.len().alias(TOTAL))
                sort_keys = [*view.sort, *(SortSpec(c, False, True) for c in keys)]
                lf = lf.sort(
                    [s.by for s in sort_keys],
                    descending=[s.desc for s in sort_keys],
                    nulls_last=[s.nulls_last for s in sort_keys],
                )
                page = req.query.page
                if page is not None:
                    lf = lf.slice(page.offset, page.limit)
                else:
                    lf = lf.head(k.config.limits.max_unpaged_rows + 1)
                key = k.cache_key(identity, drop_page=False)
                hit_leaf = k.cache.get(key)
                cached = hit_leaf is not None
                if hit_leaf is None:
                    df = k.collect(lf, engine=engine, deadline=deadline)
                    if page is None and df.height > k.config.limits.max_unpaged_rows:
                        raise _too_many_rows(k.config.limits)
                    total = int(df[TOTAL][0]) if df.height else 0
                    hit_leaf = (df.drop(TOTAL), total)
                    k.cache.put(key, hit_leaf, frame_size(hit_leaf[0]))
                frame, total = hit_leaf
                columns = plan.output
            timings["execute"] = _ms(mark)
        timings["total"] = _ms(started)
        meta = k.meta(
            identity, target, frame, total, req.query.page, columns, None, engine, cached, timings
        )
        return k.finish(CalcResult(frame, meta), ctx)

    def explain(
        self, request: CalcRequest | Mapping[str, Any], ctx: CalcContext | None = None
    ) -> dict[str, Any]:
        """Describe what a request computes: transform steps, lineage, types and the Polars
        plan."""
        k = self.kernel
        req = k.parse(CalcRequest, request)
        ctx = ctx or CalcContext()
        with k.slot():
            deadline = k.deadline(req.options)
            view = k.resolve_view(req.dataset, req.extensions, ctx, deadline=deadline)
            logical = k.plan_query(req.query, view)
        if logical.aggregated:
            lf = agg_frame(view.frame, logical, deterministic=req.options.deterministic)
        else:
            lf = leaf_frame(view.frame, logical, k.config.limits)
        from callscore_calc.spec.formula import to_formula

        derives = {d.name: to_formula(d.expr.node) for d in logical.derives}
        identity = k.identity(view.identity(), req.query, req.options, ctx)
        return {
            "fingerprint": fingerprint(identity),
            "dataset": {"id": view.dataset.id, "version": view.dataset.version},
            "steps": view.canonical,
            "extensions": {t.name: t.plan.explain() for t in view.transforms},
            "lineage": {"derived": derives},
            "columns": {name: str(t) for name, t in logical.output.items()},
            "plan": lf.explain(),
        }

    def distinct_values(
        self,
        dataset: str | DatasetRef,
        column: str,
        *,
        extensions: Mapping[str, Any] | None = None,
        filter: str | None = None,
        limit: int = 1000,
        ctx: CalcContext | None = None,
    ) -> list[Any]:
        """Sorted distinct values of a column (nulls last), e.g. for a set filter."""
        request: dict[str, Any] = {
            "spec_version": 2,
            "dataset": dataset if isinstance(dataset, str) else dataset.model_dump(),
            "extensions": dict(extensions or {}),
            "query": {"group_by": [column], "page": {"limit": limit}},
        }
        if filter is not None:
            request["query"]["filter"] = filter
        result = self.run(request, ctx)
        return result.frame[column].to_list()

    def call(
        self, operation: str, request: BaseModel | Mapping[str, Any], ctx: CalcContext | None = None
    ) -> Any:
        """Run a plugin operation (see :class:`~callscore_calc.plugins.OperationDef`)."""
        op: OperationDef | None = self.registry.operations.get(operation)
        if op is None:
            raise CalcError(
                f"unknown operation: {operation}",
                code="unknown_operation",
                detail={"available": sorted(self.registry.operations)},
            )
        req = self.kernel.parse(op.request_model, request)
        return op.run(self.kernel, req, ctx or CalcContext())


def _page(frame: pl.DataFrame, page: Page | None) -> pl.DataFrame:
    return frame if page is None else frame.slice(page.offset, page.limit)


def _with_sort(plan: LogicalQuery, sort: Sequence[Any], allowed: set[str]) -> LogicalQuery:
    specs = []
    for i, key in enumerate(sort):
        if key.by not in allowed:
            raise SpecError(
                f"cannot sort by {key.by}: not a column of this view",
                code="unknown_column",
                path=join_path("/query/sort", i, "by"),
            )
        if plan.rollup and key.by not in plan.group_by:
            raise SpecError(
                "with rollup, only group_by columns can be sorted",
                code="invalid_sort",
                path="/query/sort",
            )
        specs.append(SortSpec(key.by, key.desc, key.nulls_last))
    tiebreak = tuple(g for g in plan.tiebreak if g not in {s.by for s in specs})
    return dataclasses.replace(plan, sort=tuple(specs), tiebreak=tiebreak)


def _too_many_rows(limits: Limits) -> LimitExceeded:
    return LimitExceeded(
        f"the view has more than {limits.max_unpaged_rows} rows; request a page",
        code="too_many_rows",
    )


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 3)
