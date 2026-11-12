"""The harbor chart: its data, build script, view, wiring and rendering."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import schedule, state
from photopainter.config import SCREEN_KINDS, ConfigError, Config, Harbor, validate
from photopainter.render.draw import INK, InkCanvas
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import harbor
from photopainter.scenes.base import SCENE_KINDS

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/Los_Angeles")
AT = datetime(2026, 11, 3, 14, 10, tzinfo=TZ)


def _render(state_dir: Path = FIXTURES, at: datetime = AT, **settings) -> np.ndarray:
    config = dataclasses.replace(Config(), harbor=Harbor(**settings))
    ctx = SceneContext.from_config(config, state_dir=state_dir, data_dir=ROOT / "data")
    return render_scene(Scene("harbor", "harbor/test", draw_for=at), ctx)


def _build_module():
    spec = importlib.util.spec_from_file_location("build_harbor", ROOT / "scripts" / "build-harbor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renders_at_canvas_size_in_the_six_inks():
    out = _render()
    assert out.shape == (480, 800)
    assert out.dtype == np.uint8
    assert set(np.unique(out)) <= set(range(6))
    assert {INK["blue"], INK["yellow"], INK["black"], INK["white"], INK["red"]} <= set(np.unique(out))


def test_same_inputs_same_pixels():
    assert np.array_equal(_render(), _render())


def test_no_caches_still_draws_the_chart(tmp_path):
    out = _render(state_dir=tmp_path)
    assert out.shape == (480, 800)
    assert (out == INK["blue"]).sum() > 1000


def test_portrait_canvas_is_cropped():
    config = dataclasses.replace(Config(), display=dataclasses.replace(Config().display, portrait=True))
    ctx = SceneContext.from_config(config, state_dir=FIXTURES, data_dir=ROOT / "data")
    assert render_scene(Scene("harbor", "harbor/test", draw_for=AT), ctx).shape == (800, 480)


def test_measured_line_is_drawn_red_in_the_wave():
    # The band holds red only for the measured line, the likely dots and now.
    band = _render()[harbor.MAP_H:]
    assert (band == INK["red"]).sum() > 100
    assert (band == INK["blue"]).sum() > 1000


def test_no_tide_cache_leaves_the_band_empty(tmp_path):
    band = _render(state_dir=tmp_path)[harbor.MAP_H + 1:-1, 1:-1]
    assert (band == INK["white"]).all()


def test_the_band_spans_a_day_back_and_four_ahead():
    assert (Harbor().chart_days_behind, Harbor().chart_days_ahead) == (1, 4)


def test_the_band_has_settings_of_its_own():
    """They change the band and nothing above it, and the day screen's don't touch it."""
    plain = _render()
    for changed in (_render(chart_days_ahead=2), _render(chart_days_behind=0.5), _render(label_tides="daily"),
                    _render(label_times=False), _render(label_heights="all")):
        assert not np.array_equal(plain[harbor.MAP_H:], changed[harbor.MAP_H:])
        assert np.array_equal(plain[:harbor.MAP_H], changed[:harbor.MAP_H])
    c = Config()
    other = dataclasses.replace(c, tides=dataclasses.replace(c.tides, chart_days_ahead=7, label_tides="daily"))
    ctx = SceneContext.from_config(other, state_dir=FIXTURES, data_dir=ROOT / "data")
    assert np.array_equal(plain, render_scene(Scene("harbor", "harbor/test", draw_for=AT), ctx))


def test_other_bounds_move_the_map():
    assert not np.array_equal(_render(), _render(south=33.59, north=33.61, center_lon=-117.89))


def test_default_view_shows_the_buoy_clear_of_the_edge():
    data = harbor.load_harbor(ROOT / "data")
    view = harbor.make_view(Harbor(), data["bounds"])
    buoy = next(m for m in data["marks"] if m["kind"] == "safe_water")
    x, y = view.xy(buoy["lat"], buoy["lon"])
    assert 400 < x < 800 - 20
    assert 240 < y < 480 - 20
    out = _render()
    assert (out[round(y) - 4:round(y) + 5, round(x) - 7:round(x) + 8] == INK["red"]).sum() > 10


def test_view_matches_the_bounds_and_keeps_the_map_square():
    bounds = {"south": 33.54, "north": 33.70, "west": -118.02, "east": -117.78}
    v = harbor.make_view(Harbor(), bounds)
    assert v.south == pytest.approx(33.5835)
    assert v.top == pytest.approx(33.61575)
    assert v.west + 400 * v.deg_per_px_lon == pytest.approx(-117.9015)
    assert v.xy(v.south, v.west) == pytest.approx((0, harbor.MAP_H))
    # A degree of longitude is shorter than one of latitude by cos(lat): no stretching.
    ratio = v.deg_per_px_lat / v.deg_per_px_lon
    assert ratio == pytest.approx(0.8331, abs=0.001)


def test_view_past_the_data_is_fitted_inside_it():
    bounds = {"south": 33.58, "north": 33.67, "west": -117.95, "east": -117.84}
    v = harbor.make_view(Harbor(south=33.54, north=33.70, center_lon=-117.78), bounds)
    assert v.south >= bounds["south"]
    assert v.top <= bounds["north"] + 1e-9
    assert v.west >= bounds["west"] - 1e-9
    assert v.west + 800 * v.deg_per_px_lon <= bounds["east"] + 1e-9


def test_data_file():
    path = ROOT / "data" / "harbor.json"
    assert path.stat().st_size < 300 * 1024
    data = json.loads(path.read_text())
    assert "OpenStreetMap" in data["license"]
    assert data["fetched"]
    assert len(data["soundings"]) > 100
    assert all(isinstance(d, int) and d >= 0 for _, _, d in data["soundings"])
    assert {m["kind"] for m in data["marks"]} == {"red", "green", "safe_water"}
    assert {"Lido Isle", "Balboa Island"} <= {i["name"] for i in data["islands"]}


def test_build_on_a_small_response():
    build = _build_module()
    way = lambda i, pts, **tags: {"type": "way", "id": i, "tags": tags,  # noqa: E731
                                  "geometry": [{"lat": a, "lon": b} for a, b in pts]}
    member = lambda i, role, pts: {"type": "way", "ref": i, "role": role,  # noqa: E731
                                   "geometry": [{"lat": a, "lon": b} for a, b in pts]}
    island = [(33.605, -117.905), (33.605, -117.900), (33.610, -117.900), (33.605, -117.905)]
    osm = {"elements": [
        # two coast ways, the second reversed, joined at a point
        way(1, [(33.60, -117.95), (33.595, -117.92)], natural="coastline"),
        way(2, [(33.58, -117.87), (33.595, -117.92)], natural="coastline"),
        {"type": "relation", "id": 9, "tags": {"name": "Newport Bay", "natural": "water"}, "members": [
            member(10, "outer", [(33.60, -117.91), (33.60, -117.89), (33.62, -117.89)]),
            member(11, "outer", [(33.62, -117.89), (33.62, -117.91), (33.60, -117.91)]),
            member(40500961, "inner", island),
        ]},
        way(3, [(33.61, -117.88), (33.61, -117.87), (33.62, -117.87)], natural="wetland", wetland="saltmarsh"),
        way(4, [(33.59, -117.93), (33.59, -117.92), (33.60, -117.92)], natural="beach"),
        way(5, [(33.60, -117.93), (33.59, -117.93)], man_made="pier", name="Newport Pier"),
        way(6, [(33.60, -117.92), (33.59, -117.92)], man_made="pier"),  # a dock with no name
        way(7, [(33.59, -117.88), (33.58, -117.88)], man_made="breakwater"),
        {"type": "node", "id": 20, "lat": 33.6, "lon": -117.88, "tags": {
            "seamark:type": "beacon_lateral", "name": "4", "seamark:beacon_lateral:colour": "red"}},
        {"type": "node", "id": 21, "lat": 33.584, "lon": -117.878, "tags": {
            "seamark:type": "buoy_safe_water", "name": "NWP"}},
        {"type": "node", "id": 22, "lat": 33.606, "lon": -117.919, "tags": {
            "seamark:type": "beacon_special_purpose"}},
    ]}
    layers = [[
        {"attributes": {"Z": 6.1}, "geometry": {"x": -117.90, "y": 33.61}},  # 20.01 ft
        {"attributes": {"Z": 9.0}, "geometry": {"x": -117.9001, "y": 33.61}},  # right next to it: deeper, dropped
        {"attributes": {"Z": -0.5}, "geometry": {"x": -117.91, "y": 33.60}},  # dries: left out
        {"attributes": {"Z": None}, "geometry": {"x": -117.91, "y": 33.61}},
    ], [
        {"attributes": {"Z": 2.0}, "geometry": {"x": -117.9002, "y": 33.61}},  # a coarser chart's, near: dropped
        {"attributes": {"Z": 30.0}, "geometry": {"x": -117.95, "y": 33.57}},  # 98.4 ft: rounded down
    ]]
    out = build.build(osm, layers, "2026-11-04")
    assert out["fetched"] == "2026-11-04"
    assert out["ocean"][0] == [33.60, -117.95]  # chained, west to east
    assert out["ocean"][-1] == [33.58, -117.87]
    assert len(out["harbor"]) == 1
    assert out["harbor"][0]["outer"][0] == out["harbor"][0]["outer"][-1]  # the two ways made one ring
    assert len(out["harbor"][0]["holes"]) == 1
    assert [i["name"] for i in out["islands"]] == ["Lido Isle"]
    assert len(out["marsh"]) == 1 and len(out["beaches"]) == 1
    assert {(p["kind"], p["name"]) for p in out["piers"]} == {("pier", "Newport Pier"), ("breakwater", None)}
    assert sorted(m["kind"] for m in out["marks"]) == ["red", "safe_water"]
    assert sorted(s[2] for s in out["soundings"]) == [20, 98]


def test_build_needs_a_coastline_and_the_bay():
    build = _build_module()
    with pytest.raises(ValueError, match="coastline"):
        build.build_map([])
    coast = {"type": "way", "id": 1, "tags": {"natural": "coastline"},
             "geometry": [{"lat": 33.6, "lon": -117.9}, {"lat": 33.6, "lon": -117.8}]}
    with pytest.raises(ValueError, match="Newport Bay"):
        build.build_map([coast])


def test_simplify_keeps_the_ends_and_drops_a_straight_middle():
    build = _build_module()
    assert build.simplify([(33.6, -117.9), (33.6, -117.89), (33.6, -117.88)]) == [(33.6, -117.9), (33.6, -117.88)]


# --- wiring ----------------------------------------------------------------------------------


def test_the_name_is_in_every_list():
    assert "harbor" in SCENE_KINDS
    assert "harbor" in SCREEN_KINDS
    assert "harbor" in schedule.PIN_KINDS
    app = (ROOT / "src/photopainter/web/static/app.js").read_text()
    assert 'harbor: "Harbor", auto:' in app
    assert 'photo: "Photo", harbor: "Harbor" }' in app
    html = (ROOT / "src/photopainter/web/static/index.html").read_text()
    assert 'data-kind="harbor"' in html and 'id="icon-harbor"' in html
    assert "OpenStreetMap" in html
    assert "OpenStreetMap" in (ROOT / "data" / "README.md").read_text()


def test_config_round_trip_and_errors():
    config = validate({"harbor": {"south": 33.57, "north": 33.63, "center_lon": -117.88}})
    assert config.harbor == Harbor(33.57, 33.63, -117.88)
    assert validate(config.to_dict()).harbor == config.harbor
    with pytest.raises(ConfigError, match="harbor.north"):
        validate({"harbor": {"south": 33.62, "north": 33.60}})
    with pytest.raises(ConfigError, match="harbor.south"):
        validate({"harbor": {"south": 34.5}})
    assert validate({"schedule": {"day": [{"screen": "harbor", "minutes": 30}]}}).schedule.day[0].screen == "harbor"


def _sun(day):
    from photopainter.astro.sun import SunEvents

    tz = TZ
    at = lambda h: datetime(day.year, day.month, day.day, h, tzinfo=tz)  # noqa: E731
    return SunEvents(sunrise=at(6), sunset=at(17), civil_dawn=at(5), civil_dusk=at(18))


def test_a_rotation_step_shows_it():
    config = validate({"schedule": {"day": [{"screen": "harbor", "minutes": 60}]}})
    d = schedule.decide(AT, config, schedule.Overrides(), _sun)
    assert d.scene.kind == "harbor"
    assert d.scene.key.startswith("harbor/")
    assert d.scene.draw_for == AT


def test_a_pin_shows_it():
    pin = schedule.Pin(kind="harbor", until=None, set_at=AT)
    d = schedule.decide(AT, Config(), schedule.Overrides(pin=pin), _sun)
    assert d.scene.kind == "harbor"
    assert state.data_dir() is not None


def test_swell_and_wind_words_are_in_the_block_and_their_arrows_on_the_map():
    from photopainter.scenes.harbor import conditions_lines, day_model

    ctx = SceneContext.from_config(Config(), state_dir=FIXTURES, data_dir=ROOT / "data")
    swell, wind = conditions_lines(day_model(AT, ctx))
    assert swell[0].startswith("Swell") and "offshore" in swell[1]
    assert wind[0].startswith("Wind") and wind[1].startswith("Water")


def _model(**weather):
    ctx = SceneContext.from_config(Config(), state_dir=FIXTURES, data_dir=ROOT / "data")
    m = harbor.day_model(AT, ctx)
    return dataclasses.replace(m, weather=dataclasses.replace(m.weather, **weather))


def _chart():
    data = harbor.load_harbor(ROOT / "data")
    view = harbor.make_view(Harbor(), data["bounds"])
    return data, view, harbor.draw_map(InkCanvas((800, 480)), data, view, None)


def _draw(m):
    c = InkCanvas((800, 480), "white")
    harbor.draw_all(c, m, harbor.load_harbor(ROOT / "data"), Harbor())
    return c.indices()


def _words(m, monkeypatch):
    """Every word draw_all puts on the chart: {text: (x, y, size, bold)}."""
    seen = {}
    real = harbor.label

    def spy(c, xy, s, size, ink=harbor.BLACK, bold=False, *a, **k):
        seen[s] = (*xy, size, bold)
        return real(c, xy, s, size, ink, bold, *a, **k)

    monkeypatch.setattr(harbor, "label", spy)
    _draw(m)
    return seen


def _rough_seas(stale=False):
    m = _model(wind=25.0, wind_dir=292.5, stale=stale)
    return dataclasses.replace(m, swell=dataclasses.replace(m.swell, swell_height=12.6, swell_period=16.0,
                                                            swell_dir=292.5, stale=stale))


def test_the_swell_and_wind_words_stay_large_on_the_longest_fresh_reading(monkeypatch):
    seen = _words(_rough_seas(), monkeypatch)
    swell, wind = harbor.conditions_lines(_rough_seas())
    assert [seen[line][2] for line in swell + wind] == [harbor.CONDITION_SIZE] * 4


def test_old_readings_say_so_and_stay_inside_the_block(monkeypatch):
    seen = _words(_rough_seas(stale=True), monkeypatch)
    swell, wind = harbor.conditions_lines(_rough_seas(stale=True))
    assert "as of" in swell[1] and "offshore" in swell[1] and wind[1].startswith("as of") and "Water" in wind[1]
    for line in swell + wind:
        x, y, size, bold = seen[line]
        assert size >= 9
        assert x + harbor.text_width(line, harbor.font(size, bold)) <= harbor.BLOCK[2] - 3  # the inner frame
        assert y + size / 2 <= harbor.BLOCK[3] - 3


def test_the_block_grew_taller_only_and_no_name_was_lost_to_it(monkeypatch):
    assert harbor.BLOCK[0] == 590 and harbor.BLOCK[2] == 790  # wider would cover the bay (Owner)
    assert "CORONA DEL MAR" in _words(_model(), monkeypatch)


def test_the_rose_takes_its_disc_not_the_square_around_it():
    rx, ry, rr = harbor.ROSE
    rects = harbor.rose_rects(harbor.Rim(), rx, ry, rr)
    for deg in range(0, 360, 5):  # all of the rim is covered…
        x, y = harbor._rim(rx, ry, rr, deg)
        assert harbor.overlaps((x, y, x + 1, y + 1), rects)
    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):  # …and none of the square's corners
        x, y = rx + dx * (rr - 4), ry + dy * (rr - 4)
        assert not harbor.overlaps((x, y, x + 1, y + 1), rects)


@pytest.mark.parametrize("at", [AT, AT.replace(hour=3)])  # the Sun's up; the Moon's up
def test_the_rose_covers_what_is_on_its_rim_today(at):
    ctx = SceneContext.from_config(Config(), state_dir=FIXTURES, data_dir=ROOT / "data")
    m = harbor.day_model(at, ctx)
    rx, ry, rr = harbor.ROSE
    rim = harbor.rim_marks(m, rx, ry, rr, harbor.MAP_H)
    assert rim.sun_az is not None or rim.moon_az is not None
    rects = harbor.rose_rects(rim, rx, ry, rr)
    assert rim.words
    for xy, s in rim.words:
        assert harbor.overlaps(harbor.label_rect(xy, s, harbor.RIM_WORD_SIZE, "mm"), rects)


def _rim_at(at: datetime):
    ctx = SceneContext.from_config(Config(), state_dir=FIXTURES, data_dir=ROOT / "data")
    m = harbor.day_model(at, ctx)
    rx, ry, rr = harbor.ROSE
    return m, harbor.rim_marks(m, rx, ry, rr, harbor.MAP_H)


def test_a_new_moon_sits_inside_the_rose_apart_from_the_sun():
    m, rim = _rim_at(datetime(2026, 11, 9, 11, 13, tzinfo=TZ))  # 12 h after new moon, both up
    assert rim.sun_az is not None and rim.moon_az is not None and rim.eclipse is None
    assert abs((rim.moon_az - rim.sun_az + 180) % 360 - 180) < 10  # the same bearing…
    rx, ry, rr = harbor.ROSE
    sun, moon = harbor._rim(rx, ry, rr + 1, rim.sun_az), harbor.moon_at(rx, ry, rr, rim.moon_az)
    assert np.hypot(*np.subtract(sun, moon)) > harbor.SUN_REACH + harbor.MOON_R  # …but apart
    assert [s for _, s in rim.words if "Sun" in s or "Moon" in s] == ["Sun 39°, Moon 31° up"]


@pytest.mark.parametrize("az", range(0, 360, 15))
def test_the_moon_stays_inside_the_inner_circle_and_off_the_letters(az):
    rx, ry, rr = harbor.ROSE
    x, y = harbor.moon_at(rx, ry, rr, az)
    from_center = np.hypot(x - rx, y - ry)
    assert from_center + harbor.MOON_R < rr - harbor.RING  # inside the inner circle
    assert from_center - harbor.MOON_R > rr - 42 + 8  # past N, E, S and W (size 14, at rr - 42)


def test_in_an_eclipse_seen_here_the_sun_becomes_an_eclipse_icon():
    at = datetime(2029, 1, 14, 8, 30, tzinfo=TZ)  # partial here, 7:07 to 9:46 am
    m, rim = _rim_at(at)
    assert rim.eclipse is not None and rim.moon_az is None
    assert any(s.startswith("Eclipse, Sun") for _, s in rim.words)
    rx, ry, rr = harbor.ROSE
    c = InkCanvas((800, 480), "white")
    harbor.draw_rose(c, m, rim, rx, ry, rr)
    x, y = (round(v) for v in harbor._rim(rx, ry, rr + 1, rim.sun_az))
    ink = c.indices()
    assert ink[y, x] == INK["black"]  # a black middle…
    assert ink[y, x + harbor.SUN_R - 2] == INK["yellow"] and ink[y - harbor.SUN_R + 2, x] == INK["yellow"]  # …in a yellow ring


def test_no_eclipse_icon_on_an_ordinary_day():
    _, rim = _rim_at(AT)
    assert rim.eclipse is None


@pytest.mark.parametrize("day", [7, 8, 9, 10, 11, 12])
def test_the_sun_and_moon_words_never_print_over_each_other(day):
    for hour in range(7, 17):
        _, rim = _rim_at(datetime(2026, 11, day, hour, 0, tzinfo=TZ))
        for i, a in enumerate(rim.words):
            for b in rim.words[i + 1:]:
                if any(body in a[1] + b[1] for body in ("Sun", "Moon")):
                    assert not harbor._words_touch(a, b), (hour, a, b)


def test_close_sun_and_moon_share_one_label():
    _, rim = _rim_at(datetime(2026, 11, 11, 12, 0, tzinfo=TZ))
    assert rim.sun_az is not None and rim.moon_az is not None
    assert [s for _, s in rim.words if "Sun" in s or "Moon" in s] == ["Sun 39°, Moon 24° up"]


@pytest.mark.parametrize("from_deg", range(0, 360, 45))
def test_the_wind_fills_in_around_the_rose_without_touching_it(from_deg, monkeypatch):
    m = _model(wind=12.0, wind_dir=float(from_deg))
    rx, ry, rr = harbor.ROSE
    spots = []
    size = harbor.wind_length(12.0)
    monkeypatch.setattr(harbor, "wind_arrow", lambda c, x, y, z, *a, **k: spots.append((x, y)) if z == size else None)
    _draw(m)
    words = [harbor.label_rect(xy, s, harbor.RIM_WORD_SIZE, "mm") for xy, s in harbor.rim_marks(m, rx, ry, rr, harbor.MAP_H).words]
    us, vs = harbor._wind_footprint(size, from_deg)
    for x, y in spots:
        assert np.hypot(x + us - rx, y + vs - ry).min() > rr + harbor.ROSE_CLEAR
        for u, v in zip(us.ravel(), vs.ravel()):
            assert not harbor.overlaps((x + u, y + v, x + u + 1, y + v + 1), words)
    old_square = (rx - rr - 34, ry - rr - 34, rx + rr + 34, ry + rr + 34)
    assert any(harbor.overlaps((x, y, x + 1, y + 1), [old_square]) for x, y in spots)


def test_no_depth_lands_on_the_rose(monkeypatch):
    rx, ry, rr = harbor.ROSE
    seen = _words(_model(), monkeypatch)
    depths = [(x, y) for s, (x, y, *_) in seen.items() if s.isdigit()]
    assert depths
    assert all(np.hypot(x - rx, y - ry) > rr for x, y in depths)


def test_the_credits_and_the_depths_datum_are_off_the_panel_and_on_the_dashboard(monkeypatch):
    words = " ".join(_words(_model(), monkeypatch))
    assert "OpenStreetMap" not in words and "NOAA" not in words and "MLLW" not in words
    page = (ROOT / "src" / "photopainter" / "web" / "static" / "index.html").read_text()
    assert "© OpenStreetMap contributors" in page and "NOAA ENC" in page and "feet below MLLW" in page


def test_a_wind_arrow_is_longer_the_harder_it_blows():
    lengths = [harbor.wind_length(mph) for mph in (3, 5, 10, 15, 20)]
    assert lengths == sorted(set(lengths))
    assert lengths[-1] > 2.5 * lengths[0]
    assert harbor.wind_length(0) == harbor.WIND_SHORTEST
    assert harbor.wind_length(60) == harbor.WIND_LONGEST
    assert harbor.wind_length(10, scale=0.5) == harbor.wind_length(10) / 2


@pytest.mark.parametrize("mph", [3, 12, 25])
@pytest.mark.parametrize("from_deg", [0, 90, 225, 270])
def test_many_wind_arrows_at_any_speed_and_direction_all_on_clear_water(mph, from_deg):
    _, view, the_map = _chart()
    blocked = [(250.0, 200.0, 400.0, 300.0)]
    spots = harbor.wind_spots(_model(wind=mph, wind_dir=from_deg), the_map, view, blocked)
    assert len(spots) >= 20
    assert len(set(spots)) == len(spots)
    dx, dy = harbor._wind_footprint(harbor.wind_length(mph), from_deg)
    seen = np.zeros(the_map.ocean.shape, dtype=int)
    for x, y in spots:
        xs, ys = np.rint(x + dx).astype(int), np.rint(y + dy).astype(int)
        assert the_map.ocean[ys, xs].all(), "on the land"
        assert not ((250 <= xs) & (xs < 400) & (200 <= ys) & (ys < 300)).any(), "on something"
        rows, cols = np.unique(np.stack((ys.ravel(), xs.ravel())), axis=1)
        seen[rows, cols] += 1
    assert seen.max() == 1, "two arrows on top of each other"


@pytest.mark.parametrize("wind", [None, 0.0, 0.5])
def test_no_wind_arrows_in_a_calm(wind):
    _, view, the_map = _chart()
    assert harbor.wind_spots(_model(wind=wind), the_map, view, []) == []


def test_always_two_swell_arrows_apart_on_open_water():
    _, view, the_map = _chart()
    spots = harbor.swell_spots(_model(), harbor.SWELL_AT, the_map, [], view.height)
    assert len(spots) == 2
    rects = [harbor.swell_rect(x, y, harbor.SWELL_SIZE) for x, y in spots]
    assert not harbor.overlaps(rects[0], rects[1:])
    for x0, y0, x1, y1 in rects:
        assert the_map.ocean[int(y0):int(y1) + 1, int(x0):int(x1) + 1].all()


def test_the_second_swell_arrow_keeps_clear_of_the_first_when_they_want_the_same_place():
    _, view, the_map = _chart()
    at = harbor.SWELL_AT[0]
    spots = harbor.swell_spots(_model(), (at, at), the_map, [], view.height)
    rects = [harbor.swell_rect(x, y, harbor.SWELL_SIZE) for x, y in spots]
    assert not harbor.overlaps(rects[0], rects[1:])


def test_no_swell_arrows_without_a_swell():
    _, view, the_map = _chart()
    m = dataclasses.replace(_model(), swell=None)
    assert harbor.swell_spots(m, harbor.SWELL_AT, the_map, [], view.height) == []
    assert _draw(m) is not None


def test_swell_is_blue_and_the_wind_another_ink():
    assert harbor.SWELL_INK == INK["blue"]
    assert harbor.WIND_INK != harbor.SWELL_INK
    for draw_arrow, ink in ((lambda c: harbor.wind_arrow(c, 50, 50, 40, 270, 12), harbor.WIND_INK),
                            (lambda c: harbor.swell_arrow(c, 50, 50, 40, 180), harbor.SWELL_INK)):
        c = InkCanvas((100, 100), "white")
        draw_arrow(c)
        assert set(np.unique(c.indices())) == {INK["white"], ink}


def test_the_chart_draws_two_swell_arrows_and_a_field_of_wind_at_the_length_of_the_wind(monkeypatch):
    swell, wind = [], []
    monkeypatch.setattr(harbor, "swell_arrow", lambda c, x, y, size, *a, **k: swell.append(size))
    monkeypatch.setattr(harbor, "wind_arrow", lambda c, x, y, size, *a, **k: wind.append(size))
    _draw(_model(wind=12.0, wind_dir=270.0))
    assert swell.count(harbor.SWELL_SIZE) == 2  # and the small key in the title block
    assert wind.count(harbor.wind_length(12.0)) >= 20


def test_two_depths_never_touch():
    data, view, the_map = _chart()
    picked = harbor.pick_soundings(data, view, the_map, [])
    rects = [harbor.label_rect((x, y), s, harbor.SOUNDING_SIZE, "mm") for x, y, s in picked]
    assert len(rects) > 20
    for i, rect in enumerate(rects):
        assert not harbor.overlaps(rect, rects[:i], 4)
