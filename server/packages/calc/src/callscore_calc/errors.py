"""Error types with a stable ``code``, a JSON-pointer ``path`` and an HTTP status."""

from __future__ import annotations

from typing import Any


class CalcError(Exception):
    """Base class for every error the engine raises on purpose.

    ``code`` is stable and safe to branch on in clients, ``path`` points at the offending part of
    the request (for example ``/query/measures/1/of``) and ``status`` is the HTTP status an
    integration should answer with.
    """

    status: int = 400
    code: str = "calc_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        path: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.path = path
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON body an API should send for this error."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.detail:
            out["detail"] = self.detail
        return out


class SpecError(CalcError):
    """The request is well-formed JSON but not a valid calculation (unknown column, bad type)."""

    status = 422
    code = "invalid_spec"


class ComputeError(CalcError):
    """Polars failed while evaluating a valid request (overflow, bad cast in the data)."""

    status = 422
    code = "compute_error"


class NotFound(CalcError):
    status = 404
    code = "not_found"


class DatasetNotFound(NotFound):
    code = "dataset_not_found"


class Forbidden(CalcError):
    status = 403
    code = "forbidden"


class VersionConflict(CalcError):
    """A write used a stale ``expected_version``, or a request pinned mismatching versions."""

    status = 409
    code = "version_conflict"


class LimitExceeded(CalcError):
    status = 413
    code = "limit_exceeded"


class EngineBusy(CalcError):
    """No execution slot became free before the queue timeout."""

    status = 503
    code = "engine_busy"


class CalcTimeout(CalcError):
    status = 504
    code = "timeout"


def join_path(base: str, *parts: str | int) -> str:
    """Append JSON-pointer segments to ``base``."""
    out = base
    for part in parts:
        text = str(part).replace("~", "~0").replace("/", "~1")
        out = f"{out}/{text}"
    return out
