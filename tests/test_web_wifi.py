"""The dashboard's Wi-Fi networks (web/wifi.py, /api/wifi): the checks, the keyfile, the
API with nmcli and the root helper faked, the list in memory on the Mac, and the helper's
text. No nmcli runs here, and nothing secret may show up in a response or a log record."""

import configparser
import logging
import subprocess
from pathlib import Path

import pytest

from photopainter.web import wifi
from test_web import Site

HELPER = wifi.SCRIPT
PASSWORD = "correct horse battery"  # 21 characters, with spaces
CONNECTED = "371ec6ef-341d-3eaa-ae90-1d3e8c048311"
OTHER = "29441042-c503-4b85-aa24-b70fcf26d5c3"

# nmcli on the Pi (2026-11-05), plus a second saved network whose name has an escaped colon
LIST = (f"netplan-wlan0-Home_2.4GHz:{CONNECTED}:802-11-wireless:yes\n"
        "tailscale0:73e21ad9-b70c-46fe-b473-08eecd78d928:tun:yes\n"
        "lo:6a5fd432-290c-4627-8f62-4359add89e18:loopback:yes\n"
        f"Dad\\:s Wi-Fi:{OTHER}:802-11-wireless:no\n")
DETAILS = {
    CONNECTED: "802-11-wireless.ssid:Home_2.4GHz\n802-11-wireless.hidden:no\n",
    OTHER: "802-11-wireless.ssid:Dad:s Wi-Fi\n802-11-wireless.hidden:yes\n",
}


