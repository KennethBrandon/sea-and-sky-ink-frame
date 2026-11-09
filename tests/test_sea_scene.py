"""The sea screen (scenes/sea.py): the picture answers to the day's numbers, and never crashes."""

import json
import re
import runpy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import config as config_module
from photopainter import places, schedule
from photopainter.config import default_config
from photopainter.render.draw import INK
from photopainter.scenes import SCENE_KINDS, Scene, SceneContext, render_scene
from photopainter.scenes import sea
from photopainter.scenes.base import CLOUD_LOOKS, WAVE_LOOKS

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
AFTERNOON = datetime(2026, 11, 5, 14, 10, tzinfo=LA)
NIGHT = datetime(2026, 11, 5, 22, 0, tzinfo=LA)
pytestmark = pytest.mark.skipif(not (ROOT / "data" / "de421.bsp").exists(), reason="no ephemeris")


def caches(folder, at, *, swell=2.0, period=13.0, swell_dir=225, wind=4.0, wind_dir=270, code=0, skip=()):
    hours = [at + timedelta(hours=h) for h in range(-3, 4)]
    if "weather" not in skip:
        (folder / "weather.json").write_text(json.dumps({
            "fetched_at": at.isoformat(),
            "current": {"t": at.isoformat(), "temp": 71, "code": code, "wind": wind, "gust": wind, "wind_dir": wind_dir},
            "hourly": [], "daily": []}))
    if "surf" not in skip:
        (folder / "surf.json").write_text(json.dumps({
            "fetched_at": at.isoformat(),
            "hourly": [{"t": h.isoformat(), "wave_height": swell, "wave_period": period, "wave_dir": swell_dir,
                        "swell_height": swell, "swell_period": period, "swell_dir": swell_dir, "water_temp": 62}
                       for h in hours]}))
    return folder


def draw(folder, at=AFTERNOON, config=None):
    ctx = SceneContext.from_config(config or default_config(), state_dir=folder)
    return render_scene(Scene("sea", "sea/x", at), ctx)


def wedge_jetty():
    loc = default_config().location
    return places.sea_for(loc.lat, loc.lon, 190.0, ROOT / "data").jetty


def moved(lat=None, lon=None, window=None):
    """The default config somewhere else, or with the window turned."""
    c = default_config()
    loc = replace(c.location, lat=c.location.lat if lat is None else lat, lon=c.location.lon if lon is None else lon)
    sky = replace(c.sky, center_azimuth_deg=c.sky.center_azimuth_deg if window is None else window)
    return replace(c, location=loc, sky=sky)


def sea_white(pixels):
    """Solid white on the water, not the dots of a lit face: a white pixel with white on all
    four sides, below the far rows and above the beach."""
    w = pixels[220:420, 150:] == INK["white"]
    return int((w[1:-1, 1:-1] & w[:-2, 1:-1] & w[2:, 1:-1] & w[1:-1, :-2] & w[1:-1, 2:]).sum())


def test_fixture_caches_draw_through_render_scene():
    at = datetime(2026, 11, 3, 14, 10, tzinfo=LA)
    pixels = draw(FIXTURES, at)
    assert pixels.shape == (480, 800) and pixels.max() <= 5
    words = sea.sea_model(at, SceneContext.from_config(default_config(), state_dir=FIXTURES)).words
    assert words[0].startswith("Tuesday, November 3")
    assert re.fullmatch(r"\d+ ft at \d+ s from the [NESW]+\u2192", words[1])
    assert re.search(r"Wind \d+ mph", words[2]) and "Water" in words[2] and " tide " in words[2]


def test_same_moment_and_caches_give_the_same_pixels(tmp_path):
    caches(tmp_path, AFTERNOON, swell=8, wind=19)
    assert np.array_equal(draw(tmp_path), draw(tmp_path))


def test_a_big_swell_breaks_into_more_foam(tmp_path):
    calm, big = tmp_path / "calm", tmp_path / "big"
    calm.mkdir(), big.mkdir()
    caches(calm, AFTERNOON, swell=1, period=13)
    caches(big, AFTERNOON, swell=9, period=13)
    assert sea_white(draw(big)) > sea_white(draw(calm)) * 1.3


def test_wind_brings_whitecaps_further_out(tmp_path):
    still, windy = tmp_path / "still", tmp_path / "windy"
    still.mkdir(), windy.mkdir()
    caches(still, AFTERNOON, wind=4)
    caches(windy, AFTERNOON, wind=30)
    assert sea_white(draw(windy)) > sea_white(draw(still)) * 1.3
    rows_with_caps = lambda mph: sum(sea.whitecaps_in_row(mph, s) > 0 for s in np.linspace(0.06, 1, 15))  # noqa: E731
    assert rows_with_caps(5) == 0 < rows_with_caps(11) < rows_with_caps(20) <= rows_with_caps(30)
    assert sea.whitecaps_in_row(30, 1.0) > sea.whitecaps_in_row(12, 1.0) * 3


