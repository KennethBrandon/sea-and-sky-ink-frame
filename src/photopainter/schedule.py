"""What the frame should show at a given moment: `decide()` (PRD.md §3 "Behavior").

`decide(now, config, overrides, sun)` is pure. It never reads the clock, files or the
network: the tick (and the dashboard's tests) hand it everything, so the whole schedule is
testable on the Mac with fake sun times.

Day and night
    Day runs from sunrise to sunset, widened by `night_offset_minutes` at both ends (night
    starts that long after sunset and ends as long before sunrise; negative narrows it).

Rotations, and their keys
    The period's rotation runs (`schedule.day` by day, `schedule.night` by night): a cycle of
    steps, each a screen for some minutes, anchored at local midnight, so [tides 30, sky 30]
    changes on :00 and :30 and [tides 60, sky 60] on even and odd hours (the grid starts
    again at the next midnight). A step's refresh bucket is the step clipped to the period.
    The scene key names the bucket, e.g. `tides/2026-11-03T14:10`, so every tick inside one
    bucket gives the same key and the panel refreshes only when a new bucket or period
    begins. A bucket that would last under 3 minutes (`MIN_REFRESH_MINUTES`) isn't drawn: at
    the end of a period (or day) the previous step runs on, at its start the next step starts
    early, and a bucket leaving under 3 minutes before the end runs on to it. A sky or sea
    step is drawn for the middle of its bucket (see `_night_scene`, `_sea_scene`), a tides
    step for now, and a combo step for both: its sky and sea for the middle, its tide and
    chart for when it came up (`_combo_scene`). In the hour
    that repeats when clocks fall back, the key carries the UTC offset so the two buckets stay
    distinct.

Photo steps
    A photo is a step like any other, by day or by night: the default day rotation (every
    screen for 10 minutes, the photo first) shows one for the first 10 minutes of every hour,
    and [photo 60] alone is a slideshow.
    Each bucket of a photo step is a new photo: the scene names Up next's first, or nothing,
    and the tick picks from the photo rotation when it draws, then moves it on (tick.py). With
    no photos to show, the tick decides with `Schedule.without_photos()` instead.

Moving the rotation (`overrides.json` `cycle`, written by the buttons)
    A step of the rotation that starts at `anchor`, with the steps after it following in
    order from there, until `until` (the end of the period, or midnight, where the grid starts
    again anyway). So after a press the frame carries on through its cycle from where it was
    put, rather than jumping back to where the clock would have had it. A pin still comes first.

Pins (`overrides.json`, written by the dashboard)
    An active pin replaces the schedule until `until` (None: until cleared); an expired one
    is ignored. Tides and sky pins redraw in buckets from `set_at`, every
    `Schedule.refresh_minutes` (that screen's step in the current period's rotation); a
    photo pin with a file name ("Show now") shows that photo; a photo pin without one
    shows a photo from the rotation, a new one every `Schedule.refresh_minutes` (the first
    photo step's length, else hourly). A card pin shows the color test card and never
    redraws it. Pins apply in quiet hours too.

Redraw (`overrides.json` `redraw_at`, written by the dashboard)
    A time; the tick redraws whatever it decides on if the image on the wall is older,
    quiet hours or not, then clears it (tick.py). `decide()` ignores it.

Up next
    Photo steps and photo pins without a name show the first name in `up_next` that
    isn't hidden; the tick removes it from the queue once it has been on the panel.

Quiet hours
    Inside them (and with no pin), `Decision.quiet` is True: the tick leaves the panel
    alone. `next_change_at` is then when they end.

Times: everything is compared and added in UTC, because two times in the same ZoneInfo
compare and add by wall clock (Python ignores the DST fold). Wall-clock rules (a rotation's
midnight, quiet hours) are converted from local time once, then handled in UTC.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from photopainter import state
from photopainter.astro.sun import SunEvents
from photopainter.config import MIN_REFRESH_MINUTES, Config, QuietHours, Step
from photopainter.scenes.base import Scene, SceneKind

__all__ = [
    "PIN_KINDS", "Decision", "Overrides", "Pin", "PinKind", "SunTimes", "decide",
    "next_day_night_change", "resolve_until", "show_now_until", "upcoming_change",
]

PinKind = Literal["tides", "sky", "sea", "photo", "card", "harbor", "combo"]
PIN_KINDS: tuple[PinKind, ...] = ("tides", "sky", "sea", "photo", "card", "harbor", "combo")
SunTimes = Callable[[date], SunEvents]  # sun events for a local calendar day

UTC = timezone.utc
# Nothing shorter than the panel's minimum refresh interval is worth drawing.
MIN_SHOW = timedelta(minutes=MIN_REFRESH_MINUTES)


def _night_scene(start: datetime, end: datetime, now: datetime, tz: ZoneInfo) -> Scene:
    """The night screen for the refresh bucket [start, end): the sky, drawn for the
    middle of the bucket. Night decisions and sky pins both come here."""
    return Scene("sky", _key("sky", start, tz), draw_for=(start + (end - start) / 2).astimezone(tz))


def _sea_scene(start: datetime, end: datetime, tz: ZoneInfo) -> Scene:
    """The sea screen for the refresh bucket [start, end), drawn for its middle like the sky."""
    return Scene("sea", _key("sea", start, tz), draw_for=(start + (end - start) / 2).astimezone(tz),
                 updated=start.astimezone(tz))


def _combo_scene(start: datetime, end: datetime, tz: ZoneInfo) -> Scene:
    """The combo screen for the refresh bucket [start, end): `draw_for` the middle, for its
    sky and sea; `updated` the start, so that its tide's "now" isn't ahead of the clock."""
    return Scene("combo", _key("combo", start, tz), draw_for=(start + (end - start) / 2).astimezone(tz),
                 updated=start.astimezone(tz))


# --- overrides ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Cycle:
    """The rotation moved by a button: step `step` started at `anchor` (PRD.md §5)."""

    anchor: datetime
    step: int
    until: datetime

    def active(self, now: datetime) -> bool:
        return self.anchor <= now < self.until

    def to_dict(self) -> dict[str, Any]:
        return {"anchor": state.to_iso(self.anchor), "step": self.step, "until": state.to_iso(self.until)}


def _parse_cycle(data: Any) -> Cycle | None:
    if not isinstance(data, dict):
        return None
    step = data.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        return None
    try:
        return Cycle(state.from_iso(data["anchor"]), step, state.from_iso(data["until"]))
    except (KeyError, TypeError, ValueError):
        return None  # can't tell when it ends: the plain schedule


@dataclass(frozen=True)
class Pin:
    """A mode pin from the dashboard (PRD.md §5 "Overrides")."""

    kind: PinKind
    until: datetime | None = None  # None: until cleared
    photo: str | None = None  # photo pins: this file ("Show now"); None = the rotation
    set_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.until is None or now < self.until

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "photo": self.photo,
                "until": None if self.until is None else state.to_iso(self.until),
                "set_at": None if self.set_at is None else state.to_iso(self.set_at)}


@dataclass(frozen=True)
class Overrides:
    """`overrides.json`: the dashboard's pin, Up next queue and redraw request."""

    pin: Pin | None = None  # None: Auto
    up_next: tuple[str, ...] = ()  # photo file names, first is next
    redraw_at: datetime | None = None  # redraw the wall if its image is older than this
    cycle: Cycle | None = None  # the rotation as a button moved it; None: as the clock has it

    @classmethod
    def from_dict(cls, data: Any) -> Overrides:
        """Read the file's JSON leniently: a pin that can't be used counts as Auto, names
        that aren't plain file names are dropped, and a redraw time that can't be read is
        no request. Never raises."""
        if not isinstance(data, dict):
            return cls()
        up_next = data.get("up_next")
        names = tuple(n for n in up_next if _is_file_name(n)) if isinstance(up_next, list) else ()
        try:
            redraw_at = None if data.get("redraw_at") is None else state.from_iso(data["redraw_at"])
        except (TypeError, ValueError):
            redraw_at = None
        return cls(pin=_parse_pin(data.get("pin")), up_next=names, redraw_at=redraw_at,
                   cycle=_parse_cycle(data.get("cycle")))

    def to_dict(self) -> dict[str, Any]:
        out = {"pin": None if self.pin is None else self.pin.to_dict(), "up_next": list(self.up_next),
               "redraw_at": None if self.redraw_at is None else state.to_iso(self.redraw_at)}
        if self.cycle is not None:
            out["cycle"] = self.cycle.to_dict()
        return out

    def redrawn(self) -> Overrides:
        """Without the redraw request (after the tick has redrawn)."""
        return self if self.redraw_at is None else replace(self, redraw_at=None)

    def active_pin(self, now: datetime) -> Pin | None:
        return self.pin if self.pin is not None and self.pin.active(now) else None

    def without_expired(self, now: datetime) -> Overrides:
        """Drop an expired pin or moved rotation (PRD: whoever writes the file next removes it)."""
        pin = self.pin if self.pin is None or self.pin.active(now) else None
        cycle = self.cycle if self.cycle is None or self.cycle.active(now) else None
        return self if pin is self.pin and cycle is self.cycle else replace(self, pin=pin, cycle=cycle)

    def consume(self, name: str) -> Overrides:
        """Remove the first `name` from Up next (after it has been shown)."""
        if name not in self.up_next:
            return self
        i = self.up_next.index(name)
        return replace(self, up_next=self.up_next[:i] + self.up_next[i + 1:])


