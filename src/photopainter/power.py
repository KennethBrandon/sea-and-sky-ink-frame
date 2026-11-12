"""The battery's history and the low-power mode.

The tick logs one battery reading a minute to `battery.csv` in the state dir (about five
days are kept). This module reads it back for the dashboard and decides the low-power
mode; the tick applies the decision.

**History.** `history()` is what the dashboard charts: the last `HISTORY_DAYS` of the log,
the readings averaged into equal buckets (one point each, or one for each state where the
state changes inside a bucket, so a point never mixes charging with running on the cell;
small enough to send every minute; a bucket with no readings is a gap in the line), and
the spans of each state
("charging", "discharging", "idle", "none") at the log's full resolution, so a run on the
battery of a few minutes still shows. A span ends where the state changes or the log stops
for longer than `MAX_GAP` (the Pi off).

**Cycles and the estimate.** A cycle is a run of consecutive "discharging" readings: the
frame running on its battery, from the moment the USB power went until it came back (or
until now). `last_cycle()` finds the latest one, `estimate()` guesses how long an ongoing
one has left: the charge left is the cell's capacity (`battery.capacity_mah`, a setting)
times the percent the voltage curve gives for the latest reading, and the draw is the
average current over the last half hour. The percent is read off a voltage that sags
under load, so the guess runs a little short, which is the right way to be wrong.

**Low power** (`battery.low_power`, off by default): after `low_power_after_minutes` on
the battery, the tick turns Wi-Fi off and stops the dashboard, the two things it can
switch off that cost anything (Wi-Fi is a quarter of the frame's draw; the dashboard
costs nothing idle but is useless without Wi-Fi). Every `data_every_hours` Wi-Fi comes
back for a few minutes so the tide, weather, surf and gauge caches can refresh, then goes
off again. The moment a tick sees USB power back (charging or idle), Wi-Fi and the
dashboard come back. A `/boot/firmware/photopainter-stay-awake` file keeps the frame out
of low power whatever the setting says. The tick never fetches on the tick that turns
Wi-Fi on, since the connection takes a few seconds and a failed fetch backs off for five
minutes.

`decide()` is pure: the previous state (status.json `power`), the reading, the clock and
the config in; the new state and the actions out. `apply()` runs the actions, through a
runner the tests and `--fake-panel` replace.
"""

from __future__ import annotations

import logging
import statistics
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config
from photopainter.hardware.battery import Reading

log = logging.getLogger(__name__)

LOG_FILE = "battery.csv"
STAY_AWAKE = Path("/boot/firmware/photopainter-stay-awake")
WINDOW_MINUTES = 4  # how long Wi-Fi stays on for a data window in low power
AVERAGE_OVER = 30  # readings (a minute each) the draw is averaged over for the estimate
SMOOTH_OVER = 5  # readings whose median voltage the estimate uses
CHART_POINTS = 400  # at most this many readings go to the dashboard's chart
HISTORY_DAYS = 5  # how far back the dashboard's chart reaches (tick.py keeps a little more)
HISTORY_POINTS = 480  # buckets the chart's line is averaged into: 15 minutes each over 5 days
MAX_GAP = timedelta(minutes=10)  # readings further apart than this don't count as a draw between them
WEB_SERVICE = "photopainter-web.service"
MODES = ("normal", "low")
ACTIONS = ("wifi_off", "wifi_on", "web_stop", "web_start")
COMMANDS: dict[str, list[str]] = {
    "wifi_off": ["sudo", "-n", "nmcli", "radio", "wifi", "off"],
    "wifi_on": ["sudo", "-n", "nmcli", "radio", "wifi", "on"],
    "web_stop": ["sudo", "-n", "systemctl", "--no-block", "stop", WEB_SERVICE],
    "web_start": ["sudo", "-n", "systemctl", "--no-block", "start", WEB_SERVICE],
}
Runner = Callable[[str], None]  # runs one action by name


# --- the log -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    t: datetime
    volts: float
    milliamps: float
    state: str

    def to_dict(self) -> dict:
        return {"t": self.t, "volts": self.volts, "milliamps": self.milliamps, "state": self.state}


