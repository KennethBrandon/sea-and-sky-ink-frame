"""The measured water level: `sources/gauge.py`, and how the day screen draws it.

The fixtures are real, recorded with exactly the parameters `request_params` sends for
2026-11-03 23:18:03 PST (the moment test_sources.py uses, so `FakeAPI` can serve them):

- `noaa_gauge_water_level.json`: NOAA's six-minute readings at Los Angeles (9410660)
- `noaa_gauge_predictions.json`: the same station's table at `interval=6`
- `gauge.json`: the cache made from those two (PRD.md §5)

Fetching in general (retries, never raising, keeping the old cache) is covered for every
source by test_sources.py.
"""

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import state
from photopainter.config import ConfigError, Gauge, default_config, validate
from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import tides as day
from photopainter.sources import gauge
from photopainter.sources.common import FetchError
from photopainter.web import health

FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 3, 23, 18, 3, tzinfo=LA)
NOW = datetime(2026, 11, 3, 22, 40, tzinfo=LA)
CONFIG = default_config()
CHART = (12, 300, 788, 476)
RED = INK["red"]


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def stamp(t):
    return state.to_iso(t)


def gap_cache(rows):
    """A gauge cache from (datetime, gap) pairs."""
    return {"station": "9410660", "units": "ft", "fetched_at": stamp(NOW),
            "gap": [{"t": stamp(t), "v": v} for t, v in rows]}


def steady(gap=1.3, hours=30, end=NOW):
    """Readings every 6 minutes for `hours` up to `end`, all `gap` over the table."""
    return gap_cache([(end - timedelta(minutes=6 * i), gap) for i in range(hours * 10, -1, -1)])


@pytest.fixture
def caches():
    """The fixture caches in the state dir, weather and surf made fresh for NOW."""
    folder = state.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("tides.json", "gauge.json"):
        shutil.copy(FIXTURES / name, folder / name)
    for name in ("weather.json", "surf.json"):
        data = fixture(name)
        data["fetched_at"] = stamp(NOW - timedelta(minutes=20))
        (folder / name).write_text(json.dumps(data))
    return folder


def write_gauge(folder, cache):
    (folder / "gauge.json").write_text(json.dumps(cache))


def model(config=CONFIG, now=NOW):
    return day.day_model(now, SceneContext.from_config(config))


def chart_of(m, days_behind=1.0, days_ahead=2.0):
    """The chart alone, as ink indices."""
    c = InkCanvas((800, 480), "white")
    day.tide_chart(c, CHART, m, m.now - timedelta(days=days_behind), m.now + timedelta(days=days_ahead))
    return c.indices()


def now_column(m, days_behind=1.0, days_ahead=2.0):
    span = (days_behind + days_ahead) * 86400
    return round(CHART[0] + days_behind * 86400 / span * (CHART[2] - CHART[0]))


def red_columns(ink, x0, x1):
    """How many columns from x0 to x1 have red in the plot, below its top row."""
    plot = ink[CHART[1] + 24:CHART[3] - 30, x0:x1]
    return int((plot == RED).any(axis=0).sum())


def words_in_block(m, monkeypatch):
    """Everything the tide box writes."""
    written = []
    real = day.text

    def spy(c, xy, s, size, **kw):
        written.append(s)
        return real(c, xy, s, size, **kw)

    monkeypatch.setattr(day, "text", spy)
    day.tide_block(InkCanvas((800, 480), "white"), m, 26, 60)
    return written


# --- the source ------------------------------------------------------------------------


