"""FastAPI router exposing a :class:`CalcEngine` (``pip install 'callscore-calc[fastapi]'``).

::

    app = FastAPI()
    app.include_router(create_router(engine, prefix="/calc", context_resolver=my_auth))

Routes are plain ``def`` functions, so FastAPI runs them in its thread pool and the event loop
stays free while Polars computes. Installed plugins add their own routes (the what-if plugin adds
``/scenarios/...`` and ``/aggrid/edit``), and every plugin operation is reachable as
``POST /operations/{name}``. Errors come back as ``{"detail": {"code", "message", "path"}}``
with the status of the :class:`~callscore_calc.errors.CalcError`.
"""

# No ``from __future__ import annotations`` here: FastAPI must see the route annotations, which
# refer to aliases defined inside ``create_router``, as real objects rather than strings.

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

try:
    from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
    from fastapi.params import Depends as DependsParam
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "the FastAPI integration needs fastapi: pip install 'callscore-calc[fastapi]'"
    ) from exc

from pydantic import BaseModel

from callscore_calc.adapters.aggrid import AgGridAdapter
from callscore_calc.config import CalcContext
from callscore_calc.engine import CalcEngine
from callscore_calc.errors import CalcError, LimitExceeded
from callscore_calc.result import CalcResult, json_safe
from callscore_calc.spec.canonical import upgrade

ARROW_STREAM = "application/vnd.apache.arrow.stream"
T = TypeVar("T")
ContextResolver = Callable[[Request], CalcContext]
JsonBody = Annotated[dict[str, Any], Body()]


def _call(fn: Callable[[], T]) -> T:
    try:
        return fn()
    except CalcError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.to_dict()) from exc


def _result_response(
    result: CalcResult, accept: str | None, decimals: Literal["float", "str"]
) -> Response:
    headers = {
        "X-Calc-Fingerprint": result.meta.fingerprint,
        "X-Calc-Total-Rows": str(result.meta.total_rows),
    }
    if accept and ARROW_STREAM in accept:
        return Response(result.to_arrow_ipc(), media_type=ARROW_STREAM, headers=headers)
    return JSONResponse(result.to_dict(decimals=decimals), headers=headers)


def body_extensions(body: Mapping[str, Any]) -> dict[str, Any]:
    """The ``extensions`` of a request body; version 1 ``scenario``/``what_if`` keys are
    upgraded."""
    raw = {k: body[k] for k in ("scenario", "what_if", "extensions") if k in body}
    return dict(upgrade(raw).get("extensions") or {})


@dataclass(frozen=True)
class RouterKit:
    """What plugin route hooks get besides the router (see ``Registry.add_routes``).

    Build the parameter aliases in the hook, e.g.
    ``Ctx = Annotated[CalcContext, Depends(kit.context)]``, and wrap engine calls in
    ``kit.call`` so :class:`CalcError` becomes an HTTP error. Route modules must not use
    ``from __future__ import annotations``.
    """

    engine: CalcEngine
    context: Callable[[Request], CalcContext]
    adapter: AgGridAdapter
    decimals: Literal["float", "str"]

    def call(self, fn: Callable[[], T]) -> T:
        return _call(fn)

    def result_response(self, result: CalcResult, accept: str | None = None) -> Response:
        """JSON (or Arrow, if ``accept`` asks for it) with the fingerprint headers."""
        return _result_response(result, accept, self.decimals)

    def json(self, value: Any) -> Any:
        """A JSON-ready value: results as ``{"rows", "meta"}``, models dumped."""
        if isinstance(value, CalcResult):
            return value.to_dict(decimals=self.decimals)
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [self.json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.json(v) for k, v in value.items()}
        return json_safe(value, self.decimals)


