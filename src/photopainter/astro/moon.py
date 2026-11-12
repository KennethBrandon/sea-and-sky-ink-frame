"""The Moon's phase, and when it rises and sets, for the day and sky screens.

Computed locally with Skyfield and the JPL ephemeris, like `sun.py`: no network.

- `phase_deg` is how far the Moon is ahead of the Sun in ecliptic longitude, seen from
  Earth's center: 0 new, 90 first quarter, 180 full, 270 last quarter. This is the
  definition behind the published phase times (USNO, almanacs).
- `illuminated` is the fraction of the disc that's lit, from the Sun–Moon–Earth angle.
- `name`: the four principal phases are instants, so a principal name is given within
  `PRINCIPAL_WINDOW_DEG` of one (about 12 hours either side; the Moon gains about 12.2° a
  day on the Sun). In between it's a crescent or gibbous, waxing or waning.

Moonrise and moonset follow USNO's definition: the Moon's upper limb on a level horizon,
with 34′ of refraction, seen from the observer's position (so lunar parallax counts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from photopainter.astro import ephemeris, ephemeris_path, timescale

PRINCIPAL_WINDOW_DEG = 6.0

PRINCIPAL = ((0.0, "New moon"), (90.0, "First quarter"), (180.0, "Full moon"), (270.0, "Last quarter"))
BETWEEN = ("Waxing crescent", "Waxing gibbous", "Waning gibbous", "Waning crescent")  # by quadrant


@dataclass(frozen=True)
class MoonPhase:
    illuminated: float  # fraction of the disc lit, 0–1
    phase_deg: float  # 0 new, 90 first quarter, 180 full, 270 last quarter
    waxing: bool
    name: str  # "New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
    #            "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"


def moon_phase(at: datetime) -> MoonPhase:
    """The Moon's phase at the instant `at` (timezone-aware).

    Raises ValueError for a naive datetime, and EphemerisMissing if `de421.bsp` hasn't
    been fetched.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"naive datetime {at!r}: `at` must be timezone-aware")
    from skyfield import almanac

    eph = ephemeris()
    t = timescale().from_datetime(at)
    phase = float(almanac.moon_phase(eph, t).degrees) % 360.0
    lit = float(eph["earth"].at(t).observe(eph["moon"]).apparent().fraction_illuminated(eph["sun"]))
    return MoonPhase(illuminated=min(max(lit, 0.0), 1.0), phase_deg=phase, waxing=phase < 180.0,
                     name=phase_name(phase))


def phase_name(phase_deg: float) -> str:
    """The name for a phase angle (see the module docstring for the windows)."""
    phase = phase_deg % 360.0
    for center, name in PRINCIPAL:
        if abs((phase - center + 180.0) % 360.0 - 180.0) <= PRINCIPAL_WINDOW_DEG:
            return name
    return BETWEEN[int(phase // 90.0) % 4]


def moon_events(day: date, lat: float, lon: float, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    """(moonrise, moonset) on local calendar day `day` (midnight to midnight in `tz`).

    Either is None when the Moon doesn't rise or set that day, which happens about once a
    month for each, since it rises and sets about 50 minutes later each day. If it does
    twice (possible on a 25-hour day when the clocks go back), the first is given.
    Raises EphemerisMissing if `de421.bsp` hasn't been fetched.
    """
    return _moon_events(day, float(lat), float(lon), tz, str(ephemeris_path()))


@lru_cache(maxsize=64)
def _moon_events(day: date, lat: float, lon: float, tz: ZoneInfo,
                 _eph: str) -> tuple[datetime | None, datetime | None]:
    from skyfield import almanac
    from skyfield.api import wgs84

    eph = ephemeris()
    ts = timescale()
    observer = eph["earth"] + wgs84.latlon(lat, lon)
    moon = eph["moon"]
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)  # 23 or 25 h on DST days
    t0, t1 = ts.from_datetime(start), ts.from_datetime(end)

    def first(find) -> datetime | None:
        times, crosses = find(observer, moon, t0, t1)
        hits = sorted(t.utc_datetime() for t, ok in zip(times, crosses) if ok)
        return hits[0].astimezone(tz) if hits else None

    return first(almanac.find_risings), first(almanac.find_settings)
