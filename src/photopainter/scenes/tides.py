"""The day screen (scene kind `tides`): the tide at the Wedge, with the offshore swell,
the weather, sunrise and sunset, and the moon.

Layout at 800×480, designed with the Owner (approved 2026-11-03):

    The Wedge  Tuesday, November 3                             updated 2:10p
                                                        ◐ Waning crescent · 28%
    ╭ TIDE ──────────┬ OFFSHORE SWELL ─╮  ╭ WEATHER ────────────╮
    │ 3.4 ft ▲       │ 2.6 ft  11 s    │  │ ☀  74°              │
    │ Rising         │ from S 190° ↓   │  │ Mostly sunny         │
    │ High 4:49p 4.7 │ Waves 3.5 ft    │  │ High 74°  Low 58°    │
    │ Low 11:23p 0.3 │ Water 73°       │  │ W ← 9 mph · Rain 0%  │
    ╰────────────────┴─────────────────╯  ╰ ▲ 6:12a  ▼ 4:57p ───╯
    tide chart, by default 24 h back to 2 days ahead: water in blue; night as dots that
    fade in from 6° before sunset to astronomical dusk (and out again at dawn); the time of
    every high and low on a white label, with the height on the highest and lowest in view;
    a red line and dot at now; day names, with icons for new and full moons and eclipses;
    how far the water is from the tide table, as a red line over the last day and red dots
    ahead (blue stays the table)

The chart's span and labels come from the `tides` config (`chart_days_behind`,
`chart_days_ahead`, `label_tides`, `label_times`, `label_heights`). On a crowded chart a label that would
overlap another or hide the curve is left out: the highest and lowest come first, then the
next tides, soonest first.

Swell and wind have an arrow beside their direction, pointing the way the water or air is
moving as seen by someone facing south, as the frame's Viewer does: from the west points
left, from the south points down, toward them. `tides.flip_arrows` off makes north up
instead (see `flow_arrow`). The text still says where it comes from, as forecasts do.

Data rules (PRD.md §3): weather or swell over 2 h old says "as of <time>"; missing, over
12 h old or disabled in config, the section is left out and a "Sun & moon" box takes its
place. Without a tide cache the tide box and chart say so.

The measured water level (design chosen by the Owner 2026-11-04, PRD.md D11) is Newport's
tide table plus the gap between a gauge and its own table (`sources/gauge.py`). It's an
estimate for the Wedge, not a reading there. The gap is averaged over an hour for the line,
and held at the mean of the last 25 h ahead of now, which evens out a day's tides; it eases
from one to the other over 6 h so that the dots don't start with a step. That was off by
about 0.2 ft two days ahead in fall 2026, against 1.3 ft for the table alone, but it can't
see a new Kelvin wave or a storm coming, hence "likely". The big number, the next high and
low and the chart's labels all include the gap, so that they agree with each other. The
line's newest reading over 2 h old says "as of <time>"; over 12 h old, or with the gauge
missing or disabled, the screen is the tide table alone. "+1.3 over table" appears only from
0.5 ft: from last November through September the gap was usually about +0.4 ft.

Two halves: `day_model()` turns the caches and the sky into what to show (pure data,
tested on its own); the rest draws it on an `InkCanvas` in exact inks.

Not designed yet: a portrait layout (PRD Q6). On a 480×800 canvas this draws the
landscape layout cropped, so it renders but needs its own design if the frame hangs
portrait.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
from PIL import Image

from photopainter import state
from photopainter.astro.moon import MoonPhase, moon_phase
from photopainter.astro.sun import SunEvents, sun_events
from photopainter.config import Tides as TideSettings
from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.scenes.base import Scene, SceneContext

STALE_AFTER = timedelta(hours=2)  # say what time the data is from
HIDE_AFTER = timedelta(hours=12)  # leave the section out
CURRENT_WINDOW = timedelta(minutes=90)  # use the weather cache's "current" only this close to now
GAP_SMOOTHING_S = 3600.0  # the measured line: the gap averaged over this long
GAP_MEAN_S = 25 * 3600.0  # the gap ahead of now: the mean of this long, about a day's tides
GAP_EASE_S = 6 * 3600.0  # from the latest gap to that mean
GAP_BREAK_S = 3600.0  # the line stops where the nearest reading is further away than this
GAP_NOTE_FROM = 0.5  # ft from the table before the tide box mentions it

BLACK, WHITE, YELLOW, RED, BLUE, GREEN = (INK[n] for n in ("black", "white", "yellow", "red", "blue", "green"))


# --- model ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Extreme:
    t: datetime
    v: float  # ft above MLLW
    high: bool


@dataclass(frozen=True)
class Tide:
    times: np.ndarray  # POSIX seconds, sorted, ≤ 30 min apart
    heights: np.ndarray  # ft above MLLW
    extremes: tuple[Extreme, ...]  # every high and low in the cache, time-sorted
    height: float  # now
    rising: bool
    next_high: Extreme | None
    next_low: Extreme | None

    def height_at(self, t: float | np.ndarray) -> np.ndarray:
        """Heights at POSIX time(s) `t`: a Catmull-Rom spline through the curve points,
        held flat beyond the ends."""
        return _catmull_rom(self.times, self.heights, np.atleast_1d(np.asarray(t, dtype=float)))

    def covers(self, t: datetime) -> bool:
        return bool(self.times[0] <= t.timestamp() <= self.times[-1])

    def extremes_between(self, start: datetime, end: datetime) -> list[Extreme]:
        return [e for e in self.extremes if start <= e.t <= end]


@dataclass(frozen=True)
class Measured:
    """How far the water is from the tide table, in ft (`sources/gauge.py`)."""

    times: np.ndarray  # POSIX seconds of the readings, sorted; none after now
    gaps: np.ndarray  # over the table at those times, averaged over GAP_SMOOTHING_S
    ahead: float  # held from here on: the mean of the last GAP_MEAN_S
    last: datetime  # the newest reading
    stale: bool

    def gap_at(self, t: float | np.ndarray) -> np.ndarray:
        """The gap at POSIX time(s) `t`: measured up to the newest reading (the first one
        before that), then easing to `ahead`."""
        t = np.atleast_1d(np.asarray(t, dtype=float))
        gaps = np.interp(t, self.times, self.gaps)
        later = t > self.times[-1]
        ease = np.clip(1 - (t[later] - self.times[-1]) / GAP_EASE_S, 0, 1)
        gaps[later] = self.ahead + (self.gaps[-1] - self.ahead) * ease
        return gaps

    def has_reading(self, t: np.ndarray) -> np.ndarray:
        """Which times `t` lie among the readings, not in a hole in the gauge's record."""
        edges = np.concatenate(([-np.inf], self.times, [np.inf]))
        i = np.searchsorted(self.times, t)
        near = np.minimum(t - edges[i], edges[i + 1] - t)
        return (t >= self.times[0]) & (t <= self.times[-1]) & (near <= GAP_BREAK_S)


