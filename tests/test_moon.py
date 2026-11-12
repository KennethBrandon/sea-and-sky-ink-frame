from datetime import date, datetime, timedelta, timezone
from math import cos, radians
from zoneinfo import ZoneInfo

import pytest

from photopainter import state
from photopainter.astro import EphemerisMissing
from photopainter.astro.moon import MoonPhase, moon_events, moon_phase, phase_name

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
WEDGE = (33.593, -117.882)

# Principal phases from the US Naval Observatory, fetched 2026-11-03 (times in UT):
#   https://aa.usno.navy.mil/api/moon/phases/date?date=2026-10-28&nump=8
USNO_PHASES = [
    (datetime(2026, 11, 1, 20, 28, tzinfo=UTC), "Last quarter", 270.0),
    (datetime(2026, 11, 9, 7, 2, tzinfo=UTC), "New moon", 0.0),
    (datetime(2026, 11, 17, 11, 48, tzinfo=UTC), "First quarter", 90.0),
    (datetime(2026, 11, 24, 14, 53, tzinfo=UTC), "Full moon", 180.0),
    (datetime(2026, 12, 1, 6, 8, tzinfo=UTC), "Last quarter", 270.0),
]
# The Moon gains about 12.2° a day on the Sun, so 1.5° is about 3 hours.
PHASE_TOLERANCE_DEG = 1.5

# Moonrise and moonset at the Wedge from USNO, fetched 2026-11-03, local clock time:
#   https://aa.usno.navy.mil/api/rstt/oneday?date=<day>&coords=33.593,-117.882&tz=-8&dst=true
USNO_RISE_SET = {
    date(2026, 11, 1): ("23:33", "12:50"),  # the clocks go back: a 25-hour day
    date(2026, 11, 2): (None, "13:25"),  # no moonrise this day
    date(2026, 11, 3): ("00:40", "13:55"),
    date(2026, 11, 9): ("06:50", "16:55"),
    date(2026, 11, 16): ("12:13", "23:08"),
    date(2026, 11, 17): ("12:41", None),  # no moonset this day
    date(2026, 11, 24): ("16:43", "06:46"),
}
TOLERANCE = timedelta(minutes=2)


def _signed_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


@pytest.mark.parametrize("at, name, center", USNO_PHASES)
def test_principal_phases_match_usno(ephemeris, at, name, center):
    p = moon_phase(at)
    assert isinstance(p, MoonPhase)
    assert abs(_signed_diff(p.phase_deg, center)) <= PHASE_TOLERANCE_DEG
    assert p.name == name
    expected_lit = {0.0: 0.0, 90.0: 0.5, 180.0: 1.0, 270.0: 0.5}[center]
    assert abs(p.illuminated - expected_lit) <= 0.02
    # The name holds for a few hours either side of the instant.
    for hours in (-3, 3):
        assert moon_phase(at + timedelta(hours=hours)).name == name


def test_phase_angle_crosses_each_center_near_the_usno_time(ephemeris):
    """Our own phase instants, found by bisection, are within an hour of USNO's."""
    for at, _, center in USNO_PHASES:
        lo, hi = at - timedelta(hours=12), at + timedelta(hours=12)
        assert _signed_diff(moon_phase(lo).phase_deg, center) < 0 < _signed_diff(moon_phase(hi).phase_deg, center)
        while hi - lo > timedelta(minutes=1):
            mid = lo + (hi - lo) / 2
            if _signed_diff(moon_phase(mid).phase_deg, center) < 0:
                lo = mid
            else:
                hi = mid
        assert abs(lo - at) <= timedelta(hours=1)


@pytest.mark.parametrize("at, name, waxing", [
    (datetime(2026, 11, 5, 12, tzinfo=LA), "Waning crescent", False),
    (datetime(2026, 11, 13, 12, tzinfo=LA), "Waxing crescent", True),
    (datetime(2026, 11, 20, 12, tzinfo=LA), "Waxing gibbous", True),
    (datetime(2026, 11, 28, 12, tzinfo=LA), "Waning gibbous", False),
])
def test_in_between_phases(ephemeris, at, name, waxing):
    p = moon_phase(at)
    assert (p.name, p.waxing) == (name, waxing)
    assert 0.0 < p.illuminated < 1.0


def test_illumination_follows_the_phase_angle_through_a_month(ephemeris):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    previous = None
    for step in range(0, 30 * 24, 6):
        p = moon_phase(start + timedelta(hours=step))
        assert 0.0 <= p.illuminated <= 1.0 and 0.0 <= p.phase_deg < 360.0
        assert abs(p.illuminated - (1 - cos(radians(p.phase_deg))) / 2) < 0.01
        assert p.waxing == (p.phase_deg < 180.0)
        if previous is not None and previous.waxing and p.waxing:
            assert p.illuminated > previous.illuminated
        previous = p


def test_same_instant_any_time_zone(ephemeris):
    at = datetime(2026, 11, 24, 6, 53, tzinfo=LA)
    assert moon_phase(at) == moon_phase(at.astimezone(UTC))


def test_naive_datetime_is_refused(ephemeris):
    with pytest.raises(ValueError, match="timezone-aware"):
        moon_phase(datetime(2026, 11, 24, 12))


@pytest.mark.parametrize("phase, name", [
    (0.0, "New moon"), (6.0, "New moon"), (354.0, "New moon"), (359.99, "New moon"), (360.0, "New moon"),
    (6.1, "Waxing crescent"), (83.9, "Waxing crescent"),
    (84.0, "First quarter"), (96.0, "First quarter"),
    (96.1, "Waxing gibbous"), (173.9, "Waxing gibbous"),
    (174.0, "Full moon"), (186.0, "Full moon"),
    (186.1, "Waning gibbous"), (263.9, "Waning gibbous"),
    (264.0, "Last quarter"), (276.0, "Last quarter"),
    (276.1, "Waning crescent"), (353.9, "Waning crescent"),
])
def test_phase_names(phase, name):
    assert phase_name(phase) == name


def _local(day: date, hhmm: str | None) -> datetime | None:
    if hhmm is None:
        return None
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LA)


@pytest.mark.parametrize("day", sorted(USNO_RISE_SET))
def test_moonrise_and_moonset_match_usno(ephemeris, day):
    rise, set_ = moon_events(day, *WEDGE, LA)
    for got, hhmm, what in ((rise, USNO_RISE_SET[day][0], "rise"), (set_, USNO_RISE_SET[day][1], "set")):
        expected = _local(day, hhmm)
        if expected is None:
            assert got is None, f"{what}: expected none, got {got}"
        else:
            assert got is not None and abs(got - expected) <= TOLERANCE, f"{what}: {got} vs USNO {hhmm}"
            assert got.tzinfo is LA and got.date() == day


def test_moon_events_cover_a_month_without_errors(ephemeris):
    """About one day a month has no rise and one no set; every other day has one of each."""
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(30)]
    events = [moon_events(d, *WEDGE, LA) for d in days]
    assert 1 <= sum(r is None for r, _ in events) <= 2
    assert 1 <= sum(s is None for _, s in events) <= 2


def test_missing_ephemeris_says_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setenv(state.DATA_DIR_ENV, str(tmp_path))
    with pytest.raises(EphemerisMissing, match="scripts/fetch-ephemeris.sh"):
        moon_phase(datetime(2026, 11, 3, tzinfo=UTC))
    with pytest.raises(EphemerisMissing):
        moon_events(date(2026, 11, 3), *WEDGE, LA)