def test_long_period_waves_are_further_apart():
    assert [sea.row_count(p) for p in (6, 10, 13, 17, 25)] == [13, 8, 5, 3, 3]


def test_the_beach_narrows_as_the_tide_rises():
    assert sea.beach_px(0) > sea.beach_px(3.5) > sea.beach_px(7)
    assert sea.beach_px(None) == sea.BEACH_DEFAULT_PX


@pytest.mark.parametrize("code, kind", [(0, "clear"), (1, "clear"), (2, "partly"), (3, "overcast"),
                                        (45, "overcast"), (63, "overcast"), (None, "clear")])
def test_sky_from_the_weather_code(code, kind):
    assert sea.sky_kind(code) == kind


def test_overcast_hides_the_sun(tmp_path):
    caches(tmp_path, AFTERNOON, code=0)
    assert (draw(tmp_path)[:160] == INK["yellow"]).any()
    caches(tmp_path, AFTERNOON, code=63)
    assert not (draw(tmp_path)[:160] == INK["yellow"]).any()


def test_no_caches_draw_a_flat_sea_with_only_the_date(tmp_path):
    model = sea.sea_model(AFTERNOON, SceneContext.from_config(default_config(), state_dir=tmp_path))
    assert model.words[1] is None and model.words[2] is None
    assert "Thursday, November 5" in model.words[0] and "pm" not in model.words[0]
    assert model.updated == "Updated 2:10 pm"
    assert draw(tmp_path).shape == (480, 800)


def test_missing_surf_leaves_only_its_words_out(tmp_path):
    caches(tmp_path, AFTERNOON, skip=("surf",))
    words = sea.sea_model(AFTERNOON, SceneContext.from_config(default_config(), state_dir=tmp_path)).words
    assert words[1] is None and "Wind 4 mph W" in words[2] and "Water" not in words[2]
    assert "sunny, 71°" in words[0]


def test_night_is_dark(tmp_path):
    caches(tmp_path, NIGHT, code=0)
    model = sea.sea_model(NIGHT, SceneContext.from_config(default_config(), state_dir=tmp_path))
    assert not model.daylight and "clear, 71°" in model.words[0]
    assert (draw(tmp_path, NIGHT)[100:150] == INK["black"]).mean() > 0.8
    for light in (None, sea.Light(190, 30, sea_moon(0.9), 270.0), sea.Light(190, 30, sea_moon(0.0), 270.0)):
        c = sea.InkCanvas((800, 480), "white")
        sea.draw(c, sea.SeaModel(1, False, "clear", light, 2.0, 13.0, 200.0, 12.0, 40, ("a", "b", "c")), 190.0, None)
        assert c.indices().shape == (480, 800)


def sea_moon(lit):
    from photopainter.astro.moon import MoonPhase
    return MoonPhase(lit, 180.0, True, "")


def test_portrait_canvas(tmp_path):
    caches(tmp_path, AFTERNOON, swell=8, wind=20)
    cfg = default_config()
    ctx = SceneContext(cfg, (480, 800), tmp_path, SceneContext.from_config(cfg).data_dir)
    assert render_scene(Scene("sea", "sea/x", AFTERNOON), ctx).shape == (800, 480)


def test_the_name_is_in_every_list():
    assert "sea" in SCENE_KINDS and "sea" in schedule.PIN_KINDS and "sea" in config_module.SCREEN_KINDS
    app = (ROOT / "src/photopainter/web/static/app.js").read_text()
    assert re.search(r"const KIND = \{[^}]*\bsea:", app) and re.search(r"const SCREENS = \{[^}]*\bsea:", app)
    html = (ROOT / "src/photopainter/web/static/index.html").read_text()
    assert 'data-kind="sea"' in html and 'id="icon-sea"' in html
    assert '"sea": "Sea"' in (ROOT / "src/photopainter/web/server.py").read_text()


