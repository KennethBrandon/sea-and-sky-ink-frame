"""No Wi-Fi: knowing it's gone, the mark on the screen, and the setup hotspot.

**An outage needs nobody.** Tides are cached for weeks, and photos and the sky are local, so
the frame carries on, and NetworkManager joins a saved network again when it comes back. Nothing
here turns the hotspot on by itself, because the radio can't look for the home network while
it's up. After `MARK_AFTER_MINUTES` without Wi-Fi the screens drawn from then on carry a small
mark (`render/battery_badge.py`), with no refresh of its own, and it goes at the next redraw
once Wi-Fi is back. A router that's up with its internet out gets no mark: the dashboard still
works, and a hotspot wouldn't help. Nor does low power, where Wi-Fi is off on purpose.

**Holding the back button is the way in** (`buttons.py`), for a new router, a new password or a
new house, where nothing comes back by itself. With Wi-Fi working it only redraws the screen to
clear a mark left up. Otherwise a tick looks at what networks are around, puts up a hotspot and
shows `scenes/setup.py`: a QR code that joins the hotspot, whose password is on the screen and
new each time, because the dashboard has no login and someone who can read the frame is someone
in the house. Any press calls it off, and it ends by itself `IDLE_MINUTES` after the setup page
was last open.

**Joining.** A phone that joins is sent to `web/static/setup.html` (`web/server.py`), which saves
the network it's given as a trial profile and asks for a tick. There's one radio, so the tick
takes the hotspot down to try it and the phone drops off, which is why the result goes on the
panel, not the phone. If it works, older profiles of that name go (the password changed) and the
panel says so for `RESULT_MINUTES`. If not, the trial goes, so a wrong password can't stick, and
the hotspot comes back with the reason on the screen and the same password, so the phone rejoins.

`advance()` does one tick's worth of that: the state in `hotspot.json` (the password, so mode
0600 like every state file; `state.write_atomic`), a `Nmcli` that does the work or a `FakeNetwork`
for tests and `--fake-panel`, and an `Outcome` the tick acts on. The file is patched rather than
rewritten, because the server and the buttons write to it too. A reboot ends a hotspot: its
profile never connects by itself, so the frame just goes back to looking for its networks.
"""

from __future__ import annotations

import fcntl
import logging
import secrets
import socket
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from photopainter import state
from photopainter.web import wifi

log = logging.getLogger(__name__)

FILE = "hotspot.json"
FAKE_FILE = "fake_wifi.json"
IFACE = "wlan0"
ADDRESS = "10.42.0.1"  # the frame on its hotspot
SUBNET_PREFIX = "10.42.0."
SETUP_URL = f"http://{ADDRESS}/setup"
MARK_AFTER_MINUTES = 10  # long enough that a router's reboot never shows
IDLE_MINUTES = 15  # how long the hotspot waits, from when the setup page was last open
RESULT_MINUTES = 3  # how long "Connected" stays up, about two panel refreshes
ASK_EXPIRES_MINUTES = 5  # a press this old is forgotten (it came before a reboot)
SSID_SUFFIX = "-Setup"
MAX_SCAN = 20
PROFILE_UUID = "0f4c5b1e-6a53-4d3e-9b1a-7c2f5d8e4a10"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
SIOCGIFADDR = 0x8915

WRONG_PASSWORD, NOT_FOUND, FAILED = "wrong_password", "not_found", "failed"
WHY = {
    WRONG_PASSWORD: "The password didn't work.",
    NOT_FOUND: "The frame can't find that network. It only hears 2.4 GHz Wi-Fi.",
    FAILED: "It didn't connect.",
}

# Words for the hotspot's password: short, sea-side and easy to tell apart on the panel.
WORDS = ("tide", "kelp", "wave", "surf", "reef", "gull", "sand", "moon", "star", "shell", "pier", "dune",
         "cove", "buoy", "crab", "tern", "swell", "shore", "coral", "pearl", "otter", "whale", "squid", "drift",
         "sail", "mast", "knot", "keel", "bay", "foam", "salt", "wind")
EMPTY: dict[str, Any] = {"state": "off", "asked_at": None, "cancel_at": None, "join": None, "since": None,
                         "until": None, "boot_id": None, "ssid": None, "password": None, "scan": [], "result": None}


class HotspotFailed(RuntimeError):
    """The hotspot wouldn't start."""


# --- whether the frame is on Wi-Fi ---------------------------------------------------------