@dataclass(frozen=True)
class Weather:
    temp: float  # °F
    code: int | None  # WMO weather code
    wind: float | None  # mph
    gust: float | None
    wind_dir: float | None  # degrees true, where it comes from
    high: float | None  # today
    low: float | None
    precip_chance: float | None  # today's highest, %
    fetched_at: datetime
    stale: bool


@dataclass(frozen=True)
class Swell:
    wave_height: float | None  # ft, combined sea state
    wave_period: float | None
    wave_dir: float | None
    swell_height: float | None  # ft, primary swell
    swell_period: float | None  # s
    swell_dir: float | None  # degrees true, where it comes from
    water_temp: float | None  # °F
    fetched_at: datetime
    stale: bool


@dataclass(frozen=True)
class DayModel:
    place: str
    lat: float
    lon: float
    now: datetime  # local
    sun: SunEvents | None  # today's
    tide: Tide | None
    weather: Weather | None  # None: left out (missing, too old or disabled)
    swell: Swell | None
    moon: MoonPhase | None
    measured: Measured | None = None  # None: the tide table alone

    @property
    def daytime(self) -> bool:
        return self.sun is None or self.sun.sunrise <= self.now < self.sun.sunset

    def gap_at(self, t: float | np.ndarray) -> np.ndarray:
        """Feet the water is (or is likely to be) over the tide table at POSIX time(s) `t`."""
        t = np.atleast_1d(np.asarray(t, dtype=float))
        return np.zeros_like(t) if self.measured is None else self.measured.gap_at(t)

    def with_gap(self, e: Extreme) -> Extreme:
        """A high or low from the tide table, at the height the water is likely to reach."""
        return replace(e, v=e.v + float(self.gap_at(e.t.timestamp())[0]))


def day_model(now: datetime, ctx: SceneContext) -> DayModel:
    """Everything the day screen shows for the moment `now`."""
    from photopainter.astro import EphemerisMissing
    from photopainter.sources import gauge, surf, tides, weather

    config = ctx.config
    loc = config.location
    now = now.astimezone(loc.tz)
    try:
        sun = sun_events(now.date(), loc.lat, loc.lon, loc.tz)
    except (ValueError, EphemerisMissing):
        sun = None
    try:
        moon = moon_phase(now)
    except EphemerisMissing:
        moon = None
    return DayModel(
        place=loc.name, lat=loc.lat, lon=loc.lon, now=now, sun=sun,
        tide=load_tide(tides.load(ctx.state_dir), now),
        weather=load_weather(weather.load(ctx.state_dir), now, any_age=ctx.any_age) if config.weather.enabled else None,
        swell=load_swell(surf.load(ctx.state_dir), now, any_age=ctx.any_age) if config.surf.enabled else None,
        moon=moon,
        measured=load_measured(gauge.load(ctx.state_dir), now) if config.gauge.enabled else None,
    )


def load_tide(data: Any, now: datetime) -> Tide | None:
    """The tide at `now` from a tide cache (PRD §5), or None if it doesn't cover `now`."""
    try:
        points = sorted((state.from_iso(p["t"]).timestamp(), float(p["v"])) for p in data["curve"])
        extremes = sorted(
            (Extreme(state.from_iso(e["t"]).astimezone(now.tzinfo), float(e["v"]), e["type"] == "H")
             for e in data["hilo"]),
            key=lambda e: e.t)
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
    if len(points) < 2:
        return None
    times = np.array([p[0] for p in points])
    heights = np.array([p[1] for p in points])
    ts = now.timestamp()
    if not times[0] <= ts <= times[-1]:
        return None
    height = float(_catmull_rom(times, heights, np.array([ts]))[0])
    upcoming = [e for e in extremes if e.t > now]
    if upcoming:
        rising = upcoming[0].high
    else:  # past the last high/low in the cache: go by the curve
        rising = float(_catmull_rom(times, heights, np.array([ts + 600.0]))[0]) > height
    return Tide(times, heights, tuple(extremes), height, rising,
                next((e for e in upcoming if e.high), None),
                next((e for e in upcoming if not e.high), None))


def load_measured(data: Any, now: datetime) -> Measured | None:
    """The gap at `now` from a gauge cache (PRD §5), using only readings up to `now`; None if
    there are none or the newest is over HIDE_AFTER old."""
    try:
        rows = sorted((state.from_iso(r["t"]).timestamp(), float(r["v"])) for r in data["gap"])
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
    rows = [r for r in rows if r[0] <= now.timestamp()]
    if not rows:
        return None
    times = np.array([r[0] for r in rows])
    gaps = np.array([r[1] for r in rows])
    age = timedelta(seconds=now.timestamp() - float(times[-1]))  # right across a clock change
    if age > HIDE_AFTER:
        return None
    return Measured(times, _running_mean(times, gaps, GAP_SMOOTHING_S),
                    float(gaps[times >= times[-1] - GAP_MEAN_S].mean()),
                    datetime.fromtimestamp(times[-1], now.tzinfo), age > STALE_AFTER)


def _running_mean(times: np.ndarray, values: np.ndarray, span: float) -> np.ndarray:
    """Each value averaged with its neighbors within `span`/2, however the times are spaced."""
    totals = np.concatenate(([0.0], np.cumsum(values)))
    lo = np.searchsorted(times, times - span / 2, side="left")
    hi = np.searchsorted(times, times + span / 2, side="right")
    return (totals[hi] - totals[lo]) / (hi - lo)


def _age(data: Any, now: datetime, any_age: bool = False) -> tuple[datetime, bool] | None:
    """(fetched_at, stale), or None if the cache is missing, unreadable or too old.

    A cache stamped in the future (the clock was wrong when it was fetched, or is now)
    counts as fresh: its data is still the best there is. `any_age` is a preview of a moment
    somebody chose: `now` isn't the clock, so how long after the fetch it falls says nothing
    about how old the data is, and the forecast for it counts as fresh too.
    """
    try:
        fetched = state.from_iso(data["fetched_at"]).astimezone(now.tzinfo)
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
    if any_age:
        return fetched, False
    age = now - fetched
    if age > HIDE_AFTER:
        return None
    return fetched, age > STALE_AFTER


def _nearest_hour(rows: Any, now: datetime) -> dict | None:
    """The row closest to `now`, if one is within 45 minutes."""
    best, best_gap = None, timedelta(minutes=45)
    for row in rows or []:
        try:
            gap = abs(state.from_iso(row["t"]) - now)
        except (TypeError, KeyError, ValueError, AttributeError):
            continue
        if gap <= best_gap:
            best, best_gap = row, gap
    return best


