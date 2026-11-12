"""The day screen (scenes/tides.py): its data model, helpers and rendering in every state.

The cache fixtures (tests/fixtures/{tides,weather,surf}.json) are one real NOAA and
Open-Meteo fetch made on 2026-11-03 at 23:09 PST, in the PRD §5 cache formats.
"""

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import cli, state
from photopainter.config import default_config
from photopainter.render.draw import INK, font
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import tides as day

FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
AT = datetime(2026, 11, 3, 14, 10, tzinfo=LA)  # after the 2 PM photo slot


def at(hour, minute=0, day_of_month=3):
    return datetime(2026, 11, day_of_month, hour, minute, tzinfo=LA)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def write_caches(now=AT, age=timedelta(minutes=20), skip=(), shift=timedelta(0)):
    """The fixtures in the state dir, weather and surf fetched `age` before `now`, every
    timestamp moved by `shift`."""
    folder = state.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("tides.json", "weather.json", "surf.json"):
        if name in skip:
            continue
        data = _shifted(fixture(name), shift)
        if name != "tides.json":
            data["fetched_at"] = state.to_iso(now - age)
        (folder / name).write_text(json.dumps(data))
    return folder


def _shifted(data, shift):
    if not shift:
        return data
    move = lambda t: state.to_iso(state.from_iso(t) + shift)  # noqa: E731
    for key in ("curve", "hilo", "hourly"):
        for row in data.get(key, []):
            row["t"] = move(row["t"])
    if "current" in data:
        data["current"]["t"] = move(data["current"]["t"])
    return data


def context(config=None, portrait=False):
    config = config or default_config()
    if portrait:
        config = replace(config, display=replace(config.display, portrait=True))
    return SceneContext.from_config(config)


def draw(now=AT, config=None, portrait=False):
    return render_scene(Scene("tides", f"tides/{now:%Y-%m-%dT%H:%M}", now), context(config, portrait))


def with_tides(**settings):
    config = default_config()
    return replace(config, tides=replace(config.tides, **settings))


def drawn_labels(monkeypatch, config, now=AT):
    """Every label the chart draws: [(text, box)]. The boxes are the text's own."""
    write_caches(now)
    got = []
    real = day.pills

    def spy(c, labels, size):
        got.extend((s, day.pill_rect(c, xy, s, size, anchor=anchor, pad=(0, 0))) for xy, s, anchor in labels)
        real(c, labels, size)

    monkeypatch.setattr(day, "pills", spy)
    draw(now, config)
    return got


def red_pixels_in_top_row(indices):
    """Red ink in the boxes' area (header and boxes, not the chart): only "as of" notes."""
    return int((indices[58:292] == INK["red"]).sum())


# --- tide ------------------------------------------------------------------------------------


def test_tide_now_rising_with_next_high_and_low():
    tide = day.load_tide(fixture("tides.json"), AT)
    assert tide.rising
    assert (tide.next_high.t, tide.next_high.v) == (at(16, 49), 4.65)
    assert tide.next_low.t == at(23, 23)
    assert 1.982 < tide.height < 4.65  # between the 11:13 low and the 4:49 high
    assert round(tide.height, 1) == 3.4


def test_tide_falling_after_the_morning_high():
    tide = day.load_tide(fixture("tides.json"), at(6, 30))
    assert not tide.rising
    assert tide.next_low.t == at(11, 13)
    assert tide.next_high.t == at(16, 49)


def test_tide_curve_passes_through_the_noaa_points():
    data = fixture("tides.json")
    tide = day.load_tide(data, AT)
    for p in data["curve"][::17]:
        assert tide.height_at(state.from_iso(p["t"]).timestamp())[0] == pytest.approx(p["v"], abs=1e-9)


def test_tide_outside_the_cache_or_unreadable_is_none():
    data = fixture("tides.json")
    assert day.load_tide(data, datetime(2026, 11, 1, 12, tzinfo=LA)) is None  # before it starts
    assert day.load_tide(data, datetime(2026, 11, 10, 12, tzinfo=LA)) is None  # after it ends
    for bad in (None, {}, {"curve": "x", "hilo": []}, {"curve": [{"t": "nope", "v": 1}], "hilo": []},
                {"curve": [{"t": "2026-11-03T00:00:00-08:00", "v": 1}], "hilo": []}):
        assert day.load_tide(bad, AT) is None


