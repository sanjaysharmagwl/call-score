"""Reference-data generation: calendar, sectors, firms, analysts, securities.

Produces the point-in-time reference frames the engine consumes
(``sessions``, ``coverage_spells``, ``employment_spells``,
``security_classifications``) plus the auxiliary store-shaped frames
(firms, analysts, securities, identifiers, exchange, sectors) that the
rating-event generator (a later task) and the persistence layer need.

All randomness flows through independent ``numpy`` RNG streams keyed by
``(seed, stream_index)`` so components never share call order.
"""

import zlib
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from callscore_synth.profiles import Profile

EXCHANGE_ID = "XNYS"
EXCHANGE_NAME = "New York Stock Exchange"
EXCHANGE_COUNTRY = "US"
EXCHANGE_TZ = "America/New_York"
MARKET_TZ = ZoneInfo(EXCHANGE_TZ)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
CURRENCY = "USD"

REGIONS = ("NA", "EU", "APAC")
CAP_BUCKETS = ("mega", "large", "mid", "small", "micro")

# Approximate USD market-cap midpoints per bucket, used for benchmark weights
# and liquidity scaling (log-normal jitter is applied per security).
CAP_BUCKET_BASE: dict[str, float] = {
    "mega": 2.0e11,
    "large": 4.0e10,
    "mid": 8.0e9,
    "small": 1.5e9,
    "micro": 2.0e8,
}

# 13 leaf sectors under 4 parent groups.
SECTOR_GROUPS: tuple[tuple[str, str], ...] = (
    ("GRP_TECH", "Technology & Communications"),
    ("GRP_CYCLICAL", " cyclicals".strip()),
    ("GRP_DEFENSIVE", "Defensive"),
    ("GRP_FINANCIAL", "Financials & Real Estate"),
)

SECTORS: tuple[tuple[str, str, str], ...] = (
    ("SEC_TECHNOLOGY", "Technology Hardware", "GRP_TECH"),
    ("SEC_SOFTWARE", "Software", "GRP_TECH"),
    ("SEC_SEMICONDUCTORS", "Semiconductors", "GRP_TECH"),
    ("SEC_COMMUNICATIONS", "Communications Services", "GRP_TECH"),
    ("SEC_CONSUMER_DISCRETIONARY", "Consumer Discretionary", "GRP_CYCLICAL"),
    ("SEC_INDUSTRIALS", "Industrials", "GRP_CYCLICAL"),
    ("SEC_MATERIALS", "Materials", "GRP_CYCLICAL"),
    ("SEC_ENERGY", "Energy", "GRP_CYCLICAL"),
    ("SEC_CONSUMER_STAPLES", "Consumer Staples", "GRP_DEFENSIVE"),
    ("SEC_HEALTH_CARE", "Health Care", "GRP_DEFENSIVE"),
    ("SEC_UTILITIES", "Utilities", "GRP_DEFENSIVE"),
    ("SEC_FINANCIALS", "Financials", "GRP_FINANCIAL"),
    ("SEC_REAL_ESTATE", "Real Estate", "GRP_FINANCIAL"),
)

SECTOR_IDS = tuple(s[0] for s in SECTORS)

# Sector selection weights (roughly market-like, tech a bit heavier).
# Order matches SECTORS.
SECTOR_WEIGHTS = np.array(
    [0.07, 0.13, 0.09, 0.06, 0.11, 0.11, 0.06, 0.07, 0.06, 0.08, 0.05, 0.05, 0.06]
)
REGION_WEIGHTS = np.array([0.55, 0.25, 0.20])
CAP_BUCKET_WEIGHTS = np.array([0.08, 0.17, 0.25, 0.30, 0.20])

