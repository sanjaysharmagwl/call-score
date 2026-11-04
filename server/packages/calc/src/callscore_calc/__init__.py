"""Embeddable calculation engine on Polars, extensible with plugins.

The core filters, derives, groups, aggregates (with filtered and weighted measures), rolls up,
pivots, sorts, pages and compares, with exact decimal arithmetic, reproducible fingerprints and
a pure-Python reference evaluator. Plugins (:mod:`callscore_calc.plugins`) add functions,
aggregates, dataset transforms, operations and HTTP routes; what-if analysis is the
``callscore-calc-whatif`` plugin.
"""

from importlib.metadata import version

from callscore_calc.catalog import Catalog, Dataset, DatasetCatalog
from callscore_calc.config import CalcContext, Limits
from callscore_calc.dtypes import NumericConfig
from callscore_calc.engine import CalcEngine, EngineConfig, Kernel, View
from callscore_calc.errors import (
    CalcError,
    CalcTimeout,
    ComputeError,
    DatasetNotFound,
    EngineBusy,
    Forbidden,
    LimitExceeded,
    NotFound,
    SpecError,
    VersionConflict,
)
from callscore_calc.exec import RuntimeReport, runtime_check
from callscore_calc.plugins import (
    AggregateDef,
    BindContext,
    Bound,
    FunctionDef,
    OperationDef,
    PlanContext,
    Plugin,
    PluginError,
    Registry,
    TransformDef,
    TransformPlan,
    discover_plugins,
)
from callscore_calc.result import CalcResult, ColumnInfo, ResultMeta
from callscore_calc.schema import ColumnMeta, DatasetSchema
from callscore_calc.spec.canonical import canonical_json, fingerprint
from callscore_calc.spec.expr import col, lit
from callscore_calc.spec.formula import parse_formula, to_formula
from callscore_calc.spec.query import (
    CalcRequest,
    CompareRequest,
    DatasetRef,
    Derive,
    Measure,
    Options,
    Page,
    Pivot,
    PostAgg,
    Query,
    Side,
    SortKey,
)

__all__ = [
    "AggregateDef",
    "BindContext",
    "Bound",
    "CalcContext",
    "CalcEngine",
    "CalcError",
    "CalcRequest",
    "CalcResult",
    "CalcTimeout",
    "Catalog",
    "ColumnInfo",
    "ColumnMeta",
    "CompareRequest",
    "ComputeError",
    "Dataset",
    "DatasetCatalog",
    "DatasetNotFound",
    "DatasetRef",
    "DatasetSchema",
    "Derive",
    "EngineBusy",
    "EngineConfig",
    "Forbidden",
    "FunctionDef",
    "Kernel",
    "LimitExceeded",
    "Limits",
    "Measure",
    "NotFound",
    "NumericConfig",
    "OperationDef",
    "Options",
    "Page",
    "Pivot",
    "PlanContext",
    "Plugin",
    "PluginError",
    "PostAgg",
    "Query",
    "Registry",
    "ResultMeta",
    "RuntimeReport",
    "Side",
    "SortKey",
    "SpecError",
    "TransformDef",
    "TransformPlan",
    "VersionConflict",
    "View",
    "__version__",
    "canonical_json",
    "col",
    "discover_plugins",
    "fingerprint",
    "lit",
    "parse_formula",
    "runtime_check",
    "to_formula",
]
__version__ = version("callscore-calc")