def test_tide_past_the_last_high_or_low_goes_by_the_curve():
    data = {"hilo": [], "curve": [{"t": "2026-11-03T14:00:00-08:00", "v": 1.0},
                                  {"t": "2026-11-03T14:30:00-08:00", "v": 2.0}]}
    tide = day.load_tide(data, AT)
    assert tide.rising and tide.next_high is None and tide.next_low is None


# --- weather and swell -------------------------------------------------------------------------


def test_weather_uses_current_near_now_else_the_forecast_hour():
    data = fixture("weather.json")
    data["fetched_at"] = state.to_iso(at(23, 5))
    late = day.load_weather(data, at(23, 10))  # "current" is for 23:00
    assert (late.temp, late.code, late.wind, late.gust, late.wind_dir) == (64.5, 3, 4.7, 10.7, 316)
    data["fetched_at"] = state.to_iso(at(14, 0))
    afternoon = day.load_weather(data, AT)
    row = next(r for r in data["hourly"] if r["t"] == "2026-11-03T14:00:00-08:00")
    assert (afternoon.temp, afternoon.code, afternoon.gust) == (row["temp"], row["code"], None)
    assert (afternoon.high, afternoon.low, afternoon.precip_chance) == (74.3, 57.8, 0)


@pytest.mark.parametrize("age, shown, stale", [
    (timedelta(hours=1), True, False),
    (timedelta(hours=2, minutes=1), True, True),
    (timedelta(hours=11, minutes=59), True, True),
    (timedelta(hours=12, minutes=1), False, None),
    (timedelta(hours=-3), True, False),  # stamped in the future: the clock was off; still shown
])
def test_weather_and_swell_age(age, shown, stale):
    for name, load in (("weather.json", day.load_weather), ("surf.json", day.load_swell)):
        data = fixture(name)
        data["fetched_at"] = state.to_iso(AT - age)
        got = load(data, AT)
        assert (got is not None) == shown, name
        if shown:
            assert got.stale == stale and got.fetched_at == AT - age


def test_weather_and_swell_missing_or_unreadable_is_none():
    for load in (day.load_weather, day.load_swell):
        for bad in (None, {}, [], {"fetched_at": "yesterday"}, {"fetched_at": state.to_iso(AT)},
                    {"fetched_at": state.to_iso(AT), "hourly": [{"t": "2026-11-03T14:00:00-08:00"}]}):
            assert load(bad, AT) is None


def test_swell_for_the_nearest_hour():
    data = fixture("surf.json")
    data["fetched_at"] = state.to_iso(AT - timedelta(minutes=30))
    swell = day.load_swell(data, AT)
    row = next(r for r in data["hourly"] if r["t"] == "2026-11-03T14:00:00-08:00")
    assert (swell.swell_height, swell.swell_period, swell.swell_dir, swell.water_temp) == (
        row["swell_height"], row["swell_period"], row["swell_dir"], row["water_temp"])
    assert swell.wave_height == row["wave_height"] and not swell.stale
    assert day.load_swell(data, at(14, 10, day_of_month=9)) is None  # past the forecast


# --- the whole model ---------------------------------------------------------------------------


def test_day_model_reads_the_state_dir(ephemeris):
    write_caches()
    m = day.day_model(AT, context())
    assert m.tide and m.weather and m.swell and m.moon and m.sun
    assert m.place == "The Wedge" and m.now == AT and m.daytime
    assert m.sun.sunrise.strftime("%H:%M") == "06:12" and m.sun.sunset.strftime("%H:%M") == "16:57"
    assert m.moon.name == "Waning crescent"
    assert not day.day_model(at(22), context()).daytime


def test_day_model_leaves_out_disabled_sections(ephemeris):
    write_caches()
    config = default_config()
    off = replace(config, weather=replace(config.weather, enabled=False),
                  surf=replace(config.surf, enabled=False))
    m = day.day_model(AT, context(off))
    assert m.weather is None and m.swell is None and m.tide is not None


# --- sky events ------------------------------------------------------------------------------

# USNO, as in tests/test_moon.py
NEW_MOON = datetime(2026, 11, 9, 7, 2, tzinfo=UTC)
FULL_MOON = datetime(2026, 11, 24, 14, 53, tzinfo=UTC)

