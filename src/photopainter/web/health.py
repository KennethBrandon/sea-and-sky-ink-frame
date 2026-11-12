"""Live health of the frame for the dashboard (`GET /api/status`, PRD.md U9).

Every reading is best effort: on the Mac, or when a tool is missing, it is None and the
page shows a dash. Readings that start a child process (Wi-Fi name, clock sync, power,
addresses, Tailscale) are kept for a short while, because the page asks every 10 s.

`sources()` says how each data source is doing, from its cache file and `sources.json`
(the last fetch attempt, written by `sources/common.py`), with the day screen's rules:
weather, surf and the gauge show their age after 2 h and are left out after 12 h (PRD.md §3).
"""

from __future__ import annotations

import json
import logging
import os
import resource
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config

log = logging.getLogger(__name__)

PROC = Path("/proc")
SYS = Path("/sys")
SLOW_TTL_S = 30.0  # how long a reading that needs a child process is kept

SOURCES = ("tides", "weather", "surf", "gauge")
SOURCE_FILES = {"tides": "tides.json", "weather": "weather.json", "surf": "surf.json", "gauge": "gauge.json"}
ATTEMPTS = "sources.json"
SHOW_AGE_AFTER = timedelta(hours=2)  # the day screen prints the time weather/surf/gauge are from
HIDE_AFTER = timedelta(hours=12)  # …and leaves them out
TIDES_LOW_AHEAD = timedelta(days=3)  # warn when the tide cache runs out sooner than this


def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
    """A command's stdout, or None if it's missing, fails or takes too long."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


class _Kept:
    """A reading kept for `ttl` seconds (thread-safe)."""

    def __init__(self, fn: Callable[[], Any], ttl: float = SLOW_TTL_S):
        self.fn, self.ttl = fn, ttl
        self._at = -1e9
        self._value: Any = None
        self._lock = threading.Lock()

    def __call__(self) -> Any:
        with self._lock:
            if time.monotonic() - self._at > self.ttl:
                self._value = self.fn()
                self._at = time.monotonic()
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._at = -1e9


# --- system ----------------------------------------------------------------------------


def uptime_s() -> float | None:
    text = _read(PROC / "uptime")
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def cpu_temp_c() -> float | None:
    text = _read(SYS / "class" / "thermal" / "thermal_zone0" / "temp")
    try:
        return round(int(text.strip()) / 1000, 1) if text else None
    except ValueError:
        return None


def memory() -> dict:
    """{"server_mb": this process's resident memory, "available_mb", "total_mb"}."""
    server = None
    status = _read(PROC / "self" / "status")
    if status:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                server = round(int(line.split()[1]) / 1024, 1)
    if server is None:  # the Mac: the peak, in bytes there (kB on Linux)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        server = round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    info: dict[str, float] = {}
    meminfo = _read(PROC / "meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                info[key] = round(int(rest.split()[0]) / 1024, 1)
    return {"server_mb": server, "available_mb": info.get("MemAvailable"), "total_mb": info.get("MemTotal")}


def disk(folder: Path) -> dict | None:
    """Free space where the photos are (the SD card on the Pi)."""
    for path in (folder, *folder.parents):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        return {"free_mb": round(usage.free / 2**20), "total_mb": round(usage.total / 2**20)}
    return None


def load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _wireless() -> dict | None:
    """{"interface", "quality" (0-70), "signal_dbm"} from /proc/net/wireless."""
    text = _read(PROC / "net" / "wireless")
    if not text:
        return None
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0].endswith(":"):
            try:
                return {"interface": parts[0][:-1], "quality": float(parts[2].rstrip(".")),
                        "signal_dbm": float(parts[3].rstrip("."))}
            except ValueError:
                continue
    return None


def _ssid(interface: str) -> str | None:
    out = _run(["iw", "dev", interface, "link"])
    if out is not None:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                return line[5:].strip() or None
        return None  # "Not connected."
    out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"])
    for line in (out or "").splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":") or None
    return None


def _wifi() -> dict | None:
    w = _wireless()
    if w is None:
        return None
    ssid = _ssid(w["interface"])
    return {**w, "ssid": ssid, "connected": ssid is not None}


def _network() -> list[dict] | None:
    """[{"interface", "ipv4", "ipv6"}] for each interface with an address other machines can
    use, from `ip -j addr` (so not loopback or link-local); None without `ip`, as on the Mac."""
    out = _run(["ip", "-j", "addr"])
    if out is None:
        return None
    try:
        found = []
        for link in json.loads(out):
            addrs = [a for a in link.get("addr_info", []) if a.get("scope") == "global"]
            ipv4 = [a["local"] for a in addrs if a.get("family") == "inet"]
            ipv6 = [a["local"] for a in addrs if a.get("family") == "inet6"]
            if ipv4 or ipv6:
                found.append({"interface": link["ifname"], "ipv4": ipv4, "ipv6": ipv6})
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("couldn't read the output of ip -j addr")
        return None
    return found


def _tailscale_reading(state_: str, status: dict | None = None) -> dict:
    me = (status or {}).get("Self")
    me = me if isinstance(me, dict) else {}
    dns_name = me.get("DNSName")
    return {"state": state_, "ips": me.get("TailscaleIPs") or [],
            "dns_name": (dns_name.rstrip(".") or None) if isinstance(dns_name, str) else None,
            "online": me.get("Online")}