_FIRST_NAMES = [
    "James",
    "Maria",
    "Robert",
    "Linda",
    "David",
    "Sarah",
    "Michael",
    "Jennifer",
    "William",
    "Emily",
    "Richard",
    "Susan",
    "Joseph",
    "Margaret",
    "Thomas",
    "Dorothy",
    "Charles",
    "Olivia",
    "Daniel",
    "Emma",
    "Matthew",
    "Ava",
    "Anthony",
    "Sophia",
    "Mark",
    "Isabella",
    "Donald",
    "Mia",
    "Paul",
    "Charlotte",
    "Steven",
    "Amelia",
    "Andrew",
    "Harriett",
    "Joshua",
    "Evelyn",
    "Kevin",
    "Samantha",
    "Brian",
    "Rebecca",
]
_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Torres",
    "Nguyen",
    "Hill",
    "Flores",
    "Green",
    "Adams",
    "Nelson",
    "Baker",
    "Hall",
    "Rivera",
    "Campbell",
    "Mitchell",
    "Carter",
    "Roberts",
]
_FIRM_PREFIXES = [
    "Northgate",
    "Alderbrook",
    "Calloway",
    "Dunmore",
    "Eastvale",
    "Fairmont",
    "Glenrock",
    "Highfield",
    "Ironwood",
    "Juniper",
    "Keystone",
    "Larkspur",
    "Meridian",
    "Norwood",
    "Oakhurst",
    "Pinecrest",
    "Quail",
    "Ridge",
    "Silverline",
    "Tamsin",
    "Underwood",
    "Valewood",
    "Westbrook",
    "Yielding",
    "Ashford",
    "Briarcliff",
    "Copperfield",
    "Driftwood",
    "Emberly",
    "Foxglove",
    "Granite",
    "Hollow",
    "Ivy",
]
_FIRM_SUFFIXES = [
    "Capital",
    "Partners",
    "Asset",
    "Management",
    "Securities",
    "Group",
    "Holdings",
    "Advisors",
    "Investments",
    "Trust",
    "Bank",
    "&",
    "Co",
]
_SECURITY_ADJ = [
    "Apex",
    "Beacon",
    "Cardinal",
    "Delta",
    "Ember",
    "Falcon",
    "Granite",
    "Harbor",
    "Ion",
    "Juniper",
    "Kite",
    "Lumen",
    "Meridian",
    "Nova",
    "Orion",
    "Pinnacle",
    "Quartz",
    "Ridgeline",
    "Summit",
    "Terra",
    "Umbra",
    "Vertex",
    "Willow",
    "Xenon",
    "Yield",
]
_SECURITY_NOUN = [
    "Dynamics",
    "Systems",
    "Logistics",
    "Materials",
    "Energy",
    "Foods",
    "Pharma",
    "Software",
    "Hardware",
    "Networks",
    "Media",
    "Analytics",
    "Robotics",
    "Semiconductors",
    "Instruments",
    "Health",
    "Utilities",
    "Finance",
    "Retail",
    "Industrials",
]


def rng(seed: int, stream_index: int) -> np.random.Generator:
    """Independent RNG stream: ``default_rng(seed + stream_index)``."""
    return np.random.default_rng(seed + stream_index)


def profile_seed_offset(profile_name: str) -> int:
    """Deterministic per-profile offset folded into stream indices."""
    return zlib.crc32(profile_name.encode("utf-8")) % 1_000_003


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE observed-date shift: Saturday -> prior Friday, Sunday -> next Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month``."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def nyse_holidays(year: int) -> set[date]:
    """NYSE full-day holidays for ``year`` (observed dates included)."""
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    return holidays


def session_dates(profile: Profile) -> list[date]:
    """Weekday session dates in ``[start_date, end_date]`` minus NYSE holidays."""
    holidays: set[date] = set()
    for year in range(profile.start_date.year - 1, profile.end_date.year + 2):
        holidays |= nyse_holidays(year)
    out: list[date] = []
    d = profile.start_date
    while d <= profile.end_date:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def session_frame(profile: Profile) -> pl.DataFrame:
    """``SESSION_FRAME``: local 09:30/16:00 America/New_York converted to UTC."""
    dates = session_dates(profile)
    open_ts: list[datetime] = []
    close_ts: list[datetime] = []
    for d in dates:
        open_local = datetime.combine(d, SESSION_OPEN, tzinfo=MARKET_TZ)
        close_local = datetime.combine(d, SESSION_CLOSE, tzinfo=MARKET_TZ)
        open_ts.append(open_local.astimezone(UTC))
        close_ts.append(close_local.astimezone(UTC))
    return pl.DataFrame(
        {
            "exchange_id": [EXCHANGE_ID] * len(dates),
            "session_date": dates,
            "open_ts": open_ts,
            "close_ts": close_ts,
        },
        schema={
            "exchange_id": pl.String,
            "session_date": pl.Date,
            "open_ts": pl.Datetime("us", "UTC"),
            "close_ts": pl.Datetime("us", "UTC"),
        },
    )