# Every eclipse of 2024-2027 (NASA's eclipse catalog), greatest eclipse in UTC, to the hour.
ECLIPSES = [
    ("2024-03-25 07", "lunar_eclipse"), ("2024-04-08 18", "solar_eclipse"),
    ("2024-09-18 02", "lunar_eclipse"), ("2024-10-02 18", "solar_eclipse"),
    ("2025-03-14 06", "lunar_eclipse"), ("2025-03-29 10", "solar_eclipse"),
    ("2025-09-07 18", "lunar_eclipse"), ("2025-09-21 19", "solar_eclipse"),
    ("2026-02-17 12", "solar_eclipse"), ("2026-03-03 11", "lunar_eclipse"),
    ("2026-08-12 17", "solar_eclipse"), ("2026-08-28 04", "lunar_eclipse"),
    ("2027-02-06 15", "solar_eclipse"), ("2027-02-20 23", "lunar_eclipse"),
    ("2027-07-18 16", "lunar_eclipse"), ("2027-08-02 10", "solar_eclipse"),
    ("2027-08-17 07", "lunar_eclipse"),
]


def test_new_and_full_moons_of_november_2026(ephemeris):
    events = day.sky_events(datetime(2026, 11, 2, tzinfo=LA), datetime(2026, 12, 1, tzinfo=LA))
    assert [e.kind for e in events] == ["new", "full"]
    assert abs(events[0].t - NEW_MOON) < timedelta(minutes=10)
    assert abs(events[1].t - FULL_MOON) < timedelta(minutes=10)
    assert events[0].t.tzinfo == LA


def test_every_eclipse_2024_to_2027_and_no_others(ephemeris):
    events = day.sky_events(datetime(2024, 1, 1, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC))
    found = [(e.t.astimezone(UTC).strftime("%Y-%m-%d %H"), e.kind) for e in events if "eclipse" in e.kind]
    assert found == ECLIPSES
    for kinds in (("new", "solar_eclipse"), ("full", "lunar_eclipse")):  # none missing
        times = [e.t for e in events if e.kind in kinds]
        gaps = [(b - a) / timedelta(days=1) for a, b in zip(times, times[1:])]
        assert len(times) >= 49 and all(29.2 < g < 29.9 for g in gaps)


# --- formatting and layout helpers --------------------------------------------------------------


def test_formatting():
    assert [day.ft(v) for v in (3.44, 0.04, -0.04, -1.4, None)] == ["3.4", "0.0", "0.0", "−1.4", "–"]
    assert [day.clock(datetime(2026, 11, 3, h, m)) for h, m in ((0, 2), (5, 27), (12, 7), (16, 49))] == [
        "12:02a", "5:27a", "12:07p", "4:49p"]
    assert [day.compass(d) for d in (0, 11, 12, 190, 316, 350, None)] == ["N", "N", "NNE", "S", "NW", "N", "–"]
    assert [day.deg(v) for v in (73.2, None)] == ["73°", "–"]


def test_conditions_by_day_and_night():
    assert day.condition(0) == ("Sunny", "sun")
    assert day.condition(0, daytime=False) == ("Clear", "moon")
    assert day.condition(2, daytime=False) == ("Partly cloudy", "moon_cloud")
    assert day.condition(63) == ("Rain", "rain")
    assert day.condition(None) == day.condition(12345) == ("", "cloud")


def test_wind_line_shortens_to_fit():
    w = day.Weather(64.5, 3, 4.7, 10.7, 316, 74, 58, 0, AT, False)
    f = font(20)
    assert day.wind_line(w, 400, f) == "NW 5–11 mph · Rain 0%"
    short = day.text_width("NW 5–11 · Rain 0%", f)
    assert day.wind_line(w, short, f) == "NW 5–11 · Rain 0%"
    assert day.wind_line(w, short - 1, f) == "NW 5 · Rain 0%"
    assert day.wind_line(replace(w, gust=None, precip_chance=None), 400, f) == "NW 5 mph"
    assert day.wind_line(replace(w, gust=5.2), 400, f) == "NW 5 mph · Rain 0%"  # no gust over the wind
    assert day.wind_line(replace(w, wind=None), 400, f) == "Rain 0%"


def test_top_row_fills_a_missing_section_with_sun_and_moon():
    assert day.top_row(12, 788, True, True) == [("marine", 12, 520), ("weather", 532, 788)]
    assert day.top_row(12, 788, False, True) == [("marine", 12, 274), ("sun_moon", 286, 520),
                                                ("weather", 532, 788)]
    assert day.top_row(12, 788, True, False) == [("marine", 12, 520), ("sun_moon", 532, 788)]
    assert day.top_row(12, 788, False, False) == [("marine", 12, 274), ("sun_moon", 286, 788)]


