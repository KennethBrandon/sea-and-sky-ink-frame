"""The Wi-Fi watchdog: the frame gets itself back online when its connection is stuck.

From 2026-11-12 the frame at the Viewer's house couldn't reach the internet for three and a half
days, plugged in and running the whole time, and as far as it could tell still on its home
network: Wi-Fi up, an address, nothing getting through. Switching it off and on fixed it at once.
NetworkManager rejoins a network that comes back (`hotspot.py`), but it doesn't notice a
connection that's up and passes nothing, and it can give up on a network after failed reconnects.
This does what the Viewer shouldn't have to.

**Every tick** (on the Pi, and not while Wi-Fi is off or busy on purpose: low power, the setup
hotspot, a nearly flat battery) it checks, cheapest first:

- *link*: on a Wi-Fi network with an address (`hotspot.link_up`); on none, there's no internet;
- *dns* and *internet*: the names in `HOSTS`, the frame's own data sources, resolve, and a TCP
  connection to one of them opens. Nothing is sent over it. All within `INTERNET_WAIT_S`;
- *route*: only when the names don't resolve, a connection to an address remembered from the
  last good check (status.json `wifi.watchdog.addresses`). The frame's DNS goes through Tailscale
  (100.100.100.100 in resolv.conf), so names can fail while the way out is fine;
- *router*: only with no internet, the default gateway answers one ping.

Each tick adds a line to `wifi.csv` in the state dir (`LOG_HEADER`): the network, the access point,
its signal, those checks (1, 0, or empty when not checked) and a note of anything done or why it
paused. About five days are kept, like `battery.csv`, so the next outage leaves a record even if a
restart ends it. With no internet the tick doesn't fetch: every fetch would fail anyway.

**What it does** (`decide()`, pure; its state is status.json `wifi.watchdog`):

1. No internet for `RESET_AFTER`: *reset* Wi-Fi (`Pi.reset`). The radio goes off and on, which
   takes the chip down and up and sends NetworkManager looking again. If that doesn't put the frame
   on a network within `REJOIN_WAIT_S`, it joins the strongest saved network it hears by name,
   which gets past NetworkManager having given up on it. Again every `RESET_EVERY` while it lasts.
2. Still no internet `REBOOT_AFTER` after it went, a reset already tried, and the fault looks like
   the frame's own (`fault()`): its router doesn't answer, or names don't resolve while the way out
   works, or it's on no network while a saved one is in range. Then it *restarts* the Pi, once
   it has been up `MIN_UPTIME` and the watchdog hasn't restarted it in the last `REBOOT_EVERY`.
   The restart comes at the very end of the tick, after the panel has gone to sleep and
   status.json is on disk.

A router that answers while the way out doesn't is the house's internet, which neither step can
fix: a reset every `RESET_EVERY` is the most it gets, never a restart, which would also cost the
frame its clock until the internet's back (no RTC yet, H2; until NTP sets it the tick shows only
photos). With the router off nobody hears a saved network, so no restart either.
`/boot/firmware/photopainter-no-watchdog` turns both steps off; the checks and the log go on.

`--fake-panel` uses `Fake`, driven by `fake_wifi.json` (see `Fake`), and only logs the steps.
"""

from __future__ import annotations

import logging
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from photopainter import hotspot, state
from photopainter.web import wifi

log = logging.getLogger(__name__)

HOSTS = (("api.open-meteo.com", 443), ("api.tidesandcurrents.noaa.gov", 443))  # what the frame fetches from
CONNECT_TIMEOUT_S = 4.0  # one TCP connection
INTERNET_WAIT_S = 8.0  # all of dns, internet and route: DNS through a stuck resolver can hang longer
PING_TIMEOUT_S = 2  # the router's one ping
RESET_AFTER = timedelta(minutes=20)
RESET_EVERY = timedelta(hours=2)
REBOOT_AFTER = timedelta(minutes=90)
REBOOT_EVERY = timedelta(hours=6)
MIN_UPTIME = timedelta(minutes=30)
REJOIN_WAIT_S = 30  # after the radio comes back on, how long NetworkManager gets to find a network
JOIN_WAIT_S = 30  # `nmcli --wait` for joining a network by name
MAX_ADDRESSES = 4  # addresses remembered for the route check
NO_WATCHDOG = Path("/boot/firmware/photopainter-no-watchdog")
PROC_ROUTE = Path("/proc/net/route")
LOG = "wifi.csv"
LOG_HEADER = "time,network,access_point,signal_dbm,link,dns,internet,route,router,note"
LOG_MAX_LINES = 8640  # a line a minute: 6 days, then trimmed…
LOG_KEEP_LINES = 7200  # …to the last 5, like battery.csv