def load_weather(data: Any, now: datetime, *, any_age: bool = False) -> Weather | None:
    """The weather for `now` from a weather cache: its "current" reading if that's within
    90 minutes, else the forecast hour; today's high, low and rain chance from "daily"."""
    age = _age(data, now, any_age)
    if age is None:
        return None
    try:
        current = data.get("current")
        if current and abs(state.from_iso(current["t"]) - now) <= CURRENT_WINDOW:
            row = current
        else:
            row = _nearest_hour(data.get("hourly"), now)
        if row is None or row.get("temp") is None:
            return None
        today = next((d for d in data.get("daily") or []
                      if isinstance(d, dict) and d.get("date") == now.date().isoformat()), {})
        code = _num(row.get("code"))
        return Weather(
            temp=float(row["temp"]), code=None if code is None else int(code),
            wind=_num(row.get("wind")), gust=_num(row.get("gust")), wind_dir=_num(row.get("wind_dir")),
            high=_num(today.get("high")), low=_num(today.get("low")),
            precip_chance=_num(today.get("precip_chance")),
            fetched_at=age[0], stale=age[1])
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def load_swell(data: Any, now: datetime, *, any_age: bool = False) -> Swell | None:
    """The offshore swell for the hour nearest `now` from a surf cache."""
    age = _age(data, now, any_age)
    if age is None:
        return None
    try:
        row = _nearest_hour(data.get("hourly"), now)
        if row is None or row.get("swell_height") is None:
            return None
        return Swell(
            wave_height=_num(row.get("wave_height")), wave_period=_num(row.get("wave_period")),
            wave_dir=_num(row.get("wave_dir")), swell_height=_num(row.get("swell_height")),
            swell_period=_num(row.get("swell_period")), swell_dir=_num(row.get("swell_dir")),
            water_temp=_num(row.get("water_temp")), fetched_at=age[0], stale=age[1])
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _catmull_rom(xs: np.ndarray, ys: np.ndarray, x: np.ndarray) -> np.ndarray:
    x = np.clip(x, xs[0], xs[-1])
    i = np.clip(np.searchsorted(xs, x, side="right") - 1, 0, len(xs) - 2)
    u = (x - xs[i]) / (xs[i + 1] - xs[i])
    p0 = ys[np.maximum(i - 1, 0)]
    p1, p2 = ys[i], ys[i + 1]
    p3 = ys[np.minimum(i + 2, len(ys) - 1)]
    return 0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u ** 2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3)


def sun_altitude(times: np.ndarray, lat: float, lon: float) -> np.ndarray | None:
    """The Sun's altitude in degrees at POSIX times `times` (computed every 10 minutes, or
    1000 times at most, and interpolated). None if the ephemeris isn't available."""
    from photopainter.astro import EphemerisMissing, ephemeris, timescale

    try:
        eph = ephemeris()
    except EphemerisMissing:
        return None
    from skyfield.api import wgs84

    start, end = float(times.min()), float(times.max())
    step = max(600.0, (end - start) / 1000.0)  # a long chart mustn't cost the Pi more
    grid = np.arange(start, end + step, step)
    t = timescale().utc(1970, 1, 1, 0, 0, grid)
    observer = eph["earth"] + wgs84.latlon(lat, lon)
    alt = observer.at(t).observe(eph["sun"]).apparent().altaz()[0].degrees
    return np.interp(times, grid, alt)


@dataclass(frozen=True)
class SkyEvent:
    t: datetime
    kind: str  # "new" | "full" | "lunar_eclipse" | "solar_eclipse"


EARTH_RADIUS_KM = 6378.137
SUN_RADIUS_KM = 696_000.0
MOON_RADIUS_KM = 1737.4


def sky_events(start: datetime, end: datetime) -> list[SkyEvent]:
    """New and full moons between `start` and `end`, with the ones that are eclipses
    (anywhere on Earth) marked as such. Empty if the ephemeris isn't available."""
    from photopainter.astro import EphemerisMissing, ephemeris, timescale

    try:
        eph = ephemeris()
    except EphemerisMissing:
        return []
    from skyfield import almanac, eclipselib

    tz = start.tzinfo
    ts = timescale()
    t0, t1 = ts.from_datetime(start), ts.from_datetime(end)
    events: list[SkyEvent] = []
    times, phases = almanac.find_discrete(t0, t1, almanac.moon_phases(eph))
    for t, phase in zip(times, phases):
        if phase == 0:
            kind = "solar_eclipse" if _solar_eclipse_at(eph, t) else "new"
        elif phase == 2:
            kind = "full"
        else:
            continue
        events.append(SkyEvent(t.utc_datetime().astimezone(tz), kind))
    eclipse_times, _, _ = eclipselib.lunar_eclipses(t0, t1, eph)
    for t in eclipse_times:  # at greatest eclipse, in place of that full moon
        at = t.utc_datetime().astimezone(tz)
        events = [e for e in events if not (e.kind == "full" and abs(e.t - at) < timedelta(days=1))]
        events.append(SkyEvent(at, "lunar_eclipse"))
    return sorted(events, key=lambda e: e.t)


def _solar_eclipse_at(eph: Any, new_moon: Any) -> bool:
    """Whether the new moon at Skyfield time `new_moon` eclipses the Sun anywhere on Earth.

    Skyfield has no solar eclipse finder. Seen from Earth's center, there's an eclipse
    somewhere when the Moon passes closer to the Sun than their apparent radii plus the
    Moon's parallax (less the Sun's): the Moon's penumbra then touches Earth. Checked
    against every eclipse of 2024-2027 in the tests.
    """
    from photopainter.astro import timescale

    times = timescale().tt_jd(new_moon.tt + np.arange(-0.25, 0.2501, 1 / 144))  # ±6 h, 10 min steps
    earth = eph["earth"].at(times)
    sun = earth.observe(eph["sun"]).apparent()
    moon = earth.observe(eph["moon"]).apparent()
    sep = sun.separation_from(moon).radians
    i = int(np.argmin(sep))
    d_sun, d_moon = float(sun.distance().km[i]), float(moon.distance().km[i])
    limit = (math.asin(EARTH_RADIUS_KM / d_moon) - math.asin(EARTH_RADIUS_KM / d_sun)
             + math.asin(SUN_RADIUS_KM / d_sun) + math.asin(MOON_RADIUS_KM / d_moon))
    return float(sep[i]) < limit


# --- formatting ----------------------------------------------------------------------------

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(degrees: float | None) -> str:
    return "–" if degrees is None else COMPASS[round(degrees / 22.5) % 16]


def clock(t: datetime) -> str:
    """4:49p, 11:23a, 12:07p (noon), 12:02a (midnight)."""
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'a' if t.hour < 12 else 'p'}"


def ft(v: float | None) -> str:
    """One decimal, with a true minus sign for negative tides; '–' if unknown."""
    if v is None:
        return "–"
    s = f"{v:.1f}"
    return "0.0" if s == "-0.0" else s.replace("-", "−")


def deg(v: float | None) -> str:
    return "–" if v is None else f"{round(v)}°"


def whole(v: float | None) -> str:
    return "–" if v is None else str(round(v))


# (WMO codes, day label, night label, icon)
CONDITIONS: tuple[tuple[tuple[int, ...], str, str, str], ...] = (
    ((0,), "Sunny", "Clear", "sun"),
    ((1,), "Mostly sunny", "Mostly clear", "sun_cloud"),
    ((2,), "Partly cloudy", "Partly cloudy", "sun_cloud"),
    ((3,), "Cloudy", "Cloudy", "cloud"),
    ((45, 48), "Fog", "Fog", "fog"),
    ((51, 53, 55, 56, 57), "Drizzle", "Drizzle", "rain"),
    ((61, 63, 65, 66, 67), "Rain", "Rain", "rain"),
    ((80, 81, 82), "Showers", "Showers", "rain"),
    ((71, 73, 75, 77, 85, 86), "Snow", "Snow", "snow"),
    ((95, 96, 99), "Thunderstorms", "Thunderstorms", "thunder"),
)


