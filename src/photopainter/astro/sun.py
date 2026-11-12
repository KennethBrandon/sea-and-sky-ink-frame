"""Sunrise, sunset and civil twilight for a day and place.

Sunrise and sunset use the standard definition that NOAA and USNO publish: the Sun's
upper limb on a level horizon, with standard refraction (center 0.8333° below the
horizon). Civil dawn and dusk are when the Sun's center is 6° below the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from photopainter.astro import ephemeris, ephemeris_path, timescale

CIVIL_TWILIGHT_DEG = -6.0


@dataclass(frozen=True)
class SunEvents:
    sunrise: datetime  # aware, in the requested time zone
    sunset: datetime
    civil_dawn: datetime
    civil_dusk: datetime


def sun_events(day: date, lat: float, lon: float, tz: ZoneInfo) -> SunEvents:
    """Sun events on local calendar day `day` (midnight to midnight in `tz`).

    Raises ValueError where the Sun doesn't rise and set that day (polar regions), and
    EphemerisMissing if `de421.bsp` hasn't been fetched.
    """
    # The ephemeris path is part of the cache key, so pointing PHOTOPAINTER_DATA_DIR
    # elsewhere (tests) never returns results computed from another folder's file.
    return _sun_events(day, float(lat), float(lon), tz, str(ephemeris_path()))


@lru_cache(maxsize=64)
def _sun_events(day: date, lat: float, lon: float, tz: ZoneInfo, _eph: str) -> SunEvents:
    from skyfield import almanac
    from skyfield.api import wgs84

    eph = ephemeris()
    ts = timescale()
    observer = eph["earth"] + wgs84.latlon(lat, lon)
    sun = eph["sun"]
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)  # 23 or 25 h on DST days
    t0, t1 = ts.from_datetime(start), ts.from_datetime(end)

    def one(find, horizon: float | None, what: str) -> datetime:
        times, crosses = find(observer, sun, t0, t1, horizon_degrees=horizon)
        hits = [t for t, ok in zip(times, crosses) if ok]
        if len(hits) != 1:
            raise ValueError(f"no single {what} on {day} at ({lat}, {lon}); found {len(hits)}")
        return hits[0].utc_datetime().astimezone(tz)

    return SunEvents(
        sunrise=one(almanac.find_risings, None, "sunrise"),
        sunset=one(almanac.find_settings, None, "sunset"),
        civil_dawn=one(almanac.find_risings, CIVIL_TWILIGHT_DEG, "civil dawn"),
        civil_dusk=one(almanac.find_settings, CIVIL_TWILIGHT_DEG, "civil dusk"),
    )
