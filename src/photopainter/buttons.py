"""The buttons on the back of the frame: forward, back, a long press, and both together.

`photopainter buttons` (photopainter-buttons.service, always running) watches the two pins
(`hardware/buttons.py`) and turns presses into five actions:

- **Forward, back** (a press): the next or the previous thing, in one of two modes.
  - On the schedule, as the frame starts: the next or previous step of the rotation running
    now (`schedule.day` or `schedule.night`). The frame carries on through its cycle from
    there (`schedule.move_cycle`), until the period or the day ends. A photo step shows the next
    photo, whichever way the press went. A rotation of one step has nowhere to go, except a
    slideshow, which moves through its photos.
  - In photos only: the next photo (Up next first, then the rotation) or the one before the
    one up, or the last one shown if another screen is up.
- **Forward held for a second, then let go:** into photos only, or back to the schedule as set.
  Photos only is the dashboard's Photos mode until you change it: a slideshow at a photo step's
  pace, and it stays on until a long press or the dashboard's Auto ends it.
- **Both held for a second:** turn `battery.low_power` on or off, the dashboard's setting.
  It only acts on the battery (`power.py`), so plugged in it waits for the next time the
  frame is unplugged. On the battery it's also the way back to Wi-Fi and the dashboard
  without plugging in. The screen is redrawn so the mark that shows the setting is on
  (`render/battery_badge.py`) comes or goes.
- **Back held for a second, then let go:** with no Wi-Fi, the setup hotspot and its QR code
  (`hotspot.py`). With Wi-Fi working, only a redraw if the no-Wi-Fi mark was left up. A plain press
  stays the previous screen, so stepping through screens can't bring it up by accident. While the
  hotspot is up, any of these but both held calls it off.

None of them drive the panel: a tick does. A press fires when it's released, so that it can't
be the start of both being held. A press that comes while the last one is still on its way up
is ignored: the panel takes about 30 s, and pressing again meanwhile shouldn't skip things
unseen. Once it's up, the next press goes straight away.

This has to be its own service: low power stops the dashboard, and the buttons must work
then. It imports only what the watching needs, to stay small on 512 MB of RAM; drawing
happens in the tick, a child process (`web/jobs.py`).
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from photopainter import hotspot, screens, state
from photopainter.config import Config, ConfigError, edit_config, load_config
from photopainter.hardware.buttons import ButtonsUnavailable, GpioButtons
from photopainter.schedule import Overrides, Pin, SunTimes, move_cycle, rotation_at, show_now_until
from photopainter.slideshow import SlideshowState, list_images, rotation_order
from photopainter.web.jobs import TickRunner, photopainter_command

log = logging.getLogger(__name__)

NEXT, BACK, PHOTOS, LOW_POWER, WIFI_SETUP = "next", "back", "photos", "low_power", "wifi_setup"
ACTIONS = (NEXT, BACK, PHOTOS, LOW_POWER, WIFI_SETUP)

POLL_S = 0.03  # how often the pins are read
DEBOUNCE_S = 0.04  # a change has to last this long to count
BOTH_HOLD_S = 1.0  # both held this long turns low power on or off
LONG_PRESS_S = 1.0  # held this long, then let go: forward for photos only, back for Wi-Fi setup
MAX_PRESS_S = 5.0  # held longer than this is something leaning on it, not a press
STOP_WAIT_S = 240  # on a restart, how long a running tick gets to finish (a refresh gives up at 180 s)


class Decoder:
    """Turns samples of the two buttons into actions. Pure: time comes in with the samples."""

    def __init__(self) -> None:
        self._stable = (False, False)  # after debouncing
        self._sample = (False, False)
        self._sample_at = 0.0
        self._reset()

    def _reset(self) -> None:
        self._press_at: float | None = None  # when the first button of this press went down
        self._used: set[str] = set()  # the buttons that have been down in this press
        self._both_at: float | None = None
        self._spent = False  # both were held: nothing more until they're all up

    def feed(self, now: float, forward: bool, back: bool) -> str | None:
        """Take a sample; returns the action it completes, if any."""
        sample = (forward, back)
        if sample != self._sample:
            self._sample, self._sample_at = sample, now
        if sample != self._stable and now - self._sample_at >= DEBOUNCE_S:
            self._stable = sample
            return self._changed(now)
        if self._both_at is not None and not self._spent and now - self._both_at >= BOTH_HOLD_S:
            self._spent = True
            return LOW_POWER
        return None

    def _changed(self, now: float) -> str | None:
        forward, back = self._stable
        if not forward and not back:
            action = None
            held = now - self._press_at if self._press_at is not None else 0.0
            if not self._spent and len(self._used) == 1 and held <= MAX_PRESS_S:
                long = held >= LONG_PRESS_S
                action = (WIFI_SETUP if long else BACK) if BACK in self._used else PHOTOS if long else NEXT
            self._reset()
            return action
        if self._press_at is None:
            self._press_at = now
        if forward:
            self._used.add(NEXT)
        if back:
            self._used.add(BACK)
        if forward and back:
            self._both_at = now
        elif self._both_at is not None:  # let go of one before the second was up: called off
            self._both_at, self._spent = None, True
        return None


# --- the actions ---------------------------------------------------------------------------


def _choose(action: str, images: list[Path], current: str | None, last_shown: str | None,
            up_next: tuple[str, ...]) -> str:
    """The photo a press shows. `current`: the photo up, if one is."""
    names = [p.name for p in images]
    if action == NEXT:
        for name in up_next:
            if name in names and name != current:
                return name
        return rotation_order(images, current or last_shown)[0].name
    if current is None:  # a screen that isn't a photo: back to the photo shown last
        return last_shown if last_shown in names else rotation_order(images, last_shown)[-1].name
    order = rotation_order(images, current)  # ends with `current`, or the one before its place
    if order[-1].name != current:
        return order[-1].name
    return order[-2].name if len(order) > 1 else current


def _current_photo(overrides: Overrides, status: dict, now: datetime) -> str | None:
    pin = overrides.active_pin(now)
    if pin is not None and pin.kind == "photo" and pin.photo is not None:
        return pin.photo  # asked for by the last press, even if it isn't up yet
    showing = status.get("showing")
    if isinstance(showing, dict) and showing.get("kind") == "photo" and isinstance(showing.get("photo"), str):
        return showing["photo"]
    return None


def _photos_only(pin: Pin | None) -> bool:
    """A photo pin with no file named is the dashboard's Photos mode (a named one is Show now)."""
    return pin is not None and pin.kind == "photo" and pin.photo is None


