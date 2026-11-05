"""Make ``load`` importable for the golden-fixture tests (importlib pytest mode)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
