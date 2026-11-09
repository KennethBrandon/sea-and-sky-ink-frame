"""One tick: decide what the frame should show now, and refresh the panel if that changed.

`photopainter-tick.timer` runs `photopainter tick` every minute (and the dashboard will run
it right after an action). Each run (PRD.md §4):

1. Load the config. A bad file means the defaults, with the error in status.json.
2. Read the battery (`hardware/battery.py`): volts, current and state go in status.json
   (`battery`, null when there's no reading) and a line goes on `battery.csv` (the last
   five days or so). Two stages, each lasting until a tick sees the frame off the battery,
   so a voltage climbing back when the load drops doesn't undo them:
   - below `battery.indicator_volts`: every screen drawn carries a small battery
     (`render/battery_badge.py`), and the screen up gets one redraw so it shows at once
     (step 6). The fill changes only when the screen redraws anyway.
   - below `battery.low_volts`: the frame settles on a photo, keeping one that's up and
     otherwise showing the next, since e-ink keeps its last image after the cell dies and a
     photo never goes out of date. Then it only keeps reading the battery and flushing the
     SD card (no fetching, no schedule) until the power is back. It never shuts down: a
     halted Pi on this board stays halted when USB power returns, so someone would have to
     press the switch (docs/hardware.md "Can the Pi wake itself?"). A flat battery ends in a
     power cut like any other, and the Pi boots when the power returns.
   Then Wi-Fi (`hotspot.py`): whether the frame is on it, for status.json `wifi` and the no-Wi-Fi
   mark on screens drawn from then on, and one step of the setup hotspot, whose screens go
   up in place of the schedule's while someone is using it.
3. Check the clock. Until NTP has set it, keep the current image; after 10 minutes show photos,
   a new one an hour, since they're the one screen that doesn't need the time, or a "Clock not
   set" message if there are none. (There's no RTC yet; H2 adds it.)
4. Refresh the tide, weather, surf and gauge caches when they're due (`sources.ensure_all_fresh`,
   which never raises). Their errors go in status.json.
5. Read `overrides.json` and ask `schedule.decide()` what to show.
6. If the scene key differs from what's showing and it isn't quiet hours: write `refreshing`
   to status.json, render the scene, save it as `incoming.png` and add `panel_at` to
   `refreshing` (the dashboard changes to the new picture halfway through, as the panel does),
   refresh the panel. The same for the screen that's up if it should carry the battery indicator and doesn't. Back on USB
   power the indicator goes at the next redraw, with no refresh of its own.
7. Save `current.png` (the exact inks sent, as `preview_image` colors) and status.json.
   After a photo: move the rotation on, or take the photo off Up next.

When something goes wrong the panel keeps its image, status.json says why, and the next
tick tries again (PRD.md §3 "When things go wrong"):

- a screen fails to render, or the panel fails: `last_error`, exit status 1
- no photos in the folder: photo steps give way to the screens around them, and so does a screen
  with no data for this place, such as the harbor chart (`screens.schedule_here`); a pin of one is ignored

One tick runs at a time: a second one (the dashboard's while the timer's is refreshing)
waits for the first, then finds its key showing or draws what it came for.

`tick --at ISO` simulates a moment: the caches are used as they are (no fetching) and the clock
counts as set.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from photopainter import hotspot, power, screens, state
from photopainter.config import Config, load_config
from photopainter.hardware.battery import Battery, BatteryReader, BatteryUnavailable, FakeBattery, Reading
from photopainter.hardware.fake import FakePanel
from photopainter.hardware.panel import PanelDisplay, seconds_since_boot
from photopainter.render import battery_badge
from photopainter.render.palette import preview_image
from photopainter.render.pipeline import RenderOptions, to_panel_codes
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import photo as photo_scene
from photopainter.schedule import Overrides, SunTimes, decide, up_next_photo, upcoming_change

log = logging.getLogger(__name__)

CLOCK_GRACE_S = 10 * 60  # how long to wait for NTP before saying so on the panel
CLOCK_KEY = "message/clock-not-set"
CLOCK_PHOTO_KEY = "photo/no-clock"  # + the hours since boot: a new photo an hour
CLOCK_MESSAGE = (
    "Clock not set\n"
    "The frame sets its clock over the internet and hasn't been able to since it was plugged "
    "in. Tides and photos come back by themselves once it's online. If this stays up, check "
    "the Wi-Fi, or hold the back button for a second to set it up."
)
LOW_BATTERY_KEY = "low-battery"
BATTERY_LOG = "battery.csv"  # one line per tick: time, volts, milliamps, state
BATTERY_LOG_MAX_LINES = 8640  # trim when it gets this long (a line a minute: 6 days)…
BATTERY_LOG_KEEP_LINES = 7200  # …down to the last 5 days (the dashboard charts them)
TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")  # systemd-timesyncd, once synced
TICK_LOCK = "tick"  # state.locked() holds .tick.lock in the state dir
TICK_LOCK_TIMEOUT_S = 300  # longer than any tick: the panel gives up at 180 s
SUN_CACHE = "sun.json"  # sun times by local date, so a tick that changes nothing skips Skyfield
SUN_CACHE_DAYS = 8
SUN_FIELDS = ("sunrise", "sunset", "civil_dawn", "civil_dusk")
EMPTY_OVERRIDES: dict[str, Any] = {"pin": None, "up_next": [], "redraw_at": None}


class _Failed(Exception):
    """A step failed: (where, message) for status.json `last_error`."""

    def __init__(self, where: str, message: str):
        super().__init__(f"{where}: {message}")
        self.where, self.message = where, message


def main(*, fake_panel: bool = False, at: datetime | None = None) -> int:
    """`photopainter tick`. `at` simulates that moment (see the module docstring)."""
    folder = state.state_dir()
    battery: BatteryReader = FakeBattery(folder) if fake_panel else Battery()
    network = hotspot.FakeNetwork(folder) if fake_panel else hotspot.Nmcli()
    runner = power.log_only if fake_panel else power.run_action
    panel = FakePanel(folder) if fake_panel else PanelDisplay(state_dir=folder)
    if at is not None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError(f"naive datetime {at!r}: --at needs a time zone")
        return run(at, panel, fetch=False, clock_ok=True, wall=lambda: at, battery=battery, power_runner=runner,
                   network=network)
    return run(datetime.now(timezone.utc), panel, clock_ok=clock_synced() is not False,
               uptime_s=seconds_since_boot(), battery=battery, power_runner=runner, network=network)


def run(now: datetime, panel: PanelDisplay, *, fetch: bool = True,
        clock_ok: bool = True, uptime_s: float | None = None, sun: SunTimes | None = None,
        wall: Callable[[], datetime] | None = None, battery: BatteryReader | None = None,
        power_runner: power.Runner | None = None, network: hotspot.Network | None = None) -> int:
    """One tick at `now` on `panel`. 0 = fine (including nothing to do and quiet
    hours); 1 = something failed, and status.json `last_error` says what.

    `clock_ok`: NTP has set the clock; `uptime_s`: seconds since boot, for the clock
    grace period; `sun`: sun times by local date (default: Skyfield for the configured
    place, cached in sun.json); `wall`: the time for status stamps (default: the real clock);
    `battery`: where to read the battery (default: none, so status.json says null);
    `power_runner`: what runs the low-power actions (default: only logs them);
    `network`: the Wi-Fi radio (default: none, so status.json says null and there's no hotspot).
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(state.locked(state.state_dir() / TICK_LOCK, timeout=TICK_LOCK_TIMEOUT_S))
        except TimeoutError:
            log.warning("another tick has run for over %d s; skipping this one", TICK_LOCK_TIMEOUT_S)
            return 0
        return _Tick(now, panel, fetch=fetch, clock_ok=clock_ok, uptime_s=uptime_s,
                     sun=sun, wall=wall, battery=battery, power_runner=power_runner, network=network).run()
    return 1  # unreachable (ExitStack doesn't swallow exceptions); keeps type checkers happy


def clock_synced() -> bool | None:
    """Has NTP set the clock since boot? None when this system can't say (e.g. the Mac)."""
    if TIMESYNC_FLAG.exists():
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out.stdout.strip())


