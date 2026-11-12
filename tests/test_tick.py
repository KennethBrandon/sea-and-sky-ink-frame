"""tick.run() with a fake panel, a fake clock and fake sun times; plus the photo scene,
FakePanel and the CLI's flip handling."""

import json
import shutil
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import cli, hotspot, state, tick
from photopainter.astro.sun import SunEvents
from photopainter.config import Config, QuietHours, Step, default_config, save_config
from photopainter.hardware.battery import FakeBattery
from photopainter.hardware.fake import FakePanel
from photopainter.hardware.panel import PanelDisplay
from photopainter.render import battery_badge
from photopainter.render.draw import INK
from photopainter.render.palette import CODES, palette_colors
from photopainter.scenes import Scene, SceneContext
from photopainter.scenes import photo as photo_scene
from photopainter.sources.common import Refresh

LA = ZoneInfo("America/Los_Angeles")
FIXTURES = Path(__file__).parent / "fixtures"
INK_OF = {"tides": 4, "photo": 5, "message": 1, "sky": 0, "setup": 2,  # what the fake renderer draws
          "harbor": 4, "sea": 4, "combo": 0}


def t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


def SUN(day: date) -> SunEvents:
    rise = datetime.combine(day, time(6, 12), tzinfo=LA)
    set_ = datetime.combine(day, time(16, 57), tzinfo=LA)
    return SunEvents(rise, set_, rise - timedelta(minutes=25), set_ + timedelta(minutes=25))


def photo_then_tides() -> Config:
    """The schedule these tests are written against: a photo for the first 10 minutes of
    every hour, then tides, and the sky hourly at night (the default until 2026-11-08)."""
    c = default_config()
    return replace(c, schedule=replace(c.schedule, day=(Step("photo", 10), Step("tides", 50)),
                                       night=(Step("sky", 60),)))


class Frame:
    """The frame in a temp folder: photos, config, a fake panel on a clock the test sets."""

    def __init__(self, tmp_path: Path, monkeypatch, photos=("a.jpg", "b.jpg", "c.jpg"), fake_render=True,
                 **config_changes):
        self.dir = state.state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.photos = tmp_path / "photos"
        self.photos.mkdir()
        for i, name in enumerate(photos):
            Image.new("RGB", (160, 96), (40 * i, 120, 200)).save(self.photos / name)
        self.configure(**config_changes)
        self.now = t("2026-11-04T09:20")
        self.panel = FakePanel(self.dir)
        self.rendered: list[Scene] = []
        self.fail: set[str] = set()  # scene kinds whose rendering raises
        if fake_render:
            monkeypatch.setattr(tick, "render_scene", self._render)

    def configure(self, **changes) -> None:
        c = photo_then_tides()
        photos = replace(c.photos, folder=str(self.photos), hidden=changes.pop("hidden", ()))
        display = replace(c.display, flip=changes.pop("flip", c.display.flip))
        save_config(replace(c, photos=photos, display=display, schedule=replace(c.schedule, **changes)))

    def _render(self, scene: Scene, ctx: SceneContext) -> np.ndarray:
        if scene.kind in self.fail:
            raise RuntimeError(f"{scene.kind} broke")
        self.rendered.append(scene)
        img = np.full((480, 800), INK_OF[scene.kind], np.uint8)
        img[0, 0] = 3  # red top-left corner, to see the flip
        return img

    def tick(self, at: str | None = None, **kw) -> int:
        if at is not None:
            self.now = t(at)
        kw.setdefault("fetch", False)
        return tick.run(self.now, self.panel, sun=SUN, wall=lambda: self.now, **kw)

    @property
    def status(self) -> dict:
        return state.read_json(self.dir / state.STATUS)

    @property
    def showing(self) -> str | None:
        s = self.status["showing"]
        return s and s["key"]

    def overrides(self, data: dict | None = None) -> dict:
        if data is not None:
            state.write_json(self.dir / state.OVERRIDES, data)
        return state.read_json(self.dir / state.OVERRIDES)


@pytest.fixture
def frame(tmp_path, monkeypatch):
    return Frame(tmp_path, monkeypatch)


# --- the acceptance cases -----------------------------------------------------------------


def test_first_tick_shows_and_writes_status(frame):
    assert frame.tick("2026-11-04T09:20") == 0
    s = frame.status
    assert s["showing"] == {"key": "tides/2026-11-04T09:10", "kind": "tides", "photo": None,
                            "drawn_for": "2026-11-04T09:20:00-08:00",
                            "refreshed_at": "2026-11-04T09:20:00-08:00", "indicator": False, "no_wifi": False}
    assert s["next_change"] == {"at": "2026-11-04T10:00:00-08:00", "kind": "photo"}
    assert (s["refreshing"], s["last_error"]) == (None, None)
    assert s["data"]["clock_ok"] is True and s["data"]["config_error"] is None
    assert s["updated_at"] == "2026-11-04T09:20:00-08:00"
    assert frame.panel.refreshes == 1
    current = Image.open(frame.dir / state.CURRENT_PNG).convert("RGB")
    assert current.size == (800, 480)
    assert current.getpixel((5, 5)) == tuple(palette_colors("estimated")[INK_OF["tides"]])


def test_no_change_means_no_refresh(frame):
    frame.tick("2026-11-04T09:20")
    png = (frame.dir / state.CURRENT_PNG).read_bytes()
    for at in ("2026-11-04T09:21", "2026-11-04T09:30", "2026-11-04T09:59"):
        assert frame.tick(at) == 0
    assert frame.panel.refreshes == 1 and len(frame.rendered) == 1
    assert frame.status["updated_at"] == "2026-11-04T09:59:00-08:00"
    assert (frame.dir / state.CURRENT_PNG).read_bytes() == png


def test_key_change_refreshes_once_and_updates_status(frame):
    frame.tick("2026-11-04T09:20")
    frame.tick("2026-11-04T10:00")
    s = frame.status
    assert frame.panel.refreshes == 2
    assert (s["showing"]["key"], s["showing"]["kind"], s["showing"]["photo"]) == (
        "photo/2026-11-04T10:00", "photo", "a.jpg")
    assert s["next_change"] == {"at": "2026-11-04T10:10:00-08:00", "kind": "tides"}
    frame.tick("2026-11-04T10:05")
    frame.tick("2026-11-04T10:10")
    assert frame.panel.refreshes == 3 and frame.showing == "tides/2026-11-04T10:10"


def test_refreshing_is_written_before_the_panel_starts(frame):
    seen = []

    class Watching(FakePanel):
        def show(self, codes):
            seen.append(state.read_json(state.state_dir() / state.STATUS)["refreshing"])
            super().show(codes)

    frame.panel = Watching(frame.dir)
    frame.tick("2026-11-04T09:20")
    assert seen == [{"key": "tides/2026-11-04T09:10", "started_at": "2026-11-04T09:20:00-08:00",
                     "panel_at": "2026-11-04T09:20:00-08:00"}]
    assert frame.status["refreshing"] is None