def _parse_pin(data: Any) -> Pin | None:
    if not isinstance(data, dict) or data.get("kind") not in PIN_KINDS:
        return None
    photo = data.get("photo")
    if photo is not None and (data["kind"] != "photo" or not _is_file_name(photo)):
        return None
    try:
        until = None if data.get("until") is None else state.from_iso(data["until"])
    except (TypeError, ValueError):
        return None  # can't tell when it ends: Auto, so the frame can't get stuck on it
    try:
        set_at = None if data.get("set_at") is None else state.from_iso(data["set_at"])
    except (TypeError, ValueError):
        set_at = None
    return Pin(kind=data["kind"], until=until, photo=photo, set_at=set_at)


def _is_file_name(name: Any) -> bool:
    return (isinstance(name, str) and name not in ("", ".", "..")
            and not any(c in name for c in "/\\\0"))


# --- decide -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    scene: Scene
    next_change_at: datetime | None  # when the decision next changes; None = not on its own
    quiet: bool  # inside quiet hours: tick skips scheduled (non-pinned) changes


def decide(now: datetime, config: Config, overrides: Overrides, sun: SunTimes) -> Decision:
    """What the frame should show at `now` (PRD.md §3). Pure; see the module docstring."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"naive datetime {now!r}: `now` must be timezone-aware")
    tz = config.location.tz
    at = now.astimezone(UTC)
    pin = overrides.active_pin(at)
    if pin is not None:
        scene, next_at = _pinned(pin, at, config, overrides, sun, tz)
        quiet = False
    else:
        scene, next_at = _scheduled(at, config, overrides, sun, tz)
        quiet_end = _quiet_end(at, config.schedule.quiet_hours, tz)
        quiet = quiet_end is not None
        if quiet_end is not None:
            next_at = quiet_end  # nothing changes on the wall until quiet hours end
    return Decision(scene, None if next_at is None else next_at.astimezone(tz), quiet)


def upcoming_change(now: datetime, config: Config, overrides: Overrides,
                    sun: SunTimes) -> tuple[datetime, SceneKind] | None:
    """When the frame next changes on its own and to what kind of screen (status.json
    `next_change`). Unlike `Decision.next_change_at`, a change that falls in quiet hours
    is moved to when they end."""
    at = decide(now, config, overrides, sun).next_change_at
    for _ in range(3):  # a pin ending in quiet hours: the change waits for their end
        if at is None:
            return None
        then = decide(at, config, overrides, sun)
        if not then.quiet:
            return at, then.scene.kind
        at = then.next_change_at
    return None


def _scheduled(at: datetime, config: Config, overrides: Overrides, sun: SunTimes,
               tz: ZoneInfo) -> tuple[Scene, datetime]:
    """The default schedule: (scene, when it next changes)."""
    day, start, end = _period(at, config, sun, tz)
    steps = config.schedule.day if day else config.schedule.night
    step, b_start, b_end = _rotation_bucket(steps, start, end, at, tz, _moved(overrides, steps, at))
    if step.screen == "sky":
        return _night_scene(b_start, b_end, at, tz), b_end
    if step.screen == "sea":
        return _sea_scene(b_start, b_end, tz), b_end
    if step.screen == "combo":
        return _combo_scene(b_start, b_end, tz), b_end
    if step.screen == "photo":
        return Scene("photo", _key("photo", b_start, tz), draw_for=at.astimezone(tz),
                     photo=up_next_photo(overrides, config)), b_end
    return Scene(step.screen, _key(step.screen, b_start, tz), draw_for=at.astimezone(tz)), b_end


def _pinned(pin: Pin, at: datetime, config: Config, overrides: Overrides, sun: SunTimes,
            tz: ZoneInfo) -> tuple[Scene, datetime | None]:
    """A pinned screen: (scene, when it next changes, or None if it never does)."""
    until = None if pin.until is None else pin.until.astimezone(UTC)
    anchor = pin.set_at.astimezone(UTC) if pin.set_at is not None and pin.set_at <= at \
        else _local_midnight(at, tz)  # no set_at, or set "in the future" (the clock moved back)
    if pin.kind == "photo" and pin.photo is not None:
        key = f"{_key('photo', anchor, tz)}/{pin.photo}"
        return Scene("photo", key, draw_for=at.astimezone(tz), photo=pin.photo), until
    if pin.kind == "card":
        return Scene("card", _key("card", anchor, tz), draw_for=at.astimezone(tz)), until
    minutes = config.schedule.refresh_minutes(pin.kind, _period(at, config, sun, tz)[0])
    b_start, b_end = _bucket(anchor, until, at, timedelta(minutes=minutes))
    if pin.kind in ("tides", "harbor"):
        scene = Scene(pin.kind, _key(pin.kind, b_start, tz), draw_for=at.astimezone(tz))
    elif pin.kind == "sky":
        scene = _night_scene(b_start, b_end, at, tz)
    elif pin.kind == "sea":
        scene = _sea_scene(b_start, b_end, tz)
    elif pin.kind == "combo":
        scene = _combo_scene(b_start, b_end, tz)
    else:
        scene = Scene("photo", _key("photo", b_start, tz), draw_for=at.astimezone(tz),
                      photo=up_next_photo(overrides, config))
    return scene, b_end


# --- building blocks ----------------------------------------------------------------------


def _key(kind: str, start: datetime, tz: ZoneInfo) -> str:
    local = start.astimezone(tz)
    # The repeated hour when clocks fall back: two buckets share a wall-clock start.
    offset = f"{local:%z}" if local.fold else ""
    return f"{kind}/{local:%Y-%m-%dT%H:%M}{offset}"


def up_next_photo(overrides: Overrides, config: Config) -> str | None:
    hidden = set(config.photos.hidden)
    return next((n for n in overrides.up_next if n not in hidden), None)


def _local_midnight(at: datetime, tz: ZoneInfo, days: int = 0) -> datetime:
    """Local midnight of `at`'s date (+ `days`), in UTC."""
    return datetime.combine(at.astimezone(tz).date() + timedelta(days=days), time(0), tzinfo=tz).astimezone(UTC)


