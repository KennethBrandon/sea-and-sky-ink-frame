import subprocess
import sys
import types
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import cli, state
from photopainter.config import default_config
from photopainter.render.draw import INK
from photopainter.render.palette import palette_colors
from photopainter.scenes import Scene, SceneContext, render_scene

LA = ZoneInfo("America/Los_Angeles")
AT = datetime(2026, 11, 3, 14, 10, tzinfo=LA)


def message(text, portrait=False):
    config = default_config()
    if portrait:
        config = replace(config, display=replace(config.display, portrait=True))
    ctx = SceneContext.from_config(config)
    return render_scene(Scene("message", "message/x", AT, message=text), ctx)


# --- base -----------------------------------------------------------------------------


def test_scene_requires_aware_time_and_known_kind():
    with pytest.raises(ValueError, match="timezone-aware"):
        Scene("tides", "tides/x", datetime(2026, 11, 3, 14, 10))
    with pytest.raises(ValueError, match="unknown scene kind"):
        Scene("weather", "weather/x", AT)  # type: ignore[arg-type]
    s = Scene("photo", "photo/x", AT, photo="beach.jpg")
    assert (s.photo, s.message) == ("beach.jpg", None)
    assert s == Scene("photo", "photo/x", AT, photo="beach.jpg")


def test_context_from_config(isolated_paths):
    ctx = SceneContext.from_config(default_config())
    assert ctx.canvas == (800, 480)
    assert ctx.state_dir == isolated_paths / "state"
    assert ctx.data_dir == state.data_dir()
    portrait = replace(default_config(), display=replace(default_config().display, portrait=True))
    assert SceneContext.from_config(portrait, Path("/s"), Path("/d")) == SceneContext(
        portrait, (480, 800), Path("/s"), Path("/d"))


# --- dispatch -------------------------------------------------------------------------


def test_unbuilt_scene_kind_is_not_implemented(monkeypatch):
    monkeypatch.setitem(sys.modules, "photopainter.scenes.tides", None)  # as if the file didn't exist
    ctx = SceneContext.from_config(default_config())
    with pytest.raises(NotImplementedError, match="tides screen isn't built yet"):
        render_scene(Scene("tides", "tides/x", AT), ctx)


@pytest.mark.parametrize(
    "result, match",
    [
        (np.zeros((800, 480), np.uint8), "expected"),
        (np.zeros((480, 800), np.int64), "uint8"),
        ([[0]], "uint8"),
        (np.full((480, 800), 6, np.uint8), "outside the six ink"),
    ],
)
def test_render_scene_checks_output(monkeypatch, result, match):
    fake = types.ModuleType("photopainter.scenes.sky")
    fake.render = lambda scene, ctx: result
    monkeypatch.setitem(sys.modules, "photopainter.scenes.sky", fake)
    with pytest.raises(ValueError, match=match):
        render_scene(Scene("sky", "sky/x", AT), SceneContext.from_config(default_config()))


# --- message scene --------------------------------------------------------------------


@pytest.mark.parametrize("portrait", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "Clock not set\nThe frame can't tell the time yet. It will fix itself once it's online.",
        "No Wi-Fi\n" + "Try unplugging the router, then the frame. " * 40,  # far too long: truncated
        "Averyveryveryveryveryveryveryveryverylongwordwithnospaces",
        "",
    ],
)
def test_message_renders_canvas_of_inks(text, portrait):
    idx = message(text, portrait)
    assert idx.shape == ((800, 480) if portrait else (480, 800))
    assert idx.dtype == np.uint8
    assert set(np.unique(idx).tolist()) <= {INK["white"], INK["black"], INK["blue"]}
    if text:
        assert (idx == INK["black"]).any()


def test_message_text_stays_inside_margins():
    idx = message("Clock not set\n" + "word " * 200)
    black = np.argwhere(idx == INK["black"])
    assert black[:, 1].min() >= 30 and black[:, 1].max() <= 800 - 30
    assert black[:, 0].max() <= 480 - 30


def test_blank_message_is_a_blank_page():
    assert (message("") == INK["white"]).all()


def test_message_requires_text():
    ctx = SceneContext.from_config(default_config())
    with pytest.raises(ValueError, match="needs a message"):
        render_scene(Scene("message", "message/x", AT), ctx)


# --- CLI ------------------------------------------------------------------------------


def test_cli_scene_message_writes_preview(tmp_path):
    out = tmp_path / "hello.png"
    assert cli.main(["scene", "message", "--message", "hello", "-o", str(out)]) == 0
    img = Image.open(out)
    assert img.size == (800, 480) and img.mode == "RGB"
    colors = {tuple(c) for c in np.unique(np.asarray(img).reshape(-1, 3), axis=0)}
    assert colors <= set(palette_colors("estimated"))
    assert cli.main(["scene", "message", "--message", "hi", "--portrait", "-o", str(out)]) == 0
    assert Image.open(out).size == (480, 800)