class FakePi:
    """Answers nmcli as the Pi does and records what the root helper is asked to do."""

    def __init__(self):
        self.helper_calls: list[tuple[list[str], str | None]] = []
        self.helper_result = (0, "", "")

    def __call__(self, argv, *, stdin=None, timeout=30):
        assert "--show-secrets" not in argv and "-s" not in argv
        if argv[0] == "nmcli":
            if argv[1:] == ["-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show"]:
                return subprocess.CompletedProcess(argv, 0, LIST, "")
            if argv[-3:-1] == ["show", "uuid"]:
                return subprocess.CompletedProcess(argv, 0, DETAILS[argv[-1]], "")
            raise AssertionError(f"unexpected nmcli call {argv}")
        assert argv[:3] == ["sudo", "-n", str(HELPER)], argv
        self.helper_calls.append((argv[3:], stdin))
        code, out, err = self.helper_result
        return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A site as on the Pi (no fake panel), with nmcli and sudo answered by FakePi."""
    fake = FakePi()
    monkeypatch.setattr(wifi, "_run", fake)
    site = Site(tmp_path, fake_panel=False)
    site.pi = fake
    return site


# --- checks --------------------------------------------------------------------------------


@pytest.mark.parametrize("ssid", ["Home", " Dad's Wi-Fi ", "x" * 32, "Café", "12345678"])
def test_good_ssids(ssid):
    assert wifi.check_ssid(ssid) == ssid.strip()


@pytest.mark.parametrize("ssid", [None, 7, "", "   ", "x" * 33, "é" * 17, "a\nb", "tab\there"])
def test_bad_ssids(ssid):
    with pytest.raises(wifi.BadNetwork):
        wifi.check_ssid(ssid)


@pytest.mark.parametrize("password, expect", [
    (None, None), ("", None), ("12345678", "12345678"), (" spaces ok ", " spaces ok "), ("x" * 63, "x" * 63),
])
def test_passwords(password, expect):
    assert wifi.check_password(password) == expect


@pytest.mark.parametrize("password", [1234567890, "1234567", "x" * 64, "pässword", "line\nbreak"])
def test_bad_passwords(password):
    with pytest.raises(wifi.BadNetwork):
        wifi.check_password(password)


# --- the keyfile ---------------------------------------------------------------------------


def test_keyfile_for_a_protected_hidden_network():
    text = wifi.keyfile("Dad's Wi-Fi", "Dad's Wi-Fi", "  pass\\word", True, OTHER)
    assert text.startswith(f"[connection]\nid=Dad's Wi-Fi\nuuid={OTHER}\ntype=wifi\n\n[wifi]\n")
    assert "ssid=68;97;100;39;115;32;87;105;45;70;105;\n" in text  # D a d ' s _ W i - F i, as bytes
    assert "hidden=true" in text
    assert "[wifi-security]\nkey-mgmt=wpa-psk\npsk=\\s\\spass\\\\word\n" in text  # GKeyFile escapes
    assert "[ipv4]\nmethod=auto\n" in text and "[ipv6]\nmethod=auto\n" in text
    assert text.endswith("\n")


def test_keyfile_for_an_open_network_has_no_security_section():
    text = wifi.keyfile("Cafe", "Cafe", None, False, OTHER)
    assert "[wifi-security]" not in text and "psk" not in text and "hidden" not in text


def test_keyfile_reads_back_as_an_ini_file():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(wifi.keyfile("Home", "Home", "secret-words", False, OTHER))
    assert cp.sections() == ["connection", "wifi", "wifi-security", "ipv4", "ipv6"]
    assert cp["connection"]["id"] == "Home" and cp["wifi-security"]["psk"] == "secret-words"


def test_terse_lines_split_with_escapes():
    assert wifi._split_terse("Dad\\:s Wi-Fi:uuid:802-11-wireless:no") == ["Dad:s Wi-Fi", "uuid", "802-11-wireless", "no"]
    assert wifi._split_terse("back\\\\slash:x") == ["back\\slash", "x"]
    assert wifi._split_terse("") == [""]


def test_saved_from_nmcli_connected_first():
    nets = wifi.saved(run=FakePi())
    assert [(n.name, n.ssid, n.connected, n.hidden, n.uuid) for n in nets] == [
        ("netplan-wlan0-Home_2.4GHz", "Home_2.4GHz", True, False, CONNECTED),
        ("Dad:s Wi-Fi", "Dad:s Wi-Fi", False, True, OTHER),
    ]


# --- the API on the Pi, nmcli and the helper faked -----------------------------------------


def test_list_from_nmcli(pi):
    r = pi.get("/api/wifi")
    assert r.status_code == 200
    body = r.get_json()
    assert body["connected"] == "netplan-wlan0-Home_2.4GHz"
    assert body["networks"] == [
        {"name": "netplan-wlan0-Home_2.4GHz", "ssid": "Home_2.4GHz", "connected": True, "hidden": False,
         "uuid": CONNECTED},
        {"name": "Dad:s Wi-Fi", "ssid": "Dad:s Wi-Fi", "connected": False, "hidden": True, "uuid": OTHER},
    ]
    assert pi.pi.helper_calls == []


def test_add_runs_the_helper_with_the_keyfile_on_stdin(pi, caplog):
    caplog.set_level(logging.DEBUG)
    pi.pi.helper_result = (0, "some-uuid\n", "")
    r = pi.client.post("/api/wifi", json={"ssid": "Beach House", "password": PASSWORD, "hidden": False})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["network"]["ssid"] == "Beach House" and body["network"]["connected"] is False
    assert "in range" in body["message"]
    assert [n["ssid"] for n in body["networks"]] == ["Home_2.4GHz", "Dad:s Wi-Fi"]  # listed afresh (the fake doesn't learn)
    [(args, stdin)] = pi.pi.helper_calls
    assert len(args) == 2 and args[0] == "add" and args[1].startswith("Beach_House-")
    assert f"psk={PASSWORD}" in stdin and "ssid=66;101;97;99;104;32;72;111;117;115;101;" in stdin
    assert "hidden" not in stdin
    assert PASSWORD not in " ".join(args)
    assert PASSWORD not in r.get_data(as_text=True)
    assert PASSWORD not in caplog.text
    assert "Beach House" in caplog.text  # the name is logged, nothing more


def test_add_an_open_hidden_network(pi):
    r = pi.client.post("/api/wifi", json={"ssid": "Library", "hidden": True})
    assert r.status_code == 201
    [(args, stdin)] = pi.pi.helper_calls
    assert "[wifi-security]" not in stdin and "hidden=true" in stdin
    assert r.get_json()["network"]["hidden"] is True


@pytest.mark.parametrize("body, words", [
    ({"ssid": "Home_2.4GHz", "password": PASSWORD}, "already knows"),
    ({"ssid": "Dad:s Wi-Fi", "password": PASSWORD}, "already knows"),
    ({"ssid": "", "password": PASSWORD}, "Type the network"),
    ({"ssid": "New", "password": "short"}, "8 to 63"),
    ({"ssid": "New", "password": PASSWORD, "hidden": "yes"}, "true or false"),
    ({"ssid": "x" * 33}, "at most 32"),
])
def test_add_refuses_before_running_anything(pi, body, words):
    r = pi.client.post("/api/wifi", json=body)
    assert r.status_code == 400 and words in r.get_json()["error"]
    assert pi.pi.helper_calls == []
    assert PASSWORD not in r.get_data(as_text=True)


def test_add_needs_a_json_object(pi):
    r = pi.client.post("/api/wifi", data="nope", content_type="application/json")
    assert r.status_code == 400 and pi.pi.helper_calls == []


def test_remove_runs_the_helper_by_uuid(pi):
    r = pi.client.delete("/api/wifi/Dad%3As%20Wi-Fi")
    assert r.status_code == 200, r.get_json()
    assert pi.pi.helper_calls == [(["remove", OTHER], None)]
    assert "Dad:s Wi-Fi" in r.get_json()["message"]
    assert r.get_json()["connected"] == "netplan-wlan0-Home_2.4GHz"


def test_remove_refuses_the_connected_network_and_unknown_names(pi):
    r = pi.client.delete("/api/wifi/netplan-wlan0-Home_2.4GHz")
    assert r.status_code == 409 and "connected" in r.get_json()["error"]
    r = pi.client.delete("/api/wifi/Nowhere")
    assert r.status_code == 404
    assert pi.pi.helper_calls == []


def test_a_failed_helper_is_an_error_without_the_password(pi, caplog):
    caplog.set_level(logging.DEBUG)
    pi.pi.helper_result = (1, "", "NetworkManager didn't take the profile")
    r = pi.client.post("/api/wifi", json={"ssid": "Beach House", "password": PASSWORD})
    assert r.status_code == 500 and "didn't take" in r.get_json()["error"]
    assert PASSWORD not in r.get_data(as_text=True) and PASSWORD not in caplog.text


def test_without_nmcli_the_list_says_so(tmp_path, monkeypatch):
    def no_nmcli(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(wifi, "_run", no_nmcli)
    r = Site(tmp_path, fake_panel=False).get("/api/wifi")
    assert r.status_code == 500 and "nmcli" in r.get_json()["error"]


# --- with a fake panel: a list in memory ---------------------------------------------------


def test_fake_panel_never_runs_anything(tmp_path, monkeypatch, caplog):
    def no(*a, **k):
        raise AssertionError("ran a command")

    monkeypatch.setattr(wifi, "_run", no)
    caplog.set_level(logging.DEBUG)
    s = Site(tmp_path, fake_panel=True)
    body = s.get("/api/wifi").get_json()
    assert body["connected"] == "Home" and [n["ssid"] for n in body["networks"]] == ["Home"]
    r = s.client.post("/api/wifi", json={"ssid": "Beach House", "password": PASSWORD, "hidden": True})
    assert r.status_code == 201
    assert [(n["ssid"], n["connected"], n["hidden"]) for n in r.get_json()["networks"]] == \
        [("Home", True, False), ("Beach House", False, True)]
    assert PASSWORD not in r.get_data(as_text=True) and PASSWORD not in caplog.text
    assert s.client.post("/api/wifi", json={"ssid": "Beach House"}).status_code == 400
    assert s.client.delete("/api/wifi/Home").status_code == 409
    r = s.client.delete("/api/wifi/Beach%20House")
    assert r.status_code == 200 and [n["ssid"] for n in r.get_json()["networks"]] == ["Home"]
    assert s.client.delete("/api/wifi/Beach%20House").status_code == 404


# --- the helper and the page ---------------------------------------------------------------


def test_helper_script_text():
    text = HELPER.read_text()
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "/etc/NetworkManager/system-connections" in text
    assert "mktemp" in text and "chmod 600" in text
    assert "nmcli connection load" in text and "nmcli connection delete uuid" in text
    assert "connection show --active" in text  # never the connected one
    assert 'cat "$tmp"' not in text and 'echo "$psk"' not in text  # nothing from stdin is printed
    assert HELPER.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(HELPER)], check=True)


@pytest.mark.parametrize("args", [["add", "../etc"], ["add", ""], ["add", "-rf"], ["remove", "not-a-uuid"], ["frobnicate"]])
def test_helper_refuses_bad_arguments_before_touching_anything(args):
    # the argument checks come before anything that needs root, so they can run here
    done = subprocess.run(["bash", str(HELPER), *args], input="", capture_output=True, text=True)
    assert done.returncode == 2 and done.stdout == "", (args, done.stderr)


def test_page_has_the_group():
    static = Path(wifi.__file__).parent / "static"
    html = (static / "index.html").read_text()
    assert 'id="wifi-tool"' in html and 'id="wifi-password"' in html and "2.4 GHz" in html
    js = (static / "app.js").read_text()
    assert '"/api/wifi"' in js and "initWifi()" in js