def test_the_new_picture_is_there_for_the_dashboard_while_the_panel_refreshes(frame):
    frame.tick("2026-11-04T09:20")
    old = (frame.dir / state.CURRENT_PNG).read_bytes()
    during = []

    class Watching(FakePanel):
        def show(self, codes):
            during.append(((frame.dir / state.INCOMING_PNG).read_bytes(),
                           (frame.dir / state.CURRENT_PNG).read_bytes()))
            super().show(codes)

    frame.panel = Watching(frame.dir)
    frame.tick("2026-11-04T10:00")
    incoming, current = during[0]
    assert current == old and incoming != old  # the wall's image only changes once it's done
    assert (frame.dir / state.CURRENT_PNG).read_bytes() == incoming
    assert not (frame.dir / state.INCOMING_PNG).exists()


def test_a_panel_that_fails_takes_the_new_picture_away(frame, monkeypatch):
    frame.tick("2026-11-04T09:20")
    old = (frame.dir / state.CURRENT_PNG).read_bytes()

    def broken(codes):
        assert (frame.dir / state.INCOMING_PNG).exists()
        raise TimeoutError("panel did not finish within 180 s")

    monkeypatch.setattr(frame.panel, "show", broken)
    assert frame.tick("2026-11-04T10:00") == 1
    assert (frame.dir / state.CURRENT_PNG).read_bytes() == old
    assert not (frame.dir / state.INCOMING_PNG).exists()


def test_a_change_right_after_a_refresh_goes_up_at_once(frame):
    frame.tick("2026-11-04T09:59:50")  # day screen refreshed 10 s before the photo slot
    assert frame.tick("2026-11-04T10:00") == 0
    assert frame.showing == "photo/2026-11-04T10:00" and frame.panel.refreshes == 2


def test_render_error_keeps_the_previous_image_and_reports_it(frame):
    frame.tick("2026-11-04T09:20")
    png = (frame.dir / state.CURRENT_PNG).read_bytes()
    frame.fail.add("photo")
    assert frame.tick("2026-11-04T10:00") == 1
    s = frame.status
    assert s["showing"]["key"] == "tides/2026-11-04T09:10"
    assert s["last_error"]["where"] == "render" and "photo/2026-11-04T10:00" in s["last_error"]["message"]
    assert s["last_error"]["at"] == "2026-11-04T10:00:00-08:00"
    assert s["refreshing"] is None and frame.panel.refreshes == 1
    assert (frame.dir / state.CURRENT_PNG).read_bytes() == png
    frame.fail.clear()
    assert frame.tick("2026-11-04T10:01") == 0  # tries again
    assert frame.showing == "photo/2026-11-04T10:00"


def test_panel_error_is_reported(frame, monkeypatch):
    def broken(codes):
        raise TimeoutError("panel did not finish within 180 s")

    monkeypatch.setattr(frame.panel, "show", broken)
    assert frame.tick("2026-11-04T09:20") == 1
    s = frame.status
    assert s["showing"] is None and s["refreshing"] is None
    assert s["last_error"]["where"] == "panel" and "180 s" in s["last_error"]["message"]


def test_up_next_is_consumed_exactly_once(frame):
    frame.overrides({"pin": None, "up_next": ["c.jpg", "b.jpg"]})
    frame.tick("2026-11-04T10:00")
    assert frame.status["showing"]["photo"] == "c.jpg"
    assert frame.overrides()["up_next"] == ["b.jpg"]
    frame.tick("2026-11-04T10:05")  # same step: nothing more
    assert frame.overrides()["up_next"] == ["b.jpg"] and frame.panel.refreshes == 1
    frame.tick("2026-11-04T11:00")
    assert frame.status["showing"]["photo"] == "b.jpg"
    assert frame.overrides()["up_next"] == []
    frame.tick("2026-11-04T12:00")  # the rotation, which Up next didn't move
    assert frame.status["showing"]["photo"] == "a.jpg"


def test_up_next_stays_until_the_panel_showed_it(frame, monkeypatch):
    frame.overrides({"pin": None, "up_next": ["c.jpg"]})

    def broken(codes):
        raise TimeoutError("panel did not finish within 180 s")

    with monkeypatch.context() as m:
        m.setattr(frame.panel, "show", broken)
        assert frame.tick("2026-11-04T10:00") == 1
    assert frame.overrides()["up_next"] == ["c.jpg"]
    frame.tick("2026-11-04T10:01")
    assert frame.status["showing"]["photo"] == "c.jpg" and frame.overrides()["up_next"] == []


def test_missing_up_next_photo_is_dropped_and_the_rotation_shows(frame):
    frame.overrides({"pin": None, "up_next": ["gone.jpg"]})
    frame.tick("2026-11-04T10:00")
    assert frame.status["showing"]["photo"] == "a.jpg"
    assert frame.overrides() == {"pin": None, "up_next": [], "redraw_at": None}
    assert photo_scene.rotation(SceneContext.from_config(_config()))[0].name == "b.jpg"


def test_rotation_moves_on_only_after_the_panel_showed_it(frame, monkeypatch):
    def broken(codes):
        raise TimeoutError("panel did not finish within 180 s")

    with monkeypatch.context() as m:
        m.setattr(frame.panel, "show", broken)
        assert frame.tick("2026-11-04T10:00") == 1
    assert not (frame.dir / state.SLIDESHOW).exists()
    frame.tick("2026-11-04T10:01")
    frame.tick("2026-11-04T11:00")
    assert frame.status["showing"]["photo"] == "b.jpg"


def test_hidden_photos_are_skipped(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, hidden=("a.jpg", "b.jpg"))
    frame.overrides({"pin": None, "up_next": ["b.jpg"]})
    frame.tick("2026-11-04T10:00")
    assert frame.status["showing"]["photo"] == "c.jpg"


def test_show_now_pin_neither_consumes_up_next_nor_moves_the_rotation(frame):
    frame.overrides({"pin": {"kind": "photo", "photo": "c.jpg", "until": "2026-11-04T09:32:00-08:00",
                             "set_at": "2026-11-04T09:22:00-08:00"}, "up_next": ["c.jpg"]})
    frame.tick("2026-11-04T09:22")
    s = frame.status
    assert (s["showing"]["key"], s["showing"]["photo"]) == ("photo/2026-11-04T09:22/c.jpg", "c.jpg")
    assert s["next_change"] == {"at": "2026-11-04T09:32:00-08:00", "kind": "tides"}
    assert frame.overrides()["up_next"] == ["c.jpg"]
    assert not (frame.dir / state.SLIDESHOW).exists()
    frame.tick("2026-11-04T09:32")  # pin over: back to the schedule
    assert frame.showing == "tides/2026-11-04T09:10"


def test_expired_pin_is_removed_when_the_tick_writes_overrides(frame):
    frame.overrides({"pin": {"kind": "tides", "photo": None, "until": "2026-11-04T09:50:00-08:00",
                             "set_at": "2026-11-04T09:00:00-08:00"}, "up_next": ["b.jpg"]})
    frame.tick("2026-11-04T10:00")
    assert frame.overrides() == {"pin": None, "up_next": [], "redraw_at": None}


# --- redraw requests (the dashboard's Redraw now) -------------------------------------------


