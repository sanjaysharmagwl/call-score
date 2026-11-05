"""Profile loading and normalization for the synthetic data generator.

Profiles live in ``data/profiles/*.toml`` at the repository root. Two knob
styles exist in the wild: ``years`` (small/medium) and ``trading_days``
(full). Both are normalized to ``target_sessions`` — an approximate session
count the generator reports against in the manifest. Calendar dates
(``start_date``/``end_date``) are authoritative for the actual session set.
"""

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

TRADING_DAYS_PER_YEAR = 252

# server/packages/synth/src/callscore_synth/profiles.py -> repo root is 5 up.
_REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_PROFILES_DIR = _REPO_ROOT / "data" / "profiles"


@dataclass(frozen=True)
class Profile:
    """Normalized generation profile."""

    name: str
    seed: int
    securities: int
    analysts: int
    firms: int
    start_date: date
    end_date: date
    target_sessions: int | None

    @property
    def span_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO date string, got {value!r}")
    return date.fromisoformat(value)


def _require_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Profile field {key!r} must be an integer, got {value!r}")
    return value


def list_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Names (file stems) of all profiles in ``profiles_dir``."""
    directory = profiles_dir or DEFAULT_PROFILES_DIR
    return sorted(path.stem for path in directory.glob("*.toml"))


def load_profile(name_or_path: str | Path, profiles_dir: Path | None = None) -> Profile:
    """Load and normalize a profile from ``<name>.toml`` or an explicit path."""
    path = Path(name_or_path)
    if not path.suffix:
        directory = profiles_dir or DEFAULT_PROFILES_DIR
        path = directory / f"{name_or_path}.toml"
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    start = _parse_date(data.get("start_date"))
    end = _parse_date(data.get("end_date"))
    if end < start:
        raise ValueError(f"Profile {path.stem!r}: end_date {end} precedes start_date {start}")

    target: int | None = None
    trading_days = data.get("trading_days")
    years = data.get("years")
    if trading_days is not None:
        if not isinstance(trading_days, int) or isinstance(trading_days, bool):
            raise ValueError(
                f"Profile field 'trading_days' must be an integer, got {trading_days!r}"
            )
        target = trading_days
    elif years is not None:
        if not isinstance(years, (int, float)) or isinstance(years, bool):
            raise ValueError(f"Profile field 'years' must be a number, got {years!r}")
        target = round(float(years) * TRADING_DAYS_PER_YEAR)

    return Profile(
        name=path.stem,
        seed=_require_int(data, "seed"),
        securities=_require_int(data, "securities"),
        analysts=_require_int(data, "analysts"),
        firms=_require_int(data, "firms"),
        start_date=start,
        end_date=end,
        target_sessions=target,
    )
