"""Saved what-if scenarios: append-only, hash-chained logs of steps over a dataset version."""

from callscore_calc_whatif.scenario.manager import ScenarioManager
from callscore_calc_whatif.scenario.model import LogEntry, Scenario, verify_chain
from callscore_calc_whatif.scenario.store import InMemoryScenarioStore, ScenarioStore

__all__ = [
    "InMemoryScenarioStore",
    "LogEntry",
    "Scenario",
    "ScenarioManager",
    "ScenarioStore",
    "verify_chain",
]