def test_a_rotation_step_is_drawn_for_the_middle_of_its_bucket():
    cfg = config_module.validate({"schedule": {"day": [{"screen": "sea", "minutes": 60}]}})
    assert cfg.schedule.day[0].screen == "sea"
    sun = lambda d: sea_sun_events(d)  # noqa: E731
    d = schedule.decide(datetime(2026, 11, 5, 12, 20, tzinfo=LA), cfg, schedule.Overrides(), sun)
    assert d.scene.kind == "sea" and d.scene.key == "sea/2026-11-05T12:00"
    assert d.scene.draw_for == datetime(2026, 11, 5, 12, 30, tzinfo=LA)
    pin = schedule.Pin("sea", until=None, photo=None, set_at=datetime(2026, 11, 5, 12, 0, tzinfo=LA))
    pinned = schedule.decide(datetime(2026, 11, 5, 12, 20, tzinfo=LA), cfg, schedule.Overrides(pin=pin), sun)
    assert pinned.scene.kind == "sea"


def sea_sun_events(day):
    from photopainter.astro.sun import sun_events
    return sun_events(day, 33.593, -117.882, LA)


def test_the_playground_draws_with_any_setting():
    script = runpy.run_path(str(ROOT / "scripts/sea-playground.py"))
    fields = set(script["DEFAULT_STYLE"])
    assert set(script["LOOK_RANGES"]) == fields  # a slider for every setting
    for params in ({}, {"time": "night", "sky": "partly", "wind_mph": "40", "rows_max": "9"}):
        assert script["render_png"](params).startswith(b"\x89PNG")


@pytest.mark.parametrize("at", [AFTERNOON, NIGHT])
def test_no_waves_or_beach_left_of_the_jetty(tmp_path, at):
    caches(tmp_path, at, swell=8.0, wind=35.0)
    pixels = draw(tmp_path, at)
    below_the_tip = sea._calm_mask((800, 480), wedge_jetty())
    below_the_tip[:, 25:] = False  # clear of the rocks, which wander a little
    assert (pixels[below_the_tip] == (INK["blue"] if at == AFTERNOON else INK["black"])).all()


def test_the_jetty_reaches_out_to_the_tip():
    c = sea.InkCanvas((800, 480), "blue")
    sea._jetty(c, wedge_jetty(), sea.DAY_LOOK)
    black = c.indices() == INK["black"]
    assert black[215:225, 40:60].mean() > 0.3  # near the tip
    assert black[470:480, 120:135].mean() > 0.3  # at the bottom
    assert not black[:195].any() and not black[:, 190:].any()


def test_the_sea_says_when_its_screen_came_up_not_the_middle_of_its_bucket(tmp_path):
    start = datetime(2026, 11, 5, 12, 0, tzinfo=LA)
    scene = schedule._sea_scene(start, start + timedelta(hours=1), LA)
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    assert sea.sea_model(scene.draw_for, ctx, scene.updated).updated == "Updated 12:00 pm"


@pytest.mark.parametrize("from_deg, dx, dy", [(270, -1, 0), (90, 1, 0), (180, 0, 1), (0, 0, -1)])
def test_direction_arrows_point_as_seen_from_the_window(from_deg, dx, dy):
    """The arrow's head has more ink than its shaft, so more of it lies the way it points."""
    c = sea.InkCanvas((100, 120), "white")
    sea._line(c, 20, 40, sea.ARROW, 40, sea.DAY_LOOK, from_deg)
    ys, xs = np.nonzero(c.indices() == INK["black"])
    along = xs if dx else ys
    mid = (along.min() + along.max()) / 2
    ahead, behind = (along > mid).sum(), (along < mid).sum()
    assert (ahead > behind * 1.2) if (dx or dy) > 0 else (behind > ahead * 1.2)


def test_the_words_sit_on_a_plain_box():
    model = sea.SeaModel(seed=1, daylight=True, sky="clear", light=None, swell_ft=2.0, period_s=13.0,
                         swell_from=225.0, wind_mph=4.0, beach_px=40,
                         words=("Thursday", f"2 ft at 13 s from the SW{sea.ARROW}", f"Wind 4 mph W{sea.ARROW}"),
                         wind_from=270.0, updated="Updated 2:10 pm")
    c = sea.InkCanvas((800, 480), "white")
    sea.draw(c, model, 190.0, None)
    box = c.indices()[9:40, 13:40]
    assert set(np.unique(box)) <= {INK["white"], INK["black"]}  # no sky dots behind the words



# --- what a dashboard preview can ask for -----------------------------------------------------


def test_the_wave_look_names_are_the_shared_ones():
    assert set(sea.WAVES) == set(WAVE_LOOKS)
    assert set(sea.CLOUD_CODES) == set(CLOUD_LOOKS)


