"""The building blocks plugins use: typing, compiling and evaluating expressions.

Plugins import engine internals from here only. Everything re-exported below is part of the
public plugin API and changes only with a minor version bump of ``callscore-calc``; anything
else under ``callscore_calc`` that is not in ``callscore_calc.__all__`` is private.

* Type checking: :func:`check`, :func:`check_predicate`, :class:`Budget`, :class:`Typed`.
* Polars: :func:`compile_expr` (a typed tree as an expression), :func:`materialize` (the same,
  cast to the column's canonical dtype), :func:`as_type`, :func:`dec_dtype`, :func:`to_polars`.
* Reference: :func:`evaluate` (one row), :func:`convert`, :func:`quantize`.
* Values and types: :class:`LType`, :class:`Kind`, :func:`coerce_value`, :func:`json_value`.
"""

from callscore_calc.cache import ResultCache
from callscore_calc.catalog import ROW_INDEX
from callscore_calc.compile.exprs import as_type, compile_expr, dec_dtype, finite, materialize
from callscore_calc.compile.logical import check_name, materializable
from callscore_calc.compile.validate import Budget, Typed, check, check_predicate
from callscore_calc.dtypes import (
    BOOL,
    DATE,
    DATETIME,
    FLOAT,
    INT,
    NUMERIC,
    ORDERABLE,
    STR,
    Kind,
    LType,
    NumericConfig,
    Scalar,
    canonical_decimal,
    coerce_value,
    decimal,
    decimal_places,
    json_value,
    parse_decimal,
    quantize,
    to_polars,
)
from callscore_calc.engine import AppliedTransform, validation_error
from callscore_calc.errors import join_path
from callscore_calc.spec.base import Model
from callscore_calc.spec.canonical import to_canonical
from callscore_calc.spec.expr import Expr, Node, columns, replace_columns
from callscore_calc.verify.reference import convert, evaluate

__all__ = [
    "BOOL",
    "DATE",
    "DATETIME",
    "FLOAT",
    "INT",
    "NUMERIC",
    "ORDERABLE",
    "ROW_INDEX",
    "STR",
    "AppliedTransform",
    "Budget",
    "Expr",
    "Kind",
    "LType",
    "Model",
    "Node",
    "NumericConfig",
    "ResultCache",
    "Scalar",
    "Typed",
    "as_type",
    "check",
    "check_name",
    "check_predicate",
    "canonical_decimal",
    "coerce_value",
    "columns",
    "compile_expr",
    "convert",
    "dec_dtype",
    "decimal",
    "decimal_places",
    "evaluate",
    "finite",
    "join_path",
    "json_value",
    "materializable",
    "materialize",
    "parse_decimal",
    "quantize",
    "replace_columns",
    "to_canonical",
    "to_polars",
    "validation_error",
]
