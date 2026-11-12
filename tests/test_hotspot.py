"""hotspot.py: knowing the frame is off Wi-Fi and the setup hotspot's steps, on a fake radio;
what it asks of nmcli on the Pi; the setup screen; and the setup page's part of the server."""

import re
import stat
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import hotspot, state
from photopainter.config import ConfigError, default_config, validate
from photopainter.hotspot import FakeNetwork, advance
from photopainter.render import battery_badge
from photopainter.render.draw import INK
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import setup as setup_scene
from photopainter.web import server, wifi

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=LA)


def minutes(n: float) -> datetime:
    return NOW + timedelta(minutes=n)


@pytest.fixture
def folder():
    path = state.state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def radio(folder):
    """A radio with no network: the setup's starting point."""
    net = FakeNetwork(folder)
    state.write_json(net.path, {"connected": False})
    return net


def up(folder, radio):
    """The hotspot up, from a held button."""
    hotspot.ask(folder, NOW)
    return advance(folder, NOW, radio, name="InkFrame")


# --- on Wi-Fi or not -----------------------------------------------------------------------


@pytest.mark.parametrize("operstate, address, expected", [("up", True, True), ("up", False, False),
                                                          ("dormant", True, False), ("down", False, False)])
def test_on_wifi_means_associated_with_an_address(tmp_path, monkeypatch, operstate, address, expected):
    (tmp_path / "wlan0").mkdir()
    (tmp_path / "wlan0" / "operstate").write_text(operstate + "\n")
    monkeypatch.setattr(hotspot, "_has_ipv4", lambda iface: address)
    assert hotspot.link_up(sys_net=tmp_path) is expected


def test_no_radio_says_it_cant_tell(tmp_path):
    assert hotspot.link_up(sys_net=tmp_path) is None


def test_the_mark_waits_out_a_routers_reboot():
    assert not hotspot.mark_due(None, NOW)
    assert not hotspot.mark_due(minutes(-9), NOW)
    assert hotspot.mark_due(minutes(-10), NOW)


# --- the button ----------------------------------------------------------------------------


def test_held_with_no_wifi_starts_the_hotspot_with_a_new_password(folder, radio):
    outcome = up(folder, radio)
    data = hotspot.load(folder)
    assert data["state"] == "on" and data["ssid"] == "InkFrame-Setup" and data["asked_at"] is None
    assert re.fullmatch(r"[a-z]+-[a-z]+-\d{4}", data["password"])
    assert outcome.key.startswith("setup/") and outcome.message is None
    assert [n["ssid"] for n in data["scan"]] == ["Home", "Next door", "Coffee"]
    assert state.read_json(radio.path)["hotspot"] == "InkFrame-Setup"


def test_the_password_is_kept_for_this_user_only(folder, radio):
    up(folder, radio)
    assert stat.S_IMODE((folder / hotspot.FILE).stat().st_mode) == 0o600


def test_each_hotspot_gets_its_own_password(folder, radio):
    seen = set()
    for _ in range(6):
        up(folder, radio)
        seen.add(hotspot.load(folder)["password"])
        hotspot.cancel(folder, NOW)
        advance(folder, NOW, radio)
    assert len(seen) > 1


def test_held_with_wifi_working_only_clears_the_mark(folder, radio):
    state.write_json(radio.path, {"connected": True})
    hotspot.ask(folder, NOW)
    outcome = advance(folder, NOW, radio)
    assert outcome.clear_mark and outcome.key is None
    assert hotspot.load(folder)["state"] == "off" and hotspot.load(folder)["asked_at"] is None
    assert advance(folder, minutes(1), radio).clear_mark is False  # once


def test_held_where_it_cant_tell_starts_nothing(folder):
    hotspot.ask(folder, NOW)
    assert advance(folder, NOW, FakeNetwork(folder)).key is None  # no fake_wifi.json: no radio
    assert hotspot.load(folder)["state"] == "off"


def test_a_press_from_before_a_reboot_is_forgotten(folder, radio):
    hotspot.ask(folder, minutes(-6))
    assert advance(folder, NOW, radio).key is None
    assert hotspot.load(folder)["state"] == "off" and hotspot.load(folder)["asked_at"] is None


