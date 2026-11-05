"""Exception handlers mapping domain errors to RFC-7807-ish JSON bodies.

Every error response has the shape::

    {"detail": <human message>, "code": <machine code>, "errors"?: [...]}

No tracebacks or internals are ever leaked: unexpected exceptions become a
generic ``500 internal_error`` (the traceback is only logged server-side).

Starlette resolves handlers by walking the exception's MRO, so registering a
handler for a base class (``ValueError``, store ``ValidationError``) and a
subclass (``RunTransitionError``, ``DuplicateRowError``, pydantic
``ValidationError``) picks the most specific one automatically.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from callscore_store.repositories import DuplicateRowError, RunTransitionError
from callscore_store.validation import ValidationError as StoreValidationError

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """HTTP error carrying an explicit status code and machine-readable code."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def _body(detail: str, code: str, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"detail": detail, "code": code}
    if errors is not None:
        body["errors"] = errors
    return body


def _response(status_code: int, detail: str, code: str, **kwargs: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_body(detail, code, **kwargs))


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return _response(exc.status_code, exc.detail, exc.code)


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = {404: "not_found", 405: "method_not_allowed", 409: "conflict"}.get(
        exc.status_code, "http_error"
    )
    return _response(exc.status_code, detail, code)


async def request_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # ``ctx`` may hold non-JSON-serializable objects (e.g. ValueError) —
    # jsonable_encoder is what FastAPI's own handler uses.
    errors = jsonable_encoder(exc.errors())
    return _response(422, "request validation failed", "validation_error", errors=errors)


async def pydantic_validation_handler(
    _request: Request, exc: PydanticValidationError
) -> JSONResponse:
    errors = jsonable_encoder(exc.errors())
    return _response(422, "request validation failed", "validation_error", errors=errors)


async def duplicate_row_handler(_request: Request, exc: DuplicateRowError) -> JSONResponse:
    return _response(409, str(exc), "duplicate_row")


async def run_transition_handler(_request: Request, exc: RunTransitionError) -> JSONResponse:
    return _response(409, str(exc), "invalid_transition")


async def store_validation_handler(_request: Request, exc: StoreValidationError) -> JSONResponse:
    return _response(422, str(exc), "validation_error")


async def key_error_handler(_request: Request, exc: KeyError) -> JSONResponse:
    detail = str(exc.args[0]) if exc.args else "resource not found"
    return _response(404, detail, "not_found")


async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    return _response(422, str(exc), "validation_error")


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled API error: %s", exc)
    return _response(500, "internal server error", "internal_error")


# Starlette types handlers as ``(Request, Exception) -> Response``; our
# handlers are deliberately narrower (they only ever receive their exc type).
_Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _register(
    app: FastAPI, exc_type: type[Exception], handler: Callable[..., Awaitable[JSONResponse]]
) -> None:
    app.add_exception_handler(exc_type, cast(_Handler, handler))


def register_error_handlers(app: FastAPI) -> None:
    """Install all exception handlers on ``app`` (call once at setup)."""
    _register(app, ApiError, api_error_handler)
    _register(app, DuplicateRowError, duplicate_row_handler)
    _register(app, RunTransitionError, run_transition_handler)
    _register(app, StoreValidationError, store_validation_handler)
    _register(app, KeyError, key_error_handler)
    _register(app, RequestValidationError, request_validation_handler)
    _register(app, PydanticValidationError, pydantic_validation_handler)
    _register(app, ValueError, value_error_handler)
    _register(app, StarletteHTTPException, http_exception_handler)
    _register(app, Exception, unhandled_exception_handler)


__all__ = ["ApiError", "register_error_handlers"]