@dataclass(frozen=True)
class Check:
    """One tick's look at the connection. None: not checked (or can't say)."""

    link: bool | None = None  # on a Wi-Fi network with an address
    dns: bool | None = None  # one of HOSTS resolved
    internet: bool | None = None  # a connection to one of them opened
    route: bool | None = None  # (names failed) a connection to a remembered address opened
    router: bool | None = None  # (no internet) the default gateway answered a ping
    address: str | None = None  # the address that answered, to remember


@dataclass(frozen=True)
class Radio:
    """The network the frame is on, for the log; all None when it's on none."""

    ssid: str | None = None
    bssid: str | None = None
    signal_dbm: float | None = None


@dataclass(frozen=True)
class Step:
    kind: str  # "reset" or "reboot"
    why: str  # for the log


class Watch(Protocol):
    def enabled(self) -> bool: ...
    def uptime_s(self) -> float | None: ...
    def radio(self) -> Radio: ...
    def check(self, remembered: list[str]) -> Check: ...
    def heard_saved(self) -> bool: ...
    def reset(self) -> str: ...  # what it did, for the log
    def reboot(self) -> None: ...


# --- the rules -----------------------------------------------------------------------------


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return state.from_iso(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _state(offline_since: datetime | None, reset_at: datetime | None, reboot_at: datetime | None,
           addresses: list[str]) -> dict:
    return {"offline_since": offline_since, "reset_at": reset_at, "reboot_at": reboot_at, "addresses": addresses}


def remembered(prev: dict | None) -> list[str]:
    """The addresses kept in status.json `wifi.watchdog`, checked to be strings."""
    addresses = (prev or {}).get("addresses") if isinstance(prev, dict) else None
    return [a for a in addresses if isinstance(a, str)][:MAX_ADDRESSES] if isinstance(addresses, list) else []


def pause(prev: dict | None) -> dict:
    """Wi-Fi is off or busy on purpose: no outage is running. A past restart and the
    addresses are kept."""
    prev = prev if isinstance(prev, dict) else {}
    return _state(None, None, _when(prev.get("reboot_at")), remembered(prev))


def fault(check: Check, heard_saved: Callable[[], bool]) -> str | None:
    """Why the frame itself looks at fault, or None when it looks like the house's internet (or
    can't say). `heard_saved` scans, so it's only asked when the frame is on no network."""
    if check.link is False:
        return "on no network with a saved one in range" if heard_saved() else None
    if check.link is not True or check.internet is not False:
        return None
    if check.router is False:
        return "the router doesn't answer"
    if check.router is None:
        return "no route to a router"
    if check.dns is False and check.route is True:
        return "names don't resolve, though the way out works"
    return None


def decide(prev: dict | None, check: Check, now: datetime, *, uptime_s: float | None, enabled: bool,
           heard_saved: Callable[[], bool]) -> tuple[dict, Step | None]:
    """The new status.json `wifi.watchdog` and the step to take, if any (see the module docstring)."""
    prev = prev if isinstance(prev, dict) else {}
    addresses = remembered(prev)
    reboot_at = _when(prev.get("reboot_at"))
    if check.internet is None:  # nothing to go on: change nothing
        return _state(_when(prev.get("offline_since")), _when(prev.get("reset_at")), reboot_at, addresses), None
    if check.internet:
        if check.address:
            addresses = [check.address, *(a for a in addresses if a != check.address)][:MAX_ADDRESSES]
        return _state(None, None, reboot_at, addresses), None

    since = _when(prev.get("offline_since"))
    if since is None or since > now:  # new, or the clock has been set back since
        since = now
    reset_at = _when(prev.get("reset_at"))
    if reset_at is not None and not since <= reset_at <= now:  # from another outage, or a clock change
        reset_at = None
    out = _state(since, reset_at, reboot_at, addresses)
    if not enabled:
        return out, None
    offline = now - since

    if (reset_at is not None and offline >= REBOOT_AFTER and uptime_s is not None
            and uptime_s >= MIN_UPTIME.total_seconds()
            and (reboot_at is None or now - reboot_at >= REBOOT_EVERY)):  # one in the future blocks too
        why = fault(check, heard_saved)
        if why is not None:
            return {**out, "reboot_at": now}, Step("reboot", why)
    if offline >= RESET_AFTER and (reset_at is None or now - reset_at >= RESET_EVERY):
        return {**out, "reset_at": now}, Step("reset", f"no internet since {since:%H:%M}")
    return out, None


def log_line(stamp: datetime, radio: Radio, check: Check, note: str) -> str:
    """One `wifi.csv` line (no newline). Commas in a network's name become spaces."""

    def flag(value: bool | None) -> str:
        return "" if value is None else "1" if value else "0"

    def text(value: str | None) -> str:
        return "" if value is None else value.replace(",", " ").replace("\n", " ")

    signal = "" if radio.signal_dbm is None else f"{radio.signal_dbm:.0f}"
    return ",".join([state.to_iso(stamp), text(radio.ssid), text(radio.bssid), signal, flag(check.link),
                     flag(check.dns), flag(check.internet), flag(check.route), flag(check.router), text(note)])


def append_log(folder: Path, line: str) -> None:
    path = folder / LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if fh.tell() == 0:
            fh.write(LOG_HEADER + "\n")
        fh.write(line + "\n")
    if path.stat().st_size > LOG_MAX_LINES * 60:  # about that many lines; checked cheaply
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > LOG_MAX_LINES:
            state.write_atomic(path, "".join([lines[0], *lines[-LOG_KEEP_LINES:]]).encode("utf-8"))


# --- what's there on the Pi ----------------------------------------------------------------


def parse_iw_link(text: str) -> Radio:
    """`iw dev wlan0 link` to the network, access point and signal; all None for "Not connected."."""
    ssid = bssid = None
    signal = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Connected to "):
            bssid = line.split()[2]
        elif line.startswith("SSID:"):
            ssid = line[5:].strip() or None
        elif line.startswith("signal:"):
            try:
                signal = float(line.split()[1])
            except (IndexError, ValueError):
                signal = None
    return Radio(ssid, bssid, signal)


def gateway(text: str, iface: str = hotspot.IFACE) -> str | None:
    """The default gateway on `iface` from /proc/net/route's text, or None."""
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[0] != iface or parts[1] != "00000000":
            continue
        try:
            if int(parts[3], 16) & 0x2:  # RTF_GATEWAY
                return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
        except (ValueError, struct.error):
            continue
    return None


def best_saved(scan: list[dict], saved: list[wifi.Network]) -> wifi.Network | None:
    """The saved network with the strongest signal in `scan` (strongest first), or None."""
    by_ssid = {n.ssid: n for n in saved}
    for heard in scan:
        if heard.get("ssid") in by_ssid:
            return by_ssid[heard["ssid"]]
    return None


def _connect(address: tuple, timeout: float) -> bool:
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(address)
        except OSError:
            return False
    return True


def reach(hosts: tuple[tuple[str, int], ...], remembered_: list[str], *,
          resolve: Callable[..., list] = socket.getaddrinfo,
          connect: Callable[[tuple, float], bool] = _connect) -> tuple[bool, bool, bool | None, str | None]:
    """(dns, internet, route, the address that answered). `route` is only tried when no name
    resolved, on port 443 of the remembered addresses."""
    resolved = False
    for host, port in hosts:
        try:
            infos = resolve(host, port, type=socket.SOCK_STREAM)
        except OSError:
            continue
        resolved = True
        for info in infos:
            sockaddr = info[4]
            if connect(sockaddr, CONNECT_TIMEOUT_S):
                return True, True, None, sockaddr[0]
    if resolved or not remembered_:
        return resolved, False, None, None
    return False, False, any(connect((a, 443), CONNECT_TIMEOUT_S) for a in remembered_), None


def _within(seconds: float, fn: Callable[[], Any], default: Any) -> Any:
    """fn() if it finishes within `seconds`, else `default` (it's left running, as a daemon)."""
    box = [default]

    def target() -> None:
        box[0] = fn()

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    return default if worker.is_alive() else box[0]


class Pi:
    """The real thing. sudo needs no password for `pi` (CLAUDE.md); `iw` is in /usr/sbin."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] | None = None,
                 sleep: Callable[[float], None] = time.sleep, link: Callable[[], bool | None] = hotspot.link_up):
        self.run = run or (lambda argv, timeout=30: subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False))
        self.sleep = sleep
        self.link = link
        self._scan: list[dict] | None = None

    def enabled(self) -> bool:
        return not NO_WATCHDOG.exists()

    def uptime_s(self) -> float | None:
        from photopainter.hardware.panel import seconds_since_boot

        return seconds_since_boot()

    def _out(self, argv: list[str], timeout: float = 5) -> str | None:
        try:
            done = self.run(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    def radio(self) -> Radio:
        for iw in ("iw", "/usr/sbin/iw"):
            out = self._out([iw, "dev", hotspot.IFACE, "link"])
            if out is not None:
                return parse_iw_link(out)
        return Radio()

    def check(self, remembered_: list[str]) -> Check:
        link = self.link()
        if link is None:
            return Check()
        if not link:  # on no network: no internet, nothing more to check
            return Check(link=False, internet=False)
        dns, internet, route, address = _within(INTERNET_WAIT_S, lambda: reach(HOSTS, remembered_),
                                                (False, False, None, None))
        router = None
        if not internet:
            try:
                gw = gateway(PROC_ROUTE.read_text(encoding="ascii"))
            except OSError:
                gw = None
            if gw is not None:
                router = self._out(["ping", "-n", "-q", "-c", "1", "-W", str(PING_TIMEOUT_S), gw],
                                   timeout=PING_TIMEOUT_S + 2) is not None
        return Check(link=True, dns=dns, internet=internet, route=route, router=router, address=address)

    def _heard(self) -> list[dict]:
        if self._scan is None:
            self._scan = hotspot.Nmcli(run=self.run).scan()
        return self._scan

    def heard_saved(self) -> bool:
        try:
            return best_saved(self._heard(), wifi.saved(run=self.run)) is not None
        except wifi.WifiFailed as exc:
            log.warning("watchdog: couldn't list the saved networks: %s", exc)
            return False

    def _nmcli(self, *args: str, timeout: float = 30) -> str | None:
        """None if it worked, else why not."""
        try:
            done = self.run(["sudo", "-n", "nmcli", *args], timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        return None if done.returncode == 0 else (done.stderr.strip() or f"exit {done.returncode}")

    def reset(self) -> str:
        for onoff in ("off", "on"):
            problem = self._nmcli("radio", "wifi", onoff)
            if problem:
                return f"radio {onoff} failed: {problem}"
            self.sleep(2)
        for _ in range(REJOIN_WAIT_S // 3):
            if self.link():
                return f"radio off and on, back on {self.radio().ssid or 'a network'}"
            self.sleep(3)
        self._scan = None
        try:
            target = best_saved(self._heard(), wifi.saved(run=self.run))
        except wifi.WifiFailed as exc:
            return f"radio off and on, still off; couldn't list the saved networks: {exc}"
        if target is None:
            return "radio off and on, still off; no saved network in range"
        problem = self._nmcli("--wait", str(JOIN_WAIT_S), "connection", "up", "uuid", target.uuid,
                              timeout=JOIN_WAIT_S + 10)
        if problem:
            return f"radio off and on, still off; couldn't join {target.ssid}: {problem}"
        return f"radio off and on, then joined {target.ssid}"

    def reboot(self) -> None:
        self.run(["sudo", "-n", "systemctl", "reboot"], timeout=30)


class Fake:
    """`--fake-panel`: `fake_wifi.json` in the state dir, shared with `hotspot.FakeNetwork`. No file,
    or no `connected` in it: no radio, like the Mac, and the watchdog stays out of it. Otherwise
    `connected` is the link and, all true unless set, `internet`, `dns` (defaults to `internet`),
    `router`, `route`, `heard` (a saved network in range), `watchdog` (on); `uptime_s` (2 h unless
    set). A reset counts up `resets` and, with `"reset_fixes": true`, brings the internet back;
    a restart sets `rebooted_at`. Nothing real happens."""

    def __init__(self, folder: Path):
        self.path = folder / hotspot.FAKE_FILE

    def _read(self) -> dict:
        data = state.read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def _get(self, key: str, default: Any) -> Any:
        value = self._read().get(key)
        return value if isinstance(value, type(default)) else default

    def enabled(self) -> bool:
        return self._get("watchdog", True)

    def uptime_s(self) -> float | None:
        value = self._read().get("uptime_s", 7200)
        return float(value) if isinstance(value, (int, float)) else None

    def radio(self) -> Radio:
        return Radio("Home", "02:00:00:00:00:01", -60.0) if self._read().get("connected") is True else Radio()

    def check(self, remembered_: list[str]) -> Check:
        link = self._read().get("connected")
        if not isinstance(link, bool):
            return Check()
        if not link:
            return Check(link=False, internet=False)
        internet = self._get("internet", True)
        dns = self._get("dns", internet)
        return Check(link=True, dns=dns, internet=internet,
                     route=None if dns or internet else self._get("route", False),
                     router=None if internet else self._get("router", True),
                     address="192.0.2.1" if internet else None)

    def heard_saved(self) -> bool:
        return self._get("heard", True)

    def reset(self) -> str:
        data = self._read()
        fields: dict[str, Any] = {"resets": int(data.get("resets") or 0) + 1}
        if data.get("reset_fixes") is True:
            fields["internet"] = True
        state.write_json(self.path, {**data, **fields})
        log.info("watchdog (not applied here): nmcli radio wifi off, on")
        return "radio off and on (simulated)"

    def reboot(self) -> None:
        state.write_json(self.path, {**self._read(), "rebooted_at": state.to_iso(datetime.now().astimezone())})
        log.info("watchdog (not applied here): systemctl reboot")