def read_log(path: Path) -> list[Sample]:
    """Every readable line of `battery.csv`, oldest first; bad lines are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 4:
            continue
        try:
            out.append(Sample(state.from_iso(parts[0]), float(parts[1]), float(parts[2]), parts[3]))
        except ValueError:
            continue  # the header, or a line cut short by a power cut
    return out


@dataclass(frozen=True)
class Cycle:
    """A run on the battery: consecutive "discharging" readings."""

    samples: list[Sample]
    ongoing: bool  # the latest reading is still discharging
    ended_at: datetime | None  # the first reading after it that isn't (None while ongoing)

    @property
    def started_at(self) -> datetime:
        return self.samples[0].t

    @property
    def last(self) -> Sample:
        return self.samples[-1]

    def used_mah(self) -> float:
        """Charge drawn over the cycle: the current integrated over time (trapezoids)."""
        total = 0.0
        for a, b in zip(self.samples, self.samples[1:]):
            gap = b.t - a.t
            if timedelta(0) < gap <= MAX_GAP:  # a longer gap is a lost log or a reboot, not a draw
                total += (abs(a.milliamps) + abs(b.milliamps)) / 2 * gap.total_seconds() / 3600
        return total

    def to_dict(self, points: int = CHART_POINTS) -> dict:
        step = max(1, -(-len(self.samples) // points))  # ceil: thin the chart's points evenly
        kept = self.samples[::step]
        if kept[-1] is not self.samples[-1]:
            kept.append(self.samples[-1])
        return {"started_at": self.started_at, "ended_at": self.ended_at, "ongoing": self.ongoing,
                "used_mah": round(self.used_mah()), "readings": [s.to_dict() for s in kept]}


def last_cycle(samples: list[Sample]) -> Cycle | None:
    """The latest run on the battery, ongoing or finished; None if there has never been one."""
    end = len(samples)
    while end > 0 and samples[end - 1].state != "discharging":
        end -= 1
    if end == 0:
        return None
    start = end
    while start > 0 and samples[start - 1].state == "discharging":
        start -= 1
    ongoing = end == len(samples)
    return Cycle(samples[start:end], ongoing, None if ongoing else samples[end].t)


def history(samples: list[Sample], now: datetime, days: float = HISTORY_DAYS,
            points: int = HISTORY_POINTS) -> dict:
    """The log's last `days` up to `now`, for the dashboard's chart (see the module docstring).

    `readings`: one per bucket that has any (one per state where it changes in the bucket):
    the mean time, volts and milliamps, the state, and `after_gap` (the bucket before had no
    readings, so the line breaks); readings with no cell ("none") are left out. `spans`:
    {state, start, end}, oldest first. `step_s` is a bucket's length."""
    # In UTC: across a clock change, an aware datetime minus days is wall-clock time, and the
    # log's fixed offsets would land an hour off in the buckets.
    utc_now = now.astimezone(timezone.utc)
    start = utc_now - timedelta(days=days)
    step = (utc_now - start) / points
    rows = [s for s in samples if start <= s.t <= now]
    groups: list[tuple[int, list[Sample]]] = []  # (bucket, its readings of one state)
    for s in rows:
        if s.state == "none":
            continue
        i = min(points - 1, int((s.t - start) / step))
        if groups and groups[-1][0] == i and groups[-1][1][-1].state == s.state:
            groups[-1][1].append(s)
        else:
            groups.append((i, [s]))
    readings = []
    prev = None
    for i, group in groups:
        readings.append({
            "t": (group[0].t + sum((s.t - group[0].t for s in group), timedelta(0)) / len(group)).replace(microsecond=0),
            "volts": round(statistics.fmean(s.volts for s in group), 3),
            "milliamps": round(statistics.fmean(s.milliamps for s in group), 1),
            "state": group[0].state,
            "after_gap": prev is not None and i - prev > 1,
        })
        prev = i
    spans: list[dict] = []
    for a, b in zip(rows, [*rows[1:], None]):
        # A reading stands for the time up to the next one, or a minute where the log stops.
        joined = b is not None and b.t - a.t <= MAX_GAP
        end = b.t if joined else min(a.t + timedelta(minutes=1), now)
        last = spans[-1] if spans else None
        if last is not None and last["state"] == a.state and last["end"] == a.t:
            last["end"] = end
        else:
            spans.append({"state": a.state, "start": a.t, "end": end})
    return {"from": start.astimezone(now.tzinfo), "to": now, "step_s": round(step.total_seconds()), "readings": readings,
            "spans": spans}


def estimate(cycle: Cycle, capacity_mah: float, low_volts: float, now: datetime) -> dict | None:
    """How an ongoing cycle is likely to go on: the draw, the charge left and when it runs
    out, from the voltage curve and the recent current. None for a finished cycle or one
    too short to judge."""
    if not cycle.ongoing or len(cycle.samples) < 2:
        return None
    recent = cycle.samples[-AVERAGE_OVER:]
    avg_ma = statistics.fmean(abs(s.milliamps) for s in recent)
    if avg_ma < 1:
        return None
    volts = statistics.median(s.volts for s in cycle.samples[-SMOOTH_OVER:])
    percent = Reading(volts, -avg_ma).percent or 0
    low_percent = Reading(low_volts, -avg_ma).percent or 0
    left_mah = capacity_mah * percent / 100
    to_low_mah = capacity_mah * max(0, percent - low_percent) / 100
    hours_left = left_mah / avg_ma
    hours_to_low = to_low_mah / avg_ma
    return {
        "avg_ma": round(avg_ma), "volts": round(volts, 3), "percent": percent,
        "left_mah": round(left_mah), "hours_left": round(hours_left, 2),
        "runs_out_at": now + timedelta(hours=hours_left),
        "hours_to_low": round(hours_to_low, 2), "low_at": now + timedelta(hours=hours_to_low),
    }