def test_no_hotspot_in_low_power(folder, radio):
    hotspot.ask(folder, NOW)
    assert advance(folder, NOW, radio, allowed=False).key is None
    assert hotspot.load(folder)["state"] == "off"


def test_low_power_ends_one_thats_up(folder, radio):
    up(folder, radio)
    assert advance(folder, minutes(1), radio, allowed=False).key is None
    assert hotspot.load(folder)["state"] == "off" and state.read_json(radio.path)["hotspot"] is None


def test_a_hotspot_that_wont_start_says_so_and_the_frame_carries_on(folder, radio):
    state.write_json(radio.path, {"connected": False, "start_error": "no AP mode"})
    outcome = up(folder, radio)
    assert outcome.key.startswith("message/wifi-setup-failed/") and outcome.error == "no AP mode"
    assert "didn't start" in outcome.message
    assert hotspot.load(folder)["state"] == "off"
    assert advance(folder, minutes(1), radio).key is None


# --- while it's up -------------------------------------------------------------------------


def test_the_same_screen_stays_up_from_tick_to_tick(folder, radio):
    first = up(folder, radio)
    assert advance(folder, minutes(1), radio).key == first.key
    assert advance(folder, minutes(2), radio).key == first.key


def test_a_press_calls_it_off(folder, radio):
    up(folder, radio)
    hotspot.cancel(folder, minutes(1))
    assert advance(folder, minutes(1), radio).key is None
    assert hotspot.load(folder) == {**hotspot.EMPTY}
    assert state.read_json(radio.path)["hotspot"] is None


def test_it_ends_by_itself_when_nobody_comes(folder, radio):
    up(folder, radio)
    assert advance(folder, minutes(14), radio).key is not None
    assert advance(folder, minutes(15), radio).key is None
    assert hotspot.load(folder)["state"] == "off"


def test_an_open_setup_page_keeps_it_up(folder, radio):
    up(folder, radio)
    hotspot.touch(folder, minutes(14))
    assert advance(folder, minutes(20), radio).key is not None
    assert advance(folder, minutes(29), radio).key is None


def test_touching_does_nothing_with_the_hotspot_down(folder):
    hotspot.touch(folder, NOW)
    assert hotspot.load(folder)["until"] is None


def test_a_reboot_ends_it_and_a_trial_with_it(folder, radio):
    hotspot.ask(folder, NOW)
    advance(folder, NOW, radio, boot="first")
    hotspot.ask_join(folder, "Home", "trial-uuid", NOW)
    assert advance(folder, minutes(1), radio, boot="second").key is None
    assert hotspot.load(folder)["state"] == "off"
    assert state.read_json(radio.path)["forgot"] == ["trial-uuid"]


def test_a_try_that_comes_too_late_is_thrown_away(folder, radio):
    hotspot.ask_join(folder, "Home", "trial-uuid", NOW)
    assert advance(folder, NOW, radio).key is None
    assert hotspot.load(folder)["join"] is None
    assert state.read_json(radio.path)["forgot"] == ["trial-uuid"]


# --- trying a network ----------------------------------------------------------------------


def test_joining_replaces_the_old_profile_and_says_so(folder, radio):
    up(folder, radio)
    hotspot.ask_join(folder, "Home", "trial-uuid", minutes(1))
    outcome = advance(folder, minutes(1), radio)
    assert outcome.key.startswith("message/wifi-connected/") and outcome.message.startswith("Connected to Home\n")
    data = hotspot.load(folder)
    assert data["state"] == "off" and data["password"] is None and data["join"] is None
    fake = state.read_json(radio.path)
    assert fake["connected"] is True and fake["replaced"] == "Home" and fake["hotspot"] is None
    assert "forgot" not in fake


def test_connected_stays_up_a_few_minutes_then_the_schedule_takes_back(folder, radio):
    up(folder, radio)
    hotspot.ask_join(folder, "Home", "trial-uuid", minutes(1))
    key = advance(folder, minutes(1), radio).key
    assert advance(folder, minutes(2), radio).key == key
    assert advance(folder, minutes(4), radio).key is None