def _has_ipv4(iface: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", iface.encode()[:15]))
        except OSError:
            return False
    return True


def link_up(iface: str = IFACE, sys_net: Path = Path("/sys/class/net")) -> bool | None:
    """On a network with an address? None where there's no such radio (the Mac). A file read
    and one ioctl, as cheap as nothing in a tick that runs every minute."""
    try:
        operstate = (sys_net / iface / "operstate").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return operstate == "up" and _has_ipv4(iface)


def mark_due(down_since: datetime | None, now: datetime) -> bool:
    return down_since is not None and now - down_since >= timedelta(minutes=MARK_AFTER_MINUTES)


# --- what does the work --------------------------------------------------------------------


class Network(Protocol):
    def connected(self) -> bool | None: ...
    def scan(self) -> list[dict]: ...
    def start(self, ssid: str, password: str) -> None: ...
    def stop(self) -> None: ...
    def join(self, uuid: str) -> str | None: ...  # None, or why it failed (WHY's keys)
    def forget(self, uuid: str) -> None: ...
    def forget_others(self, ssid: str, keep_uuid: str) -> None: ...


def parse_scan(text: str) -> list[dict]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY device wifi list` to one entry a name, strongest first.
    It's what the frame's radio hears, so a network on 5 GHz alone isn't on it."""
    best: dict[str, dict] = {}
    for line in text.splitlines():
        parts = wifi._split_terse(line)
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            continue
        if parts[0] not in best or signal > best[parts[0]]["signal"]:
            best[parts[0]] = {"ssid": parts[0], "signal": signal, "secure": parts[2] not in ("", "--")}
    return sorted(best.values(), key=lambda n: (-n["signal"], n["ssid"].lower()))[:MAX_SCAN]


def hotspot_keyfile(ssid: str, password: str) -> str:
    """The hotspot's NetworkManager keyfile. `method=shared` runs dnsmasq for it, with
    `deploy/network/photopainter-captive.conf` answering every name with the frame. PMF off
    and plain WPA2, as `nmcli device wifi hotspot` sets for Wi-Fi chips like this one."""
    return "\n".join(["[connection]", f"id={wifi.HOTSPOT_PROFILE}", f"uuid={PROFILE_UUID}", "type=wifi",
                      "autoconnect=false", f"interface-name={IFACE}", "",
                      "[wifi]", "mode=ap", "band=bg", "channel=6",
                      "ssid=" + "".join(f"{b};" for b in ssid.encode("utf-8")), "",
                      "[wifi-security]", "key-mgmt=wpa-psk", "proto=rsn;", "pairwise=ccmp;", "group=ccmp;", "pmf=1",
                      f"psk={wifi._escape(password)}", "",
                      "[ipv4]", "method=shared", f"address1={ADDRESS}/24", "",
                      "[ipv6]", "method=disabled", ""])


def _reason(stderr: str) -> str:
    text = stderr.lower()
    if "secrets" in text or "password" in text or "802-1x" in text or "802.1x" in text:
        return WRONG_PASSWORD
    if "not be found" in text or "no network with ssid" in text or "not found" in text:
        return NOT_FOUND
    return FAILED


class Nmcli:
    """On the Pi. sudo needs no password for `pi` (CLAUDE.md). A password only ever goes to
    `scripts/pi/wifi-connection` on its stdin, never in an argument."""

    def __init__(self, run=None):
        self.run = run or wifi._run

    def _root(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return self.run(["sudo", "-n", "nmcli", *args], timeout=timeout)

    def connected(self) -> bool | None:
        return link_up()

    def scan(self) -> list[dict]:
        try:
            done = self._root("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes",
                              timeout=25)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("couldn't look for Wi-Fi networks: %s", exc)
            return []
        if done.returncode != 0:
            log.warning("couldn't look for Wi-Fi networks: %s", done.stderr.strip())
            return []
        return parse_scan(done.stdout)

    def start(self, ssid: str, password: str) -> None:
        self.stop()
        try:
            if self._root("-g", "connection.id", "connection", "show", "uuid", PROFILE_UUID).returncode == 0:
                wifi._helper(["remove", PROFILE_UUID], None, self.run)
            wifi._helper(["add", wifi.HOTSPOT_PROFILE], hotspot_keyfile(ssid, password), self.run)
            done = self._root("--wait", "30", "connection", "up", "uuid", PROFILE_UUID, timeout=45)
        except (OSError, subprocess.SubprocessError, wifi.WifiFailed) as exc:
            raise HotspotFailed(str(exc)) from exc
        if done.returncode != 0:
            raise HotspotFailed(done.stderr.strip() or f"nmcli exit {done.returncode}")

    def stop(self) -> None:
        """Take the hotspot down, if it's up. NetworkManager then joins a saved network it finds."""
        try:
            self._root("connection", "down", "uuid", PROFILE_UUID)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("couldn't take the hotspot down: %s", exc)

    def join(self, uuid: str) -> str | None:
        try:
            done = self._root("--wait", "40", "connection", "up", "uuid", uuid, timeout=55)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("couldn't try the network: %s", exc)
            return FAILED
        return None if done.returncode == 0 else _reason(done.stderr)

    def forget(self, uuid: str) -> None:
        try:
            wifi._helper(["remove", uuid], None, self.run)
        except wifi.WifiFailed as exc:
            log.warning("%s", exc)

    def forget_others(self, ssid: str, keep_uuid: str) -> None:
        try:
            nets = wifi.saved(run=self.run)
        except wifi.WifiFailed as exc:
            log.warning("%s", exc)
            return
        for n in nets:
            if n.ssid == ssid and n.uuid != keep_uuid and not n.connected:
                self.forget(n.uuid)


class FakeNetwork:
    """Tests and `--fake-panel`: `fake_wifi.json` in the state dir says `{"connected": false}`, and
    `"join_error": "wrong_password"` makes a join fail. No file: no radio, like the Mac."""

    SCAN = ({"ssid": "Home", "signal": 80, "secure": True}, {"ssid": "Next door", "signal": 45, "secure": True},
            {"ssid": "Coffee", "signal": 30, "secure": False})

    def __init__(self, folder: Path):
        self.path = folder / FAKE_FILE

    def _read(self) -> dict:
        data = state.read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def _set(self, **fields: Any) -> None:
        state.write_json(self.path, {**self._read(), **fields})

    def connected(self) -> bool | None:
        value = self._read().get("connected")
        return value if isinstance(value, bool) else None

    def scan(self) -> list[dict]:
        return [dict(n) for n in self.SCAN]

    def start(self, ssid: str, password: str) -> None:
        error = self._read().get("start_error")
        if error:
            raise HotspotFailed(str(error))
        self._set(hotspot=ssid)

    def stop(self) -> None:
        self._set(hotspot=None)

    def join(self, uuid: str) -> str | None:
        error = self._read().get("join_error")
        if error:
            return str(error)
        self._set(connected=True)
        return None

    def forget(self, uuid: str) -> None:
        self._set(forgot=[*self._read().get("forgot", []), uuid])

    def forget_others(self, ssid: str, keep_uuid: str) -> None:
        self._set(replaced=ssid)


# --- the state file ------------------------------------------------------------------------


def load(folder: Path) -> dict:
    data = state.read_json(folder / FILE, {})
    return {**EMPTY, **(data if isinstance(data, dict) else {})}


def _patch(folder: Path, **fields: Any) -> dict:
    with state.update_json(folder / FILE, dict(EMPTY)) as data:
        data.update(fields)
    return load(folder)


def _off(**fields: Any) -> dict:
    """What turning it off writes. A press or a join that came meanwhile goes with it."""
    return {**EMPTY, **fields}


def is_on(folder: Path) -> bool:
    return load(folder)["state"] == "on"


def ask(folder: Path, at: datetime) -> None:
    """The back button was held."""
    _patch(folder, asked_at=at)


def cancel(folder: Path, at: datetime) -> None:
    """A button was pressed with the hotspot up."""
    _patch(folder, cancel_at=at)


def ask_join(folder: Path, ssid: str, uuid: str, at: datetime) -> None:
    """The setup page saved a trial profile for the next tick to try."""
    _patch(folder, join={"ssid": ssid, "uuid": uuid, "at": at})


def touch(folder: Path, at: datetime) -> None:
    """The setup page is open: the hotspot waits for it."""
    with state.update_json(folder / FILE, dict(EMPTY)) as data:
        if data.get("state") == "on":
            data["until"] = at + timedelta(minutes=IDLE_MINUTES)


def for_page(data: dict) -> dict:
    """What the setup page gets. Whoever reads it is on the hotspot already, but the password
    has no reason to travel."""
    return {"on": data["state"] == "on", "ssid": data["ssid"], "networks": data["scan"], "result": data["result"],
            "joining": data["join"] is not None}


def make_password() -> str:
    """Two words and four digits: about 22 bits, and new each time the hotspot starts."""
    return f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(10000):04d}"


def frame_name() -> str:
    return socket.gethostname().split(".")[0] or "frame"


def setup_ssid(name: str) -> str:
    return f"{name[:wifi.MAX_SSID_BYTES - len(SSID_SUFFIX)]}{SSID_SUFFIX}"


def boot_id() -> str | None:
    try:
        return BOOT_ID.read_text(encoding="ascii").strip()
    except OSError:
        return None


# --- one tick ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What the tick does about it. `key` set: this screen instead of the schedule's."""

    key: str | None = None
    message: str | None = None  # a message screen, or None for the setup screen
    clear_mark: bool = False  # someone held the button with Wi-Fi working: redraw if a mark is up
    error: str | None = None  # for status.json


def is_setup_key(key: object) -> bool:
    """A screen that's only up for this, which the schedule takes back whatever the hour."""
    return isinstance(key, str) and key.startswith(("setup/", "message/wifi-"))


def _when(value: Any) -> datetime | None:
    try:
        return state.from_iso(value) if isinstance(value, str) else value if isinstance(value, datetime) else None
    except ValueError:
        return None


def _setup_outcome(data: dict) -> Outcome:
    result_at = (data["result"] or {}).get("at") or ""
    return Outcome(key=f"setup/{data['since']}/{result_at}")


def _connected_outcome(data: dict, now: datetime) -> Outcome:
    result = data["result"]
    until = _when(result.get("until")) if isinstance(result, dict) and result.get("ok") else None
    if until is None or now >= until:
        return Outcome()
    return Outcome(key=f"message/wifi-connected/{result['at']}",
                   message=f"Connected to {result['ssid']}\nThe frame is back to normal and carries on by itself.")


def _start(folder: Path, now: datetime, network: Network, name: str, password: str, result: dict | None,
           boot: str | None) -> Outcome:
    ssid = setup_ssid(name)
    scan = [n for n in network.scan() if n.get("ssid") != ssid]
    try:
        network.start(ssid, password)
    except HotspotFailed as exc:
        log.warning("the Wi-Fi setup hotspot wouldn't start: %s", exc)
        _patch(folder, **_off())
        return Outcome(key=f"message/wifi-setup-failed/{now.isoformat(timespec='seconds')}",
                       message="Wi-Fi setup didn't start\nThe frame carries on as it is. Hold the back button "
                               "for a second to try again.",
                       error=str(exc))
    data = _patch(folder, state="on", asked_at=None, cancel_at=None, join=None, since=load(folder)["since"] or now,
                  until=now + timedelta(minutes=IDLE_MINUTES), boot_id=boot, ssid=ssid, password=password,
                  scan=scan, result=result)
    log.info("Wi-Fi setup hotspot up: %s", ssid)
    return _setup_outcome(data)


def advance(folder: Path, now: datetime, network: Network, *, allowed: bool = True, name: str | None = None,
            boot: str | None = None) -> Outcome:
    """One tick's worth (see the module docstring). `allowed` False: low power or a nearly flat
    battery, where a hotspot has no place, and one that's up goes. Never raises for what the
    network does: the panel and the schedule must go on."""
    data = load(folder)
    on = data["state"] == "on"
    join = data["join"] if isinstance(data["join"], dict) else None

    if on and data["boot_id"] != boot:  # a reboot ended it
        log.info("Wi-Fi setup hotspot ended with the reboot")
        if join:
            network.forget(join["uuid"])
        data = _patch(folder, **_off())
        on, join = False, None

    if not on:
        if join:  # came too late for the hotspot it was meant for
            network.forget(join["uuid"])
            data = _patch(folder, join=None)
        asked = _when(data["asked_at"])
        if data["cancel_at"] is not None:
            data = _patch(folder, cancel_at=None)
        if asked is None:
            return _connected_outcome(data, now)
        _patch(folder, asked_at=None)
        if now - asked > timedelta(minutes=ASK_EXPIRES_MINUTES):
            return _connected_outcome(data, now)
        if not allowed:
            log.info("back button held in low power: no hotspot")
            return Outcome()
        if network.connected() is not False:
            log.info("back button held with Wi-Fi working: no hotspot")
            return Outcome(clear_mark=True)
        return _start(folder, now, network, name or frame_name(), make_password(), None, boot)

    if not allowed:
        network.stop()
        _patch(folder, **_off())
        return Outcome()

    if join:
        return _try(folder, now, network, data, join, name or frame_name(), boot)

    if data["cancel_at"] is not None or (_when(data["until"]) or now) <= now:
        log.info("Wi-Fi setup hotspot off (%s)", "called off" if data["cancel_at"] is not None else "nobody came")
        network.stop()
        _patch(folder, **_off())
        return Outcome()
    return _setup_outcome(data)


def _try(folder: Path, now: datetime, network: Network, data: dict, join: dict, name: str,
         boot: str | None) -> Outcome:
    """The phone drops off here. The page has told its owner to watch the panel."""
    ssid, uuid = join["ssid"], join["uuid"]
    network.stop()
    why = network.join(uuid)
    if why is None:
        log.info("joined Wi-Fi network %s", ssid)
        network.forget_others(ssid, uuid)
        data = _patch(folder, **_off(result={"ok": True, "ssid": ssid, "at": now,
                                             "until": now + timedelta(minutes=RESULT_MINUTES)}))
        return _connected_outcome(data, now)
    log.info("couldn't join Wi-Fi network %s: %s", ssid, why)
    network.forget(uuid)
    result = {"ok": False, "ssid": ssid, "why": why, "text": WHY.get(why, WHY[FAILED]), "at": now}
    return _start(folder, now, network, name, data["password"] or make_password(), result, boot)