def _sun_times(config: Config, folder: Path) -> SunTimes:
    """Sun times by local date, kept in `sun.json` in the state dir.

    Working them out loads Skyfield and the ephemeris (about 2 s on the Pi), which a tick
    that changes nothing shouldn't pay every minute (PRD budget: < 3 s). Times are kept to
    the second, and fresh ones are rounded the same way, so cached and computed agree.
    """
    from photopainter.astro.sun import SunEvents, sun_events

    loc = config.location
    place = [loc.lat, loc.lon, loc.timezone]
    path = folder / SUN_CACHE
    memo: dict[date, SunEvents] = {}

    def sun(day: date) -> SunEvents:
        if day in memo:
            return memo[day]
        data = state.read_json(path, {})
        days = data.get("days") if isinstance(data, dict) and data.get("place") == place else None
        days = days if isinstance(days, dict) else {}
        try:
            hit = days[day.isoformat()]
            events = SunEvents(**{k: state.from_iso(hit[k]).astimezone(loc.tz) for k in SUN_FIELDS})
        except (KeyError, TypeError, ValueError):
            computed = sun_events(day, loc.lat, loc.lon, loc.tz)
            events = SunEvents(**{k: getattr(computed, k).replace(microsecond=0) for k in SUN_FIELDS})
            days[day.isoformat()] = {k: getattr(events, k) for k in SUN_FIELDS}
            keep = sorted(days)[-SUN_CACHE_DAYS:]
            try:
                state.write_json(path, {"place": place, "days": {d: days[d] for d in keep}})
            except Exception:  # noqa: BLE001 - only a cache
                log.warning("couldn't save %s", path, exc_info=True)
        memo[day] = events
        return events

    return sun