def test_a_wrong_password_brings_the_hotspot_back_with_the_reason(folder, radio):
    first = up(folder, radio)
    password = hotspot.load(folder)["password"]
    state.write_json(radio.path, {"connected": False, "join_error": hotspot.WRONG_PASSWORD})
    hotspot.ask_join(folder, "Home", "trial-uuid", minutes(1))
    outcome = advance(folder, minutes(1), radio, name="InkFrame")
    data = hotspot.load(folder)
    assert data["state"] == "on" and data["join"] is None
    assert data["password"] == password  # so the phone rejoins by itself
    assert data["result"]["ok"] is False and "password" in data["result"]["text"]
    assert outcome.key != first.key  # the panel redraws, to say why
    fake = state.read_json(radio.path)
    assert fake["forgot"] == ["trial-uuid"] and fake["hotspot"] == "InkFrame-Setup"
    assert advance(folder, minutes(2), radio).key == outcome.key


def test_the_page_never_gets_the_hotspots_password(folder, radio):
    up(folder, radio)
    page = hotspot.for_page(hotspot.load(folder))
    assert page["on"] and page["ssid"] == "InkFrame-Setup"
    assert hotspot.load(folder)["password"] not in str(page) and "password" not in page


def test_setup_screens_are_known_as_such():
    assert hotspot.is_setup_key("setup/a/b") and hotspot.is_setup_key("message/wifi-connected/x")
    assert not hotspot.is_setup_key("message/clock-not-set") and not hotspot.is_setup_key(None)


# --- on the Pi -----------------------------------------------------------------------------


class FakeShell:
    """What nmcli and the helper are asked; `answers` maps a word in the command to an answer."""

    def __init__(self, **answers):
        self.calls: list[tuple[list[str], str | None]] = []
        self.answers = answers

    def __call__(self, argv, *, stdin=None, timeout=30):
        self.calls.append((argv, stdin))
        for word, (code, out, err) in self.answers.items():
            if word in argv:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self) -> list[str]:
        return [" ".join(argv) for argv, _ in self.calls]


def test_a_hotspot_password_never_goes_in_an_argument():
    shell = FakeShell()
    hotspot.Nmcli(run=shell).start("InkFrame-Setup", "tide-kelp-1234")
    assert not any("tide-kelp-1234" in c for c in shell.commands())
    stdin = [s for _, s in shell.calls if s]
    assert len(stdin) == 1 and "psk=tide-kelp-1234" in stdin[0]


def test_starting_replaces_the_profile_then_brings_it_up():
    shell = FakeShell()
    hotspot.Nmcli(run=shell).start("InkFrame-Setup", "tide-kelp-1234")
    commands = shell.commands()
    remove = next(i for i, c in enumerate(commands) if " remove " in c)
    add = next(i for i, c in enumerate(commands) if c.endswith(f"add {wifi.HOTSPOT_PROFILE}"))
    bring_up = next(i for i, c in enumerate(commands) if f"connection up uuid {hotspot.PROFILE_UUID}" in c)
    assert remove < add < bring_up
    assert all(c.startswith("sudo -n ") for c in commands)


def test_a_hotspot_that_wont_come_up_raises():
    shell = FakeShell(up=(4, "", "Error: Connection activation failed"))
    with pytest.raises(hotspot.HotspotFailed, match="activation failed"):
        hotspot.Nmcli(run=shell).start("InkFrame-Setup", "tide-kelp-1234")


def test_the_hotspots_keyfile():
    text = hotspot.hotspot_keyfile("Ink-Setup", "tide-kelp-1234")
    for line in ("autoconnect=false", "mode=ap", "band=bg", "key-mgmt=wpa-psk", "method=shared",
                 f"address1={hotspot.ADDRESS}/24", f"uuid={hotspot.PROFILE_UUID}", "psk=tide-kelp-1234"):
        assert line in text.splitlines()
    assert "ssid=" + "".join(f"{b};" for b in b"Ink-Setup") in text.splitlines()


