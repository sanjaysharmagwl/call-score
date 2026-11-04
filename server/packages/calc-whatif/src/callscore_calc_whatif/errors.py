"""Errors raised by the what-if plugin."""

from callscore_calc import NotFound


class ScenarioNotFound(NotFound):
    code = "scenario_not_found"