# --------------------------------------------------------------------------- #
# Static reference frames
# --------------------------------------------------------------------------- #


def exchange_frame() -> pl.DataFrame:
    """Aux ``exchange`` frame (store-shaped)."""
    return pl.DataFrame(
        {
            "exchange_id": [EXCHANGE_ID],
            "name": [EXCHANGE_NAME],
            "country": [EXCHANGE_COUNTRY],
            "timezone": [EXCHANGE_TZ],
        },
        schema={
            "exchange_id": pl.String,
            "name": pl.String,
            "country": pl.String,
            "timezone": pl.String,
        },
    )


def sector_frame() -> pl.DataFrame:
    """Aux ``sector`` frame: 4 parent groups + 12 leaf sectors."""
    rows: list[tuple[str, str, str | None]] = [(gid, name, None) for gid, name in SECTOR_GROUPS]
    rows += [(sid, name, parent) for sid, name, parent in SECTORS]
    return pl.DataFrame(
        {
            "sector_id": [r[0] for r in rows],
            "name": [r[1] for r in rows],
            "parent_sector_id": [r[2] for r in rows],
        },
        schema={
            "sector_id": pl.String,
            "name": pl.String,
            "parent_sector_id": pl.String,
        },
    )


def _unique_ticker(r: np.random.Generator, used: set[str]) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        candidate = "".join(r.choice(list(letters), size=3)) + f"{int(r.integers(10, 99)):02d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def firm_frame(profile: Profile, r: np.random.Generator) -> pl.DataFrame:
    """Aux ``firm`` frame."""
    names: list[str] = []
    used: set[str] = set()
    countries = ["US"] * 6 + ["GB", "DE", "JP", "FR", "CA", "NL", "CH", "IE"]
    for _ in range(profile.firms):
        while True:
            name = f"{r.choice(_FIRM_PREFIXES)} {r.choice(_FIRM_SUFFIXES)}"
            if name not in used:
                used.add(name)
                break
        names.append(name)
    return pl.DataFrame(
        {
            "firm_id": [f"FIRM_{i + 1:03d}" for i in range(profile.firms)],
            "name": names,
            "country": [str(r.choice(countries)) for _ in range(profile.firms)],
        },
        schema={"firm_id": pl.String, "name": pl.String, "country": pl.String},
    )


def analyst_frame(profile: Profile, r: np.random.Generator, firm_ids: list[str]) -> pl.DataFrame:
    """Aux ``analyst`` frame; ``firm_id`` is the analyst's final employer."""
    firm_idx = r.integers(0, len(firm_ids), size=profile.analysts)
    return pl.DataFrame(
        {
            "analyst_id": [f"AN_{i + 1:04d}" for i in range(profile.analysts)],
            "name": [
                f"{r.choice(_FIRST_NAMES)} {r.choice(_LAST_NAMES)}" for _ in range(profile.analysts)
            ],
            "firm_id": [firm_ids[int(i)] for i in firm_idx],
        },
        schema={"analyst_id": pl.String, "name": pl.String, "firm_id": pl.String},
    )


