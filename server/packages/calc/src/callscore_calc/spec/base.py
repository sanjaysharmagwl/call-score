"""Base model for every spec object: immutable, strict about unknown fields, self-describing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer


class Model(BaseModel):
    """Frozen pydantic model that rejects unknown fields.

    Dumps always carry the ``kind`` discriminator, even with ``exclude_defaults=True``, so a
    canonical (defaults-free) dump can still be parsed back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_serializer(mode="wrap")
    def _keep_kind(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        kind = getattr(self, "kind", None)
        if isinstance(data, dict) and kind is not None and "kind" not in data:
            data = {"kind": kind, **data}
        return data