def test_cache_matches_the_contract():
    measured = fixture("noaa_gauge_water_level.json")
    predicted = fixture("noaa_gauge_predictions.json")
    cache = gauge.parse(measured, predicted, CONFIG, RECORDED)
    assert list(cache) == ["station", "units", "fetched_at", "gap"]
    assert (cache["station"], cache["units"]) == ("9410660", "ft")
    assert cache["fetched_at"] == "2026-11-03T23:18:03-08:00"
    assert all(list(r) == ["t", "v"] and isinstance(r["v"], float) for r in cache["gap"])
    times = [state.from_iso(r["t"]) for r in cache["gap"]]
    assert times == sorted(times) and all(t.tzinfo is not None for t in times)
    # the last reading, by hand: 07:18 UTC is 23:18 the evening before
    last_table = next(float(p["v"]) for p in predicted["predictions"] if p["t"] == "2026-11-04 07:18")
    assert cache["gap"][-1] == {"t": "2026-11-03T23:18:00-08:00",
                                "v": round(float(measured["data"][-1]["v"]) - last_table, 3)}
    assert 1.0 < cache["gap"][-1]["v"] < 2.0  # the Kelvin wave, as it was that night


def test_the_gauge_fixture_is_that_cache():
    made = gauge.parse(fixture("noaa_gauge_water_level.json"), fixture("noaa_gauge_predictions.json"),
                       CONFIG, RECORDED)
    assert fixture("gauge.json") == made


def predicted_rows(*times):
    return {"predictions": [{"t": t, "v": "2.000"} for t in times]}


def test_holes_in_the_record_are_skipped():
    measured = {"data": [{"t": "2026-11-04 01:00", "v": "3.000"},
                         {"t": "2026-11-04 01:06", "v": ""},  # how NOAA sends a missing reading
                         {"t": "2026-11-04 01:12", "v": None},
                         {"t": "2026-11-04 01:18", "v": "3.100"},  # no prediction for this one
                         {"t": "2026-11-04 01:24", "v": "3.200"}]}
    cache = gauge.parse(measured, predicted_rows("2026-11-04 01:00", "2026-11-04 01:06", "2026-11-04 01:12",
                                                 "2026-11-04 01:24"), CONFIG, RECORDED)
    assert [r["v"] for r in cache["gap"]] == [1.0, 1.2]


def test_a_broken_sensor_is_left_out():
    measured = {"data": [{"t": "2026-11-04 01:00", "v": "40.000"}, {"t": "2026-11-04 01:06", "v": "3.000"},
                         {"t": "2026-11-04 01:12", "v": "-30.000"}]}
    cache = gauge.parse(measured, predicted_rows("2026-11-04 01:00", "2026-11-04 01:06", "2026-11-04 01:12"),
                        CONFIG, RECORDED)
    assert [r["v"] for r in cache["gap"]] == [1.0]


@pytest.mark.parametrize("measured, predicted, words", [
    (fixture("noaa_error.json"), fixture("noaa_gauge_predictions.json"), "NOAA: "),
    (fixture("noaa_gauge_water_level.json"), fixture("noaa_error.json"), "NOAA: "),
    ({"data": []}, fixture("noaa_gauge_predictions.json"), "no water levels"),
    ({"metadata": {}}, fixture("noaa_gauge_predictions.json"), "no water levels"),
    (fixture("noaa_gauge_water_level.json"), {"predictions": []}, "no predictions"),
    ([], fixture("noaa_gauge_predictions.json"), "no water levels"),
    ({"data": [{"t": "2026-11-04 01:00", "v": ""}]}, predicted_rows("2026-11-04 01:00"), "no usable"),
    ({"data": [{"t": "yesterday", "v": "3"}]}, predicted_rows("2026-11-04 01:00"), "bad time"),
    ({"data": [{"t": "2026-11-04 01:00", "v": "high"}]}, predicted_rows("2026-11-04 01:00"), "bad height"),
])
def test_bad_answers_raise(measured, predicted, words):
    with pytest.raises(FetchError, match=words):
        gauge.parse(measured, predicted, CONFIG, RECORDED)


def test_requests_reach_back_for_the_mean_and_for_the_chart():
    params = gauge.request_params(CONFIG, RECORDED, "water_level")
    assert params["begin_date"] == "20261103 03:18"  # 28 h before, in UTC
    assert params["end_date"] == "20261104 07:18"
    assert "interval" not in params
    assert gauge.request_params(CONFIG, RECORDED, "predictions")["interval"] == "6"

    long = replace(CONFIG, tides=replace(CONFIG.tides, chart_days_behind=7.0))
    assert gauge.request_params(long, RECORDED, "water_level")["begin_date"] == "20261028 06:18"