@pytest.mark.parametrize("stderr, why", [
    ("Error: Connection activation failed: Secrets were required, but not provided", hotspot.WRONG_PASSWORD),
    ("Error: Connection activation failed: The Wi-Fi network could not be found", hotspot.NOT_FOUND),
    ("Error: No network with SSID 'x' found.", hotspot.NOT_FOUND),
    ("Error: Timeout expired (40 seconds)", hotspot.FAILED),
])
def test_why_a_join_failed(stderr, why):
    assert hotspot.Nmcli(run=FakeShell(up=(4, "", stderr))).join("some-uuid") == why


def test_a_join_that_works():
    assert hotspot.Nmcli(run=FakeShell()).join("some-uuid") is None


def test_the_scan_keeps_the_strongest_of_each_name():
    text = "Home:40:WPA2\nHome:80:WPA2\nCafe\\:Bar:30:\n:90:WPA2\nGuest:bad:WPA2\nOld:20:--\n"
    assert hotspot.parse_scan(text) == [
        {"ssid": "Home", "signal": 80, "secure": True},
        {"ssid": "Cafe:Bar", "signal": 30, "secure": False},
        {"ssid": "Old", "signal": 20, "secure": False},
    ]


def test_a_failed_scan_is_an_empty_list():
    assert hotspot.Nmcli(run=FakeShell(wifi=(1, "", "busy"))).scan() == []


def test_the_scan_leaves_out_the_frames_own_hotspot(folder, radio, monkeypatch):
    monkeypatch.setattr(FakeNetwork, "SCAN", ({"ssid": "InkFrame-Setup", "signal": 99, "secure": True},
                                              {"ssid": "Home", "signal": 50, "secure": True}))
    up(folder, radio)
    assert [n["ssid"] for n in hotspot.load(folder)["scan"]] == ["Home"]


def test_a_long_frame_name_still_makes_a_legal_ssid():
    assert len(hotspot.setup_ssid("x" * 63).encode()) == wifi.MAX_SSID_BYTES


# --- the mark ------------------------------------------------------------------------------


def blank():
    return np.full((480, 800), INK["white"], np.uint8)


def box_of(pixels):
    rows, cols = np.nonzero(pixels != INK["white"])
    return rows.min(), rows.max(), cols.min(), cols.max()


def test_the_mark_alone_sits_in_the_corner():
    top, bottom, left, right = box_of(battery_badge.draw_no_wifi(blank()))
    height = battery_badge.box_size()[1]
    assert (bottom, right) == (480 - battery_badge.MARGIN - 1, 800 - battery_badge.MARGIN - 1)
    assert (bottom - top + 1, right - left + 1) == (height, height)


def test_the_mark_steps_left_of_the_crescents_place_for_the_battery():
    width, height = battery_badge.box_size()
    screen = battery_badge.draw(battery_badge.draw_low_power(blank()), 50)
    both = battery_badge.draw_no_wifi(screen, battery_up=True)
    _, _, left, _ = box_of(both)
    assert left == 800 - battery_badge.MARGIN - width - battery_badge.MARK_GAP - height - battery_badge.MARK_GAP - height
    only = both.copy()
    only[np.nonzero(screen != INK["white"])] = INK["white"]
    assert box_of(only)[3] < box_of(screen)[2]  # none of it on the other two


def test_the_mark_is_black_and_red_on_white():
    assert set(np.unique(battery_badge.draw_no_wifi(blank()))) == {INK["black"], INK["white"], INK["red"]}


def test_the_mark_needs_a_real_corner():
    with pytest.raises(ValueError):
        battery_badge.draw_no_wifi(blank(), "middle")


# --- the setup screen ----------------------------------------------------------------------


def context(folder, **changes):
    config = default_config()
    return SceneContext.from_config(config, state_dir=folder)


def draw(folder, ctx=None):
    return render_scene(Scene("setup", "setup/x", NOW), ctx or context(folder))


def test_the_qr_code_says_join_this_network():
    assert setup_scene.wifi_qr_text("Dals;Frame", "a:b") == "WIFI:T:WPA;S:Dals\\;Frame;P:a\\:b;;"