def test_redraw_request_redraws_the_same_key_and_is_cleared(frame):
    frame.tick("2026-11-04T09:20")
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T09:25:00-08:00"})
    assert frame.tick("2026-11-04T09:26") == 0
    s = frame.status
    assert frame.panel.refreshes == 2 and len(frame.rendered) == 2
    assert s["showing"]["key"] == "tides/2026-11-04T09:10"
    assert s["showing"]["refreshed_at"] == "2026-11-04T09:26:00-08:00"
    assert frame.overrides()["redraw_at"] is None
    frame.tick("2026-11-04T09:27")
    assert frame.panel.refreshes == 2  # once


def test_stale_redraw_request_is_cleared_without_a_refresh(frame):
    frame.tick("2026-11-04T09:20")
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T09:15:00-08:00"})
    frame.tick("2026-11-04T09:21")
    assert frame.panel.refreshes == 1 and frame.overrides()["redraw_at"] is None


def test_redraw_request_right_after_a_refresh_goes_up_at_once(frame):
    frame.tick("2026-11-04T09:20")
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T09:20:10-08:00"})
    frame.tick("2026-11-04T09:20:20")
    assert frame.panel.refreshes == 2 and frame.overrides()["redraw_at"] is None


def test_redraw_request_is_kept_when_the_panel_fails(frame, monkeypatch):
    frame.tick("2026-11-04T09:20")
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T09:20:10-08:00"})

    def broken(codes):
        raise TimeoutError("panel did not finish within 180 s")

    with monkeypatch.context() as m:
        m.setattr(frame.panel, "show", broken)
        assert frame.tick("2026-11-04T09:21") == 1
    assert frame.overrides()["redraw_at"] == "2026-11-04T09:20:10-08:00"
    frame.tick("2026-11-04T09:22")
    assert frame.panel.refreshes == 2 and frame.overrides()["redraw_at"] is None


def test_redraw_request_applies_in_quiet_hours(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, quiet_hours=QuietHours(time(22, 0), time(6, 0)))
    frame.tick("2026-11-04T21:30")
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T22:30:00-08:00"})
    frame.tick("2026-11-04T22:31")
    assert frame.panel.refreshes == 2 and frame.showing == "sky/2026-11-04T22:00"
    assert frame.overrides()["redraw_at"] is None


def test_redraw_request_with_nothing_on_the_wall_just_draws(frame):
    frame.overrides({"pin": None, "up_next": [], "redraw_at": "2026-11-04T09:00:00-08:00"})
    frame.tick("2026-11-04T09:20")
    assert frame.panel.refreshes == 1 and frame.overrides()["redraw_at"] is None


def test_a_slideshow_moves_on_each_step_day_and_night(tmp_path, monkeypatch):
    only_photos = (Step("photo", 60),)
    frame = Frame(tmp_path, monkeypatch, day=only_photos, night=only_photos)
    shown = []
    for now in ("2026-11-04T10:00", "2026-11-04T11:00", "2026-11-04T21:00", "2026-11-04T22:00"):
        frame.tick(now)
        shown.append(frame.status["showing"]["photo"])
    assert shown == ["a.jpg", "b.jpg", "c.jpg", "a.jpg"]


def test_a_photo_step_at_night_uses_up_next(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, night=(Step("photo", 10), Step("sky", 50)))
    frame.overrides({"pin": None, "up_next": ["c.jpg"]})
    frame.tick("2026-11-04T21:00")
    assert frame.status["showing"]["photo"] == "c.jpg" and frame.overrides()["up_next"] == []
    frame.tick("2026-11-04T21:10")
    assert frame.status["showing"]["kind"] == "sky"
    frame.tick("2026-11-04T22:00")  # the rotation, which Up next didn't move
    assert frame.status["showing"]["photo"] == "a.jpg"


def test_only_photos_with_no_photos_shows_the_periods_screen(tmp_path, monkeypatch):
    only_photos = (Step("photo", 60),)
    frame = Frame(tmp_path, monkeypatch, photos=(), day=only_photos, night=only_photos)
    frame.tick("2026-11-04T10:00")
    assert frame.status["showing"]["key"] == "tides/2026-11-04T10:00"
    frame.tick("2026-11-04T21:00")
    assert frame.status["showing"]["key"] == "sky/2026-11-04T21:00"
    assert frame.status["last_error"] is None


def test_no_photos_means_no_photo_steps(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=())
    frame.tick("2026-11-04T09:20")
    frame.tick("2026-11-04T10:00")
    s = frame.status
    assert s["showing"]["kind"] == "tides" and s["last_error"] is None
    assert s["next_change"]["kind"] == "tides"


def test_unreadable_photo_is_skipped(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, fake_render=False)
    (frame.photos / "a.jpg").write_bytes(b"not a jpeg")
    frame.tick("2026-11-04T10:00")
    assert frame.status["showing"]["photo"] == "b.jpg"


# --- clock, quiet hours, config and data errors -------------------------------------------


def test_clock_not_set_keeps_the_image_then_shows_photos(frame):
    frame.tick("2026-11-04T09:20", clock_ok=False, uptime_s=120)
    s = frame.status
    assert s["showing"] is None and s["data"]["clock_ok"] is False and frame.panel.refreshes == 0
    frame.tick("2026-11-04T09:30", clock_ok=False, uptime_s=601)
    s = frame.status
    assert (s["showing"]["key"], s["showing"]["kind"]) == ("photo/no-clock/0", "photo")
    first = s["showing"]["photo"]
    frame.tick("2026-11-04T09:31", clock_ok=False, uptime_s=661)
    assert frame.panel.refreshes == 1  # once, not every minute
    frame.tick("2026-11-04T10:30", clock_ok=False, uptime_s=3700)
    s = frame.status
    assert s["showing"]["key"] == "photo/no-clock/1" and s["showing"]["photo"] != first  # a new one an hour
    frame.tick("2026-11-04T10:40", clock_ok=True)
    assert frame.status["showing"]["kind"] != "message" and frame.status["data"]["clock_ok"] is True


def test_clock_not_set_with_no_photos_says_so(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=())
    frame.tick("2026-11-04T09:20", clock_ok=False, uptime_s=120)
    assert frame.status["showing"] is None and frame.panel.refreshes == 0
    frame.tick("2026-11-04T09:30", clock_ok=False, uptime_s=601)
    s = frame.status
    assert (s["showing"]["key"], s["showing"]["kind"]) == ("message/clock-not-set", "message")
    assert frame.rendered[-1].message.startswith("Clock not set\n")
    assert s["next_change"] is None
    frame.tick("2026-11-04T09:31", clock_ok=False, uptime_s=661)
    assert frame.panel.refreshes == 1  # once, not every minute
    frame.tick("2026-11-04T09:40", clock_ok=True)
    assert frame.showing.startswith("tides/") and frame.status["data"]["clock_ok"] is True


