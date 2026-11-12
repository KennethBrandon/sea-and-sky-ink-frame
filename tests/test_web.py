"""The dashboard (web/): every endpoint through Flask's test client, plus the tick runner,
the renderer, uploads and health readings. No network, no panel, no real ticks (except
where a test starts a tiny child process to check the runner itself)."""

import io
import json
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from photopainter import cli, state
from photopainter.astro.sun import SunEvents
from photopainter.config import Step, default_config, load_config, save_config
from photopainter.scenes import photo as photo_scene
from photopainter.scenes.base import SceneContext
from photopainter.web import frame_name, health, jobs, photos, server
from photopainter.web.jobs import RenderError, Renderer, TickRunner

LA = ZoneInfo("America/Los_Angeles")
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 4, 10, 20, 5, tzinfo=LA)


def t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


def SUN(day: date) -> SunEvents:
    rise = datetime(day.year, day.month, day.day, 6, 12, tzinfo=LA)
    set_ = datetime(day.year, day.month, day.day, 16, 57, tzinfo=LA)
    return SunEvents(rise, set_, rise - timedelta(minutes=25), set_ + timedelta(minutes=25))


class FakeTicks:
    def __init__(self):
        self.requests = 0
        self.running = False
        self.last_error = None

    def request(self):
        self.requests += 1

    def snapshot(self):
        return {"running": False, "queued": False}

    def close(self, wait_s=0):
        return True