def test_scale_marks_pick_the_highest_high_and_lowest_low():
    e = [day.Extreme(at(h), v, high) for h, v, high in
         ((1, 4.0, True), (5, -0.3, False), (9, 5.6, True), (13, 0.2, False))]
    assert day.scale_marks(e) == (e[2], e[1])
    assert day.scale_marks([]) == (None, None)


E_HIGH = day.Extreme(at(9), 5.6, True)
E_LOW = day.Extreme(at(5), -0.3, False)
E_PLAIN = day.Extreme(at(16, 49), 4.65, True)


@pytest.mark.parametrize("e, show_times, heights, label", [
    (E_PLAIN, True, "extremes", "4:49p"),
    (E_HIGH, True, "extremes", "9:00a 5.6 ft"),
    (E_LOW, True, "extremes", "5:00a −0.3 ft"),
    (E_PLAIN, True, "all", "4:49p 4.7 ft"),
    (E_HIGH, True, "none", "9:00a"),
    (E_PLAIN, False, "extremes", ""),
    (E_HIGH, False, "extremes", "5.6 ft"),
    (E_PLAIN, False, "all", "4.7 ft"),
    (E_HIGH, False, "none", ""),
])
def test_extreme_label(e, show_times, heights, label):
    assert day.extreme_label(e, (E_HIGH, E_LOW), show_times, heights) == label


def test_label_priority_is_highest_and_lowest_then_upcoming_then_past():
    past = day.Extreme(at(1), 2.0, False)
    soon = day.Extreme(at(15), 3.0, True)
    later = day.Extreme(at(20), 1.0, False)
    now = at(14)
    got = day.label_priority([past, E_LOW, E_HIGH, later, soon], (E_HIGH, E_LOW), now)
    assert got[:2] == [E_HIGH, E_LOW] or got[:2] == [E_LOW, E_HIGH]  # marks first…
    assert got[2:] == [soon, later, past]  # …then what's next


def test_daily_extremes_keep_each_days_highest_high_and_lowest_low():
    def e(day_of_month, hour, v, high):
        return day.Extreme(at(hour, 0, day_of_month), v, high)

    tuesday = [e(3, 1, 4.0, True), e(3, 5, 0.5, False), e(3, 11, 5.0, True), e(3, 18, 1.5, False)]
    wednesday = [e(4, 2, 1.0, False), e(4, 8, 3.0, True), e(4, 14, -0.2, False), e(4, 20, 4.5, True)]
    assert day.daily_extremes(tuesday + wednesday) == [tuesday[1], tuesday[2], wednesday[2], wednesday[3]]


def test_daily_extremes_with_part_of_a_day_in_view():
    only_a_high = [day.Extreme(at(23), 4.0, True)]  # the day's other tides are out of view
    assert day.daily_extremes(only_a_high) == only_a_high
    assert day.daily_extremes([]) == []


def test_rects_overlap():
    assert day.rects_overlap((0, 0, 10, 10), (5, 5, 15, 15))
    assert not day.rects_overlap((0, 0, 10, 10), (10, 0, 20, 10))  # touching is fine
    assert not day.rects_overlap((0, 0, 10, 10), (0, 10, 10, 20))


def test_hides_curve_only_where_the_line_passes_behind_the_box():
    ys = np.array([50.0, 50, 20, 20, 50, np.nan, np.nan, 25])  # columns from x0 = 100
    assert day.hides_curve((101, 15, 103, 30), ys, 100)  # the line's at y 20 in columns 102-103
    assert not day.hides_curve((101, 30, 103, 45), ys, 100)  # it's above and below this box
    assert not day.hides_curve((105, 15, 106, 30), ys, 100)  # no data there
    assert day.hides_curve((90, 15, 102, 30), ys, 100)  # a box starting left of the chart


@pytest.mark.parametrize("day_width, today, label", [
    (258, True, "Today"), (258, False, "Tue"), (60, True, "Tue"), (60, False, "Tue"),
    (37, True, "T"), (37, False, "T"),
])
def test_day_label_shortens_with_the_days_width(day_width, today, label):
    assert day.day_label(at(12).date(), today, day_width, 20) == label


