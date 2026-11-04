"""Uvicorn entry point: ``uvicorn callscore_api.main:app``."""

from callscore_api.app import create_app

app = create_app()