def condition(code: int | None, daytime: bool = True) -> tuple[str, str]:
    """(label, icon) for a WMO weather code. At night the sun icons become moons."""
    for codes, day_label, night_label, icon in CONDITIONS:
        if code in codes:
            if not daytime:
                icon = {"sun": "moon", "sun_cloud": "moon_cloud"}.get(icon, icon)
            return (day_label if daytime else night_label), icon
    return "", "cloud"


# --- drawing primitives ----------------------------------------------------------------------


def text(c: InkCanvas, xy: tuple[float, float], s: str, size: int, *, bold: bool = False,
         fill: int = BLACK, anchor: str = "ls") -> int:
    """Draw `s`; returns its width in pixels."""
    f = font(size, bold)
    c.draw.text((round(xy[0]), round(xy[1])), s, font=f, fill=fill, anchor=anchor)
    return text_width(s, f)


def pill_rect(c: InkCanvas, xy: tuple[float, float], s: str, size: int, *, anchor: str,
              pad: tuple[int, int] = (4, 3)) -> tuple[int, int, int, int]:
    """The box `pills` draws behind this text; `pad=(0, 0)` gives the text's own box."""
    l, t, r, b = c.draw.textbbox((round(xy[0]), round(xy[1])), s, font=font(size, True), anchor=anchor)
    return l - pad[0], t - pad[1], r + pad[0], b + pad[1]


def pills(c: InkCanvas, labels: list[tuple[tuple[float, float], str, str]], size: int) -> None:
    """Bold black text on white rounded boxes, readable over the night dots and the water.
    `labels` holds (xy, text, anchor). The boxes all go down first, so none covers a
    neighbor's text."""
    for xy, s, anchor in labels:
        c.draw.rounded_rectangle(pill_rect(c, xy, s, size, anchor=anchor), radius=4, fill=WHITE)
    for xy, s, anchor in labels:
        c.draw.text((round(xy[0]), round(xy[1])), s, font=font(size, True), fill=BLACK, anchor=anchor)


def disc(c: InkCanvas, cx: float, cy: float, r: float, fill: int, outline: int | None = None,
         width: int = 0) -> None:
    if outline is not None and width:
        c.draw.ellipse([cx - r - width, cy - r - width, cx + r + width, cy + r + width], fill=outline)
    c.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def triangle(c: InkCanvas, cx: float, cy: float, size: float, up: bool, fill: int) -> None:
    h = size * 0.9
    tip, base = (cy - h / 2, cy + h / 2) if up else (cy + h / 2, cy - h / 2)
    c.draw.polygon([(cx, tip), (cx + size / 2, base), (cx - size / 2, base)], fill=fill)


def flow_arrow(c: InkCanvas, cx: float, cy: float, size: float, from_deg: float, *, flip: bool = False,
               fill: int = BLUE) -> None:
    """An arrow `size` px long centered on (cx, cy), for something coming from `from_deg`
    (degrees true), pointing the way it's moving. North is up: from the west (270°) it points
    right, from the south (180°) up. `flip` turns it 180°, as seen facing south: from the
    west it points left, from the south down."""
    heading = math.radians(from_deg if flip else from_deg + 180)
    fx, fy = math.sin(heading), -math.cos(heading)  # forward, in screen pixels (y grows down)
    sx, sy = -fy, fx  # sideways
    shape = ((-0.5, -0.11), (0.02, -0.11), (0.02, -0.38), (0.5, 0.0), (0.02, 0.38), (0.02, 0.11), (-0.5, 0.11))
    c.draw.polygon([(cx + (f * fx + w * sx) * size, cy + (f * fy + w * sy) * size) for f, w in shape], fill=fill)


def paint(c: InkCanvas, x0: int, y0: int, mask: np.ndarray, ink: int) -> None:
    """Set the pixels where `mask` (rows × columns, placed at x0, y0) is true to `ink`."""
    if mask.any():
        h, w = mask.shape
        c.image.paste(ink, (x0, y0, x0 + w, y0 + h), mask=Image.fromarray(mask.astype(np.uint8) * 255, "L"))


def _cloud(c: InkCanvas, cx: float, cy: float, s: float, fill: int, width: int) -> None:
    parts = [(-0.17, 0.06, 0.17), (0.04, -0.04, 0.23), (0.23, 0.09, 0.15)]
    rect = (-0.34, 0.06, 0.38, 0.24)
    for grow, ink in ((width, BLACK), (0, fill)):
        for dx, dy, r in parts:
            disc(c, cx + dx * s, cy + dy * s, r * s + grow, ink)
        c.draw.rounded_rectangle(
            [cx + rect[0] * s - grow, cy + rect[1] * s - grow, cx + rect[2] * s + grow, cy + rect[3] * s + grow],
            radius=max(1, int(0.08 * s)), fill=ink)


def _sun(c: InkCanvas, cx: float, cy: float, s: float, width: int) -> None:
    r = 0.2 * s
    for k in range(8):
        a = k * math.pi / 4
        c.draw.line([(cx + math.cos(a) * (r + 0.08 * s), cy + math.sin(a) * (r + 0.08 * s)),
                     (cx + math.cos(a) * (r + 0.22 * s), cy + math.sin(a) * (r + 0.22 * s))],
                    fill=BLACK, width=max(2, round(s / 22)))
    disc(c, cx, cy, r, YELLOW, BLACK, width)


def _crescent(c: InkCanvas, cx: float, cy: float, s: float, width: int) -> None:
    """A yellow crescent moon with a black outline, about 0.5 s across."""
    r = 0.25 * s
    cut_dx, cut_dy, cut_r = 0.45 * r, -0.3 * r, 0.82 * r
    x0, y0 = int(cx - r - width - 1), int(cy - r - width - 1)
    size = int(2 * (r + width) + 3)
    yy, xx = np.mgrid[y0:y0 + size, x0:x0 + size]
    d_out = np.hypot(xx - cx, yy - cy)
    d_cut = np.hypot(xx - (cx + cut_dx), yy - (cy + cut_dy))
    lit = (d_out <= r) & (d_cut > cut_r)
    edge = (d_out <= r + width) & (d_cut > cut_r - width) & ~lit
    paint(c, x0, y0, edge, BLACK)
    paint(c, x0, y0, lit, YELLOW)