def test_requests_use_the_gauge_station_and_one_datum():
    other = replace(CONFIG, tides=replace(CONFIG.tides, datum="MSL"), gauge=Gauge(station="9410230"))
    params = gauge.request_params(other, RECORDED, "water_level")
    assert (params["station"], params["datum"]) == ("9410230", "MLLW")


def test_when_a_cache_goes_stale():
    cache = fixture("gauge.json")
    assert gauge.stale_reason(cache, CONFIG, RECORDED) is None
    assert gauge.stale_reason(cache, CONFIG, RECORDED + gauge.MAX_AGE) is None
    assert "older" in gauge.stale_reason(cache, CONFIG, RECORDED + gauge.MAX_AGE + timedelta(seconds=1))
    assert "9410230" in gauge.stale_reason(cache, replace(CONFIG, gauge=Gauge(station="9410230")), RECORDED)
    assert "in m" in gauge.stale_reason({**cache, "units": "m"}, CONFIG, RECORDED)


def test_disabled_fetches_nothing(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no fetch when disabled")

    monkeypatch.setattr(gauge, "fetch", refuse)
    result = gauge.ensure_fresh(replace(CONFIG, gauge=Gauge(enabled=False)), RECORDED)
    assert not result.fetched and result.error is None and result.note == "disabled in config"


# --- config ----------------------------------------------------------------------------


def test_config_defaults_and_round_trip():
    assert (CONFIG.gauge.enabled, CONFIG.gauge.station) == (True, "9410660")
    assert validate(CONFIG.to_dict()) == CONFIG
    assert validate({"gauge": {"enabled": False}}).gauge == Gauge(enabled=False)


def test_a_config_saved_before_the_gauge_gets_the_default():
    data = CONFIG.to_dict()
    del data["gauge"]
    assert validate(data).gauge == Gauge()


@pytest.mark.parametrize("section, words", [
    ({"station": "LA"}, "gauge.station"),
    ({"station": 9410660}, "gauge.station"),
    ({"enabled": "yes"}, "gauge.enabled"),
    ({"url": "x"}, "gauge"),
])
def test_config_rejects(section, words):
    with pytest.raises(ConfigError, match=words):
        validate({"gauge": section})


# --- the model -------------------------------------------------------------------------


def test_only_what_was_known_at_that_moment_is_used():
    earlier = NOW - timedelta(hours=3)
    m = day.load_measured(steady(), earlier)
    assert m.last == earlier and not m.stale


@pytest.mark.parametrize("data", [None, {}, {"gap": "x"}, {"gap": [{"t": "yesterday", "v": 1}]},
                                  {"gap": [{"t": "2026-11-03T22:00:00-08:00"}]}, {"gap": []}])
def test_a_cache_that_makes_no_sense_means_no_gap(data):
    assert day.load_measured(data, NOW) is None


def test_no_reading_before_now_means_no_gap():
    assert day.load_measured(steady(end=NOW + timedelta(days=2), hours=2), NOW) is None


def test_old_readings_say_so_and_then_go():
    fresh = day.load_measured(steady(end=NOW - timedelta(hours=2)), NOW)
    assert fresh is not None and not fresh.stale
    old = day.load_measured(steady(end=NOW - timedelta(hours=2, minutes=1)), NOW)
    assert old.stale and old.last == NOW - timedelta(hours=2, minutes=1)
    assert day.load_measured(steady(end=NOW - timedelta(hours=12)), NOW) is not None
    assert day.load_measured(steady(end=NOW - timedelta(hours=12, minutes=1)), NOW) is None


def test_running_mean_goes_by_time_not_by_row():
    times = np.array([0.0, 600.0, 1200.0, 1800.0, 9000.0])
    values = np.array([1.0, 2.0, 3.0, 4.0, 10.0])
    got = day._running_mean(times, values, 1200.0)  # ±10 minutes
    assert got.tolist() == [1.5, 2.0, 3.0, 3.5, 10.0]  # the far reading isn't mixed in


def test_the_mean_ahead_is_of_the_last_25_hours():
    end = NOW
    rows = [(end - timedelta(hours=30), 9.0), (end - timedelta(hours=26), 9.0),
            (end - timedelta(hours=24), 1.0), (end, 2.0)]
    assert day.load_measured(gap_cache(rows), NOW).ahead == pytest.approx(1.5)


def test_the_gap_holds_then_eases_to_the_mean():
    rows = [(NOW - timedelta(hours=24), 1.0), (NOW - timedelta(hours=2), 1.0), (NOW, 2.0)]
    m = day.load_measured(gap_cache(rows), NOW)
    end = NOW.timestamp()
    assert m.gap_at(end)[0] == 2.0
    assert m.gap_at(end - 86400.0 * 3)[0] == 1.0  # before the first reading
    ahead = m.gap_at(end + np.array([0.0, 3 * 3600.0, 6 * 3600.0, 48 * 3600.0]))
    assert ahead.tolist() == pytest.approx([2.0, (2.0 + m.ahead) / 2, m.ahead, m.ahead])
    assert m.ahead == pytest.approx(4 / 3)


def test_has_reading_stops_at_holes_and_ends():
    rows = [(NOW - timedelta(hours=10), 1.0), (NOW - timedelta(hours=9), 1.0),
            (NOW - timedelta(hours=2), 1.0), (NOW, 1.0)]
    m = day.load_measured(gap_cache(rows), NOW)
    at = lambda hours: NOW.timestamp() - hours * 3600.0  # noqa: E731
    got = m.has_reading(np.array([at(11), at(10), at(9.5), at(8.5), at(5.5), at(2.5), at(0), at(-1)]))
    assert got.tolist() == [False, True, True, True, False, True, True, False]


def test_age_is_right_across_the_clock_change():
    # 2026-11-01: 1 AM happens twice. These are 2 h 30 apart by the clock on the wall, 3 h 30 in fact.
    last = datetime(2026, 11, 1, 0, 30, tzinfo=LA)
    now = datetime(2026, 11, 1, 3, 0, tzinfo=LA)
    assert now.timestamp() - last.timestamp() == 3.5 * 3600
    assert day.load_measured(gap_cache([(last, 1.0)]), now).stale
    barely = datetime(2026, 11, 1, 1, 30, tzinfo=LA)  # the first 1:30, in daylight time
    assert not day.load_measured(gap_cache([(barely, 1.0)]), datetime(2026, 11, 1, 2, 30, tzinfo=LA)).stale


def test_one_reading_is_enough():
    m = day.load_measured(gap_cache([(NOW, 1.0)]), NOW)
    assert m.has_reading(np.array([NOW.timestamp(), NOW.timestamp() + 60])).tolist() == [True, False]
    assert m.gap_at(NOW.timestamp() + 3600)[0] == 1.0


def test_day_model_reads_the_cache(caches):
    m = model()
    assert m.measured is not None
    gap = m.gap_at(NOW.timestamp())[0]
    assert 1.0 < gap < 2.0
    high = m.tide.next_high
    assert m.with_gap(high).v == pytest.approx(high.v + m.gap_at(high.t.timestamp())[0])
    assert (m.with_gap(high).t, m.with_gap(high).high) == (high.t, high.high)


def test_without_a_gauge_nothing_moves(caches):
    (caches / "gauge.json").unlink()
    m = model()
    assert m.measured is None
    assert m.gap_at(NOW.timestamp()).tolist() == [0.0]
    assert m.with_gap(m.tide.next_high) == m.tide.next_high


def test_disabled_in_config_leaves_it_out(caches):
    assert model(replace(CONFIG, gauge=Gauge(enabled=False))).measured is None


# --- the screen ------------------------------------------------------------------------


@pytest.mark.parametrize("gap, note", [(0.0, None), (0.3, None), (0.44, None), (0.46, "+0.5 over table"),
                                       (0.5, "+0.5 over table"), (1.29, "+1.3 over table"),
                                       (-0.3, None), (-0.6, "−0.6 under table")])
def test_gap_note(gap, note):
    assert day.gap_note(gap) == note


@pytest.mark.parametrize("trend", ["Rising", "Falling"])
@pytest.mark.parametrize("gap", [1.3, -1.3, 2.9, -2.9])
def test_the_note_stays_inside_the_tide_column(trend, gap):
    note = day.gap_note(gap)
    room = day.TIDE_W - 28 - text_width(trend, font(24)) - 10
    size = next((s for s in (18, 16) if text_width(note, font(s, True)) <= room), 14)
    assert text_width(note, font(size, True)) <= room


def test_tide_box_includes_the_gap(caches, monkeypatch):
    write_gauge(caches, steady(gap=1.3))
    m = model()
    words = words_in_block(m, monkeypatch)
    assert day.ft(m.tide.height + 1.3) in words
    assert "+1.3 over table" in words
    high, low = m.tide.next_high, m.tide.next_low
    assert f"{day.clock(high.t)}  {day.ft(high.v + 1.3)} ft" in words
    assert f"{day.clock(low.t)}  {day.ft(low.v + 1.3)} ft" in words
    assert not any(w.startswith("as of") for w in words)


def test_tide_box_on_an_ordinary_day_gives_no_note(caches, monkeypatch):
    write_gauge(caches, steady(gap=0.3))
    m = model()
    words = words_in_block(m, monkeypatch)
    assert day.ft(m.tide.height + 0.3) in words
    assert not any("table" in w for w in words)


def test_tide_box_without_a_gauge_is_the_table(caches, monkeypatch):
    (caches / "gauge.json").unlink()
    m = model()
    words = words_in_block(m, monkeypatch)
    assert day.ft(m.tide.height) in words
    assert f"{day.clock(m.tide.next_high.t)}  {day.ft(m.tide.next_high.v)} ft" in words


def test_tide_box_says_how_old_old_readings_are(caches, monkeypatch):
    last = NOW - timedelta(hours=3)
    write_gauge(caches, steady(end=last))
    assert f"as of {day.clock(last)}" in words_in_block(model(), monkeypatch)


def test_chart_draws_a_line_behind_and_dots_ahead(caches):
    write_gauge(caches, steady(gap=1.3))
    m = model()
    ink = chart_of(m)
    x = now_column(m)
    behind = red_columns(ink, CHART[0], x - 12)
    ahead = red_columns(ink, x + 12, CHART[2])
    assert behind == x - 12 - CHART[0]  # unbroken
    assert 0 < ahead < CHART[2] - x - 12  # broken into dots


def test_chart_without_a_gauge_has_no_line_or_dots(caches):
    (caches / "gauge.json").unlink()
    m = model()
    ink = chart_of(m)
    x = now_column(m)
    assert red_columns(ink, CHART[0], x - 12) == 0
    assert red_columns(ink, x + 12, CHART[2]) == 0


def test_no_gauge_and_gauge_off_draw_the_same(caches):
    off = chart_of(model(replace(CONFIG, gauge=Gauge(enabled=False))))
    with_gauge = chart_of(model())
    (caches / "gauge.json").unlink()
    assert (chart_of(model()) == off).all()
    assert (with_gauge != off).any()


def test_the_scale_makes_room_for_the_water(caches):
    write_gauge(caches, steady(gap=3.0))
    high = chart_of(model())
    assert (high[CHART[1] + 24:CHART[3] - 30] == RED).any()
    # the red's top is near the top of the plot, wherever the table's top ends up
    rows = np.flatnonzero((high[CHART[1] + 24:CHART[3] - 30, CHART[0]:CHART[2]] == RED).any(axis=1))
    assert rows[0] < 30


def test_the_line_stops_where_the_gauge_stopped(caches):
    write_gauge(caches, steady(end=NOW - timedelta(hours=6), gap=1.3))
    m = model()
    ink = chart_of(m)
    x = now_column(m)
    per_hour = (CHART[2] - CHART[0]) / 72
    # solid until the last reading, dots from there on
    assert red_columns(ink, CHART[0], x - round(7 * per_hour)) == x - round(7 * per_hour) - CHART[0]
    between = ink[CHART[1] + 24:CHART[3] - 30, x - round(5.5 * per_hour):x - round(1 * per_hour)]
    assert 0 < int((between == RED).any(axis=0).sum()) < between.shape[1]


def test_the_line_stops_at_a_hole_in_the_record(caches):
    rows = [(NOW - timedelta(minutes=6 * i), 1.3) for i in range(300, 180, -1)]  # 30 to 18 h ago
    rows += [(NOW - timedelta(minutes=6 * i), 1.3) for i in range(60, -1, -1)]  # the last 6 h
    write_gauge(caches, gap_cache(rows))
    m = model()
    ink = chart_of(m)
    x = now_column(m)
    per_hour = (CHART[2] - CHART[0]) / 72
    assert red_columns(ink, x - round(15 * per_hour), x - round(9 * per_hour)) == 0
    assert red_columns(ink, x - round(4 * per_hour), x - 12) > 0
    assert red_columns(ink, x - round(22 * per_hour), x - round(19 * per_hour)) > 0


def test_runs():
    assert [r.tolist() for r in day.runs(np.array([0, 1, 1, 0, 1, 0, 0, 1, 1], dtype=bool))] == [[1, 2], [4], [7, 8]]
    assert day.runs(np.zeros(4, dtype=bool)) == []


def test_dots_along_go_by_distance():
    assert day.dots_along([(float(x), 5.0) for x in range(21)], 8.0) == [(0.0, 5.0), (8.0, 5.0), (16.0, 5.0)]
    steep = [(0.0, 0.0), (0.0, 10.0), (0.0, 20.0)]
    assert day.dots_along(steep, 8.0) == [(0.0, 0.0), (0.0, 10.0)]
    assert day.dots_along([], 8.0) == []


@pytest.mark.parametrize("gauge_cache", [
    steady(gap=1.3),
    steady(gap=0.3),
    steady(gap=-0.9),  # water under the table: the red runs through the blue
    steady(gap=5.5),
    steady(end=NOW - timedelta(hours=8)),
    gap_cache([(NOW, 1.3)]),
], ids=["high", "ordinary", "under", "huge", "old", "one-reading"])
@pytest.mark.parametrize("behind, ahead", [(1.0, 2.0), (0.0, 3.0), (3.0, 0.0), (7.0, 14.0)])
def test_every_state_renders_in_exact_inks(caches, gauge_cache, behind, ahead):
    write_gauge(caches, gauge_cache)
    config = replace(CONFIG, tides=replace(CONFIG.tides, chart_days_behind=behind, chart_days_ahead=ahead))
    scene = Scene("tides", f"tides/{stamp(NOW)}", NOW)
    ink = render_scene(scene, SceneContext.from_config(config))
    assert ink.shape == (480, 800)


def test_the_fixture_caches_render(caches):
    scene = Scene("tides", f"tides/{stamp(NOW)}", NOW)
    assert (render_scene(scene, SceneContext.from_config(CONFIG)) == RED).any()


# --- the dashboard ---------------------------------------------------------------------


def test_health_goes_by_the_readings_not_the_fetch(caches):
    fresh = health.sources(caches, CONFIG, RECORDED + timedelta(minutes=10))["gauge"]
    assert fresh["enabled"] and fresh["level"] == "ok"
    assert fresh["until"] == state.from_iso(fixture("gauge.json")["gap"][-1]["t"])

    # NOAA answering, but with nothing newer than this
    old = fixture("gauge.json")
    old["fetched_at"] = stamp(RECORDED + timedelta(hours=13))
    write_gauge(caches, old)
    late = health.sources(caches, CONFIG, RECORDED + timedelta(hours=13))["gauge"]
    assert late["level"] == "error" and "Too old" in late["note"]


def test_health_when_turned_off(caches):
    off = health.sources(caches, replace(CONFIG, gauge=Gauge(enabled=False)), RECORDED)["gauge"]
    assert not off["enabled"] and off["level"] == "off"
