"""Shared domain types and utilities for callscore."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("callscore-core")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