def _day_bounds(day: date, config: Config, sun: SunTimes) -> tuple[datetime, datetime]:
    """[start, end) of daytime on local date `day`, in UTC, with the night offset."""
    events = sun(day)
    off = timedelta(minutes=config.schedule.night_offset_minutes)
    return events.sunrise.astimezone(UTC) - off, events.sunset.astimezone(UTC) + off


def _period(at: datetime, config: Config, sun: SunTimes,
            tz: ZoneInfo) -> tuple[bool, datetime, datetime]:
    """(is day, start, end) of the day or night `at` is in (UTC)."""
    today = at.astimezone(tz).date()
    rise, set_ = _day_bounds(today, config, sun)
    if at < rise:
        return False, _day_bounds(today - timedelta(days=1), config, sun)[1], rise
    if at < set_:
        return True, rise, set_
    return False, set_, _day_bounds(today + timedelta(days=1), config, sun)[0]


def next_day_night_change(now: datetime, config: Config, sun: SunTimes) -> datetime:
    """The next switch between day and night (sunrise or sunset, with the night offset),
    local: what a pin "until the next scheduled change" lasts until."""
    _, _, end = _period(now.astimezone(UTC), config, sun, config.location.tz)
    return end.astimezone(config.location.tz)


def _bucket(anchor: datetime, seg_end: datetime | None, at: datetime,
            step: timedelta) -> tuple[datetime, datetime | None]:
    """The refresh bucket containing `at`: buckets start at `anchor` and every `step`
    after, until `seg_end` (None: no end). A bucket that would last under MIN_SHOW before
    `seg_end` isn't started; the one before it runs on."""
    n = (at - anchor) // step
    start = anchor + n * step
    if n > 0 and seg_end is not None and seg_end - start < MIN_SHOW:
        start -= step
    end = start + step
    if seg_end is not None and (end > seg_end or seg_end - end < MIN_SHOW):
        end = seg_end
    return start, end