@pytest.mark.parametrize("day_width, length", [(60, 3), (40, 1)])
def test_day_labels_shorten_together(day_width, length):
    """Not "Fri" beside "W", though "Fri" is narrow enough to stay at 40 px."""
    week = [at(12, 0, d).date() for d in range(1, 8)]
    assert {len(day.day_label(d, False, day_width, 20)) for d in week} == {length}


@pytest.mark.parametrize("from_deg, dx, dy", [
    (270, 1, 0),  # from the west, moving east: right
    (90, -1, 0),  # from the east: left
    (180, 0, -1),  # from the south, moving north: up
    (0, 0, 1),  # from the north: down
    (225, 1, -1),  # from the southwest: up and right
    (315, 1, 1),  # from the northwest: down and right
])
def test_flow_arrow_points_the_way_it_moves(from_deg, dx, dy):
    c = day.InkCanvas((60, 60), "white")
    day.flow_arrow(c, 30, 30, 40, from_deg)
    ys, xs = np.nonzero(c.indices() == INK["blue"])
    # the head is the heavy end: most of the ink is on the side the arrow points to
    ahead = (xs - 30) * dx + (ys - 30) * dy > 0
    assert ahead.sum() > 1.3 * (~ahead).sum()

    # turned 180° as seen facing south: from the west points left, from the south down
    c = day.InkCanvas((60, 60), "white")
    day.flow_arrow(c, 30, 30, 40, from_deg, flip=True)
    ys, xs = np.nonzero(c.indices() == INK["blue"])
    ahead = (xs - 30) * -dx + (ys - 30) * -dy > 0
    assert ahead.sum() > 1.3 * (~ahead).sum()


@pytest.mark.parametrize("settings, flip", [({}, True), ({"flip_arrows": False}, False)])
def test_flip_arrows_setting_reaches_the_swell_and_wind_arrows(monkeypatch, settings, flip):
    write_caches()
    calls = []
    monkeypatch.setattr(day, "flow_arrow", lambda *a, **kw: calls.append(kw.get("flip", False)))
    draw(config=with_tides(**settings))
    assert calls == [flip, flip]  # one for each


def _weather(**changes):
    return replace(day.Weather(64.5, 3, 4.7, 10.7, 316, 74, 58, 0, AT, False), **changes)


def test_wind_text_draws_an_arrow_only_with_a_direction():
    def blue(w):
        c = day.InkCanvas((300, 40), "white")
        day.wind_text(c, 4, 30, 290, w, 20)
        return int((c.indices() == INK["blue"]).sum())

    assert blue(_weather()) > 30
    assert blue(_weather(wind_dir=None)) == 0  # "– 5 mph"
    assert blue(_weather(wind=None)) == 0  # just the rain chance
    assert blue(_weather(wind=None, precip_chance=None)) == 0  # nothing at all


def test_place_label_steps_aside_from_icons():
    assert day.place_label(100, 40, 0, 200, []) == 100
    assert day.place_label(100, 40, 0, 200, [(90, 110)]) == 70  # nearest free side
    assert day.place_label(20, 40, 0, 200, [(10, 40)]) == 60  # the other side when one won't fit
    assert day.place_label(100, 40, 80, 120, [(90, 110)]) is None


def test_header_date_shortens_when_the_place_name_is_long():
    m = day.DayModel("The Wedge", 0, 0, datetime(2026, 9, 30, 12, tzinfo=LA), None, None, None, None, None)
    assert day.header_date(m, 568) == ("Wed, September 30", 30)
    assert day.header_date(replace(m, now=AT), 568) == ("Tuesday, November 3", 30)
    assert day.header_date(replace(m, place="Corona del Mar State Beach"), 568)[0] == "Wed, Sep 30"


def test_bayer_thresholds_cover_every_level_once():
    assert np.array_equal(np.sort(day._bayer(8).ravel()), (np.arange(64) + 0.5) / 64)


# --- rendering ---------------------------------------------------------------------------------


@pytest.mark.parametrize("now", [at(6, 30), at(12, 0), AT, at(16, 50), at(22, 0), at(23, 59)])
def test_renders_at_any_time_of_day(ephemeris, now):
    write_caches(now)
    indices = draw(now)
    assert indices.shape == (480, 800) and indices.dtype == np.uint8
    assert red_pixels_in_top_row(indices) == 0  # nothing stale
    assert (indices[300:] == INK["blue"]).sum() > 20_000  # the water