def test_the_setup_screen_has_a_big_code_and_stays_on_the_canvas(folder, radio, monkeypatch):
    monkeypatch.setattr(hotspot, "frame_name", lambda: "The-Frame-With-A-Long-Name")
    hotspot.ask(folder, NOW)
    advance(folder, NOW, radio)
    state.write_json(folder / hotspot.FILE, {**hotspot.load(folder), "result": {
        "ok": False, "ssid": "A network with quite a long name", "text": hotspot.WHY[hotspot.NOT_FOUND]}})
    from dataclasses import replace

    from photopainter.config import Wifi
    config = replace(default_config(), wifi=Wifi(help="Stuck? Call Sam at 555-0100, any time of the day at all"))
    pixels = draw(folder, SceneContext.from_config(config, state_dir=folder))
    assert pixels.shape == (480, 800)
    code = pixels[100:400, 40:330]
    assert 0.3 < (code == INK["black"]).mean() < 0.7  # a code's worth of black
    assert (pixels[-8:] == INK["white"]).all()  # nothing has run off the bottom
    assert (pixels[:, -6:][10:] == INK["white"]).all()


def test_the_setup_screen_in_portrait(folder, radio):
    from dataclasses import replace

    up(folder, radio)
    config = default_config()
    config = replace(config, display=replace(config.display, portrait=True))
    pixels = draw(folder, SceneContext.from_config(config, state_dir=folder))
    assert pixels.shape == (800, 480)
    assert (pixels[-6:] == INK["white"]).all()


def test_the_words_are_enough_without_the_qr_library(folder, radio, monkeypatch):
    up(folder, radio)
    monkeypatch.setattr(setup_scene, "_matrix", lambda text: None)
    pixels = draw(folder)
    assert (pixels[100:400, 40:200] == INK["white"]).mean() > 0.5


def test_a_preview_with_no_hotspot_up_makes_up_its_words(folder, monkeypatch):
    seen = []
    monkeypatch.setattr(setup_scene, "_matrix", lambda text: seen.append(text) or None)
    draw(folder)
    assert setup_scene.SAMPLE_PASSWORD in seen[0]


# --- the help line -------------------------------------------------------------------------


def test_the_help_line_is_a_setting_and_may_be_empty():
    assert validate({}).wifi.help == ""
    assert validate({"wifi": {"help": "Call Sam"}}).wifi.help == "Call Sam"
    assert validate({"wifi": {"help": ""}}).to_dict()["wifi"] == {"help": ""}
    with pytest.raises(ConfigError):
        validate({"wifi": {"help": 3}})


# --- the setup page ------------------------------------------------------------------------


class Ticks:
    def __init__(self):
        self.requested = 0

    def request(self):
        self.requested += 1

    def snapshot(self):
        return {"running": False, "queued": False}

    def close(self, wait_s=0):
        return True


@pytest.fixture
def page():
    ticks = Ticks()
    app = server.create_app(fake_panel=True, clock=lambda: NOW, ticks=ticks, render=lambda args, out: None,
                            sun=lambda day: None)
    client = app.test_client()
    client.ticks = ticks
    return client


PHONE = {"REMOTE_ADDR": "10.42.0.23"}


def test_what_phones_ask_for_goes_in_the_journal(page, folder, radio, caplog):
    up(folder, radio)
    with caplog.at_level("INFO", logger="photopainter.web.server"):
        page.get("/generate_204", environ_base=PHONE, headers={"Host": "connectivitycheck.gstatic.com"})
        page.get("/setup", environ_base=PHONE)
        page.post("/api/setup/join", environ_base=PHONE, json={"ssid": "Home", "password": "correct horse"})
    text = caplog.text
    assert "10.42.0.23 asked for connectivitycheck.gstatic.com/generate_204" in text
    assert "setup page opened by 10.42.0.23" in text and "asked to try Home" in text
    assert "correct horse" not in text


def test_a_phone_on_the_hotspot_is_sent_to_the_setup_page(page, folder, radio):
    up(folder, radio)
    for path in ("/", "/hotspot-detect.html", "/generate_204", "/api/status", "/static/app.js"):
        r = page.get(path, environ_base=PHONE, headers={"Host": "captive.apple.com"})
        assert r.status_code == 302 and r.headers["Location"] == hotspot.SETUP_URL, path
        assert r.headers["Cache-Control"] == "no-store"