def _rotation_bucket(steps: tuple[Step, ...], start: datetime, end: datetime, at: datetime,
                     tz: ZoneInfo, cycle: Cycle | None = None) -> tuple[Step, datetime, datetime]:
    """The rotation step at `at` and its refresh bucket, inside the period [start, end)."""
    index, b_start, b_end = _rotation_slot(steps, start, end, at, tz, cycle)
    return steps[index], b_start, b_end


def _rotation_slot(steps: tuple[Step, ...], start: datetime, end: datetime, at: datetime,
                   tz: ZoneInfo, cycle: Cycle | None = None) -> tuple[int, datetime, datetime]:
    """Which step of the rotation is up at `at`, and its refresh bucket, inside the period
    [start, end). Steps cycle from local midnight, or from `cycle` if a button has moved them,
    and the grid starts again at the next midnight. A bucket under MIN_SHOW isn't drawn: a
    short first one starts the next step early, a short last one lets the previous step run on."""
    origin = _local_midnight(at, tz)
    lo, hi = max(start, origin), min(end, _local_midnight(at, tz, days=1))
    order = list(range(len(steps)))
    if cycle is not None:
        origin, lo = cycle.anchor.astimezone(UTC), max(lo, cycle.anchor.astimezone(UTC))
        order = order[cycle.step:] + order[:cycle.step]
    length = timedelta(minutes=sum(s.minutes for s in steps))
    n = (at - origin) // length
    grid: list[tuple[datetime, datetime, int]] = []
    for k in (n - 1, n, n + 1):  # this cycle and its neighbours, for the rules below
        t0 = origin + k * length
        for index in order:
            t1 = t0 + timedelta(minutes=steps[index].minutes)
            grid.append((t0, t1, index))
            t0 = t1
    i = next(i for i, (t0, t1, _) in enumerate(grid) if t0 <= at < t1)
    t0, t1, index = grid[i]
    b_start, b_end = max(t0, lo), min(t1, hi)
    if b_start > lo and hi - b_start < MIN_SHOW:  # a short last bucket: the previous step runs on
        t0, _, index = grid[i - 1]
        b_start, b_end = max(t0, lo), hi
    elif b_start == lo and b_end < hi and b_end - b_start < MIN_SHOW:  # short first: the next starts early
        _, t1, index = grid[i + 1]
        b_end = min(t1, hi)
    if hi - b_end < MIN_SHOW:  # the one after would be too short: run on to the segment's end
        b_end = hi
    return index, b_start, b_end