def create_router(
    engine: CalcEngine,
    *,
    prefix: str = "",
    tags: Sequence[str] = ("calc",),
    dependencies: Sequence[DependsParam] = (),
    context_resolver: ContextResolver | None = None,
    aggrid: AgGridAdapter | None = None,
    decimals: Literal["float", "str"] = "float",
    max_body_bytes: int = 5 * 1024 * 1024,
) -> APIRouter:
    """Build the router. ``context_resolver(request)`` turns your auth into a
    :class:`CalcContext` (principal, row filter, visible columns); ``dependencies`` run first,
    e.g. your authentication dependency."""
    adapter = aggrid or AgGridAdapter()

    def body_limit(request: Request) -> None:
        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > max_body_bytes:
            error = LimitExceeded(f"request body is larger than {max_body_bytes} bytes")
            raise HTTPException(status_code=error.status, detail=error.to_dict())

    def context(request: Request) -> CalcContext:
        return context_resolver(request) if context_resolver else CalcContext()

    router = APIRouter(
        prefix=prefix,
        tags=list(tags),
        dependencies=[*dependencies, Depends(body_limit)],
    )
    Ctx = Annotated[CalcContext, Depends(context)]
    Accept = Annotated[str | None, Header()]

    @router.get("/datasets")
    def list_datasets(ctx: Ctx) -> list[dict[str, Any]]:
        return [
            s.restrict(ctx.allowed_columns).model_dump(mode="json") for s in engine.catalog.list()
        ]

    @router.get("/datasets/{dataset_id}/schema")
    def dataset_schema(
        dataset_id: str, ctx: Ctx, version: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        return _call(lambda: engine.schema(dataset_id, version, ctx=ctx).model_dump(mode="json"))

    @router.post("/query")
    def run_query(body: JsonBody, ctx: Ctx, accept: Accept = None) -> Response:
        """Evaluate a CalcRequest; ``Accept: application/vnd.apache.arrow.stream`` for Arrow."""
        return _result_response(_call(lambda: engine.run(body, ctx)), accept, decimals)

    @router.post("/compare")
    def run_compare(body: JsonBody, ctx: Ctx, accept: Accept = None) -> Response:
        return _result_response(_call(lambda: engine.compare(body, ctx)), accept, decimals)

    @router.post("/explain")
    def run_explain(body: JsonBody, ctx: Ctx) -> dict[str, Any]:
        return _call(lambda: engine.explain(body, ctx))

    @router.post("/distinct")
    def distinct(body: JsonBody, ctx: Ctx) -> dict[str, Any]:
        """Body: ``{"dataset", "column", "extensions"?, "filter"?, "limit"?}``."""

        def run() -> dict[str, Any]:
            if "dataset" not in body or "column" not in body:
                raise CalcError("dataset and column are required", code="invalid_request")
            values = engine.distinct_values(
                body["dataset"],
                body["column"],
                extensions=body_extensions(body),
                filter=body.get("filter"),
                limit=int(body.get("limit", 1000)),
                ctx=ctx,
            )
            return {"values": [json_safe(v, decimals) for v in values]}

        return _call(run)

    @router.post("/aggrid/rows")
    def aggrid_rows(body: JsonBody, ctx: Ctx) -> dict[str, Any]:
        """Body: ``{"dataset", "extensions"?, "request": <SSRM getRows request>}``."""

        def run() -> dict[str, Any]:
            if "dataset" not in body or "request" not in body:
                raise CalcError("dataset and request are required", code="invalid_request")
            response = adapter.rows(
                engine,
                body["request"],
                dataset=body["dataset"],
                extensions=body_extensions(body),
                ctx=ctx,
            )
            return response.model_dump(mode="json")

        return _call(run)

    @router.get("/operations")
    def list_operations() -> list[dict[str, Any]]:
        return [
            {"name": op.name, "description": op.description}
            for op in engine.registry.operations.values()
        ]

    @router.post("/operations/{name}")
    def call_operation(name: str, body: JsonBody, ctx: Ctx, accept: Accept = None) -> Response:
        """Run a plugin operation; results come back like ``/query`` results."""
        result = _call(lambda: engine.call(name, body, ctx))
        if isinstance(result, CalcResult):
            return _result_response(result, accept, decimals)
        return JSONResponse(kit.json(result))

    kit = RouterKit(engine=engine, context=context, adapter=adapter, decimals=decimals)
    for hook in engine.registry.routes:
        hook(router, kit)

    return router