def _tailscale() -> dict | None:
    """{"state", "ips", "dns_name", "online"} from `tailscale status --json`, which needs no
    root. The state is "connected" (the backend is Running), "logged_out", "stopped" or
    "not_installed". `online` is false when a connected Pi has lost Tailscale's servers.
    None where there's no /proc: the Mac's own Tailscale says nothing about the frame."""
    if not PROC.is_dir():
        return None
    if shutil.which("tailscale") is None:
        return _tailscale_reading("not_installed")
    # 45 ms on the Pi once it's in memory, but a first run loads a 32 MB program off the SD card:
    # don't let a slow start read as "off". It exits with an error when tailscaled isn't running.
    out = _run(["tailscale", "status", "--json", "--peers=false"], timeout=5.0)
    if out is None:
        return _tailscale_reading("stopped")
    try:
        status = json.loads(out)
        backend = status["BackendState"]
    except (ValueError, TypeError, KeyError):
        log.warning("couldn't read the output of tailscale status")
        return None
    if backend == "Running":
        return _tailscale_reading("connected", status)
    # the others are Stopped (`tailscale down`), and Starting and NoState on the way up
    logged_out = backend in ("NeedsLogin", "NeedsMachineAuth")
    return _tailscale_reading("logged_out" if logged_out else "stopped", status)


def _clock() -> dict:
    from photopainter.tick import clock_synced  # same rule as the tick's clock check

    return {"synced": clock_synced()}


def _power() -> dict | None:
    """Under-voltage and throttling from `vcgencmd get_throttled` (the Pi only)."""
    out = _run(["vcgencmd", "get_throttled"])
    if not out or "=" not in out:
        return None
    try:
        bits = int(out.strip().split("=", 1)[1], 16)
    except ValueError:
        return None
    return {"throttled": hex(bits), "undervoltage_now": bool(bits & 0x1),
            "undervoltage_since_boot": bool(bits & 0x10000), "throttled_now": bool(bits & 0x4),
            "throttled_since_boot": bool(bits & 0x40000)}


wifi = _Kept(_wifi)
clock = _Kept(_clock, ttl=60)
power = _Kept(_power, ttl=60)
network = _Kept(_network, ttl=60)
tailscale = _Kept(_tailscale, ttl=60)


def clear_kept() -> None:
    """Forget kept readings (tests)."""
    for kept in (wifi, clock, power, network, tailscale):
        kept.clear()


def system(photo_folder: Path) -> dict:
    """Everything about the Pi itself."""
    return {
        "hostname": socket.gethostname(),
        "uptime_s": uptime_s(),
        "cpu_temp_c": cpu_temp_c(),
        "load_1m": load_1m(),
        "memory": memory(),
        "disk": disk(photo_folder),
        "wifi": wifi(),
        "network": network(),
        "tailscale": tailscale(),
        "clock": clock(),
        "power": power(),
    }


# --- data sources ----------------------------------------------------------------------

_summaries: dict[Path, tuple[tuple[int, int], dict | None]] = {}


def _summary(path: Path) -> dict | None:
    """{"fetched_at", "until"} of a cache file; parsed again only when the file changes."""
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _summaries.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    data = state.read_json(path, None)
    summary = None
    if isinstance(data, dict):
        rows = next((data[k] for k in ("curve", "hourly", "gap") if isinstance(data.get(k), list)), None)
        last = rows[-1].get("t") if isinstance(rows, list) and rows and isinstance(rows[-1], dict) else None
        summary = {"fetched_at": _time(data.get("fetched_at")), "until": _time(last)}
    _summaries[path] = (sig, summary)
    return summary


def _time(value: Any) -> datetime | None:
    try:
        return state.from_iso(value) if isinstance(value, str) else None
    except ValueError:
        return None


def sources(state_dir: Path, config: Config, now: datetime) -> dict[str, dict]:
    """Each source: enabled, fetched_at, until, last_try, error, level ("ok", "warn",
    "error" or "off") and a short note saying why."""
    attempts = state.read_json(state_dir / ATTEMPTS, {})
    attempts = attempts if isinstance(attempts, dict) else {}
    out = {}
    for name in SOURCES:
        enabled = True if name == "tides" else getattr(config, name).enabled
        summary = _summary(state_dir / SOURCE_FILES[name]) or {}
        attempt = attempts.get(name) if isinstance(attempts.get(name), dict) else {}
        error = attempt.get("error") if isinstance(attempt.get("error"), str) else None
        fetched, until = summary.get("fetched_at"), summary.get("until")
        level, note = _judge(name, enabled, fetched, until, error, now)
        out[name] = {"enabled": enabled, "fetched_at": fetched, "until": until,
                     "last_try": _time(attempt.get("at")), "error": error, "level": level, "note": note}
    return out


def _judge(name: str, enabled: bool, fetched: datetime | None, until: datetime | None,
           error: str | None, now: datetime) -> tuple[str, str]:
    if not enabled:
        return "off", "Turned off in settings"
    if fetched is None:
        return "error", "No data yet" + (f": {error}" if error else "")
    if name == "tides":
        if until is None or until < now:
            return "error", "The tide predictions have run out"
        if until - now < TIDES_LOW_AHEAD:
            return "warn", "The tide predictions run out soon"
    else:
        # a gauge can answer with old readings, and the readings are what the screen goes by
        age = now - (until if name == "gauge" and until is not None else fetched)
        if age > HIDE_AFTER:
            return "error", "Too old to show on the day screen"
        if age > SHOW_AGE_AFTER:
            return "warn", "Shown with its age on the day screen"
    if error:
        return "warn", f"The last update failed: {error}"
    return "ok", "Up to date"
