"""What-if analysis for callscore-calc, as a plugin.

::

    from callscore_calc import CalcEngine
    from callscore_calc_whatif import InMemoryScenarioStore, WhatIfPlugin

    engine = CalcEngine(catalog, plugins=[WhatIfPlugin(store=InMemoryScenarioStore())])
    engine.run({
        "dataset": "holdings",
        "extensions": {"whatif": {"steps": [{"kind": "shock", "column": "price", "op": "pct",
                                             "value": -10}]}},
        "query": {"group_by": ["sector"], "measures": [...]},
    })

Steps (:class:`Override`, :class:`Shock`, :class:`Formula`, :class:`Disable`) change values but
keep every row. They come ad hoc in a request's ``extensions.whatif`` block, or from a saved
scenario: an append-only, hash-chained log over a pinned dataset version, managed through
``plugin.scenarios``.
"""

from importlib.metadata import version

from callscore_calc_whatif.aggrid import CellEdit, edit_to_override
from callscore_calc_whatif.errors import ScenarioNotFound
from callscore_calc_whatif.planner import LogicalMutations, WhatIfLimits
from callscore_calc_whatif.plugin import WhatIfPlan, WhatIfPlugin
from callscore_calc_whatif.scenario import (
    InMemoryScenarioStore,
    LogEntry,
    Scenario,
    ScenarioManager,
    ScenarioStore,
    verify_chain,
)
from callscore_calc_whatif.spec import (
    Disable,
    Edit,
    Formula,
    Override,
    ScenarioRef,
    ScenarioStep,
    Shock,
    WhatIf,
)

__all__ = [
    "CellEdit",
    "Disable",
    "Edit",
    "Formula",
    "InMemoryScenarioStore",
    "LogEntry",
    "LogicalMutations",
    "Override",
    "Scenario",
    "ScenarioManager",
    "ScenarioNotFound",
    "ScenarioRef",
    "ScenarioStep",
    "ScenarioStore",
    "Shock",
    "WhatIf",
    "WhatIfLimits",
    "WhatIfPlan",
    "WhatIfPlugin",
    "__version__",
    "edit_to_override",
    "verify_chain",
]
__version__ = version("callscore-calc-whatif")