def _moved(overrides: Overrides, steps: tuple[Step, ...], at: datetime) -> Cycle | None:
    """The moved rotation, if it's in force and still fits the rotation running now."""
    cycle = overrides.cycle
    return cycle if cycle is not None and cycle.active(at) and cycle.step < len(steps) else None


def rotation_at(now: datetime, config: Config, sun: SunTimes) -> tuple[Step, ...]:
    """The rotation running at `now`: the day's or the night's."""
    day, _, _ = _period(now.astimezone(UTC), config, sun, config.location.tz)
    return config.schedule.day if day else config.schedule.night


def move_cycle(now: datetime, config: Config, overrides: Overrides, sun: SunTimes, by: int) -> Cycle | None:
    """The rotation moved `by` steps from the step up at `now`: that step starts now and the
    others follow it, until the period or the day ends. None if there's only one step to go to.
    Starts on the minute, so that its changes fall on the timer's ticks."""
    tz = config.location.tz
    at = now.astimezone(UTC)
    day, start, end = _period(at, config, sun, tz)
    steps = config.schedule.day if day else config.schedule.night
    if len(steps) < 2:
        return None
    index, _, _ = _rotation_slot(steps, start, end, at, tz, _moved(overrides, steps, at))
    return Cycle(anchor=at.replace(second=0, microsecond=0).astimezone(tz), step=(index + by) % len(steps),
                 until=min(end, _local_midnight(at, tz, days=1)).astimezone(tz))