def test_chart_span_follows_the_settings(ephemeris):
    """The water starts and ends where the settings say (the cache runs Nov 2 to Nov 9)."""
    write_caches()
    x0, width = day.MARGIN, 800 - 2 * day.MARGIN
    water = lambda indices: np.flatnonzero((indices[300:440] == INK["blue"]).any(axis=0))  # noqa: E731
    default = water(draw())
    assert default[0] <= x0 + 1 and default[-1] >= x0 + width - 1

    # now is a sixth of the way across with half a day behind and two and a half ahead
    half = draw(config=with_tides(chart_days_behind=0.5, chart_days_ahead=2.5))
    red = np.flatnonzero((half[300:320] == INK["red"]).any(axis=0))
    assert abs(red.mean() - (x0 + width / 6)) < 2
    # nothing behind: now is at the left edge
    none = draw(config=with_tides(chart_days_behind=0, chart_days_ahead=1))
    red = np.flatnonzero((none[300:320] == INK["red"]).any(axis=0))
    assert red.mean() < x0 + 2


def test_chart_with_days_beyond_the_cache_leaves_them_empty(ephemeris):
    write_caches()
    indices = draw(config=with_tides(chart_days_behind=7, chart_days_ahead=14))
    water = np.flatnonzero((indices[300:440] == INK["blue"]).any(axis=0))
    assert water[0] > 100 and water[-1] < 500  # 21 days across; the cache has about 7 of them


@pytest.mark.parametrize("now", [at(6, 30), AT, at(22, 0), at(9, 20, 4)])
def test_default_chart_labels_every_high_and_low(ephemeris, monkeypatch, now):
    labels = drawn_labels(monkeypatch, default_config(), now)
    tide = day.load_tide(fixture("tides.json"), now)
    shown = [e for e in tide.extremes_between(now - timedelta(days=1), now + timedelta(days=2))
             if tide.covers(e.t)]
    assert len(labels) == len(shown)
    with_height = [s for s, _ in labels if "ft" in s]
    assert len(with_height) == 2  # the highest and the lowest


def test_time_and_height_settings_change_the_labels(ephemeris, monkeypatch):
    def texts(**settings):
        monkeypatch.setattr(day, "pills", real)
        return [s for s, _ in drawn_labels(monkeypatch, with_tides(**settings))]

    real = day.pills
    every = texts(label_heights="all")
    assert every and all(s.endswith(" ft") and ("a " in s or "p " in s) for s in every)
    assert all(not s.endswith("ft") for s in texts(label_heights="none"))
    only_heights = texts(label_times=False)
    assert len(only_heights) == 2 and all(s.endswith(" ft") for s in only_heights)
    assert all(s.endswith(" ft") and ":" not in s for s in texts(label_times=False, label_heights="all"))
    assert texts(label_times=False, label_heights="none") == []


def test_one_high_and_one_low_a_day(ephemeris, monkeypatch):
    start, end = AT - timedelta(days=1), AT + timedelta(days=2)
    tide = day.load_tide(fixture("tides.json"), AT)
    in_view = [e for e in tide.extremes_between(start, end) if tide.covers(e.t)]
    expected = day.daily_extremes(in_view)
    assert len(expected) < len(in_view)

    labels = drawn_labels(monkeypatch, with_tides(label_tides="daily"))
    assert sorted(s.split()[0] for s, _ in labels) == sorted(day.clock(e.t) for e in expected)
    assert len([s for s, _ in labels if "ft" in s]) == 2  # still the highest and lowest in view


def test_one_a_day_works_with_the_other_label_settings(ephemeris, monkeypatch):
    def texts(**settings):
        monkeypatch.setattr(day, "pills", real)
        return [s for s, _ in drawn_labels(monkeypatch, with_tides(label_tides="daily", **settings))]

    real = day.pills
    every = texts(label_heights="all")
    assert every and all(s.endswith(" ft") for s in every)
    assert all(not s.endswith("ft") for s in texts(label_heights="none"))
    assert len(texts(label_times=False)) == 2  # heights only, on the highest and lowest
    assert texts(label_times=False, label_heights="none") == []