def weather_icon(c: InkCanvas, cx: float, cy: float, s: float, kind: str, *, on_dark: bool = False) -> None:
    """A weather icon about `s` px square, centered on (cx, cy). `on_dark`: for a blue or
    black background, where blue rain and snow, black fog and a black cloud would be lost,
    so they're white."""
    w = max(2, round(s / 28))
    accent = WHITE if on_dark else BLUE
    if kind == "sun":
        _sun(c, cx, cy, s, w)
    elif kind == "moon":
        _crescent(c, cx, cy, s * 1.4, w)
    elif kind in ("sun_cloud", "moon_cloud"):
        if kind == "sun_cloud":
            _sun(c, cx - 0.16 * s, cy - 0.14 * s, s * 0.8, w)
        else:
            _crescent(c, cx - 0.14 * s, cy - 0.16 * s, s * 1.05, w)
        _cloud(c, cx + 0.08 * s, cy + 0.08 * s, s * 0.8, WHITE, w)
    elif kind == "fog":
        for k, (a, b) in enumerate([(-0.4, 0.3), (-0.3, 0.4), (-0.4, 0.25), (-0.25, 0.4)]):
            y = cy - 0.24 * s + k * 0.16 * s
            c.draw.line([(cx + a * s, y), (cx + b * s, y)], fill=WHITE if on_dark else BLACK,
                        width=max(3, round(s / 14)))
    else:  # cloud, rain, snow, thunder
        below = kind in ("rain", "snow", "thunder")
        _cloud(c, cx, cy - (0.12 * s if below else 0), s, BLACK if kind == "thunder" and not on_dark else WHITE, w)
        if kind == "rain":
            for k in range(4):
                x = cx - 0.24 * s + k * 0.16 * s
                c.draw.line([(x, cy + 0.2 * s), (x - 0.07 * s, cy + 0.42 * s)], fill=accent,
                            width=max(3, round(s / 16)))
        elif kind == "snow":
            for k in range(3):
                disc(c, cx - 0.2 * s + k * 0.2 * s, cy + 0.32 * s, 0.05 * s, accent)
        elif kind == "thunder":
            bolt = [(0.04, 0.08), (-0.1, 0.3), (0.0, 0.3), (-0.06, 0.48), (0.14, 0.22), (0.03, 0.22), (0.1, 0.08)]
            c.draw.polygon([(cx + x * s, cy + y * s) for x, y in bolt], fill=YELLOW, outline=BLACK,
                           width=max(2, w - 1))


def moon_disc(c: InkCanvas, cx: float, cy: float, r: float, phase_deg: float) -> None:
    """The Moon's phase as seen from the northern hemisphere: lit (white) on the right
    while waxing, dark part black."""
    disc(c, cx, cy, r, BLACK, BLACK, 2)
    cos_p = math.cos(math.radians(phase_deg))
    waxing = (phase_deg % 360) < 180
    for y in range(-int(r), int(r) + 1):
        w = math.sqrt(max(0.0, r * r - y * y))
        a, b = (w * cos_p, w) if waxing else (-w, -w * cos_p)
        if b - a >= 1:
            c.draw.line([(cx + a, cy + y), (cx + b, cy + y)], fill=WHITE)


def sky_icon(c: InkCanvas, cx: float, cy: float, r: float, kind: str) -> None:
    """New moon ●, full moon ○, lunar eclipse (a red moon), solar eclipse (black disc in a
    yellow ring)."""
    if kind == "new":
        disc(c, cx, cy, r, BLACK)
    elif kind == "full":
        disc(c, cx, cy, r, WHITE, BLACK, 2)
    elif kind == "lunar_eclipse":
        disc(c, cx, cy, r, RED, BLACK, 2)
    elif kind == "solar_eclipse":
        disc(c, cx, cy, r + 1, YELLOW, BLACK, 2)
        disc(c, cx, cy, r - 3, BLACK)


def sun_glyph(c: InkCanvas, cx: float, cy: float, s: float, rising: bool) -> None:
    """A half sun on a horizon line (at cy) with an arrow up (sunrise) or down (sunset)."""
    r = s * 0.3
    c.draw.pieslice([cx - r, cy - r, cx + r, cy + r], 180, 360, fill=YELLOW, outline=BLACK, width=2)
    c.draw.line([(cx - s * 0.5, cy), (cx + s * 0.5, cy)], fill=BLACK, width=2)
    triangle(c, cx, cy - r - s * 0.22, s * 0.28, rising, BLACK)


def _bayer(n: int) -> np.ndarray:
    """n × n ordered-dither thresholds, evenly spread in (0, 1)."""
    m = np.zeros((1, 1))
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


BAYER8 = _bayer(8)


def shade(c: InkCanvas, x0: int, y0: int, y1: int, density: np.ndarray, ink: int) -> None:
    """Ordered dots of `ink` from column x0 on, rows y0..y1, covering `density` (0-1, one
    value per column) of the pixels. Crisp on e-ink, and smooth as a gradient."""
    ys = np.arange(y0, y1)[:, None] % 8
    xs = (x0 + np.arange(len(density)))[None, :] % 8
    paint(c, x0, y0, BAYER8[ys, xs] < density[None, :], ink)


# --- tide chart ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartStyle:
    night_density: float = 0.5  # dot coverage of the sky at full night
    twilight_from: float = 6.0  # Sun altitude (deg) where the dots start, a little before sunset…
    twilight_to: float = -18.0  # …and where they reach full night (astronomical dusk/dawn)
    axis_size: int = 20  # day names
    label_size: int = 16  # labels at each high and low; 0 = none
    icon_radius: int = 9  # new/full moon and eclipse icons
    daily_labels: bool = False  # label only each day's highest high and lowest low in view
    show_times: bool = True  # the time on each label
    heights: str = "extremes"  # height on the highest and lowest in view | "all" | "none"
    measured_width: int = 3  # the red line
    dot_gap: float = 8.0  # the red dots ahead of now: from one to the next along the line
    dot_radius: float = 2.0
    clear_margin: float = 2.0  # white around the red, to keep it clear of the night dots
    now_radius: int = 8  # the dot at now


def scale_marks(extremes: list[Extreme]) -> tuple[Extreme | None, Extreme | None]:
    """The highest high and lowest low among `extremes`: their labels give heights."""
    highs = [e for e in extremes if e.high]
    lows = [e for e in extremes if not e.high]
    return (max(highs, key=lambda e: e.v) if highs else None,
            min(lows, key=lambda e: e.v) if lows else None)


def daily_extremes(extremes: list[Extreme]) -> list[Extreme]:
    """Each local day's highest high and lowest low among `extremes`, in time order."""
    days: dict[date, list[Extreme]] = {}
    for e in extremes:
        days.setdefault(e.t.date(), []).append(e)
    kept = [e for day in days.values() for e in scale_marks(day) if e is not None]
    return sorted(kept, key=lambda e: e.t)


def extreme_label(e: Extreme, marks: tuple[Extreme | None, Extreme | None], show_times: bool,
                  heights: str) -> str:
    """'4:49p', '4:49p 4.7 ft', '4.7 ft' or '' (no label) for a high or low."""
    parts = [clock(e.t)] if show_times else []
    if heights == "all" or (heights == "extremes" and e in marks):
        parts.append(f"{ft(e.v)} ft")
    return " ".join(parts)