def test_cli_scene_default_output_and_at(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scene", "message", "--message", "x", "--at", "2026-11-03T21:00"]) == 0
    assert capsys.readouterr().out.strip() == str(Path("previews") / "message.png")
    assert (tmp_path / "previews" / "message.png").is_file()


def test_cli_scene_parse_at():
    assert cli._parse_at("2026-11-03T21:00", LA) == datetime(2026, 11, 3, 21, 0, tzinfo=LA)
    assert cli._parse_at("2026-11-04T05:00:00+00:00", LA) == datetime(2026, 11, 3, 21, 0, tzinfo=LA)
    assert cli._parse_at(None, LA).tzinfo is LA
    with pytest.raises(ValueError, match="ISO 8601"):
        cli._parse_at("tonight", LA)


def test_cli_scene_takes_a_named_moment():
    config = default_config()
    assert cli._scene_at("2026-11-03T21:00", config) == datetime(2026, 11, 3, 21, 0, tzinfo=LA)
    with pytest.raises(ValueError, match="a date before the @"):
        cli._scene_at("tomorrow@sunset", config)
    with pytest.raises(ValueError, match="unknown moment"):
        cli._scene_at("2026-11-03@teatime", config)


@pytest.mark.skipif(not (Path(__file__).parent.parent / "data" / "de421.bsp").exists(), reason="no ephemeris")
def test_named_moments_show_the_sun_and_the_moon():
    """Sunrise and sunset keep the Sun above the horizon; dawn and dusk are dark enough to tell;
    `moon` puts the Moon west of the window, or at its highest if it never gets there."""
    from datetime import date, timedelta

    from photopainter.astro.moments import moment_at
    from photopainter.astro.sky import moon_altaz, sun_altaz

    loc = default_config().location
    day = date(2026, 11, 24)  # a nearly full Moon

    def at(name, on=day):
        return moment_at(on, name, loc.lat, loc.lon, loc.tz, 190.0)

    def sun(t):
        return float(sun_altaz([t], loc.lat, loc.lon)[0][0])

    assert 0.5 < sun(at("sunrise")) < 5 and 0.5 < sun(at("sunset")) < 5
    assert at("sunrise").date() == day and at("sunset").date() == day
    assert at("sunrise") < at("sunset") < at("dusk")
    assert -7 < sun(at("dawn")) < -5 and -7 < sun(at("dusk")) < -5
    moon = at("moon")
    assert at("dusk") <= moon <= at("dusk") + timedelta(hours=16)
    alt, az = moon_altaz(moon, loc.lat, loc.lon)
    assert alt >= 10 and 205 <= az <= 240
    waning = date(2026, 12, 2)  # up late in the east: never west of the window before dawn
    moon = at("moon", waning)
    best = moon_altaz(moon, loc.lat, loc.lon)[0]
    assert best > 10
    for hours in (-1, 1):
        later = moon + timedelta(hours=hours)
        assert later > at("dawn", waning + timedelta(days=1)) or moon_altaz(later, loc.lat, loc.lon)[0] <= best


def test_cli_scene_errors_are_clean(tmp_path, caplog, monkeypatch):
    out = str(tmp_path / "x.png")
    assert cli.main(["scene", "message", "-o", out]) == 1
    assert "--message" in caplog.text
    monkeypatch.setitem(sys.modules, "photopainter.scenes.tides", None)
    assert cli.main(["scene", "tides", "-o", out]) == 1
    assert "isn't built yet" in caplog.text


def test_cli_scene_bad_config_falls_back_to_defaults(tmp_path, caplog):
    state.config_path().parent.mkdir(parents=True)
    state.config_path().write_text("{broken")
    assert cli.main(["scene", "message", "--message", "x", "-o", str(tmp_path / "x.png")]) == 0
    assert "using default settings" in caplog.text


def test_cli_parser_has_new_commands():
    p = cli.build_parser()
    a = p.parse_args(["tick", "--fake-panel"])
    assert (a.command, a.fake_panel) == ("tick", True)
    a = p.parse_args(["serve"])
    assert (a.host, a.port, a.fake_panel) == ("0.0.0.0", 80, False)
    a = p.parse_args(["serve", "--host", "127.0.0.1", "--port", "8080", "--fake-panel"])
    assert (a.host, a.port, a.fake_panel) == ("127.0.0.1", 8080, True)
    a = p.parse_args(["scene", "sky", "--at", "2026-11-03T21:00", "--photo", "a.jpg", "-o", "x.png"])
    assert (a.kind, a.at, a.photo, a.output) == ("sky", "2026-11-03T21:00", "a.jpg", "x.png")
    with pytest.raises(SystemExit):
        p.parse_args(["scene", "weather"])


def test_cli_tick_and_serve_call_their_modules(monkeypatch):
    calls = []
    tick = types.ModuleType("photopainter.tick")
    tick.main = lambda **kw: calls.append(("tick", kw)) or 0
    web = types.ModuleType("photopainter.web")
    server = types.ModuleType("photopainter.web.server")
    server.main = lambda **kw: calls.append(("serve", kw)) or 0
    monkeypatch.setitem(sys.modules, "photopainter.tick", tick)
    monkeypatch.setitem(sys.modules, "photopainter.web", web)
    monkeypatch.setitem(sys.modules, "photopainter.web.server", server)
    assert cli.main(["tick", "--fake-panel"]) == 0
    assert cli.main(["serve", "--port", "8080"]) == 0
    assert calls == [("tick", {"fake_panel": True}),
                     ("serve", {"host": "0.0.0.0", "port": 8080, "fake_panel": False})]


def test_cli_tick_and_serve_stubs_say_not_built(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "photopainter.tick", None)
    monkeypatch.setitem(sys.modules, "photopainter.web", None)
    # test_web.py imports the real server when it's collected; hide that one too, or this
    # would start it on port 80.
    monkeypatch.setitem(sys.modules, "photopainter.web.server", None)
    assert cli.main(["tick"]) == 2
    assert "C2" in caplog.text
    assert cli.main(["serve"]) == 2
    assert "W1" in caplog.text


# --- import hygiene -------------------------------------------------------------------


def test_new_modules_import_without_hardware_or_heavy_libraries():
    code = (
        "import sys, photopainter.cli, photopainter.config, photopainter.state, "
        "photopainter.scenes, photopainter.scenes.message, photopainter.render.draw, "
        "photopainter.astro, photopainter.astro.sun; "
        "bad = {'spidev', 'gpiozero', 'lgpio', 'smbus', 'smbus2', 'skyfield', 'flask'} & set(sys.modules); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
