"""What-if request spec: scenario steps and the ``extensions.whatif`` block.

Steps are what-if changes that keep every row of the dataset.

A scenario is an ordered log of these steps over a pinned dataset version. Value changes
(:class:`Override`, :class:`Shock`) apply in log order; :class:`Formula` columns are evaluated
after all value changes, in dependency order, so a later override of ``price`` still flows into
``mv = price * quantity * fx_rate``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from callscore_calc.ext import Expr, Model, Scalar, canonical_decimal, parse_decimal


class Edit(Model):
    """Set one cell. ``key`` holds a value for every key column of the dataset."""

    key: dict[str, Scalar] = Field(min_length=1)
    column: str
    value: Scalar


class Override(Model):
    """Set cells to values. Within one step, the last edit of a (key, column) wins."""

    kind: Literal["override"] = "override"
    edits: tuple[Edit, ...] = Field(min_length=1)


class Shock(Model):
    """Change a column in bulk: ``add`` a value, ``mul`` by a factor or move by ``pct`` percent.

    ``where`` limits the rows (null counts as false). Decimal results are rounded half-to-even to
    the column scale. On integer columns a non-integral result is an error unless ``round`` is
    set, in which case it is rounded half-to-even.
    """

    kind: Literal["shock"] = "shock"
    column: str
    op: Literal["add", "mul", "pct"]
    value: Decimal
    where: Expr | None = None
    round: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def _exact(cls, value: object) -> Decimal:
        return Decimal(canonical_decimal(parse_decimal(value)))

    def factor(self) -> Decimal:
        """The multiplier for ``mul``/``pct`` (``pct`` 5 means ``* 1.05``)."""
        if self.op == "pct":
            return 1 + self.value / 100
        return self.value


class Formula(Model):
    """Define (or redefine) a derived column that is recomputed from the current values."""

    kind: Literal["formula"] = "formula"
    name: str = Field(min_length=1)
    expr: Expr


class Disable(Model):
    """Undo an earlier step of the same scenario by its sequence number."""

    kind: Literal["disable"] = "disable"
    seq: int = Field(ge=1)


ScenarioStep = Annotated[Override | Shock | Formula | Disable, Field(discriminator="kind")]


class ScenarioRef(Model):
    """A saved scenario and, optionally, a version (log length) to read it at."""

    id: str = Field(min_length=1)
    version: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, data: Any) -> Any:
        return {"id": data} if isinstance(data, str) else data


class WhatIf(Model):
    """The ``extensions.whatif`` block of a request: a saved ``scenario`` (optional) followed by
    extra ``steps``. ``strict_edits`` rejects overrides whose key matches no row."""

    scenario: ScenarioRef | None = None
    steps: tuple[ScenarioStep, ...] = ()
    strict_edits: bool = True