def employment_frame(
    profile: Profile,
    analysts: pl.DataFrame,
    firms: pl.DataFrame,
    r: np.random.Generator,
) -> pl.DataFrame:
    """``EMPLOYMENT_SPELL_FRAME``: ~15% job moves, ~8% mid-sample departures."""
    analyst_ids = analysts["analyst_id"].to_list()
    firm_ids = firms["firm_id"].to_list()
    n = len(analyst_ids)
    start = profile.start_date
    emp_from = [start - timedelta(days=int(x)) for x in r.integers(180, 540, size=n)]

    rows_a: list[str] = []
    rows_f: list[str] = []
    rows_from: list[date] = []
    rows_to: list[date | None] = []

    mover = r.random(n) < 0.15
    depart = (r.random(n) < 0.08) & ~mover
    move_day = start + timedelta(days=int(r.integers(0, max(1, profile.span_days // 2))))
    depart_day = start + timedelta(
        days=int(r.integers(profile.span_days // 4, max(profile.span_days // 2, 2)))
    )

    for i, aid in enumerate(analyst_ids):
        initial_firm = str(analysts["firm_id"][i])
        if initial_firm not in firm_ids:
            initial_firm = firm_ids[int(r.integers(0, len(firm_ids)))]
        if mover[i]:
            new_firm = firm_ids[int(r.integers(0, len(firm_ids)))]
            rows_a += [aid, aid]
            rows_f += [initial_firm, new_firm]
            rows_from += [emp_from[i], move_day]
            rows_to += [move_day - timedelta(days=1), None]
        elif depart[i]:
            rows_a.append(aid)
            rows_f.append(initial_firm)
            rows_from.append(emp_from[i])
            rows_to.append(depart_day)
        else:
            rows_a.append(aid)
            rows_f.append(initial_firm)
            rows_from.append(emp_from[i])
            rows_to.append(None)

    return pl.DataFrame(
        {
            "analyst_id": rows_a,
            "firm_id": rows_f,
            "from_date": rows_from,
            "to_date": rows_to,
        },
        schema={
            "analyst_id": pl.String,
            "firm_id": pl.String,
            "from_date": pl.Date,
            "to_date": pl.Date,
        },
    )


class SecuritySpec(NamedTuple):
    """Per-security static attributes shared by reference and market stages."""

    security_id: str
    name: str
    sector_id: str
    region: str
    cap_bucket: str
    listing_date: date
    market_cap: float


def security_specs(
    profile: Profile, r: np.random.Generator, dates: list[date]
) -> list[SecuritySpec]:
    """Deterministic per-security attribute draw (sorted by security_id)."""
    n = profile.securities
    sector_idx = r.choice(len(SECTOR_IDS), size=n, p=SECTOR_WEIGHTS / SECTOR_WEIGHTS.sum())
    region_idx = r.choice(len(REGIONS), size=n, p=REGION_WEIGHTS / REGION_WEIGHTS.sum())
    cap_idx = r.choice(len(CAP_BUCKETS), size=n, p=CAP_BUCKET_WEIGHTS / CAP_BUCKET_WEIGHTS.sum())
    cap_jitter = np.exp(r.normal(0.0, 0.45, size=n))

    n_sessions = len(dates)
    # ~88% listed before the sample; the rest IPO during the first ~55%.
    late = r.random(n) < 0.12
    early_offset = r.integers(30, 400, size=n)
    late_lo = max(1, n_sessions // 20)
    late_hi = max(late_lo + 1, int(n_sessions * 0.55))
    late_idx = r.integers(late_lo, late_hi, size=n)

    used_names: set[str] = set()
    specs: list[SecuritySpec] = []
    for i in range(n):
        while True:
            name = f"{r.choice(_SECURITY_ADJ)} {r.choice(_SECURITY_NOUN)}"
            if name not in used_names:
                used_names.add(name)
                break
        if late[i]:
            listing_date = dates[int(late_idx[i])]
        else:
            listing_date = dates[0] - timedelta(days=int(early_offset[i]))
        bucket = CAP_BUCKETS[int(cap_idx[i])]
        market_cap = CAP_BUCKET_BASE[bucket] * float(cap_jitter[i])
        specs.append(
            SecuritySpec(
                security_id=f"SEC_{i + 1:04d}",
                name=name,
                sector_id=SECTOR_IDS[int(sector_idx[i])],
                region=REGIONS[int(region_idx[i])],
                cap_bucket=bucket,
                listing_date=listing_date,
                market_cap=market_cap,
            )
        )
    return specs


def security_frame(specs: list[SecuritySpec]) -> pl.DataFrame:
    """Aux ``security`` frame (store-shaped: id + name)."""
    return pl.DataFrame(
        {
            "security_id": [s.security_id for s in specs],
            "name": [s.name for s in specs],
        },
        schema={"security_id": pl.String, "name": pl.String},
    )


def security_attribute_frame(specs: list[SecuritySpec]) -> pl.DataFrame:
    """Aux per-security attributes (sector/region/cap/listing/market cap)."""
    return pl.DataFrame(
        {
            "security_id": [s.security_id for s in specs],
            "sector_id": [s.sector_id for s in specs],
            "region": [s.region for s in specs],
            "cap_bucket": [s.cap_bucket for s in specs],
            "listing_date": [s.listing_date for s in specs],
            "market_cap": [s.market_cap for s in specs],
        },
        schema={
            "security_id": pl.String,
            "sector_id": pl.String,
            "region": pl.String,
            "cap_bucket": pl.String,
            "listing_date": pl.Date,
            "market_cap": pl.Float64,
        },
    )


def identifier_frame(specs: list[SecuritySpec], delistings: pl.DataFrame) -> pl.DataFrame:
    """Aux ``security_identifier`` PIT spells: ticker, isin, sedol."""
    delist_by_sec = {
        row[0]: row[1] for row in delistings.select("security_id", "delisting_date").iter_rows()
    }
    rows: list[tuple[str, str, str, date, date | None]] = []
    used_tickers: set[str] = set()
    for i, spec in enumerate(specs):
        delist_date = delist_by_sec.get(spec.security_id)
        ticker = _unique_ticker(np.random.default_rng(1234 + i), used_tickers)
        isin = f"US{(i + 1):09d}0"[:12].ljust(12, "0")
        sedol = f"{(i + 1) * 7 % 10**7:07d}"
        for id_type, value in (("ticker", ticker), ("isin", isin), ("sedol", sedol)):
            rows.append((spec.security_id, id_type, value, spec.listing_date, delist_date))
    return pl.DataFrame(
        rows,
        schema={
            "security_id": pl.String,
            "id_type": pl.String,
            "value": pl.String,
            "from_date": pl.Date,
            "to_date": pl.Date,
        },
        orient="row",
    )


def classification_frame(
    profile: Profile,
    specs: list[SecuritySpec],
    delistings: pl.DataFrame,
    r: np.random.Generator,
) -> pl.DataFrame:
    """``SECURITY_CLASSIFICATION_FRAME`` with a few sector/cap reclassifications.

    Spells are non-overlapping per security: ``[listing, split-1]`` then
    ``[split, delisting]``. ``delisting_date`` appears only on the final spell
    (point-in-time correct: earlier spells cannot know the future delisting).
    """
    delist_by_sec = {
        row[0]: row[1] for row in delistings.select("security_id", "delisting_date").iter_rows()
    }
    cap_order = {b: i for i, b in enumerate(CAP_BUCKETS)}
    rows: list[tuple[str, date, date | None, str, str, str, str, str, date, date | None]] = []
    for spec in specs:
        delist_date = delist_by_sec.get(spec.security_id)
        reclass = r.random() < 0.12
        end = delist_date
        if reclass:
            span_days = (end or profile.end_date) - spec.listing_date
            if span_days.days > 300:
                split = spec.listing_date + timedelta(days=int(r.integers(90, span_days.days - 30)))
                # Half the time change sector, half the time shift cap bucket.
                if r.random() < 0.5:
                    new_sector = spec.sector_id
                    cur = cap_order[spec.cap_bucket]
                    direction = int(r.choice([-1, 1]))
                    new_cap = CAP_BUCKETS[min(max(cur + direction, 0), len(CAP_BUCKETS) - 1)]
                else:
                    new_sector = str(r.choice([s for s in SECTOR_IDS if s != spec.sector_id]))
                    new_cap = spec.cap_bucket
                rows.append(
                    (
                        spec.security_id,
                        spec.listing_date,
                        split - timedelta(days=1),
                        EXCHANGE_ID,
                        CURRENCY,
                        spec.sector_id,
                        spec.region,
                        spec.cap_bucket,
                        spec.listing_date,
                        None,
                    )
                )
                rows.append(
                    (
                        spec.security_id,
                        split,
                        end,
                        EXCHANGE_ID,
                        CURRENCY,
                        new_sector,
                        spec.region,
                        new_cap,
                        spec.listing_date,
                        delist_date,
                    )
                )
                continue
        rows.append(
            (
                spec.security_id,
                spec.listing_date,
                end,
                EXCHANGE_ID,
                CURRENCY,
                spec.sector_id,
                spec.region,
                spec.cap_bucket,
                spec.listing_date,
                delist_date,
            )
        )
    return pl.DataFrame(
        rows,
        schema={
            "security_id": pl.String,
            "from_date": pl.Date,
            "to_date": pl.Date,
            "exchange_id": pl.String,
            "currency": pl.String,
            "sector_id": pl.String,
            "region": pl.String,
            "cap_bucket": pl.String,
            "listing_date": pl.Date,
            "delisting_date": pl.Date,
        },
        orient="row",
    )


def _emp_end_by_analyst(employment: pl.DataFrame) -> dict[str, date | None]:
    """Employment end per analyst: ``None`` while any spell is open-ended."""
    has_open: dict[str, bool] = {}
    last_end: dict[str, date] = {}
    for aid, to in employment.select("analyst_id", "to_date").iter_rows():
        if to is None:
            has_open[aid] = True
        else:
            has_open.setdefault(aid, False)
            last_end[aid] = max(last_end.get(aid, date.min), to)
    return {aid: (None if has_open.get(aid) else last_end[aid]) for aid in has_open}


def coverage_frame(
    profile: Profile,
    specs: list[SecuritySpec],
    analysts: pl.DataFrame,
    employment: pl.DataFrame,
    delistings: pl.DataFrame,
    r: np.random.Generator,
) -> pl.DataFrame:
    """``COVERAGE_SPELL_FRAME``.

    Each security is covered by 1-2 analysts whose home cluster matches its
    (sector, cap_bucket); spells run multi-year with ~25% ending early for
    turnover. Departed analysts' spells end at their employment end.
    """
    analyst_ids = analysts["analyst_id"].to_list()
    n_analysts = len(analyst_ids)

    # Home cluster per analyst, weighted toward larger clusters.
    cluster_counts: dict[tuple[str, str], int] = {}
    for spec in specs:
        key = (spec.sector_id, spec.cap_bucket)
        cluster_counts[key] = cluster_counts.get(key, 0) + 1
    clusters = sorted(cluster_counts)
    weights = np.array([cluster_counts[c] for c in clusters], dtype=float)
    weights /= weights.sum()
    home: dict[str, tuple[str, str]] = {
        aid: clusters[int(i)]
        for aid, i in zip(
            analyst_ids,
            r.choice(len(clusters), size=n_analysts, p=weights),
            strict=True,
        )
    }

    # Analysts by sector for fallback matching.
    by_sector: dict[str, list[str]] = {}
    for aid, (sec, _cap) in home.items():
        by_sector.setdefault(sec, []).append(aid)
    all_analysts = list(analyst_ids)

    delist_by_sec = {
        row[0]: row[1] for row in delistings.select("security_id", "delisting_date").iter_rows()
    }
    emp_end = _emp_end_by_analyst(employment)
    rows_a: list[str] = []
    rows_s: list[str] = []
    rows_from: list[date] = []
    rows_to: list[date | None] = []
    rows_lead: list[bool] = []

    for spec in specs:
        delist_date = delist_by_sec.get(spec.security_id)
        # Candidate analysts: same sector+cap home first, then same sector, then any.
        exact = [a for a in all_analysts if home[a] == (spec.sector_id, spec.cap_bucket)]
        same_sector = by_sector.get(spec.sector_id, [])
        pool = exact or same_sector or all_analysts
        k = 1 + int(r.random() < 0.35 and len(pool) > 1)
        chosen = list(r.choice(np.array(pool, dtype=object), size=min(k, len(pool)), replace=False))
        # Coverage start: listing date minus a short seasoning window, but not
        # before the analyst's employment start minus a grace month.
        latest_end = delist_date or profile.end_date
        for j, aid in enumerate(chosen):
            cov_from = spec.listing_date - timedelta(days=int(r.integers(0, 120)))
            cov_to: date | None = latest_end
            if r.random() < 0.25:
                # Early termination for turnover, but keep >= 6 months of spell.
                min_to = cov_from + timedelta(days=180)
                if min_to < latest_end:
                    cov_to = min_to + timedelta(
                        days=int(r.integers(0, max(1, (latest_end - min_to).days)))
                    )
            emp_last = emp_end.get(aid)
            if emp_last is not None and (cov_to is None or cov_to > emp_last):
                cov_to = emp_last
            if cov_to is not None and cov_to <= cov_from:
                cov_to = cov_from + timedelta(days=30)
            rows_a.append(aid)
            rows_s.append(spec.security_id)
            rows_from.append(cov_from)
            rows_to.append(cov_to)
            rows_lead.append(j == 0)

    frame = pl.DataFrame(
        {
            "analyst_id": rows_a,
            "security_id": rows_s,
            "from_date": rows_from,
            "to_date": rows_to,
            "is_lead": rows_lead,
        },
        schema={
            "analyst_id": pl.String,
            "security_id": pl.String,
            "from_date": pl.Date,
            "to_date": pl.Date,
            "is_lead": pl.Boolean,
        },
    )
    return frame
