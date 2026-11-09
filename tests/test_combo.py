"""The combo screen (scenes/combo.py): four screens at once, each in a quarter of the panel."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import places
from photopainter.config import SCREEN_KINDS, default_config, validate
from photopainter.render.draw import INK, InkCanvas
from photopainter.schedule import PIN_KINDS
from photopainter.scenes import SCENE_KINDS, Scene, SceneContext, combo, harbor, render_scene, sea, sky

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
AFTERNOON = datetime(2026, 11, 3, 14, 10, tzinfo=LA)
NIGHT = datetime(2026, 11, 3, 22, 30, tzinfo=LA)
pytestmark = pytest.mark.skipif(not (ROOT / "data" / "de421.bsp").exists(), reason="no ephemeris")


def draw(at=AFTERNOON, state_dir=FIXTURES, updated=None, canvas=None):
    ctx = SceneContext.from_config(default_config(), state_dir=state_dir)
    if canvas is not None:
        ctx = replace(ctx, canvas=canvas)
    return render_scene(Scene("combo", "combo/x", at, updated=updated), ctx)


def panels(ink):
    """(tides, sky, harbor, sea), without the dividers."""
    return ink[:239, :399], ink[:239, 401:], ink[241:, :399], ink[241:, 401:]


def share(a, name):
    return float((a == INK[name]).mean())


def test_the_name_is_in_every_list():
    assert "combo" in SCENE_KINDS and "combo" in SCREEN_KINDS and "combo" in PIN_KINDS
    app = (ROOT / "src/photopainter/web/static/app.js").read_text()
    assert 'combo: "Combo", harbor: "Harbor", auto:' in app
    assert 'sea: "Sea", combo: "Combo", photo:' in app
    html = (ROOT / "src/photopainter/web/static/index.html").read_text()
    assert 'data-kind="combo"' in html and 'data-preview="combo"' in html and 'id="icon-combo"' in html


def test_it_fills_the_panel_in_the_six_inks_with_a_black_cross():
    ink = draw()
    assert ink.shape == (480, 800)
    assert (ink[:, 399:401] == INK["black"]).all()
    assert (ink[239:241, :] == INK["black"]).all()


def test_each_quarter_is_its_own_screen():
    tide, heavens, chart, ocean = panels(draw())
    assert share(tide, "blue") > 0.1 and share(tide, "white") > 0.4  # water under a white chart
    assert share(heavens, "blue") > 0.2  # a daytime sky
    assert share(chart, "yellow") > 0.1  # the land
    assert share(ocean, "blue") > 0.3 and share(ocean[-15:], "yellow") > 0.1  # waves, then the beach


def test_the_window_side_goes_dark_at_night_and_the_chart_side_stays_light():
    tide, heavens, chart, ocean = panels(draw(NIGHT))
    assert share(heavens[:150], "black") > 0.7
    assert share(ocean[:60], "black") > 0.4
    assert share(tide, "white") > 0.4 and share(chart, "white") > 0.3


def test_the_same_moment_gives_the_same_pixels():
    assert (draw() == draw()).all()


def test_the_tide_follows_when_the_screen_came_up_and_the_window_the_middle_of_its_bucket():
    """`updated` is when it came up: only the tide side reads it."""
    now, earlier = panels(draw()), panels(draw(updated=AFTERNOON - timedelta(minutes=30)))
    assert (now[0] != earlier[0]).any()
    assert (now[1] == earlier[1]).all() and (now[3] == earlier[3]).all()
    later = panels(draw(AFTERNOON + timedelta(minutes=30), updated=AFTERNOON))
    assert (now[0] == later[0]).all()
    assert (now[1] != later[1]).any() and (now[3] != later[3]).any()


def test_it_draws_with_no_caches_at_all(tmp_path):
    ink = draw(state_dir=tmp_path)
    assert ink.shape == (480, 800)
    assert share(panels(ink)[1], "blue") > 0.2  # the sky doesn't need them


def test_a_portrait_canvas_gets_it_cropped():
    assert draw(canvas=(480, 800)).shape == (800, 480)


def test_the_harbor_panel_has_two_swell_arrows_and_a_smaller_field_of_wind(monkeypatch):
    ctx = SceneContext.from_config(default_config(), state_dir=FIXTURES)
    m = harbor.day_model(AFTERNOON, ctx)
    m = replace(m, weather=replace(m.weather, wind=12.0, wind_dir=270.0))
    swell, wind = [], []
    monkeypatch.setattr(harbor, "swell_arrow", lambda c, x, y, size, *a, **k: swell.append((x, y, size)))
    monkeypatch.setattr(harbor, "wind_arrow", lambda c, x, y, size, *a, **k: wind.append((x, y, size)))
    combo.harbor_panel(m, ctx)
    assert [size for _, _, size in swell] == [combo.SWELL_SIZE] * 2
    assert len(wind) >= 12
    assert {size for _, _, size in wind} == {harbor.wind_length(12.0, combo.WIND_SCALE)}
    width, height = combo.PANEL
    assert all(0 <= x < width and 0 <= y < height for x, y, _ in swell + wind), "off the panel"


def test_the_harbor_panel_follows_the_harbor_settings_view():
    config = default_config()
    moved = validate({"harbor": {"center_lon": -117.93}})
    data = combo.chart.harbor_data(SceneContext.from_config(config, state_dir=FIXTURES))
    here = combo.harbor_view(SceneContext.from_config(config, state_dir=FIXTURES), data)
    there = combo.harbor_view(SceneContext.from_config(moved, state_dir=FIXTURES), data)
    assert there.west < here.west
    assert here.height == combo.PANEL[1] and here.south == config.harbor.south


def test_the_harbor_panel_has_its_label_but_no_map_credit(monkeypatch):
    drawn = []
    real = combo.chart.label
    monkeypatch.setattr(combo.chart, "label", lambda c, xy, s, *a, **k: (drawn.append(s), real(c, xy, s, *a, **k))[1])
    draw()
    assert "NEWPORT BAY" in drawn
    assert not any("OpenStreetMap" in s or "NOAA" in s for s in drawn)  # on the dashboard instead


def test_the_sea_panel_leaves_the_next_tide_to_the_tide_panel():
    line = "Wind 9 mph W  ·  Water 73°  ·  High tide 4:49 pm"
    assert combo._no_tide(line) == "Wind 9 mph W  ·  Water 73°"
    assert combo._no_tide("Low tide 4:49 pm") is None
    assert combo._no_tide(None) is None


def test_the_sea_at_half_size_is_the_same_drawing_at_half_size():
    model = sea.SeaModel(seed=1, daylight=True, sky="clear", light=None, swell_ft=3.0, period_s=11.0, swell_from=190.0,
                         wind_mph=9.0, beach_px=40, words=(None, None, None), wind_from=270.0)
    big, small = InkCanvas((800, 480), "white"), InkCanvas((400, 240), "white")
    jetty = places.sea_for(33.593, -117.882, 190.0, ROOT / "data").jetty  # the Wedge's
    sea.draw(big, model, 190.0, None, jetty=jetty)
    sea.draw(small, model, 190.0, None, scale=0.5, jetty=jetty)
    big, small = big.indices(), small.indices()
    # The jetty is black, in the same place and just as big, and so is the beach's top edge.
    assert abs((big == INK["black"]).mean() - (small == INK["black"]).mean()) < 0.03
    assert (small[110:115, 20:30] == INK["black"]).mean() > 0.3
    assert (small[:95, 90:] == INK["black"]).sum() == 0
    assert (big[-10:] == INK["yellow"]).mean() == pytest.approx((small[-5:] == INK["yellow"]).mean(), abs=0.1)


def test_the_words_with_nothing_to_say_leave_no_box():
    model = sea.SeaModel(seed=1, daylight=True, sky="clear", light=None, swell_ft=None, period_s=None, swell_from=None,
                         wind_mph=0.0, beach_px=40, words=(None, None, None))
    c = InkCanvas((400, 240), "white")
    sea._words(c, model, sea.DAY_LOOK, combo.SEA_WORDS)
    assert (c.indices() == INK["white"]).all()


def test_the_sky_leaves_room_for_the_corners_and_can_leave_out_its_notes():
    """No name is placed on a reserved box, and without notes the top right is just sky."""
    ctx = SceneContext.from_config(default_config(), state_dir=FIXTURES)
    model = replace(sky.sky_model(NIGHT, ctx), weather=None)

    def night(style):
        c = InkCanvas((400, 240), "black")
        sky.draw(c, model, ctx.config, style)
        return c.indices()

    plain = night(combo.SKY_STYLE)
    with_notes = night(replace(combo.SKY_STYLE, notes=True))
    assert (plain[:30, 300:] != with_notes[:30, 300:]).any()

    box = (0.0, 0.0, 400.0, 170.0)
    assert (night(replace(combo.SKY_STYLE, reserved=(box,)))[:170] != plain[:170]).any()