def test_battery_reading_goes_in_status_and_the_log(frame):
    battery = FakeBattery(frame.dir)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["battery"] is None  # no fake_battery.json: unavailable, like a Mac
    assert not (frame.dir / tick.BATTERY_LOG).exists()
    battery.set(3.98, 312)
    frame.tick("2026-11-04T09:21", battery=battery)
    assert frame.status["battery"] == {"volts": 3.98, "milliamps": 312.0, "state": "charging", "percent": 84,
                                       "on_battery_since": None, "indicator": False, "low": False,
                                       "read_at": "2026-11-04T09:21:00-08:00"}
    battery.set(3.9, -180)
    frame.tick("2026-11-04T09:22", battery=battery)
    frame.tick("2026-11-04T09:23", battery=battery)
    b = frame.status["battery"]
    assert (b["state"], b["low"], b["on_battery_since"]) == ("discharging", False, "2026-11-04T09:22:00-08:00")
    assert frame.showing == "tides/2026-11-04T09:10" and frame.status["last_error"] is None
    battery.set(4.1, 400)
    frame.tick("2026-11-04T09:24", battery=battery)
    assert frame.status["battery"]["on_battery_since"] is None
    log = (frame.dir / tick.BATTERY_LOG).read_text().splitlines()
    assert log[0] == "time,volts,milliamps,state"
    assert log[1] == "2026-11-04T09:21:00-08:00,3.980,312.0,charging"
    assert log[2:] == ["2026-11-04T09:22:00-08:00,3.900,-180.0,discharging",
                       "2026-11-04T09:23:00-08:00,3.900,-180.0,discharging",
                       "2026-11-04T09:24:00-08:00,4.100,400.0,charging"]


def test_battery_log_is_trimmed(frame, monkeypatch):
    monkeypatch.setattr(tick, "BATTERY_LOG_MAX_LINES", 5)
    monkeypatch.setattr(tick, "BATTERY_LOG_KEEP_LINES", 3)
    battery = FakeBattery(frame.dir)
    battery.set(4.1, 0)
    for minute in range(7):
        frame.tick(f"2026-11-04T09:{20 + minute:02d}", battery=battery)
    log = (frame.dir / tick.BATTERY_LOG).read_text().splitlines()
    assert log[0] == "time,volts,milliamps,state"
    minutes = [line[11:16] for line in log[1:]]
    assert 3 <= len(minutes) <= 5 and minutes[-1] == "09:26"  # trimmed (the size check is approximate)
    assert minutes == [f"09:{27 - len(minutes) + i}" for i in range(len(minutes))]  # the latest, in order


def set_battery_config(frame, **battery) -> None:
    frame.configure()
    data = json.loads(state.config_path().read_text())
    data["battery"] = battery
    state.config_path().write_text(json.dumps(data))


def badge_box(corner: str = battery_badge.DEFAULT_CORNER) -> np.ndarray:
    """Where the indicator goes on an 800x480 canvas: a boolean mask."""
    blank = np.full((480, 800), INK["blue"], np.uint8)
    return battery_badge.draw(blank, 50, corner) != INK["blue"]


def has_badge(frame) -> bool:
    """Is the indicator on `current.png`? Its corner would be solid white and black."""
    x, y = 800 - battery_badge.MARGIN - 1, 480 - battery_badge.MARGIN - 1  # the box's bottom-right pixel
    black = tuple(palette_colors("estimated")[INK["black"]])
    current = Image.open(frame.dir / state.CURRENT_PNG).convert("RGB")
    return current.getpixel((x, y)) == black and current.getpixel((x - 1, y - 1)) != black


def test_indicator_only_below_its_voltage_on_the_battery(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)  # on battery, above 3.6 V
    frame.tick("2026-11-04T09:20", battery=battery)
    s = frame.status
    assert s["showing"]["indicator"] is False and s["battery"]["indicator"] is False and not has_badge(frame)
    frame.tick("2026-11-04T09:21", battery=battery)
    assert frame.panel.refreshes == 1