def test_no_labels_gives_the_curve_more_room(ephemeris):
    write_caches()

    def water_top(indices):
        return np.flatnonzero((indices[300:440] == INK["blue"]).any(axis=1))[0]

    assert water_top(draw(config=with_tides(label_times=False, label_heights="none"))) < water_top(draw()) - 10


def test_crowded_chart_drops_labels_rather_than_overlap(ephemeris, monkeypatch):
    settings = with_tides(chart_days_behind=0.5, chart_days_ahead=7, label_heights="all")
    labels = drawn_labels(monkeypatch, settings)
    assert labels
    boxes = [box for _, box in labels]
    for i, a in enumerate(boxes):
        assert not any(day.rects_overlap(a, b) for b in boxes[i + 1:])
    tide = day.load_tide(fixture("tides.json"), AT)
    in_view = tide.extremes_between(AT - timedelta(days=0.5), AT + timedelta(days=7))
    assert len(labels) < len([e for e in in_view if tide.covers(e.t)])
    assert any("−" in s or s.split()[-2] == day.ft(day.scale_marks(in_view)[1].v) for s, _ in labels)


def test_now_line_runs_under_the_labels(ephemeris, monkeypatch):
    """A label across the now line stays readable: the box covers the line (at 4:49 p.m.,
    the line goes right through the high's label)."""
    now = at(16, 49)
    labels = drawn_labels(monkeypatch, default_config(), now)
    box = next(b for s, b in labels if s.startswith("4:49p"))
    indices = draw(now)
    assert not (indices[box[1]:box[3], box[0]:box[2]] == INK["red"]).any()


@pytest.mark.parametrize("settings", [
    dict(chart_days_behind=0, chart_days_ahead=0.25),
    dict(chart_days_behind=0.25, chart_days_ahead=0),
    dict(chart_days_behind=7, chart_days_ahead=14),
    dict(chart_days_behind=0.5, chart_days_ahead=7, label_heights="all", label_times=False),
    dict(chart_days_behind=7, chart_days_ahead=14, label_tides="daily", label_heights="all"),
])
def test_chart_renders_at_the_ends_of_the_ranges(ephemeris, settings):
    write_caches()
    assert draw(config=with_tides(**settings)).shape == (480, 800)


def test_renders_on_the_day_the_clocks_go_back(ephemeris):
    now = datetime(2026, 11, 1, 10, tzinfo=LA)  # a 25-hour day
    write_caches(now, shift=now - AT)
    assert draw(now).shape == (480, 800)


def test_stale_data_says_how_old():
    write_caches(age=timedelta(hours=3))
    assert red_pixels_in_top_row(draw()) > 0
    write_caches(age=timedelta(minutes=20))
    assert red_pixels_in_top_row(draw()) == 0


@pytest.mark.parametrize("skip", [
    ("weather.json",), ("surf.json",), ("weather.json", "surf.json"), ("tides.json",),
    ("tides.json", "weather.json", "surf.json"),
])
def test_renders_with_sections_missing(skip):
    write_caches(skip=skip)
    indices = draw()
    assert indices.shape == (480, 800)
    if "tides.json" in skip:
        assert (indices == INK["blue"]).sum() < 5_000  # no water drawn


def test_renders_with_old_or_corrupt_caches():
    folder = write_caches(age=timedelta(hours=13))
    draw()
    for name in ("tides.json", "weather.json", "surf.json"):
        (folder / name).write_text("{not json")
    assert draw().shape == (480, 800)


def test_renders_without_the_ephemeris(monkeypatch, tmp_path):
    """No de421.bsp: no sun times, moon, night shading or sky icons, but still a screen."""
    data = tmp_path / "data"
    shutil.copytree(state.data_dir() / "fonts", data / "fonts")
    monkeypatch.setenv(state.DATA_DIR_ENV, str(data))
    write_caches()
    m = day.day_model(AT, context())
    assert m.sun is None and m.moon is None and m.tide is not None
    assert draw().shape == (480, 800)


def test_renders_portrait_at_its_size():
    """The portrait layout isn't designed yet (PRD Q6); it must still render at the right size."""
    write_caches()
    assert draw(portrait=True).shape == (800, 480)


def test_cli_scene_tides_writes_a_preview(tmp_path):
    write_caches()
    out = tmp_path / "day.png"
    assert cli.main(["scene", "tides", "--at", "2026-11-03T14:10", "-o", str(out)]) == 0
    assert Image.open(out).size == (800, 480)
