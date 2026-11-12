"""Screens that need data this place doesn't have (screens.py, PRD.md §9 L2)."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import screens
from photopainter.config import Schedule, Step, default_config, load_config, save_config
from photopainter.render.draw import INK
from photopainter.scenes import Scene, SceneContext, combo, render_scene

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
FAR = {"name": "Oceanside Pier", "lat": 33.1934, "lon": -117.3866}  # a public spot, no place data


def far(config=None):
    c = config or default_config()
    return replace(c, location=replace(c.location, **FAR))


def test_a_rotation_without_a_screen_keeps_its_cycle():
    day = Schedule().day  # photo, tides, harbor, sky, sea, combo: 10 minutes each
    assert Schedule().without(("harbor",)).day == (Step("photo", 10), Step("tides", 20), Step("sky", 10),
                                                   Step("sea", 10), Step("combo", 10))
    assert sum(s.minutes for s in Schedule().without(("harbor", "photo")).day) == sum(s.minutes for s in day)
    only = Schedule(day=(Step("harbor", 30),), night=(Step("harbor", 60),)).without(("harbor",))
    assert (only.day, only.night) == ((Step("tides", 30),), (Step("sky", 60),))
    assert Schedule().without_photos() == Schedule().without(("photo",))


def test_the_wedge_has_every_screen_and_elsewhere_has_no_harbor_chart():
    assert screens.missing(default_config()) == {}
    gone = screens.missing(far())
    assert list(gone) == ["harbor"] and "No harbor chart" in gone["harbor"]


def test_the_schedule_here_drops_what_cant_be_shown():
    assert screens.schedule_here(default_config(), photos=True) == default_config()
    here = screens.schedule_here(far(), photos=True)
    assert [s.screen for s in here.schedule.day] == ["photo", "tides", "sky", "sea", "combo"]
    neither = screens.schedule_here(far(), photos=False)
    assert [s.screen for s in neither.schedule.day] == ["tides", "sky", "sea", "combo"]
    assert neither.location == far().location  # only the schedule changes


# --- the tick ------------------------------------------------------------------------------------


@pytest.fixture
def frame(tmp_path, monkeypatch):
    from test_tick import Frame

    f = Frame(tmp_path, monkeypatch)
    config, _ = load_config()
    save_config(far(replace(config, schedule=replace(config.schedule, day=(Step("tides", 30), Step("harbor", 30))))))
    return f


def test_the_tick_gives_the_harbors_time_to_the_screen_before_it(frame):
    frame.tick("2026-11-04T10:40")
    assert frame.status["showing"]["key"] == "tides/2026-11-04T10:00"
    assert frame.status["last_error"] is None


def test_a_harbor_pin_from_before_a_move_lapses(frame):
    frame.overrides({"pin": {"kind": "harbor", "photo": None, "until": None, "set_at": "2026-11-04T09:00:00-08:00"},
                     "up_next": []})
    frame.tick("2026-11-04T10:40")
    assert frame.status["showing"]["kind"] == "tides"


# --- the dashboard -------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path):
    from test_web import Site

    s = Site(tmp_path)
    config, _ = load_config()
    save_config(far(config))
    return s


def test_the_dashboard_says_why_and_refuses_the_pin(site):
    body = site.get("/api/status").get_json()
    assert body["screens"]["harbor"] is False and body["screens"]["tides"] is True
    assert "No harbor chart" in body["screens_why"]["harbor"]
    r = site.post("/api/mode", json={"kind": "harbor"})
    assert r.status_code == 400 and "No harbor chart" in r.get_json()["error"]
    assert site.post("/api/mode", json={"kind": "sea"}).status_code == 200


def test_the_dashboard_at_the_wedge_has_every_screen(tmp_path):
    from test_web import Site

    body = Site(tmp_path).get("/api/status").get_json()
    assert body["screens"]["harbor"] is True and body["screens_why"] == {}


# --- the combo -----------------------------------------------------------------------------------


AT = datetime(2026, 11, 3, 14, 10, tzinfo=LA)


def draw_combo(config):
    return render_scene(Scene("combo", "combo/x", AT), SceneContext.from_config(config, state_dir=FIXTURES))


def with_photos(tmp_path, n=1):
    folder = tmp_path / "photos"
    folder.mkdir()
    for i in range(n):
        Image.new("RGB", (300, 180), (220, 40 * i, 30)).save(folder / f"p{i}.jpg")
    return folder


def test_with_no_harbor_chart_the_combo_shows_the_photo(tmp_path):
    c = far()
    config = replace(c, photos=replace(c.photos, folder=str(with_photos(tmp_path))))
    ink = draw_combo(config)
    quarter = ink[242:, :398]
    assert (quarter == INK["red"]).mean() > 0.5  # the red photo
    assert (ink[:, 399:401] == INK["black"]).all()  # the cross is whole
    sea = combo.sea_panel(AT, SceneContext.from_config(config, state_dir=FIXTURES))
    assert (ink[242:, 402:] == sea[2:, 2:]).all()  # the sea as ever


def test_with_no_harbor_chart_and_no_photos_the_sea_takes_the_bottom_row(tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    c = far()
    ink = draw_combo(replace(c, photos=replace(c.photos, folder=str(empty))))
    assert not (ink[245:, 399:401] == INK["black"]).all()  # no divider down the bottom row
    assert (ink[:238, 399:401] == INK["black"]).all()
    bottom = ink[242:]
    assert (bottom == INK["blue"]).mean() > 0.3 and (bottom[-10:] == INK["yellow"]).mean() > 0.3  # sea, then sand


def test_an_unreadable_photo_gives_way_to_the_next(tmp_path):
    folder = with_photos(tmp_path, 2)
    (folder / "p0.jpg").write_bytes(b"not a jpeg")
    c = far()
    config = replace(c, photos=replace(c.photos, folder=str(folder)))
    assert combo.photo_panel(SceneContext.from_config(config, state_dir=FIXTURES)) is not None
    assert np.array_equal(draw_combo(config), draw_combo(config))