def _images(config: Config) -> list[Path]:
    hidden = set(config.photos.hidden)
    try:
        return [p for p in list_images(config.photos.path) if p.name not in hidden]
    except OSError:
        return []


def step_photo(action: str, now: datetime, config: Config, folder: Path) -> str | None:
    """Show the next or previous photo. Its name, or None if there's nothing to change.

    In photos only, the slideshow stays on and carries on from that photo: the pin starts again
    now, and Up next has the photo first, for the tick to show and take off. Otherwise the photo
    is pinned like Show now. Either way the rotation moves there, so what comes next follows it.
    """
    images = _images(config)
    if not images:
        log.info("button: no photos to show")
        return None
    slideshow = SlideshowState(folder / state.SLIDESHOW)
    status = state.read_json(folder / state.STATUS, {})
    status = status if isinstance(status, dict) else {}
    with state.update_json(folder / state.OVERRIDES, Overrides().to_dict()) as data:
        overrides = Overrides.from_dict(data).without_expired(now)
        pin = overrides.active_pin(now)
        current = _current_photo(overrides, status, now)
        name = _choose(action, images, current, slideshow.last_shown(config.photos.path), overrides.up_next)
        if name != current:
            overrides = overrides.consume(name)
            if _photos_only(pin):
                overrides = replace(overrides, pin=Pin("photo", until=pin.until, set_at=now),
                                    up_next=(name, *overrides.up_next))
            else:
                overrides = replace(overrides, pin=Pin("photo", until=show_now_until(now, config), photo=name,
                                                       set_at=now))
        data.clear()
        data.update(overrides.to_dict())
    if name == current:  # one photo, and it's up
        return None
    slideshow.record(config.photos.path, name)
    return name


def step(action: str, now: datetime, config: Config, folder: Path, sun: SunTimes) -> str | None:
    """Forward or back: what it moved to, or None if there's nothing to change."""
    overrides = Overrides.from_dict(state.read_json(folder / state.OVERRIDES, {}))
    if _photos_only(overrides.active_pin(now)):
        return step_photo(action, now, config, folder)
    config = screens.schedule_here(config, photos=bool(_images(config)))
    steps = rotation_at(now, config, sun)
    if len(steps) == 1:  # a slideshow goes through its photos; anything else has nowhere to go
        return step_photo(action, now, config, folder) if steps[0].screen == "photo" else None
    by = 1 if action == NEXT else -1
    with state.update_json(folder / state.OVERRIDES, Overrides().to_dict()) as data:
        overrides = Overrides.from_dict(data).without_expired(now)
        cycle = move_cycle(now, config, overrides, sun, by)
        # The redraw is for quiet hours, where a moved rotation alone wouldn't reach the wall.
        fresh = replace(overrides, pin=None, cycle=cycle, redraw_at=now)
        data.clear()
        data.update(fresh.to_dict())
    return steps[cycle.step].screen