def test_indicator_drawn_on_a_new_screen_below_its_voltage(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.5, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["indicator"] is True and has_badge(frame)
    assert frame.status["battery"]["indicator"] is True and frame.status["battery"]["low"] is False
    assert frame.panel.refreshes == 1  # drawn with it from the start: no second refresh


def test_indicator_is_not_drawn_on_usb_power(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.3, 300)  # a flat cell charging
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["indicator"] is False and not has_badge(frame)
    assert frame.status["battery"]["indicator"] is False


def test_crossing_redraws_the_screen_once_and_a_level_change_doesnt(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    key = frame.showing
    battery.set(3.58, -180)  # crossed
    frame.tick("2026-11-04T09:25", battery=battery)
    assert frame.panel.refreshes == 2 and frame.showing == key and frame.status["showing"]["indicator"] is True
    assert has_badge(frame)
    for minute, volts in ((26, 3.55), (27, 3.5), (28, 3.45)):  # the level falls: no refresh
        battery.set(volts, -180)
        frame.tick(f"2026-11-04T09:{minute}", battery=battery)
    assert frame.panel.refreshes == 2


def test_a_voltage_bouncing_around_the_setting_redraws_once(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    for minute, volts in ((25, 3.59), (26, 3.61), (27, 3.59), (28, 3.61)):
        battery.set(volts, -180)
        frame.tick(f"2026-11-04T09:{minute}", battery=battery)
        assert frame.status["battery"]["indicator"] is True
    assert frame.panel.refreshes == 2


def test_crossing_from_usb_power_redraws_too(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.5, 200)  # charging, already under the setting's voltage
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["indicator"] is False
    battery.set(3.45, -180)  # the power went
    frame.tick("2026-11-04T09:25", battery=battery)
    assert frame.panel.refreshes == 2 and frame.status["showing"]["indicator"] is True


def test_crossing_waits_for_quiet_hours(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, quiet_hours=QuietHours(time(9, 0), time(11, 0)))
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)
    frame.tick("2026-11-04T08:59", battery=battery)
    before = frame.panel.refreshes
    battery.set(3.58, -180)
    frame.tick("2026-11-04T09:30", battery=battery)
    assert frame.panel.refreshes == before  # quiet: the indicator waits
    assert frame.status["battery"]["indicator"] is True and frame.status["showing"]["indicator"] is False
    frame.tick("2026-11-04T11:01", battery=battery)  # after quiet hours the next screen carries it
    assert frame.panel.refreshes == before + 1 and frame.status["showing"]["indicator"] is True


def test_crossing_on_a_rotation_photo_redraws_the_same_photo(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)
    frame.tick("2026-11-04T10:01", battery=battery)
    assert frame.status["showing"]["kind"] == "photo"
    name, key = frame.status["showing"]["photo"], frame.showing
    battery.set(3.58, -180)
    frame.tick("2026-11-04T10:03", battery=battery)
    s = frame.status["showing"]
    assert frame.panel.refreshes == 2 and (s["photo"], s["key"], s["indicator"]) == (name, key, True)
    assert name == "a.jpg"
    frame.tick("2026-11-04T11:01", battery=battery)
    assert frame.status["showing"]["photo"] == "b.jpg"  # the redraw didn't move the rotation on


def test_indicator_goes_at_the_next_redraw_with_no_refresh_of_its_own(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.5, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["indicator"] is True
    battery.set(3.6, 300)  # the power is back
    frame.tick("2026-11-04T09:21", battery=battery)
    frame.tick("2026-11-04T09:22", battery=battery)
    assert frame.panel.refreshes == 1 and frame.status["showing"]["indicator"] is True  # still up: e-ink
    assert frame.status["battery"]["indicator"] is False
    frame.tick("2026-11-04T10:00", battery=battery)  # the next screen
    assert frame.panel.refreshes == 2 and frame.status["showing"]["indicator"] is False and not has_badge(frame)


def test_indicator_setting_of_4_2_means_all_the_time_on_battery(frame):
    set_battery_config(frame, indicator_volts=4.2)
    battery = FakeBattery(frame.dir)
    battery.set(4.1, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["indicator"] is True


def test_low_flips_to_the_next_photo_then_waits_for_power(frame, monkeypatch):
    synced = []
    monkeypatch.setattr(tick.os, "sync", lambda: synced.append(True))
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.showing == "tides/2026-11-04T09:10" and synced == []
    battery.set(3.15, -180)  # below 3.3 V, on battery
    frame.tick("2026-11-04T09:30", battery=battery)
    s = frame.status
    assert (s["showing"]["key"], s["showing"]["kind"], s["showing"]["photo"]) == (tick.LOW_BATTERY_KEY, "photo", "a.jpg")
    assert s["showing"]["indicator"] is True and has_badge(frame)
    assert s["battery"]["low"] is True and s["battery"]["on_battery_since"] == "2026-11-04T09:20:00-08:00"
    assert s["next_change"] is None and synced == [True]
    fetched = []
    monkeypatch.setattr(tick._Tick, "_refresh_data", lambda self: fetched.append(True))
    battery.set(3.1, -180)
    frame.tick("2026-11-04T10:00", battery=battery)  # a photo would be due: it isn't drawn
    assert frame.panel.refreshes == 2 and fetched == [] and synced == [True, True]
    assert frame.status["battery"]["volts"] == 3.1 and frame.status["updated_at"] == "2026-11-04T10:00:00-08:00"
    battery.set(3.15, 300)  # the power is back: charging
    frame.tick("2026-11-04T10:01", battery=battery)
    assert fetched == [True] and frame.showing == "photo/2026-11-04T10:00" and frame.panel.refreshes == 3
    assert frame.status["battery"]["low"] is False and synced == [True, True]
    assert frame.status["showing"]["photo"] == "b.jpg"  # the rotation moved on past the low-battery photo
    assert frame.status["showing"]["indicator"] is False


def test_low_keeps_a_photo_that_is_up_with_no_refresh(frame, monkeypatch):
    synced = []
    monkeypatch.setattr(tick.os, "sync", lambda: synced.append(True))
    battery = FakeBattery(frame.dir)
    battery.set(3.5, -180)
    frame.tick("2026-11-04T10:01", battery=battery)
    key = frame.showing
    assert frame.status["showing"]["kind"] == "photo" and frame.panel.refreshes == 1
    battery.set(3.15, -180)
    frame.tick("2026-11-04T10:02", battery=battery)
    frame.tick("2026-11-04T10:30", battery=battery)  # tides would be due: they aren't drawn
    assert frame.showing == key and frame.panel.refreshes == 1 and frame.status["battery"]["low"] is True
    assert synced == [True, True]


def test_low_takes_up_next_first(frame):
    frame.overrides({"pin": None, "up_next": ["c.jpg"], "redraw_at": None})
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    battery.set(3.15, -180)
    frame.tick("2026-11-04T09:30", battery=battery)
    assert frame.status["showing"]["photo"] == "c.jpg" and frame.overrides()["up_next"] == []


@pytest.mark.parametrize("at", ["2026-11-04T09:20", "2026-11-04T10:01"])  # flipped to a photo, or one kept
def test_low_stays_when_the_voltage_climbs_back(frame, at):
    battery = FakeBattery(frame.dir)
    battery.set(3.15, -180)
    frame.tick(at, battery=battery)
    key, refreshes = frame.showing, frame.panel.refreshes
    battery.set(3.3, -120)  # the load dropped and the cell recovered a little: still no power
    frame.now += timedelta(minutes=1)
    frame.tick(battery=battery)
    frame.now += timedelta(hours=1)  # another screen would be due: it isn't drawn
    frame.tick(battery=battery)
    assert frame.showing == key and frame.panel.refreshes == refreshes and frame.status["battery"]["low"] is True


def test_low_with_no_photos_is_a_blank_page(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=())
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.status["showing"]["kind"] == "tides"
    battery.set(3.15, -180)
    assert frame.tick("2026-11-04T09:30", battery=battery) == 0
    shown = frame.rendered[-1]
    assert (frame.showing, shown.kind, shown.message) == (tick.LOW_BATTERY_KEY, "message", "")
    assert frame.status["showing"]["indicator"] is True and frame.status["last_error"] is None
    assert frame.panel.refreshes == 2
    frame.tick("2026-11-04T09:31", battery=battery)
    assert frame.panel.refreshes == 2


def test_low_photo_really_has_the_indicator(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, fake_render=False)
    battery = FakeBattery(frame.dir)
    battery.set(3.15, -180)
    assert frame.tick("2026-11-04T09:20", battery=battery) == 0
    assert frame.status["showing"]["photo"] == "a.jpg" and has_badge(frame)


def test_low_on_usb_power_is_not_low(frame):
    battery = FakeBattery(frame.dir)
    battery.set(3.1, 5)  # a flat cell charging slowly, or resting: the frame runs on USB
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.showing == "tides/2026-11-04T09:10" and frame.status["battery"]["low"] is False


def test_low_threshold_is_a_setting(frame):
    set_battery_config(frame, low_volts=3.8)
    battery = FakeBattery(frame.dir)
    battery.set(3.7, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.showing == tick.LOW_BATTERY_KEY


def test_indicator_below_low_is_used_as_given(frame):
    set_battery_config(frame, indicator_volts=3.1, low_volts=3.5)
    battery = FakeBattery(frame.dir)
    battery.set(3.4, -180)
    frame.tick("2026-11-04T09:20", battery=battery)
    assert frame.showing == tick.LOW_BATTERY_KEY and frame.status["showing"]["indicator"] is False


def test_low_power_turns_wifi_and_the_dashboard_off_on_battery(frame, monkeypatch):
    from photopainter import power

    monkeypatch.setattr(power, "STAY_AWAKE", frame.dir / "no-such-file")
    frame.configure()
    data = json.loads(state.config_path().read_text())
    data["battery"] = {"low_power": True, "low_power_after_minutes": 1, "data_every_hours": 2}
    state.config_path().write_text(json.dumps(data))
    battery = FakeBattery(frame.dir)
    ran: list[str] = []
    fetched: list[datetime] = []
    monkeypatch.setattr(tick._Tick, "_refresh_data", lambda self: fetched.append(self.now) if self.network_ok else None)
    battery.set(4.1, 300)  # on USB
    frame.tick("2026-11-04T09:20", battery=battery, power_runner=ran.append)
    assert frame.status["power"] == {"mode": "normal", "wifi": "on", "since": "2026-11-04T09:20:00-08:00",
                                     "window_until": None, "next_window_at": None}
    battery.set(3.9, -200)  # the power goes
    frame.tick("2026-11-04T09:21", battery=battery, power_runner=ran.append)
    assert frame.status["power"]["mode"] == "normal" and ran == []  # not a minute on battery yet
    frame.tick("2026-11-04T09:22", battery=battery, power_runner=ran.append)
    p = frame.status["power"]
    assert (p["mode"], p["wifi"], p["since"], p["next_window_at"]) == ("low", "off", "2026-11-04T09:22:00-08:00",
                                                                        "2026-11-04T11:22:00-08:00")
    assert ran == ["web_stop", "wifi_off"]
    assert len(fetched) == 2  # the two ticks before Wi-Fi went off
    frame.tick("2026-11-04T10:00", battery=battery, power_runner=ran.append)  # a photo is due: still drawn
    assert frame.showing == "photo/2026-11-04T10:00" and len(fetched) == 2
    frame.tick("2026-11-04T11:22", battery=battery, power_runner=ran.append)  # the data window opens
    assert ran[-1] == "wifi_on" and frame.status["power"]["wifi"] == "on" and len(fetched) == 2  # not this tick
    frame.tick("2026-11-04T11:23", battery=battery, power_runner=ran.append)
    assert len(fetched) == 3  # connected by now: fetch
    frame.tick("2026-11-04T11:26", battery=battery, power_runner=ran.append)  # the window closes
    assert ran[-1] == "wifi_off" and frame.status["power"]["next_window_at"] == "2026-11-04T13:26:00-08:00"
    battery.set(4.0, 500)  # the power is back
    frame.tick("2026-11-04T11:30", battery=battery, power_runner=ran.append)
    assert ran[-2:] == ["wifi_on", "web_start"] and frame.status["power"]["mode"] == "normal"
    assert len(fetched) == 3  # Wi-Fi just came on: next tick
    frame.tick("2026-11-04T11:31", battery=battery, power_runner=ran.append)
    assert len(fetched) == 4


def test_low_power_action_failures_are_reported_not_fatal(frame, monkeypatch):
    from photopainter import power

    monkeypatch.setattr(power, "STAY_AWAKE", frame.dir / "no-such-file")
    frame.configure()
    data = json.loads(state.config_path().read_text())
    data["battery"] = {"low_power": True, "low_power_after_minutes": 0}
    state.config_path().write_text(json.dumps(data))
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -200)

    def runner(action):
        raise RuntimeError(f"{action} broke")

    assert frame.tick("2026-11-04T09:20", battery=battery, power_runner=runner) == 0
    assert frame.status["power"]["mode"] == "low"
    assert frame.status["last_error"]["where"] == "power" and "wifi_off broke" in frame.status["last_error"]["message"]
    assert frame.showing == "tides/2026-11-04T09:10"


def test_stay_awake_file_blocks_low_power(frame, monkeypatch):
    from photopainter import power

    flag = frame.dir / "stay-awake"
    flag.write_text("")
    monkeypatch.setattr(power, "STAY_AWAKE", flag)
    frame.configure()
    data = json.loads(state.config_path().read_text())
    data["battery"] = {"low_power": True, "low_power_after_minutes": 0}
    state.config_path().write_text(json.dumps(data))
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -200)
    ran: list[str] = []
    frame.tick("2026-11-04T09:20", battery=battery, power_runner=ran.append)
    assert frame.status["power"]["mode"] == "normal" and ran == []


def test_battery_reader_errors_never_fail_the_tick(frame):
    class Broken:
        def read(self):
            raise RuntimeError("bus on fire")

    assert frame.tick("2026-11-04T09:20", battery=Broken()) == 0
    assert frame.showing == "tides/2026-11-04T09:10" and frame.status["battery"] is None
    assert frame.status["last_error"]["where"] == "battery" and "bus on fire" in frame.status["last_error"]["message"]


def test_quiet_hours_keep_whatever_was_last_shown(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, quiet_hours=QuietHours(time(22, 0), time(6, 0)))
    frame.tick("2026-11-04T21:30")
    assert frame.showing == "sky/2026-11-04T21:00"
    frame.tick("2026-11-04T22:30")
    s = frame.status
    assert s["showing"]["key"] == "sky/2026-11-04T21:00" and frame.panel.refreshes == 1
    assert s["next_change"] == {"at": "2026-11-05T06:00:00-08:00", "kind": "sky"}
    frame.tick("2026-11-05T06:00")
    assert frame.showing == "sky/2026-11-05T06:00" and frame.panel.refreshes == 2


def test_quiet_hours_still_show_something_the_first_time(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, quiet_hours=QuietHours(time(22, 0), time(6, 0)))
    frame.tick("2026-11-04T23:00")
    assert frame.panel.refreshes == 1


def test_bad_config_uses_defaults_and_reports_it(frame):
    state.config_path().write_text('{"photos": {"show_now_minutes": "ten"}}')
    assert frame.tick("2026-11-04T09:20") == 0
    assert "show_now_minutes" in frame.status["data"]["config_error"]
    assert frame.panel.refreshes == 1


def test_fetch_errors_go_in_status(frame, monkeypatch):
    import photopainter.sources as sources

    fetched = t("2026-11-04T01:00")

    def fake_fresh(config, now=None, *, force=False):
        return {"tides": Refresh("tides", False, None, fetched, "fresh", {"curve": [], "hilo": []}),
                "weather": Refresh("weather", False, "can't reach api.open-meteo.com", None, "failed"),
                "surf": Refresh("surf", False, None, None, "disabled in config")}

    monkeypatch.setattr(sources, "ensure_all_fresh", fake_fresh)
    assert frame.tick("2026-11-04T09:20", fetch=True) == 0  # being offline isn't a failure
    s = frame.status
    assert s["last_error"] == {"at": "2026-11-04T09:20:00-08:00", "where": "weather-fetch",
                               "message": "can't reach api.open-meteo.com"}
    assert s["data"]["tides_fetched_at"] == "2026-11-04T01:00:00-08:00"
    assert frame.panel.refreshes == 1


def test_tides_data_in_status_from_the_cache(frame):
    shutil.copy(FIXTURES / "tides.json", frame.dir / "tides.json")
    frame.tick("2026-11-04T09:20")
    data = frame.status["data"]
    assert data["tides_fetched_at"] == "2026-11-03T23:09:58-08:00"
    assert data["tides_until"] == "2026-11-08"


def test_schedule_failure_is_reported(frame):
    def no_sun(day):
        raise FileNotFoundError("de421.bsp missing")

    assert tick.run(t("2026-11-04T09:20"), frame.panel, fetch=False, sun=no_sun,
                    wall=lambda: t("2026-11-04T09:20")) == 1
    assert frame.status["last_error"]["where"] == "schedule"


def test_a_second_tick_waits_for_the_first(frame, monkeypatch):
    monkeypatch.setattr(tick, "TICK_LOCK_TIMEOUT_S", 0.1)
    with state.locked(frame.dir / tick.TICK_LOCK):
        assert frame.tick("2026-11-04T09:20") == 0
    assert frame.panel.refreshes == 0


def test_flip_turns_the_panel_image_around(frame):
    frame.tick("2026-11-04T09:20")  # flip is on by default: the panel draws upside down
    codes = frame.panel.last_codes()
    assert codes[479, 799] == CODES[3] and codes[0, 0] == CODES[4]
    # current.png is the picture as the viewer sees it, not turned around
    current = Image.open(frame.dir / state.CURRENT_PNG).convert("RGB")
    assert current.getpixel((0, 0)) == tuple(palette_colors("estimated")[3])
    frame.configure(flip=False)
    frame.tick("2026-11-04T10:00")
    assert frame.panel.last_codes()[0, 0] == CODES[3]


# --- the real thing, through the CLI -------------------------------------------------------


def test_cli_tick_simulates_a_moment(ephemeris, tmp_path):
    state.state_dir().mkdir(parents=True)
    for name in ("tides.json", "weather.json", "surf.json"):
        shutil.copy(FIXTURES / name, state.state_dir() / name)
    c = photo_then_tides()
    save_config(replace(c, photos=replace(c.photos, folder=str(tmp_path / "none"))))
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T09:20"]) == 0
    s = state.read_json(state.state_dir() / state.STATUS)
    assert s["showing"]["key"] == "tides/2026-11-04T09:00"  # no photos: hourly on the hour
    assert s["showing"]["drawn_for"] == "2026-11-04T09:20:00-08:00"
    assert Image.open(state.state_dir() / state.CURRENT_PNG).size == (800, 480)
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T09:22"]) == 0
    assert state.read_json(state.state_dir() / state.STATUS)["showing"]["drawn_for"] == "2026-11-04T09:20:00-08:00"  # same key
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T10:14"]) == 0
    assert state.read_json(state.state_dir() / state.STATUS)["showing"]["key"] == "tides/2026-11-04T10:00"


def test_cli_tick_at_changes_screen_seconds_after_the_last(ephemeris, tmp_path):
    save_config(replace(default_config(), photos=replace(default_config().photos, folder=str(tmp_path / "none"))))
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T16:56:20"]) == 0  # sunset 16:56:29
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T16:56:40"]) == 0  # just after it
    assert state.read_json(state.state_dir() / state.STATUS)["showing"]["key"] == "sky/2026-11-04T16:56"


# --- FakePanel -------------------------------------------------------------------------------


def test_fake_panel_keeps_what_it_was_sent(tmp_path):
    panel = FakePanel(tmp_path)
    panel.show(np.full((480, 800), CODES[1], np.uint8))
    panel.clear()
    assert panel.refreshes == 2 and (panel.last_codes() == CODES[1]).all()


def test_fake_panel_lock_is_the_panel_lock(tmp_path):
    with PanelDisplay(state_dir=tmp_path)._lock():
        with pytest.raises(RuntimeError, match="another photopainter process"):
            FakePanel(tmp_path).show(np.ones((480, 800), np.uint8))


# --- the photo scene ---------------------------------------------------------------------------


def _config(**photos):
    c = default_config()
    folder = Path(state.state_dir()).parent / "photos"
    return replace(c, photos=replace(c.photos, folder=str(folder), **photos))


def test_photo_scene_renders_named_and_rotation(frame):
    ctx = SceneContext.from_config(_config())
    named = photo_scene.render(Scene("photo", "photo/x", t("2026-11-04T10:00"), photo="b.jpg"), ctx)
    assert named.shape == (480, 800) and named.dtype == np.uint8
    with pytest.raises(photo_scene.NoPhoto, match="isn't in"):
        photo_scene.render(Scene("photo", "photo/x", t("2026-11-04T10:00"), photo="nope.jpg"), ctx)
    with pytest.raises(photo_scene.NoPhoto):
        photo_scene.render(Scene("photo", "photo/x", t("2026-11-04T10:00"), photo="../a.jpg"), ctx)
    photo_scene.record_shown(ctx, "a.jpg")
    names = [p.name for p in photo_scene.candidates(Scene("photo", "k", t("2026-11-04T10:00"), photo="c.jpg"), ctx)]
    assert names == ["c.jpg", "b.jpg", "a.jpg"]
    hidden = SceneContext.from_config(_config(hidden=("b.jpg",)))
    assert [p.name for p in photo_scene.rotation(hidden)] == ["c.jpg", "a.jpg"]


def test_photo_scene_portrait_canvas(frame):
    ctx = SceneContext.from_config(replace(_config(), display=replace(default_config().display, portrait=True)))
    assert photo_scene.render(Scene("photo", "k", t("2026-11-04T10:00")), ctx).shape == (800, 480)


def test_photo_scene_with_no_photos(tmp_path):
    c = default_config()
    config = replace(c, photos=replace(c.photos, folder=str(tmp_path / "missing")))
    assert not photo_scene.available(config)
    with pytest.raises(photo_scene.NoPhoto, match="no photos"):
        photo_scene.render(Scene("photo", "k", t("2026-11-04T10:00")), SceneContext.from_config(config))


def test_cli_scene_photo(frame, tmp_path):
    out = tmp_path / "p.png"
    assert cli.main(["scene", "photo", "--photo", "c.jpg", "-o", str(out)]) == 0
    assert Image.open(out).size == (800, 480)


# --- the manual tools follow the config's flip ------------------------------------------------


def test_show_and_next_follow_the_configs_flip():
    p = cli.build_parser()
    assert cli._panel_options(p.parse_args(["show", "x.jpg"])).flip is True  # the default config
    assert cli._panel_options(p.parse_args(["show", "x.jpg", "--no-flip"])).flip is False
    save_config(replace(default_config(), display=replace(default_config().display, flip=False)))
    assert cli._panel_options(p.parse_args(["next"])).flip is False
    assert cli._panel_options(p.parse_args(["next", "--flip"])).flip is True
    assert cli._options(p.parse_args(["preview", "x.jpg"])).flip is False  # previews aren't turned


def test_status_is_valid_json_with_offsets(frame):
    frame.tick("2026-11-04T09:20")
    text = (frame.dir / state.STATUS).read_text()
    data = json.loads(text)
    for stamp in (data["updated_at"], data["showing"]["refreshed_at"], data["next_change"]["at"]):
        assert state.from_iso(stamp).utcoffset() is not None


def test_sun_times_are_cached_so_a_quiet_tick_skips_skyfield(ephemeris, frame, monkeypatch):
    import photopainter.astro.sun as sun_mod

    calls = []
    real = sun_mod.sun_events
    monkeypatch.setattr(sun_mod, "sun_events", lambda *a: calls.append(a[0]) or real(*a))
    tick.run(t("2026-11-04T09:20"), frame.panel, fetch=False, wall=lambda: t("2026-11-04T09:20"))
    assert calls == [date(2026, 11, 4)]
    cached = state.read_json(frame.dir / tick.SUN_CACHE)
    assert cached["days"]["2026-11-04"]["sunset"] == "2026-11-04T16:56:29-08:00"
    calls.clear()
    tick.run(t("2026-11-04T09:21"), frame.panel, fetch=False, wall=lambda: t("2026-11-04T09:21"))
    assert calls == []  # read from sun.json
    first = tick._sun_times(default_config(), frame.dir)(date(2026, 11, 4))
    assert first == sun_mod.SunEvents(*(getattr(real(date(2026, 11, 4), 33.593, -117.882, LA), k)
                                        .replace(microsecond=0) for k in tick.SUN_FIELDS))
    assert first.sunrise.tzinfo == LA
    # another place: worked out again
    moved = replace(default_config(), location=replace(default_config().location, lat=34.0))
    tick._sun_times(moved, frame.dir)(date(2026, 11, 4))
    assert calls == [date(2026, 11, 4)]


# --- no Wi-Fi ------------------------------------------------------------------------------


@pytest.fixture
def radio(frame):
    net = hotspot.FakeNetwork(frame.dir)
    state.write_json(net.path, {"connected": True})
    return net


def set_wifi(radio, connected: bool) -> None:
    state.write_json(radio.path, {**state.read_json(radio.path, {}), "connected": connected})


def on_the_wall(frame) -> np.ndarray:
    """The inks of current.png, near enough: whether the mark's red is in its corner."""
    img = np.asarray(Image.open(frame.dir / state.CURRENT_PNG).convert("RGB"))
    return img[448:472, 768:792]


def test_no_network_object_means_no_wifi_status(frame):
    frame.tick("2026-11-04T09:20")
    assert frame.status["wifi"] is None


def test_wifi_up_is_recorded_with_no_mark(frame, radio):
    frame.tick("2026-11-04T09:20", network=radio)
    w = frame.status["wifi"]
    assert w == {"connected": True, "down_since": None, "mark": False, "hotspot": False}
    assert frame.status["showing"]["no_wifi"] is False


def test_the_mark_comes_after_ten_minutes_at_the_next_redraw_with_no_refresh_of_its_own(frame, radio):
    frame.tick("2026-11-04T09:20", network=radio)
    refreshes = frame.panel.refreshes
    set_wifi(radio, False)
    frame.tick("2026-11-04T09:21", network=radio)
    assert frame.status["wifi"]["down_since"] is not None and frame.status["wifi"]["mark"] is False
    frame.tick("2026-11-04T09:35", network=radio)
    assert frame.status["wifi"]["mark"] is True
    assert frame.panel.refreshes == refreshes and frame.status["showing"]["no_wifi"] is False
    frame.tick("2026-11-04T10:10", network=radio)  # the schedule's next screen
    assert frame.panel.refreshes > refreshes and frame.status["showing"]["no_wifi"] is True
    assert frame.status["last_error"] is None


def test_a_short_blip_never_shows(frame, radio):
    frame.tick("2026-11-04T09:20", network=radio)
    set_wifi(radio, False)
    frame.tick("2026-11-04T09:21", network=radio)
    set_wifi(radio, True)
    frame.tick("2026-11-04T09:25", network=radio)
    assert frame.status["wifi"]["down_since"] is None
    set_wifi(radio, False)
    frame.tick("2026-11-04T09:26", network=radio)
    assert frame.status["wifi"]["down_since"].startswith("2026-11-04T09:26")


def test_the_mark_goes_at_the_next_redraw_once_wifi_is_back(frame, radio):
    set_wifi(radio, False)
    frame.tick("2026-11-04T09:00", network=radio)
    frame.tick("2026-11-04T09:15", network=radio)
    frame.tick("2026-11-04T10:10", network=radio)
    assert frame.status["showing"]["no_wifi"] is True
    set_wifi(radio, True)
    frame.tick("2026-11-04T10:11", network=radio)
    assert frame.status["wifi"]["mark"] is False and frame.status["showing"]["no_wifi"] is True  # left up
    frame.tick("2026-11-04T11:10", network=radio)
    assert frame.status["showing"]["no_wifi"] is False


def test_holding_back_with_wifi_working_clears_a_mark_left_up(frame, radio):
    set_wifi(radio, False)
    frame.tick("2026-11-04T09:00", network=radio)
    frame.tick("2026-11-04T09:15", network=radio)
    frame.tick("2026-11-04T10:10", network=radio)
    set_wifi(radio, True)
    refreshes = frame.panel.refreshes
    hotspot.ask(frame.dir, t("2026-11-04T10:11"))
    frame.tick("2026-11-04T10:11", network=radio)
    assert frame.panel.refreshes == refreshes + 1 and frame.status["showing"]["no_wifi"] is False
    assert frame.status["wifi"]["hotspot"] is False


def test_holding_back_with_wifi_working_and_no_mark_changes_nothing(frame, radio):
    frame.tick("2026-11-04T09:20", network=radio)
    refreshes = frame.panel.refreshes
    hotspot.ask(frame.dir, t("2026-11-04T09:21"))
    frame.tick("2026-11-04T09:21", network=radio)
    assert frame.panel.refreshes == refreshes


def test_low_power_counts_as_wifi_off_on_purpose(frame, radio, monkeypatch):
    from photopainter.config import edit_config

    edit_config(lambda c: replace(c, battery=replace(c.battery, low_power=True, low_power_after_minutes=0)))
    state.write_json(frame.dir / "fake_battery.json", {"volts": 3.9, "milliamps": -200})
    set_wifi(radio, False)
    battery = FakeBattery(frame.dir)
    for at in ("2026-11-04T09:00", "2026-11-04T09:01", "2026-11-04T09:30"):
        frame.tick(at, network=radio, battery=battery)
    w = frame.status["wifi"]
    assert frame.status["power"]["mode"] == "low"
    assert w["connected"] is None and w["mark"] is False and w["down_since"] is None
    hotspot.ask(frame.dir, t("2026-11-04T09:31"))
    frame.tick("2026-11-04T09:31", network=radio, battery=battery)
    assert frame.status["wifi"]["hotspot"] is False


def test_the_setup_screen_goes_up_in_place_of_the_schedule_and_comes_down(frame, radio):
    frame.tick("2026-11-04T09:20", network=radio)
    set_wifi(radio, False)
    hotspot.ask(frame.dir, t("2026-11-04T09:21"))
    frame.tick("2026-11-04T09:21", network=radio)
    s = frame.status
    assert s["showing"]["kind"] == "setup" and s["wifi"]["hotspot"] is True and s["showing"]["no_wifi"] is False
    assert "password" not in json.dumps(s)
    refreshes = frame.panel.refreshes
    frame.tick("2026-11-04T09:22", network=radio)
    assert frame.panel.refreshes == refreshes  # stays up, with no refresh
    hotspot.cancel(frame.dir, t("2026-11-04T09:23"))
    frame.tick("2026-11-04T09:23", network=radio)
    assert frame.status["showing"]["kind"] != "setup" and frame.status["wifi"]["hotspot"] is False


def test_setup_works_with_the_clock_not_set(frame, radio):
    set_wifi(radio, False)
    hotspot.ask(frame.dir, t("2026-11-04T09:21"))
    frame.tick("2026-11-04T09:21", network=radio, clock_ok=False, uptime_s=60)
    assert frame.status["showing"]["kind"] == "setup"


def test_a_setup_screen_doesnt_stay_up_through_quiet_hours(frame, radio):
    frame.configure(quiet_hours=QuietHours(time(22, 0), time(7, 0)))
    set_wifi(radio, False)
    hotspot.ask(frame.dir, t("2026-11-04T23:00"))
    frame.tick("2026-11-04T23:00", network=radio)
    assert frame.status["showing"]["kind"] == "setup"
    hotspot.cancel(frame.dir, t("2026-11-04T23:01"))
    frame.tick("2026-11-04T23:01", network=radio)
    assert frame.status["showing"]["kind"] != "setup"


def test_a_hotspot_that_goes_wrong_doesnt_stop_the_schedule(frame, radio, monkeypatch):
    monkeypatch.setattr(hotspot, "advance", lambda *a, **k: 1 / 0)
    assert frame.tick("2026-11-04T09:20", network=radio) == 0
    assert frame.status["showing"] is not None and frame.status["last_error"]["where"] == "wifi"