def _png(indices: np.ndarray) -> bytes:
    buf = io.BytesIO()
    preview_image(indices).save(buf, format="PNG")
    return buf.getvalue()


class _Tick:
    def __init__(self, now: datetime, panel: PanelDisplay, *, fetch: bool,
                 clock_ok: bool, uptime_s: float | None, sun: SunTimes | None,
                 wall: Callable[[], datetime] | None, battery: BatteryReader | None,
                 power_runner: power.Runner | None = None, network: hotspot.Network | None = None):
        self.dir = state.state_dir()
        self.config, self.config_error = load_config()
        self.tz = self.config.location.tz
        self.now = now.astimezone(self.tz)
        self.panel, self.fetch = panel, fetch
        self.clock_ok, self.uptime_s = clock_ok, uptime_s
        self.sun = sun or _sun_times(self.config, self.dir)
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.battery = battery
        self.power_runner = power_runner or power.log_only
        self.network = network
        self.no_wifi = False  # True: screens drawn now carry the no-Wi-Fi mark
        self.network_ok = True  # False: Wi-Fi is off, or was just switched on: don't fetch this tick
        self.sync_after = False  # True: flush the SD card after saving (the battery is low)
        self.indicator_percent: int | None = None  # set: every screen drawn carries the battery indicator
        prev = state.read_json(self.dir / state.STATUS, {})
        prev = prev if isinstance(prev, dict) else {}
        self.prev_battery = prev["battery"] if isinstance(prev.get("battery"), dict) else None
        self.prev_power = prev["power"] if isinstance(prev.get("power"), dict) else None
        self.prev_wifi = prev["wifi"] if isinstance(prev.get("wifi"), dict) else None
        self.status: dict[str, Any] = {
            "updated_at": None,
            "showing": prev["showing"] if isinstance(prev.get("showing"), dict) else None,
            "refreshing": None,
            "next_change": None,
            "last_error": prev["last_error"] if isinstance(prev.get("last_error"), dict) else None,
            "battery": None,
            "power": None,
            "wifi": None,
            "data": {"tides_fetched_at": None, "tides_until": None,
                     "config_error": self.config_error, "clock_ok": clock_ok},
        }

    # --- status ----------------------------------------------------------------------

    def stamp(self) -> datetime:
        return self.wall().astimezone(self.tz).replace(microsecond=0)

    def error(self, where: str, message: str, *, level: int = logging.ERROR) -> None:
        log.log(level, "%s: %s", where, message)
        self.status["last_error"] = {"at": self.stamp(), "where": where, "message": message}

    def save(self) -> None:
        self.status["updated_at"] = self.stamp()
        state.write_json(self.dir / state.STATUS, self.status)

    # --- the tick ----------------------------------------------------------------------

    def run(self) -> int:
        code = 0
        try:
            self._run()
        except _Failed as exc:
            self.error(exc.where, exc.message)
            code = 1
        except Exception as exc:  # noqa: BLE001 - record anything unexpected for the dashboard
            log.exception("tick failed")
            self.error("tick", f"{type(exc).__name__}: {exc}")
            code = 1
        self.status["refreshing"] = None
        try:
            self.save()
        except Exception:  # noqa: BLE001
            log.exception("couldn't write %s", state.STATUS)
            code = 1
        if self.sync_after:
            os.sync()
        return code

    def _run(self) -> None:
        if self.config_error:
            log.warning("using default settings: %s", self.config_error)
        reading = self._read_battery()
        if reading is not None and self._below(reading, "indicator", self.config.battery.indicator_volts):
            self.indicator_percent = reading.percent
        self._power(reading)
        low = reading is not None and self._is_low(reading)
        setup = self._hotspot(low)
        if low:
            self._low_battery(reading)
            return
        if setup.key is not None:
            self._show_setup(setup)
            return
        self._refresh_data()
        decided = self._decide()
        if decided is None:
            return
        scene, quiet, overrides = decided
        showing = self.status["showing"]
        if self._redraw_wanted(scene, overrides, showing):
            same = showing is not None and showing.get("key") == scene.key
            self._show(self._same_screen(scene, showing) if same else scene, overrides)
            return
        if showing is not None and showing.get("key") == scene.key:
            if self.indicator_percent is not None and not showing.get("indicator") and not quiet:
                log.info("battery below the indicator's setting: redrawing %s", scene.key)
                self._show(self._same_screen(scene, showing), overrides)
                return
            if setup.clear_mark and showing.get("no_wifi"):
                log.info("back button held with Wi-Fi working: redrawing %s without the mark", scene.key)
                self._show(self._same_screen(scene, showing), overrides)
                return
            log.debug("still showing %s", scene.key)
            return
        if quiet and showing is not None and not hotspot.is_setup_key(showing.get("key")):
            log.debug("quiet hours: leaving %s up instead of %s", showing.get("key"), scene.key)
            return
        self._show(scene, overrides)

    @staticmethod
    def _same_screen(scene: Scene, showing: dict) -> Scene:
        """`scene` as it's up now: a rotation photo's redraw is the same photo, not the next."""
        if scene.kind == "photo" and showing.get("kind") == "photo" and isinstance(showing.get("photo"), str):
            return replace(scene, photo=showing["photo"])
        return scene

    def _redraw_wanted(self, scene: Scene, overrides: Overrides, showing: dict | None) -> bool:
        """The dashboard asked for a redraw (`overrides.json` `redraw_at`) after the image
        on the wall was drawn. Quiet hours don't stop it, like a pin. A request older than
        the image is stale (the redraw already happened): cleared, and no refresh."""
        asked = overrides.redraw_at
        if asked is None:
            return False
        drawn = None
        if isinstance(showing, dict) and showing.get("refreshed_at"):
            try:
                drawn = state.from_iso(showing["refreshed_at"])
            except (TypeError, ValueError):
                drawn = None
        if drawn is not None and drawn >= asked:
            log.debug("redraw asked at %s is older than the image on the wall: clearing it", asked.isoformat())
            self._clear_redraw()
            return False
        log.info("redraw asked at %s: redrawing %s", f"{asked.astimezone(self.tz):%H:%M:%S}", scene.key)
        return True

    def _clear_redraw(self) -> None:
        try:
            with state.update_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES) as data:
                fresh = Overrides.from_dict(data).without_expired(self.now).redrawn()
                data.clear()
                data.update(fresh.to_dict())
        except Exception as exc:  # noqa: BLE001
            self.error("overrides", f"couldn't clear the redraw request: {exc}")

    # --- the battery -------------------------------------------------------------------

    def _power(self, reading: Reading | None) -> None:
        """Low power (power.py): decide from the reading, apply it, record it in status.json
        `power`. A tick that turns Wi-Fi off, or has just turned it on, doesn't fetch."""
        battery = self.status["battery"] or {}
        try:
            new, actions = power.decide(self.prev_power, reading, power.when(battery.get("on_battery_since")),
                                        self.now, self.config, stay_awake=power.STAY_AWAKE.exists())
        except Exception as exc:  # noqa: BLE001 - never worth failing the tick
            self.error("power", f"{type(exc).__name__}: {exc}", level=logging.WARNING)
            return
        self.status["power"] = new
        if new["wifi"] == "off" or "wifi_on" in actions:
            self.network_ok = False
        for problem in power.apply(actions, self.power_runner):
            self.error("power", problem, level=logging.WARNING)

    def _read_battery(self) -> Reading | None:
        """Read the battery into status.json `battery` and `battery.csv`; None if it can't be read."""
        if self.battery is None:
            return None
        try:
            reading = self.battery.read()
        except BatteryUnavailable as exc:
            log.debug("no battery reading: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - a reading is never worth failing the tick
            self.error("battery", f"{type(exc).__name__}: {exc}", level=logging.WARNING)
            return None
        since = None
        if reading.on_battery:
            prev = self.prev_battery or {}
            since = prev.get("on_battery_since") if prev.get("state") == "discharging" else None
            since = since if isinstance(since, str) else self.stamp()
        self.status["battery"] = {**reading.to_dict(), "on_battery_since": since,
                                  "indicator": self._below(reading, "indicator", self.config.battery.indicator_volts),
                                  "low": self._is_low(reading),
                                  "read_at": self.stamp()}
        try:
            self._log_battery(reading)
        except OSError as exc:
            log.warning("couldn't write %s: %s", BATTERY_LOG, exc)
        return reading

    def _log_battery(self, reading: Reading) -> None:
        path = self.dir / BATTERY_LOG
        line = f"{state.to_iso(self.stamp())},{reading.volts:.3f},{reading.milliamps:.1f},{reading.state}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            if fh.tell() == 0:
                fh.write("time,volts,milliamps,state\n")
            fh.write(line)
        if path.stat().st_size > BATTERY_LOG_MAX_LINES * 48:  # about that many lines; checked cheaply
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(lines) > BATTERY_LOG_MAX_LINES:
                state.write_atomic(path, "".join([lines[0], *lines[-BATTERY_LOG_KEEP_LINES:]]).encode("utf-8"))

    def _below(self, reading: Reading, field: str, volts: float) -> bool:
        """On the battery and below `volts`, or already found so (status.json `battery[field]`)
        on this run on the battery: a cell's voltage climbs back when the load drops, and that
        mustn't flap the indicator or put a screen that goes out of date back on the wall."""
        if not reading.on_battery:
            return False
        return reading.volts < volts or (self.prev_battery or {}).get(field) is True

    def _is_low(self, reading: Reading) -> bool:
        return self._below(reading, "low", self.config.battery.low_volts)

    def _low_battery(self, reading: Reading) -> None:
        """On battery and nearly flat: settle on a photo, then leave the panel alone until the
        power is back (see the module docstring). A photo that's up stays, with no refresh;
        otherwise the next one, as a photo step picks it. No fetching or schedule; status.json
        and battery.csv still record each reading, and the SD card is flushed after. With no
        photo to show, a blank page, not an old chart."""
        self.sync_after = True
        showing = self.status["showing"] or {}
        if showing.get("kind") == "photo" or showing.get("key") == LOW_BATTERY_KEY:
            log.debug("battery low (%s): waiting for power", reading)
            return
        log.warning("battery low (%s): moving to a photo and waiting for power", reading)
        overrides = Overrides()
        if photo_scene.available(self.config):
            overrides = Overrides.from_dict(state.read_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES))
            scene = Scene("photo", LOW_BATTERY_KEY, self.now, photo=up_next_photo(overrides, self.config))
        else:
            scene = Scene("message", LOW_BATTERY_KEY, self.now, message="")
        self._show(scene, overrides)

    # --- Wi-Fi --------------------------------------------------------------------------

    def _hotspot(self, low_battery: bool) -> hotspot.Outcome:
        """One step of the setup hotspot, then status.json `wifi` and whether the mark is due.
        Nothing here is worth failing the tick: the schedule goes on without it."""
        if self.network is None:
            return hotspot.Outcome()
        low_power = (self.status["power"] or {}).get("mode") == "low"
        try:
            outcome = hotspot.advance(self.dir, self.now, self.network, allowed=not (low_battery or low_power),
                                      boot=hotspot.boot_id())
            if outcome.error:
                self.error("wifi", outcome.error, level=logging.WARNING)
            self._read_wifi(hotspot.is_on(self.dir), low_power)
        except Exception as exc:  # noqa: BLE001
            self.error("wifi", f"{type(exc).__name__}: {exc}", level=logging.WARNING)
            return hotspot.Outcome()
        return outcome

    def _read_wifi(self, hotspot_on: bool, low_power: bool) -> None:
        """status.json `wifi`: `connected` (null: can't say, or Wi-Fi is off on purpose),
        `down_since`, `mark` and `hotspot`. With the hotspot up the radio is busy, so it counts
        as down for as long as it has been."""
        connected = None if hotspot_on or low_power else self.network.connected()
        down = hotspot_on or connected is False
        since = power.when((self.prev_wifi or {}).get("down_since")) if down else None
        if down and (since is None or since > self.now):  # new, or the clock has been set back since
            since = self.now
        self.no_wifi = down and not hotspot_on and hotspot.mark_due(since, self.now)
        self.status["wifi"] = {"connected": connected, "down_since": since, "mark": self.no_wifi,
                               "hotspot": hotspot_on}

    def _show_setup(self, setup: hotspot.Outcome) -> None:
        showing = self.status["showing"]
        if showing is not None and showing.get("key") == setup.key:
            log.debug("still showing %s", setup.key)
            return
        kind = "message" if setup.message is not None else "setup"
        self._show(Scene(kind, setup.key, self.now, message=setup.message), Overrides())

    # --- the data ----------------------------------------------------------------------

    def _refresh_data(self) -> None:
        from photopainter.sources import ensure_all_fresh, tides
        from photopainter.sources.common import fetched_at

        if self.fetch and self.clock_ok and self.network_ok:
            results = ensure_all_fresh(self.config, self.now)
            for r in results.values():
                if r.error:
                    self.error(r.where, r.error, level=logging.WARNING)  # offline is expected
            cache, fetched = results["tides"].cache, results["tides"].fetched_at
        else:  # simulating, or the clock is wrong (TLS would fail anyway): use what's cached
            cache = tides.load(self.dir)
            fetched = fetched_at(cache)
        until = tides.covers_until(cache)
        self.status["data"]["tides_fetched_at"] = fetched
        self.status["data"]["tides_until"] = None if until is None else until.astimezone(self.tz).date()

    def _decide(self) -> tuple[Scene, bool, Overrides] | None:
        """(scene, quiet, overrides), or None to leave the panel alone this tick."""
        if not self.clock_ok:
            if self.uptime_s is not None and self.uptime_s < CLOCK_GRACE_S:
                log.info("clock not set yet, %.0f s after boot; keeping the current image", self.uptime_s)
                return None
            if photo_scene.available(self.config):  # the one screen that doesn't need the time
                hour = int((self.uptime_s or 0) // 3600)
                return Scene("photo", f"{CLOCK_PHOTO_KEY}/{hour}", self.now), False, Overrides()
            return Scene("message", CLOCK_KEY, self.now, message=CLOCK_MESSAGE), False, Overrides()
        overrides = Overrides.from_dict(state.read_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES))
        gone = screens.missing(self.config)
        config = screens.schedule_here(self.config, photos=photo_scene.available(self.config), gone=gone)
        pin = overrides.active_pin(self.now)
        if pin is not None and pin.kind in gone:  # pinned before a move: let it lapse
            log.info("ignoring the %s pin: %s", pin.kind, gone[pin.kind])
            overrides = replace(overrides, pin=None)
        try:
            decision = decide(self.now, config, overrides, self.sun)
            change = upcoming_change(self.now, config, overrides, self.sun)
        except Exception as exc:  # noqa: BLE001 - e.g. the ephemeris is missing
            raise _Failed("schedule", f"{type(exc).__name__}: {exc}") from exc
        self.status["next_change"] = None if change is None else {"at": change[0], "kind": change[1]}
        return decision.scene, decision.quiet, overrides

    def _show(self, scene: Scene, overrides: Overrides) -> None:
        self.status["refreshing"] = {"key": scene.key, "started_at": self.stamp(), "panel_at": None}
        self.save()
        ctx = SceneContext.from_config(self.config, state_dir=self.dir)
        try:
            indices, shown = self._render(scene, ctx)
            if self.indicator_percent is not None:
                indices = battery_badge.draw(indices, self.indicator_percent)
            if self.config.battery.low_power:
                indices = battery_badge.draw_low_power(indices)
            mark = self.no_wifi and not hotspot.is_setup_key(scene.key)
            if mark:
                indices = battery_badge.draw_no_wifi(indices, battery_up=self.indicator_percent is not None)
            png = _png(indices)
        except Exception as exc:  # noqa: BLE001 - keep the previous image, try again next tick
            raise _Failed("render", f"{scene.key}: {exc}") from exc
        opts = RenderOptions(portrait=self.config.display.portrait, flip=self.config.display.flip)
        incoming = self.dir / state.INCOMING_PNG
        self._panel_starts(incoming, png)
        try:
            self.panel.show(to_panel_codes(indices, opts))
        except Exception as exc:  # noqa: BLE001
            incoming.unlink(missing_ok=True)
            raise _Failed("panel", f"{type(exc).__name__}: {exc}") from exc

        # On the wall now. Record that first: a failure below must not cause a redraw.
        self.status["showing"] = {"key": scene.key, "kind": shown.kind, "photo": shown.photo,
                                  "drawn_for": shown.draw_for, "refreshed_at": self.stamp(),
                                  "indicator": self.indicator_percent is not None, "no_wifi": mark}
        log.info("showing %s%s", scene.key, f" ({shown.photo})" if shown.photo else "")
        if overrides.redraw_at is not None:
            self._clear_redraw()
        try:
            state.write_atomic(self.dir / state.CURRENT_PNG, png)
            incoming.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            self.error("preview", f"couldn't save {state.CURRENT_PNG}: {exc}")
        if shown.kind == "photo":
            self._after_photo(scene, shown, overrides, ctx)

    def _panel_starts(self, incoming: Path, png: bytes) -> None:
        """Tell the dashboard the panel is starting and leave it the new picture, which it
        changes to halfway through, as the panel does. Nothing here may stop the refresh."""
        try:
            state.write_atomic(incoming, png)
        except Exception as exc:  # noqa: BLE001
            self.error("preview", f"couldn't save {state.INCOMING_PNG}: {exc}")
        self.status["refreshing"]["panel_at"] = self.stamp()
        self.save()

    def _render(self, scene: Scene, ctx: SceneContext) -> tuple[np.ndarray, Scene]:
        """(ink indices, the scene as drawn): for photos, the file actually used."""
        if scene.kind != "photo":
            return render_scene(scene, ctx), scene
        problem: Exception | None = None
        for path in photo_scene.candidates(scene, ctx):
            chosen = replace(scene, photo=path.name)
            try:
                return render_scene(chosen, ctx), chosen
            except Exception as exc:  # noqa: BLE001 - skip unreadable files
                log.warning("skipping photo %s: %s", path.name, exc)
                problem = exc
        where = photo_scene.folder(ctx)
        raise photo_scene.NoPhoto(f"no photo in {where} could be shown"
                                  + (f" (last problem: {problem})" if problem else ""))

    def _after_photo(self, scene: Scene, shown: Scene, overrides: Overrides, ctx: SceneContext) -> None:
        """Move the rotation on, or take the photo off Up next."""
        assert shown.photo is not None
        pin = overrides.active_pin(self.now)
        show_now = pin is not None and pin.kind == "photo" and pin.photo is not None
        try:
            if shown.photo != scene.photo:  # from the rotation (none asked for, or it wasn't there)
                photo_scene.record_shown(ctx, shown.photo)
            if scene.photo is not None and not show_now:  # it came from Up next: used up
                with state.update_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES) as data:
                    fresh = Overrides.from_dict(data).without_expired(self.now).consume(scene.photo)
                    data.clear()
                    data.update(fresh.to_dict())
        except Exception as exc:  # noqa: BLE001
            self.error("photo", f"after showing {shown.photo}: {exc}")
