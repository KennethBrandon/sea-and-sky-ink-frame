from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from photopainter import state
from photopainter.astro import EphemerisMissing
from photopainter.astro.sun import SunEvents, sun_events

LA = ZoneInfo("America/Los_Angeles")
WEDGE = (33.593, -117.882)
TOLERANCE = timedelta(minutes=2)

# Reference times from the US Naval Observatory, fetched once on 2026-11-03:
#   https://aa.usno.navy.mil/api/rstt/oneday?date=<day>&coords=33.593,-117.882&tz=-8&dst=true
# Local clock times (PDT/PST as USNO applies US daylight saving), rounded to the minute.
# Two of the days are 2026's daylight-saving changes (23- and 25-hour days).
USNO = {
    #               civil dawn  sunrise   sunset   civil dusk
    date(2026, 3, 8): ("06:46", "07:11", "18:54", "19:19"),  # DST starts
    date(2026, 6, 21): ("05:13", "05:42", "20:05", "20:34"),  # June solstice
    date(2026, 11, 1): ("05:45", "06:11", "16:59", "17:25"),  # DST ends
    date(2026, 12, 21): ("06:24", "06:52", "16:47", "17:15"),  # December solstice
}


def _local(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LA)


@pytest.mark.parametrize("day", sorted(USNO))
def test_matches_usno(ephemeris, day):
    events = sun_events(day, *WEDGE, LA)
    expected = dict(zip(("civil_dawn", "sunrise", "sunset", "civil_dusk"), USNO[day]))
    for name, hhmm in expected.items():
        got = getattr(events, name)
        assert abs(got - _local(day, hhmm)) <= TOLERANCE, f"{name}: {got} vs USNO {hhmm}"


def test_results_are_aware_local_and_ordered(ephemeris):
    day = date(2026, 11, 3)
    events = sun_events(day, *WEDGE, LA)
    assert isinstance(events, SunEvents)
    times = [events.civil_dawn, events.sunrise, events.sunset, events.civil_dusk]
    assert times == sorted(times)
    for t in times:
        assert t.tzinfo is LA
        assert t.date() == day
    with pytest.raises(AttributeError):
        events.sunrise = events.sunset  # frozen


def test_other_time_zone_gives_same_instants(ephemeris):
    utc = ZoneInfo("UTC")
    a = sun_events(date(2026, 6, 21), *WEDGE, LA)
    b = sun_events(date(2026, 6, 22), *WEDGE, utc)  # UTC day 06-22 holds LA's 06-21 sunset
    assert abs(a.sunset - b.sunset) < timedelta(seconds=1)
    assert b.sunset.tzinfo is utc


def test_polar_night_raises(ephemeris):
    with pytest.raises(ValueError, match="sunrise"):
        sun_events(date(2026, 12, 21), 80.0, 15.0, ZoneInfo("Arctic/Longyearbyen"))


def test_missing_ephemeris_says_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setenv(state.DATA_DIR_ENV, str(tmp_path))
    with pytest.raises(EphemerisMissing, match="scripts/fetch-ephemeris.sh"):
        sun_events(date(2026, 11, 3), *WEDGE, LA)