class FakeRender:
    """Stands in for `photopainter scene … -o out`: writes a small PNG, records the args."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail: str | None = None

    def __call__(self, args, out: Path):
        self.calls.append(list(args))
        if self.fail:
            raise RenderError(self.fail)
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 480), (200, 200, 200)).save(out, "PNG")


def jpeg(size=(160, 96), color=(40, 120, 200), **save) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", **save)
    return buf.getvalue()


class Site:
    """A frame in a temp folder: config, photos, state, and the app with fakes."""

    def __init__(self, tmp_path: Path, photo_names=("a.jpg", "b.jpg", "c.jpg"), fake_panel=True, **photo_cfg):
        self.dir = state.state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.photos = tmp_path / "photos"
        self.photos.mkdir()
        for i, name in enumerate(photo_names):
            (self.photos / name).write_bytes(jpeg(color=(40 * i, 120, 200)))
        self.configure(**photo_cfg)
        self.now = NOW
        self.ticks = FakeTicks()
        self.render = FakeRender()
        self.app = server.create_app(fake_panel=fake_panel, clock=lambda: self.now, ticks=self.ticks,
                                     render=self.render, sun=SUN)
        self.client = self.app.test_client()

    def configure(self, **photo_cfg):
        # The schedule these tests are written against: a photo on the hour, then tides; the
        # sky at night (the default until 2026-11-08).
        c = default_config()
        schedule = replace(c.schedule, day=(Step("photo", 10), Step("tides", 50)), night=(Step("sky", 60),))
        save_config(replace(c, schedule=schedule, photos=replace(c.photos, folder=str(self.photos), **photo_cfg)))

    def overrides(self) -> dict:
        return json.loads((self.dir / state.OVERRIDES).read_text())

    def write_overrides(self, data: dict):
        state.write_json(self.dir / state.OVERRIDES, data)

    def write_status(self, **fields):
        base = {"updated_at": state.to_iso(self.now), "showing": None, "refreshing": None,
                "next_change": None, "last_error": None,
                "data": {"tides_fetched_at": None, "tides_until": None, "config_error": None, "clock_ok": True}}
        base.update(fields)
        state.write_json(self.dir / state.STATUS, base)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, **kw):
        return self.client.post(path, **kw)


@pytest.fixture
def site(tmp_path):
    health.clear_kept()
    return Site(tmp_path)


# --- the page ------------------------------------------------------------------------------


def test_index_and_static_files(site):
    r = site.get("/")
    assert r.status_code == 200 and b"<html" in r.data
    for name, kind in (("app.js", "javascript"), ("style.css", "css")):
        r = site.get(f"/static/{name}")
        assert r.status_code == 200 and kind in r.content_type


def test_the_schedule_card_tops_the_right_column(site):
    page = site.get("/").data.decode()
    assert page.index('<main class="right">') < page.index('id="schedule-card"') < page.index('id="photos-card"')


def test_the_battery_is_one_box_with_its_own_save(site):
    page = site.get("/").data.decode()
    assert page.index('id="photos-card"') < page.index('id="battery-card"') < page.index('id="settings-card"')
    box = page[page.index('id="battery-card"'):page.index('id="settings-card"')]
    for part in ('data-sum="battery"', 'data-dirty="battery"', 'id="battery-now"', 'id="battery-chart"',
                 'id="battery-summary"', 'id="battery" novalidate', 'id="battery-bar"', 'id="battery-cancel"', 'id="battery-save"'):
        assert part in box, part
    assert 'id="battery-card" data-box="battery"' in page and " hidden>\n    <h2 id=\"battery-title\"" not in page  # always there
    js = site.get("/static/app.js").data.decode()
    assert 'const BOXES = ["tides", "sky", "harbor", "photos", "battery"]' in js  # so not in the Frame box
    assert "batteryRow(st.battery)," not in js[js.index("function renderHealth"):]  # nor in Health's rows


def test_frame_and_health_close_to_one_line_like_the_other_boxes(site):
    page = site.get("/").data.decode()
    assert '<details class="card box" id="settings-card" data-box="settings">' in page
    assert '<details class="card box" id="health-card" data-box="health">' in page
    assert 'data-dirty="settings"' in page  # a closed box still says it has unsaved changes
    assert 'data-sum="health"' in page
    js = site.get("/static/app.js").data.decode()
    assert '["#health-dot", "#health-card"]' in js  # the dot opens it, or it would scroll to a closed line


REQUIRED_CREDITS = ("Open-Meteo.com", "ESO/S. Brunier", "© OpenStreetMap contributors", "HYG database")


def test_about_is_the_last_box_with_the_copyright_and_every_credit(site):
    page = site.get("/").data.decode()  # data/README.md: the credits the licenses require
    start = page.index('<details class="card box" id="about-card" data-box="about">')
    # The bottom of the page: Frame, Health, Help, then About (Owner, 2026-11-07).
    order = [page.index(f'id="{box}-card"') for box in ("battery", "settings", "health", "help", "about")]
    assert order == sorted(order)
    about = page[start:page.index("</main>")]
    assert about.count("<details") == 1 and about.rstrip().endswith("</details>")  # nothing after it
    assert "© Kenneth Brandon" in about[:about.index("</summary>")]  # said with the box closed too
    assert "© 2026 Kenneth Brandon" in about
    for credit in REQUIRED_CREDITS:
        assert credit in about, credit
    for license in ("CC BY 4.0", "CC BY-SA 4.0", "ODbL", "public domain", "MIT", "Bitstream Vera"):
        assert license in about, license
    assert "Los Angeles gauge" in about and "feet below MLLW" in about and "Not for navigation" in about
    # The fine print that sat under the boxes is all in here now, and the Frame box is settings only.
    assert 'class="credit"' not in page
    assert 'id="about-tool"' not in page
    assert 'id="icon-about"' in page


def help_box(site) -> str:
    page = site.get("/").data.decode()
    return page[page.index('id="help-card"'):page.index('id="about-card"')]


def test_help_says_what_the_buttons_do(site):
    box = help_box(site)
    assert 'data-box="help"' in box and 'id="help-open"' in site.get("/").data.decode()
    for words in ("On the schedule", "In photos only", "held for a second", "about 30 seconds", "flickers",
                  "is ignored", "tap Auto", "hold both buttons", "plug the frame in", "crescent"):
        assert words in box, words
    assert '["#help-open", "#help-card"]' in site.get("/static/app.js").data.decode()


def test_help_says_what_to_do_without_wifi(site):
    box = help_box(site)
    for words in ("Back held for a second", "No Wi-Fi", "reconnects by itself", "a new router", "QR code",
                  "tap Join", "watch the frame", "Any button calls it off", "after 15 minutes", 'class="mark-art"',
                  "won't open while the frame has no Wi-Fi"):
        assert words in box, words


def test_help_draws_the_buttons_to_scale_where_they_are(site):
    box = help_box(site)
    assert 'viewBox="0 -19 216 172"' in box  # the frame in mm, 216 x 153, with room above it for the labels
    assert '<circle cx="45.500" cy="25.500" r="3"' in box and '<circle cx="60.500" cy="25.500" r="3"' in box  # 6 mm, 15 mm apart
    assert box.index('y="-8" text-anchor="end">Forward</text>') < box.index('y="-8">Back</text>')  # above the frame
    drawing = box[box.index("</title>"):box.index("</svg>")]
    assert "TOP" not in drawing and "of the frame" not in drawing  # only what's really printed on it
    assert "<figcaption>The back of the frame, as it hangs</figcaption>" in box
    assert "a quarter of the way in from the right" in box  # facing the picture: from the left, seen from behind


def test_help_is_words_and_a_drawing_with_nothing_to_press(site):
    box = help_box(site)
    assert "<button" not in box and "<input" not in box and "<form" not in box


def test_the_schedule_rows_have_their_reorder_icon(site):
    assert 'id="icon-grip"' in site.get("/").data.decode()
    assert 'icon("grip")' in site.get("/static/app.js").data.decode()


def test_unknown_api_paths_answer_json(site):
    r = site.get("/api/nope")
    assert r.status_code == 404 and "error" in r.get_json()
    r = site.client.delete("/api/status")
    assert r.status_code == 405 and "error" in r.get_json()


# --- status --------------------------------------------------------------------------------


def test_status_before_any_tick(site):
    body = site.get("/api/status").get_json()
    for key in server.STATUS_KEYS:
        assert body[key] is None
    assert body["mode"] == {"kind": "auto", "photo": None, "until": None, "set_at": None}
    assert body["image"] is None
    assert body["timezone"] == "America/Los_Angeles" and body["location"] == "The Wedge"
    assert body["server_time"] == "2026-11-04T10:20:05-08:00"
    assert body["day_night"]["is_day"] is True
    assert body["day_night"]["next"] == "sunset"
    assert body["day_night"]["next_at"] == "2026-11-04T16:57:00-08:00"
    assert body["ticks"] == {"running": False, "queued": False}
    assert set(body["health"]) >= {"uptime_s", "cpu_temp_c", "wifi", "network", "tailscale", "clock", "memory",
                                  "disk", "power"}
    assert set(body["sources"]) == {"tides", "weather", "surf", "gauge"}
    assert body["fake_panel"] is True
    assert body["screens"]["tides"] is True


def test_status_passes_status_json_through_and_adds_the_pin(site):
    showing = {"key": "tides/2026-11-04T10:10", "kind": "tides", "photo": None,
               "drawn_for": "2026-11-04T10:10:00-08:00", "refreshed_at": "2026-11-04T10:10:31-08:00"}
    refreshing = {"key": "sky/2026-11-04T10:19", "started_at": "2026-11-04T10:19:00-08:00",
                  "panel_at": "2026-11-04T10:19:04-08:00"}
    site.write_status(showing=showing, refreshing=refreshing, next_change={"at": "2026-11-04T11:00:00-08:00",
                                                                            "kind": "photo"})
    site.write_overrides({"pin": {"kind": "sky", "photo": None, "until": "2026-11-04T16:57:00-08:00",
                                  "set_at": "2026-11-04T10:19:00-08:00"}, "up_next": ["b.jpg"]})
    body = site.get("/api/status").get_json()
    assert body["showing"] == showing and body["refreshing"] == refreshing
    assert body["next_change"]["kind"] == "photo"
    assert body["mode"]["kind"] == "sky" and body["mode"]["until"] == "2026-11-04T16:57:00-08:00"
    assert body["overrides"]["up_next"] == ["b.jpg"]


def test_status_drops_an_expired_pin(site):
    site.write_overrides({"pin": {"kind": "sky", "photo": None, "until": "2026-11-04T09:00:00-08:00",
                                  "set_at": "2026-11-04T08:00:00-08:00"}, "up_next": []})
    assert site.get("/api/status").get_json()["mode"]["kind"] == "auto"


def test_status_reports_problems(site, tmp_path):
    state.config_path().write_text("{not json")
    site.write_status(last_error={"at": state.to_iso(NOW - timedelta(minutes=5)), "where": "render",
                                  "message": "tides/2026-11-04T10:10: boom"})
    body = site.get("/api/status").get_json()
    texts = [p["text"] for p in body["problems"]]
    assert body["config_error"] and "not valid JSON" in body["config_error"]
    assert any("settings file has a problem" in x for x in texts)
    assert any("render: tides/2026-11-04T10:10: boom" in x for x in texts)
    assert body["problems"][0]["level"] == "error"  # worst first


def test_status_says_when_the_timer_has_stopped(tmp_path):
    health.clear_kept()
    s = Site(tmp_path, fake_panel=False)
    s.write_status(updated_at=state.to_iso(NOW - timedelta(minutes=12)))
    texts = [p["text"] for p in s.get("/api/status").get_json()["problems"]]
    assert any("hasn't run for 12 minutes" in x for x in texts)


def test_status_image_info(site):
    Image.new("RGB", (800, 480), "white").save(site.dir / state.CURRENT_PNG)
    image = site.get("/api/status").get_json()["image"]
    assert image["width"] == 800 and image["height"] == 480 and image["version"]


def test_current_png(site):
    r = site.get("/api/current.png")
    assert r.status_code == 404 and r.get_json()["error"]
    Image.new("RGB", (800, 480), "white").save(site.dir / state.CURRENT_PNG)
    r = site.get("/api/current.png")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert Image.open(io.BytesIO(r.data)).size == (800, 480)
    again = site.get("/api/current.png", headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


def test_incoming_png(site):
    r = site.get("/api/incoming.png")
    assert r.status_code == 404 and r.get_json()["error"]
    Image.new("RGB", (800, 480), "white").save(site.dir / state.INCOMING_PNG)
    r = site.get("/api/incoming.png")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert Image.open(io.BytesIO(r.data)).size == (800, 480)


# --- config --------------------------------------------------------------------------------


def test_get_config(site):
    body = site.get("/api/config").get_json()
    assert body["photos"]["folder"] == str(site.photos)
    assert body == load_config()[0].to_dict()


def test_put_config_saves_and_starts_a_tick(site):
    data = site.get("/api/config").get_json()
    data["photos"]["show_now_minutes"] = 15
    r = site.client.put("/api/config", json=data)
    assert r.status_code == 200 and r.get_json()["message"]
    assert load_config()[0].photos.show_now_minutes == 15
    assert site.ticks.requests == 1


def test_put_config_keeps_a_rotation_in_the_order_given(site):
    data = site.get("/api/config").get_json()
    data["schedule"]["day"] = [{"screen": "tides", "minutes": 50}, {"screen": "photo", "minutes": 10}]
    assert site.client.put("/api/config", json=data).status_code == 200
    assert site.get("/api/config").get_json()["schedule"]["day"] == data["schedule"]["day"]


def test_put_config_takes_the_tide_chart_settings(site):
    data = site.get("/api/config").get_json()
    assert data["tides"]["chart_days_behind"] == 1 and data["tides"]["label_heights"] == "extremes"
    data["tides"].update(chart_days_behind=0.5, chart_days_ahead=7, label_times=False, label_heights="all",
                         flip_arrows=False, label_tides="daily")
    r = site.client.put("/api/config", json=data)
    assert r.status_code == 200
    saved = site.get("/api/config").get_json()["tides"]
    assert (saved["chart_days_behind"], saved["chart_days_ahead"]) == (0.5, 7)
    assert (saved["label_times"], saved["label_heights"], saved["flip_arrows"]) == (False, "all", False)
    assert saved["label_tides"] == "daily"


def test_put_bad_config_is_rejected_with_every_problem(site):
    before = state.config_path().read_text()
    data = site.get("/api/config").get_json()
    data["schedule"]["day"][0]["minutes"] = 0
    data["location"]["lat"] = 123
    r = site.client.put("/api/config", json=data)
    assert r.status_code == 400
    body = r.get_json()
    assert "schedule.day[0].minutes" in body["error"]
    assert len(body["errors"]) == 2 and any(e.startswith("location.lat") for e in body["errors"])
    assert state.config_path().read_text() == before
    assert site.ticks.requests == 0


@pytest.mark.parametrize("body", ["[]", "not json", '"text"'])
def test_put_config_needs_a_json_object(site, body):
    r = site.client.put("/api/config", data=body, content_type="application/json")
    assert r.status_code == 400 and "JSON object" in r.get_json()["error"]


# --- mode ----------------------------------------------------------------------------------


def test_mode_pin_until_the_next_change_by_default(site):
    r = site.post("/api/mode", json={"kind": "tides"})
    assert r.status_code == 200
    pin = site.overrides()["pin"]
    assert pin == {"kind": "tides", "photo": None, "until": "2026-11-04T16:57:00-08:00",
                   "set_at": "2026-11-04T10:20:05-08:00"}
    assert r.get_json()["message"] == "Day screen until 4:57 pm"
    assert site.ticks.requests == 1


def test_the_combo_screen_can_be_pinned(site):
    r = site.post("/api/mode", json={"kind": "combo"})
    assert r.status_code == 200
    assert site.overrides()["pin"]["kind"] == "combo"
    assert r.get_json()["message"] == "Combo until 4:57 pm"


@pytest.mark.parametrize("until, expect", [
    ("1h", "2026-11-04T11:20:05-08:00"),
    ("90m", "2026-11-04T11:50:05-08:00"),
    (None, None),
    ("2026-11-04T13:00:00-08:00", "2026-11-04T13:00:00-08:00"),
])
def test_mode_until_choices(site, until, expect):
    r = site.post("/api/mode", json={"kind": "sky", "until": until})
    assert r.status_code == 200
    assert site.overrides()["pin"]["until"] == expect


def test_mode_auto_clears_a_rotation_a_button_moved_and_a_pin_keeps_it(site):
    moved = {"anchor": "2026-11-04T09:20:00-08:00", "step": 1, "until": "2030-01-01T00:00:00-08:00"}
    path = state.state_dir() / state.OVERRIDES
    state.write_json(path, {"pin": None, "up_next": [], "redraw_at": None, "cycle": moved})
    assert site.post("/api/mode", json={"kind": "tides", "until": "1h"}).status_code == 200
    assert state.read_json(path)["cycle"]["step"] == 1
    assert site.post("/api/mode", json={"kind": "auto"}).status_code == 200
    assert "cycle" not in state.read_json(path)


def test_mode_auto_clears_the_pin_and_keeps_up_next(site):
    site.write_overrides({"pin": {"kind": "sky", "photo": None, "until": None, "set_at": None},
                          "up_next": ["a.jpg"]})
    r = site.post("/api/mode", json={"kind": "auto"})
    assert r.status_code == 200 and r.get_json()["mode"]["kind"] == "auto"
    assert site.overrides() == {"pin": None, "up_next": ["a.jpg"], "redraw_at": None}


@pytest.mark.parametrize("body, words", [
    ({"kind": "party"}, "kind must be one of"),
    ({}, "kind must be one of"),
    ({"kind": "tides", "until": "2026-11-04T09:00:00-08:00"}, "in the past"),
    ({"kind": "tides", "until": "tomorrow"}, "expected null"),
    ({"kind": "tides", "until": "2026-11-04T13:00"}, "expected null"),  # no UTC offset
])
def test_mode_rejects_bad_requests(site, body, words):
    r = site.post("/api/mode", json=body)
    assert r.status_code == 400 and words in r.get_json()["error"]
    assert not (site.dir / state.OVERRIDES).exists()
    assert site.ticks.requests == 0


def test_mode_needs_json(site):
    r = site.post("/api/mode", data="kind=tides")
    assert r.status_code == 400


# --- photos: list and previews ---------------------------------------------------------------


def test_photo_list(site):
    site.configure(hidden=("c.jpg",))
    site.write_overrides({"pin": None, "up_next": ["b.jpg", "a.jpg"]})
    site.write_status(showing={"key": "photo/2026-11-04T10:00", "kind": "photo", "photo": "a.jpg",
                               "drawn_for": "2026-11-04T10:00:00-08:00", "refreshed_at": "2026-11-04T10:00:30-08:00"})
    items = site.get("/api/photos").get_json()
    assert [p["name"] for p in items] == ["a.jpg", "b.jpg", "c.jpg"]
    a, b, c = items
    assert a["up_next_position"] == 2 and b["up_next_position"] == 1 and c["up_next_position"] is None
    assert c["hidden"] and not a["hidden"]
    assert a["showing"] and not b["showing"]
    assert b["next"] and not a["next"] and not c["next"]
    assert a["size"] == (site.photos / "a.jpg").stat().st_size
    assert a["preview_url"].startswith("/api/photos/a.jpg/preview.png?v=")
    assert a["modified"].endswith("-08:00")


def test_photo_list_next_follows_the_rotation(site):
    (site.dir / state.SLIDESHOW).write_text(json.dumps({str(site.photos.resolve()): "a.jpg"}))
    items = {p["name"]: p for p in site.get("/api/photos").get_json()}
    assert items["b.jpg"]["next"] and not items["a.jpg"]["next"]


def test_order_is_saved_and_listed(site):
    r = site.client.put("/api/photos/order", json={"order": ["c.jpg", "a.jpg", "b.jpg"]})
    assert r.status_code == 200 and r.get_json() == {"order": ["c.jpg", "a.jpg", "b.jpg"]}
    assert names_listed(site) == ["c.jpg", "a.jpg", "b.jpg"]
    assert site.ticks.requests == 0  # nothing on the wall changes


def test_order_decides_which_photo_is_next(site):
    (site.dir / state.SLIDESHOW).write_text(json.dumps({str(site.photos.resolve()): "c.jpg"}))
    items = {p["name"]: p for p in site.get("/api/photos").get_json()}
    assert items["a.jpg"]["next"]  # wraps round, by name
    site.client.put("/api/photos/order", json={"order": ["c.jpg", "b.jpg", "a.jpg"]})
    items = {p["name"]: p for p in site.get("/api/photos").get_json()}
    assert items["b.jpg"]["next"] and not items["a.jpg"]["next"]


def test_order_reaches_the_rotation_the_tick_uses(site):
    site.client.put("/api/photos/order", json={"order": ["c.jpg", "a.jpg", "b.jpg"]})
    ctx = SceneContext.from_config(load_config()[0], state_dir=site.dir)
    assert [p.name for p in photo_scene.rotation(ctx)] == ["c.jpg", "a.jpg", "b.jpg"]
    photo_scene.record_shown(ctx, "c.jpg")
    assert [p.name for p in photo_scene.rotation(ctx)] == ["a.jpg", "b.jpg", "c.jpg"]


def test_order_leaves_out_photos_that_have_gone_and_keeps_new_ones(site):
    (site.photos / "z.jpg").write_bytes(jpeg())  # arrived after the page loaded
    r = site.client.put("/api/photos/order", json={"order": ["gone.jpg", "b.jpg", "b.jpg", "a.jpg", "c.jpg"]})
    assert r.get_json()["order"] == ["b.jpg", "a.jpg", "c.jpg", "z.jpg"]
    assert json.loads((site.photos / ".order.json").read_text())["order"] == ["b.jpg", "a.jpg", "c.jpg"]


@pytest.mark.parametrize("body", [{}, {"order": "a.jpg"}, {"order": [1, 2]}, ["a.jpg"]])
def test_order_refuses_what_isnt_a_list_of_names(site, body):
    r = site.client.put("/api/photos/order", json=body)
    assert r.status_code == 400 and "error" in r.get_json()
    assert names_listed(site) == ["a.jpg", "b.jpg", "c.jpg"]


@pytest.mark.parametrize("name", ["../x.jpg", ".order.json", "a/b.jpg", ""])
def test_order_refuses_paths(site, name):
    r = site.client.put("/api/photos/order", json={"order": [name]})
    assert r.status_code == 400
    assert not (site.photos / ".order.json").exists()


def test_order_with_no_folder(tmp_path):
    s = Site(tmp_path)
    missing = tmp_path / "no-such-folder"
    save_config(replace(default_config(), photos=replace(default_config().photos, folder=str(missing))))
    r = s.client.put("/api/photos/order", json={"order": ["a.jpg"]})
    assert r.status_code == 200 and r.get_json() == {"order": []}
    assert not missing.exists()


def test_photo_list_with_no_folder(tmp_path):
    s = Site(tmp_path)
    s.configure()
    for p in s.photos.iterdir():
        p.unlink()
    s.photos.rmdir()
    assert s.get("/api/photos").get_json() == []


def test_photo_names_are_quoted_in_urls(tmp_path):
    s = Site(tmp_path, photo_names=("Eagle & Waterfall.jpg",))
    item = s.get("/api/photos").get_json()[0]
    assert item["preview_url"].startswith("/api/photos/Eagle%20%26%20Waterfall.jpg/preview.png")
    assert s.get(item["preview_url"]).status_code == 200


def test_preview_is_drawn_once_and_cached(site):
    url = site.get("/api/photos").get_json()[0]["preview_url"]
    r = site.get(url)
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert "immutable" in r.headers["Cache-Control"]
    assert site.render.calls == [["photo", "--photo", "a.jpg"]]
    assert site.get(url).status_code == 200
    assert len(site.render.calls) == 1
    assert len(list((site.dir / state.PHOTO_PREVIEWS).glob("*.png"))) == 1


def test_preview_is_redrawn_when_the_fit_changes(site):
    url = site.get("/api/photos").get_json()[0]["preview_url"]
    site.get(url)
    site.configure(fit="fit")
    url2 = site.get("/api/photos").get_json()[0]["preview_url"]
    assert url2 != url
    site.get(url2)
    assert len(site.render.calls) == 2
    assert len(list((site.dir / state.PHOTO_PREVIEWS).glob("*.png"))) == 1  # the old one is gone


def test_preview_without_the_version_revalidates(site):
    r = site.get("/api/photos/a.jpg/preview.png")
    assert r.status_code == 200 and "immutable" not in r.headers.get("Cache-Control", "")


@pytest.mark.parametrize("name, code", [("..", 400), (".hidden.jpg", 400), ("nope.jpg", 404),
                                        ("..%2Fconfig.json", 404), ("a%5Cb.jpg", 400)])
def test_bad_photo_names(site, name, code):
    r = site.get(f"/api/photos/{name}/preview.png")
    assert r.status_code == code
    assert r.get_json()["error"]
    assert site.render.calls == []


def test_preview_render_failure(site):
    site.render.fail = "cannot identify image file"
    r = site.get("/api/photos/a.jpg/preview.png")
    assert r.status_code == 422 and "cannot identify image file" in r.get_json()["error"]


def test_real_preview_render_in_a_child_process(site):
    """The default renderer: `photopainter scene photo` in a child process, as on the Pi."""
    (site.photos / "a.jpg").write_bytes(jpeg((400, 300)))
    out = site.dir / "x.png"
    Renderer()(["photo", "--photo", "a.jpg"], out)
    assert Image.open(out).size == (800, 480)
    with pytest.raises(RenderError, match="isn't in"):
        Renderer()(["photo", "--photo", "missing.jpg"], site.dir / "y.png")
    assert not (site.dir / "y.png").exists()


# --- photos: uploads -----------------------------------------------------------------------


def upload(site, data: bytes, filename: str, field="photo"):
    return site.post("/api/photos", data={field: (io.BytesIO(data), filename)},
                     content_type="multipart/form-data")


def test_upload_small_jpeg_is_kept_byte_for_byte(site):
    data = jpeg((1200, 900))
    r = upload(site, data, "beach.jpg")
    assert r.status_code == 201
    saved = r.get_json()["saved"][0]
    assert saved["name"] == "beach.jpg" and not saved["resized"] and not saved["duplicate"]
    assert (site.photos / "beach.jpg").read_bytes() == data
    assert (site.photos / "beach.jpg").stat().st_mode & 0o777 == 0o644
    assert site.ticks.requests == 1


def test_upload_large_photo_is_shrunk_and_turned_upright(site):
    img = Image.new("RGB", (3200, 2000), (10, 200, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: rotate 90° clockwise to view
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    r = upload(site, buf.getvalue(), "IMG_0001.HEIC")  # converted by the browser, old name kept
    assert r.status_code == 201
    saved = r.get_json()["saved"][0]
    assert saved["name"] == "IMG_0001.jpg" and saved["resized"]
    with Image.open(site.photos / "IMG_0001.jpg") as out:
        assert out.format == "JPEG"
        assert out.size == (1000, 1600)  # upright portrait, long side 1600


@pytest.mark.parametrize("fmt, mode, ext", [("PNG", "RGBA", ".png"), ("WEBP", "RGB", ".webp"),
                                            ("PNG", "P", ".png")])
def test_upload_png_and_webp(site, fmt, mode, ext):
    img = Image.new(mode, (2000, 1000))
    buf = io.BytesIO()
    img.save(buf, fmt)
    r = upload(site, buf.getvalue(), f"pic.{fmt.lower()}")
    assert r.status_code == 201
    name = r.get_json()["saved"][0]["name"]
    assert name == f"pic{ext}"
    with Image.open(site.photos / name) as out:
        assert out.format == fmt and max(out.size) == 1600


@pytest.mark.parametrize("data, filename, code, words", [
    (b"", "x.jpg", 400, "empty"),
    (b"this is not a photo", "x.jpg", 415, "JPEG, PNG or WebP"),
    (None, "x.gif", 415, "GIF files aren't supported"),
    ("cut", "cut.jpg", 415, "couldn't be read"),
])
def test_upload_refuses_what_the_frame_cant_show(site, data, filename, code, words):
    if data is None:
        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, "GIF")
        data = buf.getvalue()
    elif data == "cut":  # a JPEG whose end is missing (a dropped connection)
        whole = jpeg((400, 300), color=(10, 20, 30))
        data = whole[: len(whole) * 2 // 3]
    r = upload(site, data, filename)
    assert r.status_code == code and words in r.get_json()["error"]
    assert sorted(p.name for p in site.photos.iterdir()) == ["a.jpg", "b.jpg", "c.jpg"]
    assert site.ticks.requests == 0


def test_upload_over_the_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "MAX_UPLOAD_BYTES", 50_000)
    s = Site(tmp_path)
    r = upload(s, jpeg((1000, 1000), quality=100) + b"\0" * 60_000, "big.jpg")
    assert r.status_code == 413 and "MB" in r.get_json()["error"]
    big = b"x" * 2_000_000  # past MAX_CONTENT_LENGTH: refused before it's read
    r = upload(s, big, "huge.jpg")
    assert r.status_code == 413 and r.get_json()["error"]


def test_upload_names_are_cleaned(site):
    r = upload(site, jpeg(), "../../etc/pass wd?.jpg")
    assert r.get_json()["saved"][0]["name"] == "pass wd.jpg"
    r = upload(site, jpeg(color=(1, 2, 3)), ".hidden.JPEG")
    assert r.get_json()["saved"][0]["name"] == "hidden.jpg"
    r = upload(site, jpeg(color=(9, 9, 9)), "")
    assert r.get_json()["saved"][0]["name"] == "photo.jpg"


def test_upload_never_replaces_a_photo(site):
    data = (site.photos / "a.jpg").read_bytes()
    r = upload(site, data, "a.jpg")
    assert r.status_code == 200 and r.get_json()["saved"][0]["duplicate"]
    assert site.ticks.requests == 0
    r = upload(site, jpeg(color=(250, 0, 0)), "a.jpg")
    assert r.status_code == 201 and r.get_json()["saved"][0]["name"] == "a-2.jpg"
    assert (site.photos / "a.jpg").read_bytes() == data


def names_listed(site) -> list[str]:
    return [p["name"] for p in site.get("/api/photos").get_json()]


def test_upload_goes_at_the_end_of_the_order(site):
    upload(site, jpeg(color=(1, 2, 3)), "aaa.jpg")
    upload(site, jpeg(color=(4, 5, 6)), "bbb.jpg")
    assert names_listed(site) == ["a.jpg", "b.jpg", "c.jpg", "aaa.jpg", "bbb.jpg"]


def test_uploading_a_photo_that_is_already_there_leaves_the_order_alone(site):
    upload(site, (site.photos / "a.jpg").read_bytes(), "a.jpg")
    assert names_listed(site) == ["a.jpg", "b.jpg", "c.jpg"]


def test_uploading_a_deleted_photo_again_puts_it_at_the_end(site):
    site.client.put("/api/photos/order", json={"order": ["b.jpg", "a.jpg", "c.jpg"]})
    data = (site.photos / "b.jpg").read_bytes()
    site.client.delete("/api/photos/b.jpg")
    upload(site, data, "b.jpg")
    assert names_listed(site) == ["a.jpg", "c.jpg", "b.jpg"]


def test_upload_survives_an_order_that_cannot_be_written(site, monkeypatch):
    def refuse(folder, name):
        raise OSError("read-only")
    monkeypatch.setattr(photos, "add_to_end", refuse)
    r = upload(site, jpeg(color=(1, 2, 3)), "aaa.jpg")
    assert r.status_code == 201 and (site.photos / "aaa.jpg").is_file()


def test_upload_several_files_and_into_a_new_folder(tmp_path):
    s = Site(tmp_path)
    folder = tmp_path / "new" / "photos"
    s.configure()
    save_config(replace(default_config(), photos=replace(default_config().photos, folder=str(folder))))
    r = s.post("/api/photos", data={"photo": [(io.BytesIO(jpeg()), "one.jpg"),
                                              (io.BytesIO(b"nope"), "two.jpg")]},
               content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 207
    assert [x["name"] for x in body["saved"]] == ["one.jpg"]
    assert body["errors"][0]["file"] == "two.jpg" and "two.jpg" in body["error"]
    assert (folder / "one.jpg").is_file()


def test_upload_without_a_file(site):
    r = site.post("/api/photos", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and "No photo" in r.get_json()["error"]


def test_upload_when_the_card_is_full(site, monkeypatch):
    class Usage:
        free = 10 * 2**20
    monkeypatch.setattr(photos.shutil, "disk_usage", lambda p: Usage)
    r = upload(site, jpeg(), "x.jpg")
    assert r.status_code == 507 and "nearly full" in r.get_json()["error"]


# --- photos: actions -----------------------------------------------------------------------


def test_show_now_lasts_its_own_setting(site):
    data = site.get("/api/config").get_json()
    data["photos"]["show_now_minutes"] = 25
    assert site.client.put("/api/config", json=data).status_code == 200
    assert site.post("/api/photos/b.jpg/show-now").status_code == 200
    assert site.overrides()["pin"]["until"] == "2026-11-04T10:45:05-08:00"


def test_put_config_in_the_old_form_is_saved_in_the_new(site):
    data = site.get("/api/config").get_json()
    data["schedule"]["day"] = [{"screen": "tides", "minutes": 30}]
    data["schedule"]["photo"] = {"enabled": True, "at_minute": 0, "every_minutes": 60,
                                 "show_minutes": 10, "during": "day"}
    assert site.client.put("/api/config", json=data).status_code == 200
    saved = site.get("/api/config").get_json()["schedule"]
    assert "photo" not in saved
    assert [s["screen"] for s in saved["day"]] == ["photo", "tides", "tides"]


def test_show_now_pins_the_photo_for_show_now_minutes(site):
    r = site.post("/api/photos/b.jpg/show-now")
    assert r.status_code == 200
    assert site.overrides()["pin"] == {"kind": "photo", "photo": "b.jpg", "until": "2026-11-04T10:30:05-08:00",
                                       "set_at": "2026-11-04T10:20:05-08:00"}
    assert "b.jpg" in r.get_json()["message"] and site.ticks.requests == 1
    body = site.get("/api/status").get_json()
    assert body["mode"]["photo"] == "b.jpg"
    assert {p["name"]: p["pinned"] for p in site.get("/api/photos").get_json()}["b.jpg"]


def test_show_now_missing_photo(site):
    assert site.post("/api/photos/zzz.jpg/show-now").status_code == 404
    assert site.ticks.requests == 0


def test_up_next_add_and_remove(site):
    r = site.post("/api/photos/c.jpg/up-next")
    assert r.status_code == 200 and r.get_json()["up_next"] == ["c.jpg"]
    assert "up next" in r.get_json()["message"]
    site.post("/api/photos/a.jpg/up-next")
    r = site.post("/api/photos/c.jpg/up-next")  # already queued: stays where it is
    assert r.get_json()["up_next"] == ["c.jpg", "a.jpg"]
    r = site.client.delete("/api/photos/c.jpg/up-next")
    assert r.get_json()["up_next"] == ["a.jpg"]
    assert site.overrides()["up_next"] == ["a.jpg"]
    assert site.ticks.requests == 0  # doesn't change what's on the wall now


def test_up_next_keeps_the_pin(site):
    site.post("/api/mode", json={"kind": "sky", "until": None})
    site.post("/api/photos/a.jpg/up-next")
    assert site.overrides()["pin"]["kind"] == "sky"


def test_up_next_refuses_hidden_and_missing_photos(site):
    site.configure(hidden=("b.jpg",))
    assert site.post("/api/photos/b.jpg/up-next").status_code == 409
    assert site.post("/api/photos/zzz.jpg/up-next").status_code == 404
    assert site.client.delete("/api/photos/..%5C/up-next").status_code in (400, 404)


def test_hide_toggles_and_takes_the_photo_off_up_next(site):
    site.write_overrides({"pin": None, "up_next": ["a.jpg", "b.jpg"]})
    r = site.post("/api/photos/a.jpg/hide")
    assert r.status_code == 200 and r.get_json()["hidden"] is True
    assert load_config()[0].photos.hidden == ("a.jpg",)
    assert site.overrides()["up_next"] == ["b.jpg"]
    r = site.post("/api/photos/a.jpg/hide")
    assert r.get_json()["hidden"] is False and load_config()[0].photos.hidden == ()


def test_hide_with_an_explicit_value_is_idempotent(site):
    for _ in range(2):
        assert site.post("/api/photos/a.jpg/hide", json={"hidden": True}).get_json()["hidden"] is True
    assert load_config()[0].photos.hidden == ("a.jpg",)
    assert site.post("/api/photos/a.jpg/hide", json={"hidden": "yes"}).status_code == 400


def test_hide_with_a_broken_settings_file_says_why(site, monkeypatch):
    bad = {"photos": {"folder": str(site.photos)}, "bogus": 1}
    state.config_path().write_text(json.dumps(bad))
    monkeypatch.setattr(server.Dashboard, "config",
                        lambda self: (replace(default_config(), photos=replace(default_config().photos,
                                                                                folder=str(site.photos))), "bad"))
    r = site.post("/api/photos/a.jpg/hide")
    assert r.status_code == 409 and "Fix it under Settings" in r.get_json()["error"]
    assert json.loads(state.config_path().read_text()) == bad


def test_delete_moves_to_the_trash_and_cleans_up(site):
    site.configure(hidden=("a.jpg",))
    site.write_overrides({"pin": {"kind": "photo", "photo": "a.jpg", "until": "2026-11-04T10:30:00-08:00",
                                  "set_at": "2026-11-04T10:20:00-08:00"}, "up_next": ["a.jpg", "b.jpg"]})
    site.get(site.get("/api/photos").get_json()[0]["preview_url"])
    r = site.client.delete("/api/photos/a.jpg")
    assert r.status_code == 200 and "trash" in r.get_json()["message"]
    assert not (site.photos / "a.jpg").exists() and (site.photos / ".trash" / "a.jpg").is_file()
    assert site.overrides() == {"pin": None, "up_next": ["b.jpg"], "redraw_at": None}
    assert load_config()[0].photos.hidden == ()
    assert list((site.dir / state.PHOTO_PREVIEWS).glob("*.png")) == []
    assert [p["name"] for p in site.get("/api/photos").get_json()] == ["b.jpg", "c.jpg"]
    assert site.ticks.requests == 1


def test_delete_twice_keeps_both_in_the_trash(site):
    original = (site.photos / "a.jpg").read_bytes()
    site.client.delete("/api/photos/a.jpg")
    (site.photos / "a.jpg").write_bytes(jpeg(color=(1, 1, 1)))
    site.client.delete("/api/photos/a.jpg")
    trashed = sorted(p.name for p in (site.photos / ".trash").iterdir())
    assert len(trashed) == 2 and "a.jpg" in trashed
    assert (site.photos / ".trash" / "a.jpg").read_bytes() == original


def test_delete_missing_or_bad_name(site):
    assert site.client.delete("/api/photos/zzz.jpg").status_code == 404
    assert site.client.delete("/api/photos/..").status_code == 400
    assert site.client.delete("/api/photos/.trash").status_code == 400


# --- screen previews -----------------------------------------------------------------------


def test_scene_preview(site):
    r = site.get("/api/preview.png?kind=sky&at=2026-11-04T21:00")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.headers["Cache-Control"] == "no-store"
    assert site.render.calls == [["sky", "--at", "2026-11-04T21:00", "--forecast"]]
    site.get("/api/preview.png?kind=photo&photo=a.jpg")
    site.get("/api/preview.png?kind=message&message=Hi%0Athere")
    assert site.render.calls[1:] == [["photo", "--photo", "a.jpg"], ["message", "--message", "Hi\nthere"]]
    assert not list((site.dir / "previews-tmp").glob("*.png"))


def test_a_preview_as_it_looks_now_goes_by_the_walls_rules(site):
    site.get("/api/preview.png?kind=tides")
    assert site.render.calls == [["tides"]]


def test_scene_preview_of_a_named_moment_and_a_look(site):
    r = site.get("/api/preview.png?kind=sea&at=2026-11-20@sunset&waves=big&clouds=overcast")
    assert r.status_code == 200
    assert site.render.calls == [["sea", "--at", "2026-11-20@sunset", "--forecast", "--waves", "big",
                                  "--clouds", "overcast"]]


@pytest.mark.parametrize("query, words", [("kind=moon", "kind must be one of"),
                                          ("kind=tides&at=noon", "ISO 8601"),
                                          ("kind=tides&at=soon@sunset", "ISO 8601"),
                                          ("kind=tides&at=2026-11-20@teatime", "moment must be one of"),
                                          ("kind=sea&waves=huge", "waves must be one of"),
                                          ("kind=sea&clouds=pink", "clouds must be one of"),
                                          ("kind=sky&waves=big", "for the sea screen"),
                                          ("kind=photo&photo=../x.jpg", "isn't a photo name")])
def test_scene_preview_rejects_bad_requests(site, query, words):
    r = site.get(f"/api/preview.png?{query}")
    assert r.status_code == 400 and words in r.get_json()["error"]
    assert site.render.calls == []


def test_scene_preview_failure(site):
    site.render.fail = "the sky screen isn't built yet (photopainter.scenes.sky)"
    r = site.get("/api/preview.png?kind=sky")
    assert r.status_code == 500 and "isn't built yet" in r.get_json()["error"]


# --- the tick runner and renderer ----------------------------------------------------------


def sleeper(seconds: float, marker: Path) -> list[str]:
    code = f"import time,pathlib; p=pathlib.Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); time.sleep({seconds})"
    return [sys.executable, "-c", code]


def test_tick_runner_runs_one_at_a_time_and_queues_one_more(tmp_path):
    marker = tmp_path / "runs"
    runner = TickRunner(sleeper(0.3, marker))
    for _ in range(5):
        runner.request()
    assert runner.snapshot()["running"] and runner.snapshot()["queued"]
    assert runner.wait_idle(10)
    assert runner.started == 2 and marker.read_text() == "xx"
    assert runner.last_exit == 0


def test_tick_runner_kills_a_stuck_tick(tmp_path):
    runner = TickRunner(sleeper(30, tmp_path / "m"), max_runtime_s=0.3)
    runner.request()
    assert runner.wait_idle(10)
    assert runner.last_exit not in (0, None)


def test_tick_runner_close_stops_new_ticks(tmp_path):
    runner = TickRunner(sleeper(0.2, tmp_path / "m"))
    runner.request()
    runner.request()
    assert runner.close(wait_s=10)
    runner.request()
    assert runner.started == 1 and not runner.running


def test_tick_runner_reports_a_command_that_cant_start(tmp_path):
    runner = TickRunner([str(tmp_path / "no-such-program")])
    runner.request()
    assert runner.started == 0 and "couldn't start a tick" in runner.last_error


def test_tick_command_uses_the_fake_panel_flag(tmp_path):
    app = server.create_app(fake_panel=True)
    assert app.extensions["photopainter"].ticks.command[-3:] == ["photopainter", "tick", "--fake-panel"]
    app = server.create_app(fake_panel=False)
    assert app.extensions["photopainter"].ticks.command[-2:] == ["photopainter", "tick"]


def test_renderer_reports_the_cli_error(tmp_path):
    code = "import sys; sys.stderr.write('2026-11-04 10:00:00,000 ERROR photopainter: no photo here\\n'); sys.exit(1)"
    with pytest.raises(RenderError, match="^no photo here$"):
        Renderer([sys.executable, "-c", code])(["x"], tmp_path / "out.png")


def test_cli_error():
    assert jobs.cli_error("a\n2026-11-04 10:00:00,000 ERROR photopainter: boom\nTraceback junk") == "boom"
    assert jobs.cli_error("just a line\n") == "just a line"
    assert jobs.cli_error("") == ""


# --- health --------------------------------------------------------------------------------


def test_sources_health_from_the_caches(site):
    for name in ("tides", "weather", "surf"):
        (site.dir / f"{name}.json").write_text((FIXTURES / f"{name}.json").read_text())
    (site.dir / "sources.json").write_text(json.dumps({
        "weather": {"at": "2026-11-04T10:15:00-08:00", "error": "can't reach api.open-meteo.com"}}))
    config = load_config()[0]
    fetched = {n: state.from_iso(json.loads((FIXTURES / f"{n}.json").read_text())["fetched_at"])
               for n in ("tides", "weather", "surf")}
    src = health.sources(site.dir, config, fetched["surf"] + timedelta(minutes=30))
    assert src["tides"]["level"] == "ok" and src["tides"]["until"] is not None
    assert src["weather"]["level"] == "warn" and "can't reach" in src["weather"]["note"]
    assert src["surf"]["level"] == "ok" and src["surf"]["fetched_at"] == fetched["surf"]
    later = health.sources(site.dir, config, fetched["surf"] + timedelta(hours=13))
    assert later["surf"]["level"] == "error"
    much_later = health.sources(site.dir, config, fetched["tides"] + timedelta(days=60))
    assert much_later["tides"]["level"] == "error"


def test_sources_health_disabled_and_missing(site):
    c = default_config()
    config = replace(c, weather=replace(c.weather, enabled=False))
    src = health.sources(site.dir, config, NOW)
    assert src["weather"]["level"] == "off"
    assert src["tides"]["level"] == "error" and src["tides"]["note"].startswith("No data yet")


def test_system_health_on_a_pi(tmp_path, monkeypatch):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    (proc / "net").mkdir()
    (proc / "uptime").write_text("12345.67 40000.00\n")
    (proc / "self" / "status").write_text("Name:\tpython3\nVmRSS:\t   41000 kB\n")
    (proc / "meminfo").write_text("MemTotal:  427000 kB\nMemFree: 1 kB\nMemAvailable:  250000 kB\n")
    (proc / "net" / "wireless").write_text(
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        " wlan0: 0000   58.  -52.  -256        0      0      0      0      0        0\n")
    zone = sys_ / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("48312\n")
    monkeypatch.setattr(health, "PROC", proc)
    monkeypatch.setattr(health, "SYS", sys_)
    outputs = {"iw": "Connected to aa:bb (on wlan0)\n\tSSID: Beach House\n\tsignal: -52 dBm\n",
               "vcgencmd": "throttled=0x50000\n"}
    monkeypatch.setattr(health, "_run", lambda cmd, timeout=2.0: outputs.get(cmd[0]))
    monkeypatch.setattr("photopainter.tick.clock_synced", lambda: True)
    health.clear_kept()
    h = health.system(tmp_path)
    assert h["uptime_s"] == 12345.67 and h["cpu_temp_c"] == 48.3
    assert h["memory"] == {"server_mb": 40.0, "available_mb": 244.1, "total_mb": 417.0}
    assert h["wifi"] == {"interface": "wlan0", "quality": 58.0, "signal_dbm": -52.0, "ssid": "Beach House",
                         "connected": True}
    assert h["clock"] == {"synced": True}
    assert h["power"]["undervoltage_since_boot"] and not h["power"]["undervoltage_now"]
    assert h["disk"]["free_mb"] > 0


def test_wifi_name_from_nmcli_when_iw_is_missing(tmp_path, monkeypatch):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "wireless").write_text("a\nb\n wlan0: 0000   40.  -70.  -256  0 0 0 0 0 0\n")
    monkeypatch.setattr(health, "PROC", tmp_path)
    outputs = {"nmcli": "no:Other\nyes:My\\:Net\n"}
    monkeypatch.setattr(health, "_run", lambda cmd, timeout=2.0: outputs.get(cmd[0]))
    health.clear_kept()
    assert health.wifi()["ssid"] == "My:Net"


def test_system_health_on_the_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PROC", tmp_path / "none")
    monkeypatch.setattr(health, "SYS", tmp_path / "none")
    monkeypatch.setattr(health, "_run", lambda cmd, timeout=2.0: None)
    health.clear_kept()
    h = health.system(tmp_path / "no" / "folder")
    assert h["uptime_s"] is None and h["wifi"] is None and h["power"] is None
    assert h["network"] is None and h["tailscale"] is None
    assert h["memory"]["server_mb"] > 0 and h["disk"] is not None


# Recorded on the Pi on 2026-11-05 and shortened, with the addresses and tailnet name changed.
IP_ADDR = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "addr_info": [
        {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo"},
        {"family": "inet6", "local": "::1", "prefixlen": 128, "scope": "host"}]},
    {"ifname": "wlan0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "addr_info": [
        {"family": "inet", "local": "192.168.1.49", "prefixlen": 24, "scope": "global", "label": "wlan0"},
        {"family": "inet6", "local": "fd00:1:2:3::49", "prefixlen": 64, "scope": "global"},
        {"family": "inet6", "local": "fe80::49", "prefixlen": 64, "scope": "link"}]},
    {"ifname": "tailscale0", "flags": ["POINTOPOINT", "UP", "LOWER_UP"], "addr_info": [
        {"family": "inet", "local": "100.64.0.7", "prefixlen": 32, "scope": "global", "label": "tailscale0"},
        {"family": "inet6", "local": "fd7a:115c:a1e0::7", "prefixlen": 128, "scope": "global"},
        {"family": "inet6", "local": "fe80::7", "prefixlen": 64, "scope": "link"}]},
    {"ifname": "eth0", "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "addr_info": []},
])
TAILSCALE_RUNNING = json.dumps({
    "Version": "1.104.5", "BackendState": "Running", "AuthURL": "",
    "TailscaleIPs": ["100.64.0.7", "fd7a:115c:a1e0::7"],
    "Self": {"HostName": "InkFrame", "DNSName": "inkframe.tail0000.ts.net.", "OS": "linux",
             "TailscaleIPs": ["100.64.0.7", "fd7a:115c:a1e0::7"], "Online": True},
    "MagicDNSSuffix": "tail0000.ts.net", "Peer": None})


def on_a_pi(monkeypatch, tmp_path, *, ip=IP_ADDR, tailscale=TAILSCALE_RUNNING, installed=True):
    """`ip` and `tailscale` answer with these outputs (None: the command fails)."""
    outputs = {"ip": ip, "tailscale": tailscale}
    calls = []

    def run(cmd, timeout=2.0):
        calls.append(cmd[0])
        return outputs.get(cmd[0])

    monkeypatch.setattr(health, "PROC", tmp_path)
    monkeypatch.setattr(health, "_run", run)
    monkeypatch.setattr(health.shutil, "which", lambda name: f"/usr/bin/{name}" if installed else None)
    health.clear_kept()
    return calls


def test_network_lists_the_addresses_others_can_use(tmp_path, monkeypatch):
    on_a_pi(monkeypatch, tmp_path)
    assert health.network() == [
        {"interface": "wlan0", "ipv4": ["192.168.1.49"], "ipv6": ["fd00:1:2:3::49"]},
        {"interface": "tailscale0", "ipv4": ["100.64.0.7"], "ipv6": ["fd7a:115c:a1e0::7"]}]


def test_network_without_ip_or_with_output_it_cant_read(tmp_path, monkeypatch):
    on_a_pi(monkeypatch, tmp_path, ip=None)
    assert health.network() is None
    for garbage in ("not json", "[1]", '[{"addr_info": [{"scope": "global", "family": "inet"}]}]'):
        on_a_pi(monkeypatch, tmp_path, ip=garbage)
        assert health.network() is None


def test_tailscale_connected(tmp_path, monkeypatch):
    on_a_pi(monkeypatch, tmp_path)
    assert health.tailscale() == {"state": "connected", "ips": ["100.64.0.7", "fd7a:115c:a1e0::7"],
                                  "dns_name": "inkframe.tail0000.ts.net", "online": True}


def test_tailscale_connected_but_cut_off_from_its_servers(tmp_path, monkeypatch):
    status = json.loads(TAILSCALE_RUNNING)
    status["Self"]["Online"] = False
    on_a_pi(monkeypatch, tmp_path, tailscale=json.dumps(status))
    assert health.tailscale()["state"] == "connected" and health.tailscale()["online"] is False


@pytest.mark.parametrize("backend, expected", [("Stopped", "stopped"), ("Starting", "stopped"), ("NoState", "stopped"),
                                               ("NeedsLogin", "logged_out"), ("NeedsMachineAuth", "logged_out")])
def test_tailscale_not_connected(tmp_path, monkeypatch, backend, expected):
    # with no tailnet to go on, Self is there with no addresses and no name
    status = {"BackendState": backend, "TailscaleIPs": None,
              "Self": {"DNSName": "", "TailscaleIPs": None, "Online": False}}
    on_a_pi(monkeypatch, tmp_path, tailscale=json.dumps(status))
    assert health.tailscale() == {"state": expected, "ips": [], "dns_name": None, "online": False}


def test_tailscale_with_its_daemon_down_or_no_program(tmp_path, monkeypatch):
    on_a_pi(monkeypatch, tmp_path, tailscale=None)  # `tailscale status` exits with an error
    assert health.tailscale() == {"state": "stopped", "ips": [], "dns_name": None, "online": None}
    calls = on_a_pi(monkeypatch, tmp_path, installed=False)
    assert health.tailscale() == {"state": "not_installed", "ips": [], "dns_name": None, "online": None}
    assert "tailscale" not in calls


def test_tailscale_output_it_cant_read_is_no_reading(tmp_path, monkeypatch):
    for garbage in ("not json", "{}", "[]"):
        on_a_pi(monkeypatch, tmp_path, tailscale=garbage)
        assert health.tailscale() is None


def test_tailscale_on_the_mac_even_with_tailscale_installed(tmp_path, monkeypatch):
    calls = on_a_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(health, "PROC", tmp_path / "none")
    assert health.tailscale() is None and calls == []


def test_network_and_tailscale_are_kept_until_cleared(tmp_path, monkeypatch):
    calls = on_a_pi(monkeypatch, tmp_path)
    for _ in range(3):
        h = health.system(tmp_path)
    assert h["network"] and h["tailscale"]["state"] == "connected"
    assert calls.count("ip") == 1 and calls.count("tailscale") == 1
    health.clear_kept()
    health.system(tmp_path)
    assert calls.count("ip") == 2 and calls.count("tailscale") == 2


@pytest.mark.parametrize("reading, warns", [
    ({"state": "stopped"}, True), ({"state": "logged_out"}, True), ({"state": "connected"}, False),
    ({"state": "not_installed"}, False), (None, False)])
def test_problems_for_tailscale(site, reading, warns):
    out = server.problems({}, None, {"tailscale": reading}, {}, NOW, FakeTicks(), True)
    expected = [{"level": "warn", "text": "Tailscale is off: the dashboard is only reachable on the home Wi-Fi"}]
    assert out == (expected if warns else [])


def test_problems_for_power_and_clock(site):
    sys_health = {"clock": {"synced": False}, "power": {"undervoltage_now": True}}
    out = server.problems({"data": {"clock_ok": True}}, None, sys_health, {}, NOW, FakeTicks(), True)
    assert [p["level"] for p in out] == ["error", "error"]
    assert "clock" in out[0]["text"] and "Under-voltage" in out[1]["text"]


def test_problems_for_the_battery(site):
    since = state.to_iso(NOW - timedelta(minutes=95))
    on_battery = {"volts": 3.72, "milliamps": -181.0, "state": "discharging", "percent": 28,
                  "on_battery_since": since, "low": False, "read_at": state.to_iso(NOW)}
    out = server.problems({"battery": on_battery}, None, {}, {}, NOW, FakeTicks(), True)
    assert [(p["level"], p["text"]) for p in out] == [("warn", "Running on the battery for 95 minutes: is the power out?")]
    low = {**on_battery, "volts": 3.45, "low": True}
    out = server.problems({"battery": low}, None, {}, {}, NOW, FakeTicks(), True)
    assert out[0]["level"] == "error" and out[0]["text"].startswith("Battery low: the frame has run on its battery for 95 minutes")
    assert "settled on a photo" in out[0]["text"]
    for quiet in ({**on_battery, "state": "charging", "milliamps": 300.0, "on_battery_since": None},
                  {**on_battery, "state": "none"}, None):
        assert server.problems({"battery": quiet}, None, {}, {}, NOW, FakeTicks(), True) == []


def test_battery_endpoint_reports_the_last_cycle_and_a_guess(site):
    from datetime import timedelta as td
    lines = ["time,volts,milliamps,state"]
    for m in range(90, -1, -1):
        t = NOW - td(minutes=m)
        lines.append(f"{state.to_iso(t)},{3.8:.3f},-200.0,discharging")
    (site.dir / "battery.csv").write_text("\n".join(lines) + "\n")
    site.write_status(power={"mode": "normal", "wifi": "on", "since": state.to_iso(NOW), "window_until": None,
                             "next_window_at": None})
    body = site.get("/api/battery").get_json()
    assert body["capacity_mah"] == 2000 and body["low_volts"] == 3.3 and body["indicator_volts"] == 3.6 and body["power"]["mode"] == "normal"
    assert body["cycle"]["ongoing"] is True and len(body["cycle"]["readings"]) == 91
    assert body["cycle"]["used_mah"] == 300
    assert body["estimate"]["percent"] == 48 and body["estimate"]["hours_left"] == 4.8
    assert body["estimate"]["runs_out_at"] == state.to_iso(NOW + td(hours=4.8))
    h = body["history"]
    assert h["to"] == state.to_iso(NOW) and h["step_s"] == 900 and len(h["readings"]) == 6  # 15-minute buckets
    assert h["spans"] == [{"state": "discharging", "start": state.to_iso(NOW - td(minutes=90)), "end": state.to_iso(NOW)}]
    assert all(r["milliamps"] == -200.0 for r in h["readings"])  # the chart's current strip
    assert site.get("/api/status").get_json()["power"]["mode"] == "normal"


def test_battery_endpoint_with_no_log(site):
    body = site.get("/api/battery").get_json()
    assert body["cycle"] is None and body["estimate"] is None and body["power"] is None
    assert body["history"]["readings"] == [] and body["history"]["spans"] == []
    assert body["indicator_volts"] == 3.6 and body["low_volts"] == 3.3


def test_the_battery_chart_has_a_strip_for_the_current(site):
    """Under the voltage, the current: green above zero while charging, amber below on the cell
    (the Owner's pick on 2026-11-09 over a second line on the voltage's plot)."""
    js = site.get("/static/app.js").data.decode()
    chart = js[js.index("function batteryChart"):js.index("function initBatteryChart")]
    for part in ('id: "battery-amps-in"', 'id: "battery-amps-out"', 'class: "amps-edge"', 'ya(r.milliamps)'):
        assert part in chart, part
    css = site.get("/static/style.css").data.decode()
    assert ".chart .amps-in" in css and ".chart .amps-out" in css
    assert "voltage and current over the last 5 days" in site.get("/").data.decode()


def test_status_passes_the_battery_through(site):
    battery = {"volts": 3.98, "milliamps": 312.0, "state": "charging", "percent": 76,
               "on_battery_since": None, "low": False, "read_at": state.to_iso(NOW)}
    site.write_status(battery=battery)
    assert site.get("/api/status").get_json()["battery"] == battery


# --- running it ----------------------------------------------------------------------------


def test_serve_command_calls_main(monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "main", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["serve", "--port", "8123", "--fake-panel"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 8123, "fake_panel": True}


def test_main_reports_a_port_it_cant_use(caplog):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert server.main(host="127.0.0.1", port=port, fake_panel=True) == 1
    assert "can't listen" in caplog.text


# --- what comes next, redraw, previews with unsaved settings ------------------------------


def test_status_lists_the_coming_changes(site):
    # 10:20 by day: the next photo step (11:00) shows the rotation's next photo, then tides.
    body = site.get("/api/status").get_json()
    up = body["upcoming"]
    assert [u["kind"] for u in up[:2]] == ["photo", "tides"]
    assert up[0]["at"].startswith("2026-11-04T11:00") and up[0]["why"] == "photo"
    assert up[0]["photo"] == "a.jpg"
    assert up[1]["at"].startswith("2026-11-04T11:10") and up[1]["why"] == "photo_ends"
    assert [u["minutes"] for u in up[:2]] == [10, 50]  # until the change after it
    assert len(up) == server.UPCOMING


def test_status_coming_changes_of_a_slideshow(site):
    data = site.get("/api/config").get_json()
    data["schedule"]["day"] = [{"screen": "photo", "minutes": 30}]
    assert site.client.put("/api/config", json=data).status_code == 200
    up = site.get("/api/status").get_json()["upcoming"]
    assert [(u["kind"], u["why"], u["photo"], u["minutes"]) for u in up[:2]] == [
        ("photo", "photo", "a.jpg", 30), ("photo", "photo", "b.jpg", 30)]


def test_status_coming_changes_with_no_photos_to_show(site):
    data = site.get("/api/config").get_json()
    data["photos"]["hidden"] = ["a.jpg", "b.jpg", "c.jpg"]
    assert site.client.put("/api/config", json=data).status_code == 200
    up = site.get("/api/status").get_json()["upcoming"]
    assert up and all(u["kind"] != "photo" for u in up)


def test_status_coming_changes_name_the_up_next_photos_in_order(site):
    site.post("/api/photos/c.jpg/up-next")
    site.post("/api/photos/b.jpg/up-next")
    up = [u for u in site.get("/api/status").get_json()["upcoming"] if u["kind"] == "photo"]
    assert [u["photo"] for u in up[:2]] == ["c.jpg", "b.jpg"]


def test_status_coming_changes_skip_repeated_redraws(site):
    site.now = t("2026-11-04T19:20")  # night: the sky redraws hourly until sunrise
    up = site.get("/api/status").get_json()["upcoming"]
    assert [(u["kind"], u["why"]) for u in up[:3]] == [("sky", "redraw"), ("tides", "sunrise"), ("photo", "photo")]
    assert up[0]["at"].startswith("2026-11-04T20:00") and up[1]["at"].startswith("2026-11-05T06:12")
    assert up[2]["at"].startswith("2026-11-05T07:00") and up[2]["photo"] == "a.jpg"


def test_status_coming_changes_say_why(site):
    site.now = t("2026-11-04T16:30")  # half an hour before sunset (16:57 in SUN)
    site.post("/api/mode", json={"kind": "tides", "until": "next_change"})
    up = site.get("/api/status").get_json()["upcoming"]
    assert up[0]["kind"] == "sky" and up[0]["why"] in ("pin_ends", "sunset")
    assert up[0]["at"].startswith("2026-11-04T16:57")


def test_status_without_sun_times_has_no_coming_changes(tmp_path):
    def no_sun(day):
        raise RuntimeError("no ephemeris")

    s = Site(tmp_path)
    s.app = server.create_app(fake_panel=True, clock=lambda: s.now, ticks=s.ticks, render=s.render, sun=no_sun)
    s.client = s.app.test_client()
    assert s.get("/api/status").get_json()["upcoming"] == []


def test_redraw_marks_the_wall_stale_and_starts_a_tick(site):
    r = site.post("/api/redraw")
    assert r.status_code == 200 and "again" in r.get_json()["message"]
    assert site.overrides()["redraw_at"] == state.to_iso(site.now)
    assert site.ticks.requests == 1
    # the status passes it through
    assert site.get("/api/status").get_json()["overrides"]["redraw_at"] == state.to_iso(site.now)


def test_preview_with_unsaved_settings_hands_them_to_the_renderer(site):
    seen = {}

    def render(args, out):
        i = args.index("--config")
        seen["settings"] = json.loads(Path(args[i + 1]).read_text())
        site.render(args, out)

    site.app.extensions["photopainter"].render = render
    cfg = site.get("/api/config").get_json()
    cfg["tides"]["chart_days_ahead"] = 10
    r = site.post("/api/preview.png", json={"kind": "tides", "at": "2026-11-05T08:00", "config": cfg})
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert seen["settings"]["tides"]["chart_days_ahead"] == 10
    assert site.render.calls[-1][:3] == ["tides", "--at", "2026-11-05T08:00"]
    assert not list((site.dir / "previews-tmp").glob("*"))  # the settings file is gone too
    assert site.get("/api/config").get_json()["tides"]["chart_days_ahead"] != 10  # never saved


def test_preview_with_bad_settings_is_rejected_like_a_save(site):
    cfg = site.get("/api/config").get_json()
    cfg["tides"]["chart_days_ahead"] = 99
    r = site.post("/api/preview.png", json={"kind": "tides", "config": cfg})
    assert r.status_code == 400
    assert any(e.startswith("tides.chart_days_ahead") for e in r.get_json()["errors"])
    assert site.render.calls == []


def test_preview_post_without_settings_draws_as_get_does(site):
    r = site.post("/api/preview.png", json={"kind": "sky", "at": "2026-11-04T21:00", "photo": None})
    assert r.status_code == 200
    assert site.render.calls[-1] == ["sky", "--at", "2026-11-04T21:00", "--forecast"]
    r = site.post("/api/preview.png", json={"kind": "sky", "at": "noon"})
    assert r.status_code == 400 and "ISO 8601" in r.get_json()["error"]


# --- the frame's name ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["InkFrame", "frame-2", "a", "x" * 32])
def test_good_frame_names(name):
    assert frame_name.check(f" {name} ") == name


@pytest.mark.parametrize("name", [None, 7, "", "  ", "Dad's Frame", "a.b", "-a", "a-", "1234",
                                  "localhost", "x" * 33, "a;reboot", "a\nb"])
def test_bad_frame_names(name):
    with pytest.raises(frame_name.BadName):
        frame_name.check(name)


def test_rename_with_a_fake_panel_never_touches_the_computer(tmp_path, monkeypatch):
    def no(*a, **k):
        raise AssertionError("tried to rename the computer")
    monkeypatch.setattr(frame_name, "set_name", no)
    s = Site(tmp_path, fake_panel=True)
    r = s.post("/api/name", json={"name": "InkFrame"})
    assert r.status_code == 200 and r.get_json()["name"] == "InkFrame"
    assert r.get_json()["previous"] == socket.gethostname()
    assert s.get("/api/status").get_json()["health"]["hostname"] == "InkFrame"


def test_rename_on_the_pi_runs_the_script_under_sudo(tmp_path, monkeypatch):
    calls = []
    real = frame_name.set_name

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "warning: the Tailscale name didn't update\n", "")

    monkeypatch.setattr(frame_name, "set_name", lambda name: real(name, run=run))
    s = Site(tmp_path, fake_panel=False)
    r = s.post("/api/name", json={"name": "InkFrame"})
    assert r.status_code == 200
    assert r.get_json()["warnings"] == ["the Tailscale name didn't update"]
    assert calls == [["sudo", "-n", str(frame_name.SCRIPT), "InkFrame"]]
    assert frame_name.SCRIPT.exists()


def test_rename_refuses_a_bad_name_before_running_anything(tmp_path, monkeypatch):
    def no(*a, **k):
        raise AssertionError("ran the script")
    monkeypatch.setattr(frame_name, "set_name", no)
    r = Site(tmp_path, fake_panel=False).post("/api/name", json={"name": "a;reboot"})
    assert r.status_code == 400 and "letters" in r.get_json()["error"]


def test_a_failed_rename_is_an_error(tmp_path, monkeypatch):
    real = frame_name.set_name
    failed = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")
    monkeypatch.setattr(frame_name, "set_name", lambda name: real(name, run=failed))
    r = Site(tmp_path, fake_panel=False).post("/api/name", json={"name": "InkFrame"})
    assert r.status_code == 500 and "password" in r.get_json()["error"]