def test_the_same_goes_for_an_ipv6_mapped_address(page, folder, radio):
    up(folder, radio)
    assert page.get("/", environ_base={"REMOTE_ADDR": "::ffff:10.42.0.9"}).status_code == 302


def test_the_setup_page_itself_opens(page, folder, radio):
    up(folder, radio)
    r = page.get("/setup", environ_base=PHONE)
    assert r.status_code == 200 and b"Connect the frame to Wi-Fi" in r.data


def test_nobody_else_is_redirected(page, folder, radio):
    up(folder, radio)
    assert page.get("/", environ_base={"REMOTE_ADDR": "192.168.86.40"}).status_code == 200


def test_with_the_hotspot_down_that_address_gets_the_dashboard(page, folder):
    assert page.get("/", environ_base=PHONE).status_code == 200


def test_the_page_gets_the_networks_and_keeps_the_hotspot_up(page, folder, radio):
    up(folder, radio)
    until = hotspot.load(folder)["until"]
    r = page.get("/api/setup", environ_base=PHONE)
    body = r.get_json()
    assert r.status_code == 200 and body["on"] and body["joining"] is False
    assert [n["ssid"] for n in body["networks"]] == ["Home", "Next door", "Coffee"]
    assert hotspot.load(folder)["password"] not in r.text
    assert hotspot.load(folder)["until"] == until  # the same moment here; moved on by a later test
    state.write_json(folder / hotspot.FILE, {**hotspot.load(folder), "until": state.to_iso(minutes(1))})
    page.get("/api/setup", environ_base=PHONE)
    assert hotspot.load(folder)["until"] == state.to_iso(minutes(hotspot.IDLE_MINUTES))


def test_trying_a_network_asks_for_a_tick_and_keeps_no_password(page, folder, radio):
    up(folder, radio)
    r = page.post("/api/setup/join", environ_base=PHONE, json={"ssid": " Home ", "password": "correct horse"})
    assert r.status_code == 202 and r.get_json()["ssid"] == "Home" and page.ticks.requested == 1
    join = hotspot.load(folder)["join"]
    assert join["ssid"] == "Home" and join["uuid"]
    assert "correct horse" not in (folder / hotspot.FILE).read_text()


def test_a_second_try_waits_for_the_first(page, folder, radio):
    up(folder, radio)
    page.post("/api/setup/join", environ_base=PHONE, json={"ssid": "Home", "password": "correct horse"})
    r = page.post("/api/setup/join", environ_base=PHONE, json={"ssid": "Home", "password": "correct horse"})
    assert r.status_code == 409 and page.ticks.requested == 1


@pytest.mark.parametrize("body", [{"ssid": "", "password": "correct horse"}, {"ssid": "Home", "password": "short"}])
def test_a_network_that_cant_be_used_is_told_to_the_phone(page, folder, radio, body):
    up(folder, radio)
    r = page.post("/api/setup/join", environ_base=PHONE, json=body)
    assert r.status_code == 400 and r.get_json()["error"] and page.ticks.requested == 0
    assert hotspot.load(folder)["join"] is None


def test_trying_with_no_hotspot_up_is_refused(page, folder):
    r = page.post("/api/setup/join", json={"ssid": "Home", "password": "correct horse"})
    assert r.status_code == 409 and "back button" in r.get_json()["error"]


# --- the trial profile ---------------------------------------------------------------------


def test_a_trial_may_share_a_name_the_frame_knows():
    shell = FakeShell()
    uid = wifi.add_trial("Home", "new password", run=shell)
    argv, stdin = shell.calls[-1]
    assert argv[3] == "add" and f"uuid={uid}" in stdin and "psk=new password" in stdin
    assert "new password" not in " ".join(argv)


def test_the_hotspot_isnt_listed_as_a_saved_network():
    shell = FakeShell(show=(0, f"{wifi.HOTSPOT_PROFILE}:{hotspot.PROFILE_UUID}:802-11-wireless:no\n", ""))
    assert wifi.saved(run=shell) == []