def test_a_wave_look_replaces_the_forecast_and_its_words(tmp_path):
    caches(tmp_path, AFTERNOON)
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    plain = sea.sea_model(AFTERNOON, ctx)
    big = sea.sea_model(AFTERNOON, ctx, waves="big")
    assert (big.swell_ft, big.period_s, big.swell_from) == (8, 17, 190)
    assert big.words[1].startswith("8 ft at 17 s from the S")
    assert big.words[2].startswith("Wind 15 mph W")
    assert "Water 62" in big.words[2]  # what the look doesn't set stays
    assert (big.light, big.daylight, big.beach_px) == (plain.light, plain.daylight, plain.beach_px)
    assert plain.swell_ft == 2.0


def test_a_look_draws_without_any_forecast(tmp_path):
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    flat = sea.sea_model(AFTERNOON, ctx)
    assert flat.swell_ft is None and flat.words[1] is None
    gale = sea.sea_model(AFTERNOON, ctx, waves="gale", clouds="overcast")
    assert gale.swell_ft == 6 and gale.wind_mph == 35 and gale.sky == "overcast"
    assert "cloudy" in gale.words[0]
    pixels = render_scene(Scene("sea", "sea/x", AFTERNOON, waves="gale", clouds="overcast"), ctx)
    assert not np.array_equal(pixels, render_scene(Scene("sea", "sea/x", AFTERNOON), ctx))


@pytest.mark.parametrize("clouds", ["clear", "partly", "overcast"])
def test_a_cloud_look_replaces_the_forecast_sky(tmp_path, clouds):
    caches(tmp_path, AFTERNOON, code=61)
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    assert sea.sea_model(AFTERNOON, ctx).sky == "overcast"
    model = sea.sea_model(AFTERNOON, ctx, clouds=clouds)
    assert model.sky == clouds
    assert "71" in model.words[0]  # the temperature is still the forecast's


def test_a_preview_uses_the_forecast_for_the_time_it_shows(tmp_path):
    """A day ahead of the fetch: the wall would leave the swell out, a preview doesn't."""
    tomorrow = AFTERNOON + timedelta(days=1)
    caches(tmp_path, AFTERNOON)
    surf = json.loads((tmp_path / "surf.json").read_text())
    surf["hourly"].append({**surf["hourly"][0], "t": tomorrow.isoformat(), "swell_height": 5.0})
    (tmp_path / "surf.json").write_text(json.dumps(surf))
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    assert sea.sea_model(tomorrow, ctx).swell_ft is None
    preview = replace(ctx, any_age=True)
    assert sea.sea_model(tomorrow, preview).swell_ft == 5.0
    assert sea.sea_model(AFTERNOON + timedelta(days=30), preview).swell_ft is None  # past the forecast


def test_the_time_sets_the_light_and_the_dark_look(tmp_path):
    caches(tmp_path, AFTERNOON)
    ctx = SceneContext.from_config(default_config(), state_dir=tmp_path)
    day = sea.sea_model(AFTERNOON, ctx, waves="calm")
    night = sea.sea_model(NIGHT, ctx, waves="calm")
    assert day.daylight and not night.daylight
    assert day.light.phase is None
    assert night.light is None or night.light.phase is not None


def test_the_jetty_is_the_wedges_and_stays_there(tmp_path):
    """The jetty and its calm water come from the place's data: within 2 km of where they were
    drawn, with the window within 45° of the way it faced (places.sea_for)."""
    caches(tmp_path, AFTERNOON, swell=8.0, wind=35.0)
    calm = sea._calm_mask((800, 480), wedge_jetty())
    calm[:, 25:] = False
    here = draw(tmp_path)
    assert (here[calm] == INK["blue"]).all()
    for config in (moved(lat=33.2), moved(window=100.0)):  # 44 km south; a window facing east
        elsewhere = draw(tmp_path, config=config)
        assert not (elsewhere[calm] == INK["blue"]).all()  # waves and sand where the calm water was
        assert not (elsewhere[300:480, 30:180] == INK["black"]).mean() > 0.3  # no boulders
    assert places.sea_for(33.593, -117.882, 190.0 + 44, ROOT / "data") is not None
    assert places.sea_for(33.593, -117.882, 190.0 + 46, ROOT / "data") is None


def test_with_no_place_the_beach_follows_the_stations_own_range():
    place = places.SeaPlace(None, (0.0, 7.0))
    assert sea.beach_span(place, np.array([1.0, 2.0])) == (0.0, 7.0)
    assert sea.beach_span(None, np.array([-1.5, 2.0, 11.0])) == (-1.5, 11.0)
    assert sea.beach_span(None, np.array([0.2, 0.4])) == (0.2, 1.2)  # never a range under 1 ft
    assert sea.beach_px(-1.5, (-1.5, 11.0)) == 60 and sea.beach_px(11.0, (-1.5, 11.0)) == 26
    assert sea.beach_px(3.5) == sea.beach_px(3.5, (0.0, 7.0))