def summary(state_dir: Path, config: Config, now: datetime, power: dict | None) -> dict:
    """What `GET /api/battery` returns: the last cycle, the estimate, the last days' history,
    the settings in play."""
    samples = read_log(state_dir / LOG_FILE)
    cycle = last_cycle(samples)
    b = config.battery
    return {
        "capacity_mah": b.capacity_mah,
        "indicator_volts": b.indicator_volts,
        "low_volts": b.low_volts,
        "cycle": None if cycle is None else cycle.to_dict(),
        "estimate": None if cycle is None else estimate(cycle, b.capacity_mah, b.low_volts, now),
        "history": history(samples, now),
        "power": power,
    }


# --- low power ---------------------------------------------------------------------------


def decide(prev: dict | None, reading: Reading | None, on_battery_since: datetime | None,
           now: datetime, config: Config, stay_awake: bool = False) -> tuple[dict, list[str]]:
    """The next power state and the actions that get there (see the module docstring).

    `prev` is status.json's `power` from the last tick (None the first time); the state
    returned is what the tick writes back. Actions are names from `ACTIONS`, in order.
    """
    prev = prev if isinstance(prev, dict) else {}
    mode = prev.get("mode") if prev.get("mode") in MODES else "normal"
    wifi = prev.get("wifi") if prev.get("wifi") in ("on", "off") else "on"
    since = when(prev.get("since"))
    b = config.battery
    on_battery_for = None if on_battery_since is None else now - on_battery_since
    want_low = (b.low_power and not stay_awake and reading is not None and reading.on_battery
                and on_battery_for is not None and on_battery_for >= timedelta(minutes=b.low_power_after_minutes))

    if not want_low:
        actions = []
        if wifi == "off":
            actions.append("wifi_on")
        if mode == "low":
            actions.append("web_start")
        return _state("normal", "on", now if mode != "normal" or since is None else since), actions

    every = timedelta(hours=b.data_every_hours)
    if mode != "low":
        return _state("low", "off", now, next_window_at=now + every), ["web_stop", "wifi_off"]

    window_until = when(prev.get("window_until"))
    next_window_at = when(prev.get("next_window_at"))
    if window_until is not None:  # a data window is open
        if now >= window_until:
            return _state("low", "off", since, next_window_at=now + every), ["wifi_off"]
        return _state("low", "on", since, window_until=window_until, next_window_at=next_window_at), []
    if next_window_at is None or now >= next_window_at:
        return _state("low", "on", since, window_until=now + timedelta(minutes=WINDOW_MINUTES)), ["wifi_on"]
    return _state("low", "off", since, next_window_at=next_window_at), []


def _state(mode: str, wifi: str, since: datetime | None, *, window_until: datetime | None = None,
           next_window_at: datetime | None = None) -> dict:
    return {"mode": mode, "wifi": wifi, "since": since, "window_until": window_until,
            "next_window_at": next_window_at}


def when(value: Any) -> datetime | None:
    try:
        return state.from_iso(value) if isinstance(value, str) else value if isinstance(value, datetime) else None
    except ValueError:
        return None


def run_action(action: str) -> None:
    """Run one action on the Pi (sudo needs no password there; see CLAUDE.md)."""
    subprocess.run(COMMANDS[action], check=True, timeout=30, capture_output=True, text=True)


def log_only(action: str) -> None:
    """The runner for `--fake-panel` and the Mac: say what would happen."""
    log.info("power (not applied here): %s", " ".join(COMMANDS[action]))


def apply(actions: list[str], runner: Runner) -> list[str]:
    """Run the actions in order; the errors, as text, for status.json (never raises)."""
    errors = []
    for action in actions:
        try:
            runner(action)
            log.info("power: %s", action)
        except Exception as exc:  # noqa: BLE001 - a failed switch must not fail the tick
            detail = getattr(exc, "stderr", None) or str(exc)
            errors.append(f"{action}: {str(detail).strip()}")
            log.warning("power: %s failed: %s", action, detail)
    return errors