def label_priority(extremes: list[Extreme], marks: tuple[Extreme | None, Extreme | None],
                   now: datetime) -> list[Extreme]:
    """The order to place labels in, the first to keep when they'd overlap: the highest and
    lowest, then the tides to come (soonest first), then the past (latest first)."""
    return sorted(extremes, key=lambda e: (e not in marks, e.t < now, abs(e.t - now)))


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def hides_curve(rect: tuple[int, int, int, int], curve_ys: np.ndarray, x0: int) -> bool:
    """Whether a label box would cover the tide line. `curve_ys` is the line's y in each
    column from x0 on (NaN where there's no data)."""
    ys = curve_ys[max(rect[0] - x0, 0):max(rect[2] + 1 - x0, 0)]
    return bool(((ys >= rect[1] - 1) & (ys <= rect[3] + 1)).any())


def day_label(day: date, today: bool, day_width: float, axis_size: int) -> str:
    """Today, Tue or T: the longest form that fits one whole day of the chart. It's judged
    by the widest name in bold, so every day on the row shortens together."""
    bold = font(axis_size, True)
    if today and text_width("Today", bold) + 8 <= day_width:
        return "Today"
    name = f"{day:%a}"
    return name if text_width("Wed", bold) + 8 <= day_width else name[0]


def runs(mask: np.ndarray) -> list[np.ndarray]:
    """The indices where `mask` is true, in unbroken pieces."""
    idx = np.flatnonzero(mask)
    return np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if idx.size else []