def _quiet_end(at: datetime, quiet: QuietHours | None, tz: ZoneInfo) -> datetime | None:
    """When the quiet hours `at` falls in end (UTC); None when `at` isn't in quiet hours."""
    if quiet is None:
        return None
    local = at.astimezone(tz)
    t = local.time()
    if quiet.start < quiet.end:
        inside = quiet.start <= t < quiet.end
    else:  # across midnight
        inside = t >= quiet.start or t < quiet.end
    if not inside:
        return None
    end_day = local.date() if t < quiet.end else local.date() + timedelta(days=1)
    return datetime.combine(end_day, quiet.end, tzinfo=tz).astimezone(UTC)


# --- helpers for the dashboard (W1) -----------------------------------------------------

_DURATION = re.compile(r"^(\d{1,4})([mh])$")


def resolve_until(value: Any, now: datetime, config: Config, sun: SunTimes) -> datetime | None:
    """`POST /api/mode`'s "until" as a time (local), or None for "until I change it".

    - None: until cleared
    - "next_change": the next sunrise or sunset (`next_day_night_change`), the default
    - "1h" (or any "<n>h" / "<n>m"): that long from now
    - an ISO 8601 time with a UTC offset, in the future

    Raises ValueError with a readable message for anything else.
    """
    tz = config.location.tz
    if value is None:
        return None
    if value == "next_change":
        return next_day_night_change(now, config, sun)
    if isinstance(value, str):
        m = _DURATION.match(value)
        if m:
            amount = timedelta(**{"minutes" if m[2] == "m" else "hours": int(m[1])})
            if amount <= timedelta(0):
                raise ValueError(f"until {value!r}: must be longer than zero")
            return (now.astimezone(UTC) + amount).astimezone(tz)
        try:
            until = state.from_iso(value)
        except ValueError:
            pass
        else:
            if until <= now:
                raise ValueError(f"until {value!r} is in the past")
            return until.astimezone(tz)
    raise ValueError(f"until {value!r}: expected null, \"next_change\", \"1h\" or an ISO time "
                     f"with a UTC offset")


def show_now_until(now: datetime, config: Config) -> datetime:
    """When a "Show now" photo pin set at `now` ends: `photos.show_now_minutes` later (local)."""
    later = now.astimezone(UTC) + timedelta(minutes=config.photos.show_now_minutes)
    return later.astimezone(config.location.tz)