def toggle_photos_only(now: datetime, config: Config, folder: Path) -> bool | None:
    """Into photos only, or back to the schedule as set: True or False, for which. None if
    there are no photos to go to."""
    with state.update_json(folder / state.OVERRIDES, Overrides().to_dict()) as data:
        overrides = Overrides.from_dict(data).without_expired(now)
        leaving = _photos_only(overrides.active_pin(now))
        if not leaving and not _images(config):
            log.info("button: no photos to show")
            return None
        if leaving:  # the redraw is for quiet hours, where the schedule alone wouldn't reach the wall
            fresh = replace(overrides, pin=None, cycle=None, redraw_at=now)
        else:
            fresh = replace(overrides, pin=Pin("photo", until=None, set_at=now), cycle=None)
        data.clear()
        data.update(fresh.to_dict())
    return not leaving


def toggle_low_power(now: datetime, folder: Path) -> bool:
    """Turn `battery.low_power` the other way and ask for a redraw, for the mark. What it's
    set to now. Raises ConfigError if the settings file is bad: it's left as it is."""
    new = edit_config(lambda c: replace(c, battery=replace(c.battery, low_power=not c.battery.low_power)))
    with state.update_json(folder / state.OVERRIDES, Overrides().to_dict()) as data:
        fresh = replace(Overrides.from_dict(data).without_expired(now), redraw_at=now)
        data.clear()
        data.update(fresh.to_dict())
    return new.battery.low_power


def _sun(config: Config, folder: Path) -> SunTimes:
    """The tick's cached sun times. It runs every minute, so they're there without Skyfield."""
    from photopainter.tick import _sun_times

    return _sun_times(config, folder)


def act(action: str, now: datetime | None = None, sun: SunTimes | None = None) -> bool:
    """Carry out a press. Whether a tick should follow. Never raises: a press that goes
    wrong is logged, and the watching carries on."""
    folder = state.state_dir()
    config, error = load_config()
    if error:
        log.warning("using default settings: %s", error)
    at = (now or datetime.now(timezone.utc)).astimezone(config.location.tz).replace(microsecond=0)
    try:
        if action != LOW_POWER and hotspot.is_on(folder):
            log.info("button: %s calls off Wi-Fi setup", action)
            hotspot.cancel(folder, at)
            return True
        if action == WIFI_SETUP:
            log.info("button: Wi-Fi setup")
            hotspot.ask(folder, at)
            return True
        if action == LOW_POWER:
            log.info("button: low power %s", "on" if toggle_low_power(at, folder) else "off")
            return True
        if action == PHOTOS:
            on = toggle_photos_only(at, config, folder)
            if on is not None:
                log.info("button: %s", "photos only" if on else "back to the schedule")
            return on is not None
        moved_to = step(action, at, config, folder, sun or _sun(config, folder))
        if moved_to is None:
            log.info("button: %s has nowhere to go", action)
        else:
            log.info("button: %s to %s", action, moved_to)
        return moved_to is not None
    except ConfigError as exc:
        log.warning("button: %s: %s", action, exc)
    except Exception:  # noqa: BLE001 - a press must not stop the buttons working
        log.exception("button: %s failed", action)
    return False


# --- the watching --------------------------------------------------------------------------


def watch(buttons, on_action: Callable[[str], None], *, busy: Callable[[], bool] = lambda: False,
          stop: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> None:
    """Read the buttons until `stop()`, calling `on_action` for each press (unless `busy()`)."""
    decoder = Decoder()
    while not stop():
        forward, back = buttons.read()
        action = decoder.feed(clock(), forward, back)
        if action is not None:
            if busy():
                log.info("button: %s ignored: the last press is still going up", action)
            else:
                on_action(action)
        sleep(POLL_S)


def _going_up(ticks: TickRunner) -> bool:
    """The last press hasn't reached the wall: a tick is running or queued."""
    now = ticks.snapshot()
    return bool(now["running"] or now["queued"])


def main() -> int:
    """`photopainter buttons`: watch the buttons until SIGTERM or Ctrl-C."""
    try:
        buttons = GpioButtons()
    except ButtonsUnavailable as exc:
        log.error("%s", exc)
        return 2
    ticks = TickRunner(photopainter_command("tick"))
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))

    def on_action(action: str) -> None:
        if act(action):
            ticks.request()

    log.info("watching the buttons")
    try:
        watch(buttons, on_action, busy=lambda: _going_up(ticks), stop=lambda: bool(stopping))
    except KeyboardInterrupt:
        pass
    finally:
        buttons.close()
    if not ticks.close(wait_s=STOP_WAIT_S):
        log.warning("a tick was still running after %d s", STOP_WAIT_S)
    return 0


def press(action: str, *, fake_panel: bool = False, at: datetime | None = None) -> int:
    """`photopainter press ACTION`: what a button does, from the command line: the action,
    then a tick."""
    from photopainter.tick import main as tick_main

    if not act(action, at):
        return 0
    return tick_main(fake_panel=fake_panel, **({"at": at} if at is not None else {}))