def dots_along(pts: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    """Points every `gap` along a line, starting at its first point."""
    dots, travelled, next_at = [], 0.0, 0.0
    for a, b in zip(pts, pts[1:]):
        if travelled >= next_at:
            dots.append(a)
            next_at += gap
        travelled += math.hypot(b[0] - a[0], b[1] - a[1])
    return dots


def place_label(center: float, width: float, lo: float, hi: float,
                taken: list[tuple[float, float]]) -> float | None:
    """Center x for a label `width` wide inside [lo, hi] that avoids the `taken` spans: the
    preferred `center` if it's free, else the nearest free spot; None if nothing fits."""
    candidates = [center] + [x for a, b in taken for x in (a - width / 2, b + width / 2)]
    for x in sorted(candidates, key=lambda x: abs(x - center)):
        left, right = x - width / 2, x + width / 2
        if left >= lo and right <= hi and all(right <= a or left >= b for a, b in taken):
            return x
    return None


def tide_chart(c: InkCanvas, box: tuple[int, int, int, int], m: DayModel, start: datetime,
               end: datetime, style: ChartStyle = ChartStyle()) -> None:
    """The tide from `start` to `end` in `box` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    tide = m.tide
    if tide is None:
        text(c, ((x0 + x1) / 2, (y0 + y1) / 2), "No tide data yet", 26, anchor="mm")
        return
    yb = y1 - style.axis_size - 10  # baseline of the plot
    labels = bool(style.label_size) and (style.show_times or style.heights != "none")
    label_h = style.label_size + 12 if labels else 0
    y_top = y0 + 6 + label_h  # the highest point in view sits here…
    y_bottom = yb - 6 - label_h  # …and the lowest here, leaving room for their labels
    t0, t1 = start.timestamp(), end.timestamp()
    cols = np.arange(x0, x1 + 1)
    ts = t0 + (cols - x0) / (x1 - x0) * (t1 - t0)
    have = (ts >= tide.times[0]) & (ts <= tide.times[-1])  # the cache may not reach both ends
    vs = tide.height_at(ts)  # the tide table: the blue
    levels = vs + m.gap_at(ts)  # where the water is, or is likely to be: the red
    lo = float(min(vs[have].min(), levels[have].min())) if have.any() else 0.0
    hi = max(float(max(vs[have].max(), levels[have].max())) if have.any() else 0.0, lo + 1.0)

    def X(t: float) -> float:
        return x0 + (t - t0) / (t1 - t0) * (x1 - x0)

    def Y(v: float) -> float:
        return y_bottom - (v - lo) / (hi - lo) * (y_bottom - y_top)

    alt = sun_altitude(ts, m.lat, m.lon)
    if alt is not None:
        dark = np.clip((style.twilight_from - alt) / (style.twilight_from - style.twilight_to), 0, 1)
        shade(c, x0, y0, int(yb), dark * style.night_density, BLACK)

    measured_pts: list[list[tuple[float, float]]] = []  # in pieces, where the gauge has holes
    dots: list[tuple[float, float]] = []
    if m.measured is not None:
        level_pts = [(float(a), float(b)) for a, b in zip(cols, Y(levels))]
        measured_pts = [[level_pts[i] for i in run] for run in runs(m.measured.has_reading(ts) & have)]
        measured_pts = [p for p in measured_pts if len(p) > 1]
        ahead = np.flatnonzero(have & (ts > m.measured.times[-1]))
        dots = dots_along([level_pts[i] for i in ahead], style.dot_gap)
        # white first, so the night dots don't eat the red; the water goes on top of it
        for p in measured_pts:
            c.draw.line(p, fill=WHITE, width=round(style.measured_width + 2 * style.clear_margin))
        r = style.dot_radius + style.clear_margin - 0.5
        for x, y in dots:
            c.draw.ellipse((x - r, y - r, x + r, y + r), fill=WHITE)

    if have.any():
        pts = [(float(cols[i]), Y(float(vs[i]))) for i in np.flatnonzero(have)]
        c.draw.polygon(pts + [(pts[-1][0], yb), (pts[0][0], yb)], fill=BLUE)
        c.draw.line(pts, fill=BLACK, width=3)
    c.draw.line([(x0, yb), (x1, yb)], fill=BLACK, width=2)
    if tide.covers(m.now):  # under the labels, which stay readable on top of it
        c.draw.line([(X(m.now.timestamp()), y0), (X(m.now.timestamp()), yb)], fill=RED, width=3)

    for p in measured_pts:
        c.draw.line(p, fill=RED, width=style.measured_width)
    for x, y in dots:
        r = style.dot_radius
        c.draw.ellipse((x - r, y - r, x + r, y + r), fill=RED)

    def spot(e: Extreme) -> tuple[tuple[float, float], str, str]:
        """(xy, words, anchor) of a high's or low's label: outside both the table and the water."""
        label = extreme_label(e, marks, style.show_times, style.heights)
        half = text_width(label, font(style.label_size, True)) / 2 + 4
        x = min(max(X(e.t.timestamp()), x0 + half + 2), x1 - half - 2)
        table = e.v - float(m.gap_at(e.t.timestamp())[0])
        return ((x, Y(max(e.v, table)) - 8), label, "mb") if e.high else ((x, Y(min(e.v, table)) + 9), label, "mt")

    def words_rect(e: Extreme) -> tuple[int, int, int, int]:
        # the text, 2 px wider each way so that labels side by side don't read as one
        xy, label, anchor = spot(e)
        return pill_rect(c, xy, label, style.label_size, anchor=anchor, pad=(2, -1))

    shown: list[Extreme] = []
    marks: tuple[Extreme | None, Extreme | None] = (None, None)
    if labels:
        shown = [m.with_gap(e) for e in tide.extremes_between(start, end) if tide.covers(e.t)]
        marks = scale_marks(shown)
        if style.daily_labels:
            shown = daily_extremes(shown)  # the highest and lowest in view are always among these

    taken: list[tuple[int, int, int, int]] = []
    if labels:
        curve_ys = np.where(have, Y(vs), np.nan)
        level_ys = np.where(have, Y(levels), np.nan)
        kept: list[tuple[tuple[float, float], str, str]] = []
        for e in label_priority(shown, marks, m.now):
            if not extreme_label(e, marks, style.show_times, style.heights):
                continue
            xy, label, anchor = spot(e)
            words = words_rect(e)
            if any(rects_overlap(words, o) for o in taken):
                continue
            box_rect = pill_rect(c, xy, label, style.label_size, anchor=anchor)
            if e not in marks and (hides_curve(box_rect, curve_ys, x0) or hides_curve(box_rect, level_ys, x0)):
                continue
            taken.append(words)
            kept.append((xy, label, anchor))
        pills(c, kept, style.label_size)

    # new and full moons and eclipses, as icons on the day-name row
    r = style.icon_radius
    icon_y = y1 - 2 - style.axis_size * 0.36
    icons: list[tuple[float, float]] = []
    for ev in sky_events(start, end):
        ix = min(max(X(ev.t.timestamp()), x0 + r + 2), x1 - r - 2)
        sky_icon(c, ix, icon_y, r, ev.kind)
        icons.append((ix - r - 6, ix + r + 6))

    # midnight ticks; each day's name centered on the part of it in view, beside any icon
    tz = m.now.tzinfo
    day_width = (x1 - x0) * 86400.0 / (t1 - t0)
    day = start.date()
    while day <= end.date():
        d0 = datetime.combine(day, time(0), tzinfo=tz).timestamp()
        d1 = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).timestamp()
        if t0 < d0 < t1:
            c.draw.line([(X(d0), yb), (X(d0), yb + 8)], fill=BLACK, width=2)
        a, b = X(max(d0, t0)), X(min(d1, t1))
        today = day == m.now.date()
        label = day_label(day, today, day_width, style.axis_size)
        x = place_label((a + b) / 2, text_width(label, font(style.axis_size, today)), a + 4, b - 4, icons)
        if x is not None:
            text(c, (x, y1 - 2), label, style.axis_size, bold=today, anchor="ms")
        day += timedelta(days=1)

    if tide.covers(m.now):
        disc(c, X(m.now.timestamp()), Y(tide.height + float(m.gap_at(m.now.timestamp())[0])),
             style.now_radius, RED, WHITE, min(3, style.now_radius // 2))


# --- sections ------------------------------------------------------------------------------


def heading(c: InkCanvas, x: float, y: float, s: str) -> None:
    text(c, (x, y), s, 18, bold=True, fill=BLUE)


def stale_note(c: InkCanvas, xy: tuple[float, float], fetched_at: datetime, anchor: str = "ls") -> None:
    text(c, xy, f"as of {clock(fetched_at)}", 16, bold=True, fill=RED, anchor=anchor)


def gap_note(gap: float) -> str | None:
    """'+1.3 over table', once the water is far enough from the tide table to mention."""
    if round(abs(gap), 1) < GAP_NOTE_FROM:
        return None
    return f"+{ft(gap)} over table" if gap > 0 else f"{ft(gap)} under table"


def tide_block(c: InkCanvas, m: DayModel, x: int, y: int, big: int = 78) -> None:
    heading(c, x, y + 18, "TIDE")
    if m.tide is None:
        text(c, (x, y + 70), "No tide data", 22)
        return
    if m.measured is not None and m.measured.stale:
        stale_note(c, (x + 60, y + 18), m.measured.last)
    base = y + 24 + big * 0.95
    gap = float(m.gap_at(m.now.timestamp())[0])
    w = text(c, (x, base), ft(m.tide.height + gap), big, bold=True)
    w += 6 + text(c, (x + w + 6, base), "ft", round(big * 0.38))
    s = round(big * 0.36)
    triangle(c, x + w + 12 + s / 2, base - big * 0.56, s, m.tide.rising, BLUE)
    yy = base + 32
    trend = text(c, (x, yy), "Rising" if m.tide.rising else "Falling", 24)
    note = gap_note(gap)
    if note:
        room = TIDE_W - 28 - trend - 10
        size = next((s for s in (18, 16) if text_width(note, font(s, True)) <= room), 14)
        text(c, (x + trend + 10, yy), note, size, bold=True, fill=RED)
    yy += 38
    for e in (m.tide.next_high, m.tide.next_low):
        if e is None:
            continue
        e = m.with_gap(e)
        text(c, (x, yy), "High" if e.high else "Low", 22, bold=True)
        text(c, (x + 66, yy), f"{clock(e.t)}  {ft(e.v)} ft", 22)
        yy += 30


def swell_block(c: InkCanvas, m: DayModel, x: int, y: int, big: int = 50, small: int = 22,
                flip_arrows: bool = False) -> None:
    s = m.swell
    if s is None:
        return
    heading(c, x, y + 18, "OFFSHORE SWELL")  # the model's swell off Newport, not surf at the Wedge
    base = y + 24 + big * 0.95
    xx = x + text(c, (x, base), ft(s.swell_height), big, bold=True)
    xx += text(c, (xx + 4, base), " ft", round(big * 0.5)) + 14
    if s.swell_period is not None:
        xx += text(c, (xx, base), whole(s.swell_period), big, bold=True)
        text(c, (xx + 4, base), " s", round(big * 0.5))
    yy = base + 32
    w = text(c, (x, yy), f"from {compass(s.swell_dir)} {deg(s.swell_dir)}", small)
    if s.swell_dir is not None:
        flow_arrow(c, x + w + 8 + small / 2, yy - small * 0.36, small, s.swell_dir, flip=flip_arrows)
    yy += small + 8
    text(c, (x, yy), f"Waves {ft(s.wave_height)} ft", small)
    yy += small + 8
    text(c, (x, yy), f"Water {deg(s.water_temp)}", small)
    if s.stale:
        stale_note(c, (x, yy + small + 8), s.fetched_at)


def weather_block(c: InkCanvas, m: DayModel, x: int, y: int, x1: int, big: int = 60,
                  small: int = 20, icon: int = 74, flip_arrows: bool = False) -> None:
    w = m.weather
    if w is None:
        return
    heading(c, x, y + 18, "WEATHER")
    if w.stale:
        stale_note(c, (x1, y + 18), w.fetched_at, anchor="rs")
    y += 24
    label, kind = condition(w.code, m.daytime)
    weather_icon(c, x + icon / 2, y + icon / 2 + 2, icon, kind)
    text(c, (x + icon + 10, y + big * 0.9), f"{round(w.temp)}°", big, bold=True)
    yy = y + max(icon, big) + small + 4
    if label:
        text(c, (x, yy), label, small)
    yy += small + 8
    text(c, (x, yy), f"High {deg(w.high)}  Low {deg(w.low)}", small)
    yy += small + 8
    wind_text(c, x, yy, x1 - x, w, small, flip_arrows)


def wind_text(c: InkCanvas, x: int, y: float, max_width: int, w: Weather, size: int,
              flip_arrows: bool = False) -> None:
    """The wind line, with an arrow after the wind's compass direction."""
    room = 0 if w.wind is None or w.wind_dir is None else size + 8
    # the arrow takes the place of the space after the direction
    line = wind_line(w, max_width - max(room - text_width(" ", font(size)), 0), font(size))
    if not room:
        text(c, (x, y), line, size)
        return
    direction, _, rest = line.partition(" ")
    x += text(c, (x, y), direction, size)
    flow_arrow(c, x + 4 + size / 2, y - size * 0.36, size, w.wind_dir, flip=flip_arrows)
    text(c, (x + room, y), rest, size)


def wind_line(w: Weather, max_width: int, fnt: Any) -> str:
    """'NW 5–11 mph · Rain 0%' (wind, gusts, today's rain chance), shortened to fit."""
    rain = [] if w.precip_chance is None else [f"Rain {round(w.precip_chance)}%"]
    if w.wind is None:
        return " · ".join(rain)
    speed = f"{round(w.wind)}"
    if w.gust is not None and round(w.gust) > round(w.wind):
        speed += f"–{round(w.gust)}"
    for wind in (f"{compass(w.wind_dir)} {speed} mph", f"{compass(w.wind_dir)} {speed}",
                 f"{compass(w.wind_dir)} {round(w.wind)}"):
        line = " · ".join([wind] + rain)
        if text_width(line, fnt) <= max_width:
            return line
    return line


def sun_times(c: InkCanvas, m: DayModel, x: int, y: int, size: int = 20, stacked: bool = False) -> None:
    """▲ 6:12a  ▼ 4:57p with the baseline at y (or one under the other)."""
    if m.sun is None:
        return
    sun_glyph(c, x + 14, y - 4, 26, True)
    w = text(c, (x + 32, y), clock(m.sun.sunrise), size, bold=True)
    if stacked:
        y += size + 18
    else:
        x += 32 + w + 16
    sun_glyph(c, x + 14, y - 4, 26, False)
    text(c, (x + 32, y), clock(m.sun.sunset), size, bold=True)


def sun_moon_block(c: InkCanvas, m: DayModel, x0: int, y0: int, x1: int, y1: int) -> None:
    """Sunrise, sunset and the moon: fills the space of a section that's left out."""
    x = x0 + 14
    heading(c, x, y0 + 20, "SUN & MOON")
    wide = x1 - x0 >= 400  # sun times left, moon right; else one above the other
    sun_times(c, m, x, y0 + (106 if wide else 62), 26, stacked=True)
    if m.moon is None:
        return
    if wide:
        r, mx, my = 40, x0 + (x1 - x0) // 2 - 10, y0 + 128
    else:
        c.draw.line([(x, y0 + 122), (x1 - 14, y0 + 122)], fill=BLACK, width=1)
        r, mx, my = 30, x, y0 + 172
    moon_disc(c, mx + r, my, r, m.moon.phase_deg)
    lines = m.moon.name.split(" ", 1) + [f"{round(m.moon.illuminated * 100)}% lit"]
    for k, line in enumerate(lines):
        text(c, (mx + 2 * r + 14, my - 18 + k * 24), line, 20)


def box(c: InkCanvas, x0: int, y0: int, x1: int, y1: int) -> None:
    c.draw.rounded_rectangle([x0, y0, x1, y1], radius=10, outline=BLACK, width=2)


# --- layout --------------------------------------------------------------------------------

MARGIN = 12
GAP = 12
TIDE_W = 262  # tide column, inside the tide + swell box
SWELL_W = 246
WEATHER_W = 256
HEADER_RIGHT_W = 200  # reserved at the right of the header for "updated" and the moon


def top_row(left: int, right: int, has_swell: bool, has_weather: bool) -> list[tuple[str, int, int]]:
    """[(box, x0, x1)] for the boxes across the top: "marine" (tide, and the swell if
    there is any), "sun_moon" in the space a missing section leaves, and "weather"."""
    marine_end = left + TIDE_W + (SWELL_W if has_swell else 0)
    boxes = [("marine", left, marine_end)]
    weather_start = right - WEATHER_W if has_weather else right + GAP
    if not (has_swell and has_weather):
        boxes.append(("sun_moon", marine_end + GAP, weather_start - GAP))
    if has_weather:
        boxes.append(("weather", weather_start, right))
    return boxes


def header_date(m: DayModel, max_width: int) -> tuple[str, int]:
    """The date line after the place name, shortened or shrunk until it fits."""
    place_w = text_width(m.place, font(30, True)) + 22
    for fmt, size in (("%A, %B {d}", 30), ("%a, %B {d}", 30), ("%a, %b {d}", 30), ("%a, %b {d}", 24)):
        s = m.now.strftime(fmt).replace("{d}", str(m.now.day))
        if place_w + text_width(s, font(size)) <= max_width:
            return s, size
    return m.now.strftime("%a, %b {d}").replace("{d}", str(m.now.day)), 20


def layout(c: InkCanvas, m: DayModel, settings: TideSettings = TideSettings()) -> None:
    W, H = c.size
    top, bottom = 58, 292
    boxes = top_row(MARGIN, W - MARGIN, m.swell is not None, m.weather is not None)
    shows_moon_box = any(kind == "sun_moon" for kind, _, _ in boxes)

    # header
    place_w = text(c, (16, 40), m.place, 30, bold=True)
    date_s, date_size = header_date(m, W - 16 - HEADER_RIGHT_W - 16)
    text(c, (16 + place_w + 22, 40), date_s, date_size)
    text(c, (W - 16, 18), f"updated {clock(m.now)}", 13, anchor="rs")
    if m.moon is not None and not shows_moon_box:
        lw = text(c, (W - 16, 44), f"{m.moon.name} · {round(m.moon.illuminated * 100)}%", 15, anchor="rs")
        moon_disc(c, W - 16 - lw - 18, 39, 11, m.moon.phase_deg)

    for kind, x0, x1 in boxes:
        box(c, x0, top, x1, bottom)
        if kind == "marine":
            tide_block(c, m, x0 + 14, top + 2)
            if m.swell is not None:
                mid = x0 + TIDE_W
                c.draw.line([(mid, top + 14), (mid, bottom - 14)], fill=BLACK, width=1)
                swell_block(c, m, mid + 14, top + 2, flip_arrows=settings.flip_arrows)
        elif kind == "weather":
            weather_block(c, m, x0 + 14, top + 2, x1 - 14, flip_arrows=settings.flip_arrows)
            if not shows_moon_box:
                sun_times(c, m, x0 + 14, bottom - 14)
        else:
            sun_moon_block(c, m, x0, top, x1, bottom)

    tide_chart(c, (MARGIN, 300, W - MARGIN, H - 4), m, m.now - timedelta(days=settings.chart_days_behind),
               m.now + timedelta(days=settings.chart_days_ahead),
               ChartStyle(daily_labels=settings.label_tides == "daily", show_times=settings.label_times,
                          heights=settings.label_heights))


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    m = day_model(scene.draw_for, ctx)
    c = InkCanvas(ctx.canvas, "white")
    layout(c, m, ctx.config.tides)
    return c.indices()
