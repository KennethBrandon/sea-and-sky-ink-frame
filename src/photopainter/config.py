"""Settings: `config.json` on the Pi, edited by the dashboard (PRD.md §5 "Config").

The dataclasses mirror the JSON exactly: `config.photos.show_now_minutes` is
`photos.show_now_minutes` in the file. `config.example.json` (committed) is
`default_config().to_dict()`, and a test keeps the two in sync.

- `validate(data)` turns a dict into a `Config`. Keys left out take their default, so a
  file can hold only the settings that differ. Unknown keys, wrong types and values out of
  range raise `ConfigError`, whose message lists every problem by its dotted path.
- `load_config()` never raises: a missing file gives the defaults, and a bad file gives
  the defaults plus the error text for the dashboard to show.

Old files still load (`validate`), and `to_dict` writes only the new form:

- `tides_refresh_minutes` / `sky_refresh_minutes` become one-step rotations (`_rotation`).
- `schedule.photo` (photos on the hour, a rule of its own until 2026-11-04) folds into the
  rotations, and its `show_minutes` becomes `photos.show_now_minutes`. See `_fold_old_photo`.
  Its `at_minute` is ignored: a photo step starts where the rotation's grid puts it.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path
from typing import Any, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from photopainter import state

log = logging.getLogger(__name__)

ScreenKind = Literal["tides", "sky", "sea", "photo", "harbor", "combo"]  # the screens a schedule rotation can hold
FitMode = Literal["auto", "fill", "fit"]
TideLabels = Literal["all", "daily"]
TideHeightLabels = Literal["extremes", "all", "none"]

SCREEN_KINDS: tuple[str, ...] = ("tides", "sky", "sea", "photo", "harbor", "combo")
MAX_ROTATION_STEPS = 12
FIT_MODES: tuple[str, ...] = ("auto", "fill", "fit")
# Which highs and lows on the tide chart get a label: every one, or each day's highest high
# and lowest low.
TIDE_LABELS: tuple[str, ...] = ("all", "daily")
# Which highs and lows on the tide chart give their height: the highest and lowest in view,
# every one, or none.
TIDE_HEIGHT_LABELS: tuple[str, ...] = ("extremes", "all", "none")
# Datums NOAA CO-OPS accepts for tide predictions.
TIDE_DATUMS: tuple[str, ...] = ("MLLW", "MLW", "MTL", "MSL", "MHW", "MHHW", "NAVD", "STND",
                                "CRD", "IGLD", "LWD")
# The schedule never plans screens closer together than this, and refresh settings can't go
# lower: a refresh takes ~30 s, so anything shorter isn't worth drawing. Only this holds the
# schedule back: a change someone asks for goes up as soon as the panel is free.
MIN_REFRESH_MINUTES = 3
MAX_CHART_DAYS_BEHIND = 7.0  # the tide chart: any further and it's too crowded to read
MAX_CHART_DAYS_AHEAD = 14.0  # the tide cache always reaches this far ahead (sources/tides.py)
MIN_CHART_DAYS = 0.25  # behind + ahead: at least 6 hours of tide to draw


class ConfigError(ValueError):
    """A config that can't be used. `errors` lists each problem; str() joins them."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Location:
    name: str = "The Wedge"
    lat: float = 33.593
    lon: float = -117.882
    timezone: str = "America/Los_Angeles"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Display:
    portrait: bool = False  # hung in portrait: the canvas is 480x800
    # Rotate everything 180°. True because our panel draws upside down as the frame is hung
    # (found by the Owner, 2026-11-04).
    flip: bool = True


@dataclass(frozen=True)
class QuietHours:
    """Local wall-clock times; `end` may be earlier than `start` (crosses midnight)."""

    start: time
    end: time


@dataclass(frozen=True)
class Step:
    """One step of a rotation: a screen and how long it stays up before the next step."""

    screen: ScreenKind
    minutes: int


@dataclass(frozen=True)
class Schedule:
    """The frame cycles through a rotation of screens, one for the day and one for the night
    (schedule.py: anchored at local midnight, so [tides 30, sky 30] changes on :00 and :30).
    A one-step rotation is a screen redrawn every N minutes; [photo 60] is a slideshow."""

    # By day every screen gets ten minutes an hour, a photo on the hour and all four at once
    # last (the Owner's own rotation, made the default on 2026-11-08); by night, the sky alone.
    day: tuple[Step, ...] = (Step("photo", 10), Step("tides", 10), Step("harbor", 10),
                             Step("sky", 10), Step("sea", 10), Step("combo", 10))
    night: tuple[Step, ...] = (Step("sky", 60),)
    night_offset_minutes: int = 0  # night starts this long after sunset, ends as long before sunrise
    quiet_hours: QuietHours | None = None

    def refresh_minutes(self, screen: ScreenKind, day: bool) -> int:
        """How often a pinned `screen` is redrawn: its first step in this period's
        rotation, else in the other period's, else hourly."""
        first, second = (self.day, self.night) if day else (self.night, self.day)
        for steps in (first, second):
            for step in steps:
                if step.screen == screen:
                    return step.minutes
        return 60

    def has_photos(self) -> bool:
        return any(s.screen == "photo" for s in self.day + self.night)

    def without_photos(self) -> Schedule:
        """This schedule when there's no photo to show: each rotation keeps its cycle, so
        the other steps still change when they would have."""
        return replace(self, day=_without_photos(self.day, "tides"),
                       night=_without_photos(self.night, "sky"))


@dataclass(frozen=True)
class Tides:
    station: str = "9410580"  # NOAA CO-OPS, Newport Bay Entrance
    datum: str = "MLLW"
    chart_days_behind: float = 1.0  # the day screen's chart starts this long before now…
    chart_days_ahead: float = 2.0  # …and ends this long after
    label_tides: TideLabels = "all"  # which highs and lows get a label
    label_times: bool = True  # the time on each label
    label_heights: TideHeightLabels = "extremes"  # which labels also give the height
    # The swell and wind arrows turned 180°, as seen facing south like the Viewer (Owner,
    # 2026-11-04): from the west points left. False: north is up, so from the west points right.
    flip_arrows: bool = True


@dataclass(frozen=True)
class Weather:
    enabled: bool = True  # Open-Meteo forecast for `location` (sources/weather.py)


@dataclass(frozen=True)
class Surf:
    enabled: bool = True  # Open-Meteo marine model nearest `location` (sources/surf.py)


@dataclass(frozen=True)
class Gauge:
    enabled: bool = True  # how far the water is from the tide table (sources/gauge.py)
    station: str = "9410660"  # NOAA CO-OPS, Los Angeles: the nearest gauge still reporting


@dataclass(frozen=True)
class Sky:
    center_azimuth_deg: float = 190.0  # the Viewer's window faces about 10° west of south (Owner, 2026-11-04)
    field_of_view_deg: float = 170.0  # across the horizon
    max_altitude_deg: float = 90.0  # up to the zenith
    magnitude_limit: float = 4.5
    constellation_lines: bool = True
    labels: bool = True
    day_magnitude_limit: float = 2.0  # by day, only stars at least this bright (never more than at night)
    day_constellation_lines: bool = True  # with constellation_lines, keep them on while it's light
    sun_arc: bool = True  # while the Sun is up, today's path across the sky with the hours marked


@dataclass(frozen=True)
class Battery:
    # Running on the battery below indicator_volts, every screen carries a small battery
    # (render/battery_badge.py); 4.2 means the whole time it's on the battery. Below low_volts
    # the frame settles on a photo and waits for power (tick.py). The first run-down
    # (2026-11-07, docs/hardware.md) cut out at 3.00 V; 3.3 V came about 17 minutes before.
    indicator_volts: float = 3.6
    low_volts: float = 3.3
    capacity_mah: int = 2000  # the pack's rating, for the dashboard's time-left guess
    # Low power (power.py): after this long on the battery, Wi-Fi and the dashboard go off,
    # with Wi-Fi back for a few minutes every data_every_hours so the caches can refresh.
    low_power: bool = False
    low_power_after_minutes: int = 1
    data_every_hours: float = 2.0


@dataclass(frozen=True)
class Wifi:
    help: str = ""  # a line for the Wi-Fi setup screen, e.g. who to call; empty: none


@dataclass(frozen=True)
class Photos:
    folder: str = "~/photos"
    fit: FitMode = "auto"
    hidden: tuple[str, ...] = ()  # file names in `folder` left out of the rotation
    show_now_minutes: int = 10  # how long the dashboard's Show now pins a photo

    @property
    def path(self) -> Path:
        return Path(self.folder).expanduser()


@dataclass(frozen=True)
class Harbor:
    """The harbor chart (`scenes/harbor.py`): what the map above the tide band shows. Its
    width follows from its shape; a view past what the place's chart covers is fitted back
    inside it, and one that misses it altogether takes the place's own view (places.py)."""

    south: float = 33.5835  # the Wedge's: low enough to keep the safe-water buoy on the map
    north: float = 33.61575
    center_lon: float = -117.9015
    chart_days_behind: float = 1.0  # the tide band: its own span and labels, apart from the day screen's
    chart_days_ahead: float = 4.0
    label_tides: TideLabels = "all"
    label_times: bool = True
    label_heights: TideHeightLabels = "extremes"


@dataclass(frozen=True)
class Config:
    location: Location = field(default_factory=Location)
    display: Display = field(default_factory=Display)
    schedule: Schedule = field(default_factory=Schedule)
    tides: Tides = field(default_factory=Tides)
    weather: Weather = field(default_factory=Weather)
    surf: Surf = field(default_factory=Surf)
    gauge: Gauge = field(default_factory=Gauge)
    sky: Sky = field(default_factory=Sky)
    harbor: Harbor = field(default_factory=Harbor)
    battery: Battery = field(default_factory=Battery)
    photos: Photos = field(default_factory=Photos)
    wifi: Wifi = field(default_factory=Wifi)

    @property
    def chart_days_behind(self) -> float:
        """How far back the charts drawn from the tide and gauge caches start: the day screen's or the harbor's."""
        return max(self.tides.chart_days_behind, self.harbor.chart_days_behind)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form (what `config.json` holds and `validate` accepts)."""
        q = self.schedule.quiet_hours
        return {
            "location": {"name": self.location.name, "lat": self.location.lat,
                         "lon": self.location.lon, "timezone": self.location.timezone},
            "display": {"portrait": self.display.portrait, "flip": self.display.flip},
            "schedule": {
                "day": [{"screen": s.screen, "minutes": s.minutes} for s in self.schedule.day],
                "night": [{"screen": s.screen, "minutes": s.minutes} for s in self.schedule.night],
                "night_offset_minutes": self.schedule.night_offset_minutes,
                "quiet_hours": None if q is None else {"start": _hhmm(q.start), "end": _hhmm(q.end)},
            },
            "tides": {
                "station": self.tides.station,
                "datum": self.tides.datum,
                "chart_days_behind": self.tides.chart_days_behind,
                "chart_days_ahead": self.tides.chart_days_ahead,
                "label_tides": self.tides.label_tides,
                "label_times": self.tides.label_times,
                "label_heights": self.tides.label_heights,
                "flip_arrows": self.tides.flip_arrows,
            },
            "weather": {"enabled": self.weather.enabled},
            "surf": {"enabled": self.surf.enabled},
            "gauge": {"enabled": self.gauge.enabled, "station": self.gauge.station},
            "sky": {
                "center_azimuth_deg": self.sky.center_azimuth_deg,
                "field_of_view_deg": self.sky.field_of_view_deg,
                "max_altitude_deg": self.sky.max_altitude_deg,
                "magnitude_limit": self.sky.magnitude_limit,
                "constellation_lines": self.sky.constellation_lines,
                "labels": self.sky.labels,
                "day_magnitude_limit": self.sky.day_magnitude_limit,
                "day_constellation_lines": self.sky.day_constellation_lines,
                "sun_arc": self.sky.sun_arc,
            },
            "harbor": {"south": self.harbor.south, "north": self.harbor.north,
                       "center_lon": self.harbor.center_lon,
                       "chart_days_behind": self.harbor.chart_days_behind,
                       "chart_days_ahead": self.harbor.chart_days_ahead,
                       "label_tides": self.harbor.label_tides, "label_times": self.harbor.label_times,
                       "label_heights": self.harbor.label_heights},
            "battery": {"indicator_volts": self.battery.indicator_volts, "low_volts": self.battery.low_volts,
                        "capacity_mah": self.battery.capacity_mah,
                        "low_power": self.battery.low_power,
                        "low_power_after_minutes": self.battery.low_power_after_minutes,
                        "data_every_hours": self.battery.data_every_hours},
            "photos": {"folder": self.photos.folder, "fit": self.photos.fit,
                       "hidden": list(self.photos.hidden),
                       "show_now_minutes": self.photos.show_now_minutes},
            "wifi": {"help": self.wifi.help},
        }


def default_config() -> Config:
    return Config()


# --- validation ------------------------------------------------------------------------


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


class _Checker:
    """Pulls typed values out of nested dicts, recording readable errors as it goes."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def section(self, data: Any, path: str, keys: tuple[str, ...]) -> dict:
        """The dict at `path` (empty if missing/invalid), after flagging unknown keys."""
        if not isinstance(data, dict):
            self.errors.append(f"{path or 'config'}: must be an object (got {_show(data)})")
            return {}
        for key in data:
            if key not in keys:
                where = f"{path}.{key}" if path else str(key)
                self.errors.append(f"{where}: unknown setting")
        return data

    def boolean(self, data: dict, path: str, key: str, default: bool) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            self.errors.append(f"{path}.{key}: must be true or false (got {_show(value)})")
            return default
        return value

    def integer(self, data: dict, path: str, key: str, default: int, lo: int, hi: int) -> int:
        value = data.get(key, default)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            self.errors.append(f"{path}.{key}: must be a whole number from {lo} to {hi} (got {_show(value)})")
            return default
        return value

    def number(self, data: dict, path: str, key: str, default: float, lo: float, hi: float,
               hi_inclusive: bool = True) -> float:
        value = data.get(key, default)
        ok = (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
              and lo <= value and (value <= hi if hi_inclusive else value < hi))
        if not ok:
            upper = f"up to {hi:g}" if hi_inclusive else f"below {hi:g}"
            self.errors.append(f"{path}.{key}: must be a number from {lo:g} {upper} (got {_show(value)})")
            return default
        return float(value)

    def string(self, data: dict, path: str, key: str, default: str, *, allow_empty: bool = False) -> str:
        value = data.get(key, default)
        if not isinstance(value, str) or not (value.strip() or allow_empty):
            self.errors.append(f"{path}.{key}: must be {'' if allow_empty else 'non-empty '}text (got {_show(value)})")
            return default
        return value

    def choice(self, data: dict, path: str, key: str, default: str, choices: tuple[str, ...]) -> str:
        value = data.get(key, default)
        if value not in choices:
            self.errors.append(f"{path}.{key}: must be one of {', '.join(choices)} (got {_show(value)})")
            return default
        return value


def _show(value: Any) -> str:
    text = json.dumps(value) if isinstance(value, (str, int, float, bool, type(None))) else type(value).__name__
    return text if len(text) <= 40 else text[:37] + "..."


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(c: _Checker, data: dict, path: str, key: str) -> time | None:
    value = data.get(key)
    m = _HHMM.match(value) if isinstance(value, str) else None
    if m is None:
        c.errors.append(f"{path}.{key}: must be a time like \"05:30\" (got {_show(value)})")
        return None
    return time(int(m[1]), int(m[2]))


def _chart_span(c: _Checker, s: dict, section: str, behind: float, ahead: float) -> tuple[float, float]:
    """A tide chart's days back and ahead, for the sections that have one."""
    behind = c.number(s, section, "chart_days_behind", behind, 0, MAX_CHART_DAYS_BEHIND)
    ahead = c.number(s, section, "chart_days_ahead", ahead, 0, MAX_CHART_DAYS_AHEAD)
    if behind + ahead < MIN_CHART_DAYS:
        c.errors.append(f"{section}.chart_days_ahead: days behind and ahead must add up to at least "
                        f"{MIN_CHART_DAYS:g} (got {behind + ahead:g})")
    return behind, ahead


def validate(data: dict) -> Config:
    """Build a Config from JSON data, filling in defaults; raise ConfigError if it's bad."""
    c = _Checker()
    d = Config()
    root = c.section(data, "", ("location", "display", "schedule", "tides", "weather", "surf", "gauge",
                                "sky", "harbor", "battery", "photos", "wifi"))

    s = c.section(root.get("location", {}), "location", ("name", "lat", "lon", "timezone"))
    tz_name = c.string(s, "location", "timezone", d.location.timezone)
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        c.errors.append(f"location.timezone: unknown time zone {_show(tz_name)} (e.g. \"America/Los_Angeles\")")
        tz_name = d.location.timezone
    location = Location(
        name=c.string(s, "location", "name", d.location.name),
        lat=c.number(s, "location", "lat", d.location.lat, -90, 90),
        lon=c.number(s, "location", "lon", d.location.lon, -180, 180),
        timezone=tz_name,
    )

    s = c.section(root.get("display", {}), "display", ("portrait", "flip"))
    display = Display(
        portrait=c.boolean(s, "display", "portrait", d.display.portrait),
        flip=c.boolean(s, "display", "flip", d.display.flip),
    )

    sched = c.section(root.get("schedule", {}), "schedule", (
        "photo", "day", "night", "tides_refresh_minutes", "sky_refresh_minutes", "night_offset_minutes",
        "quiet_hours"))
    quiet = None
    q = sched.get("quiet_hours")
    if q is not None:
        q = c.section(q, "schedule.quiet_hours", ("start", "end"))
        start = _parse_hhmm(c, q, "schedule.quiet_hours", "start")
        end = _parse_hhmm(c, q, "schedule.quiet_hours", "end")
        if start is not None and end is not None:
            if start == end:
                c.errors.append("schedule.quiet_hours: start and end must differ (use null for none)")
            else:
                quiet = QuietHours(start, end)

    ds = d.schedule
    old_photo = _read_old_photo(sched.get("photo"))
    # In a file with the old rule, a day rotation left out meant the old default.
    day_default = (Step("tides", 60),) if old_photo is not None else ds.day
    day_steps = _rotation(c, sched, "day", "tides_refresh_minutes", "tides", day_default)
    night_steps = _rotation(c, sched, "night", "sky_refresh_minutes", "sky", ds.night)
    if old_photo is not None:
        day_steps, night_steps = _fold_old_photo(old_photo, day_steps, night_steps)
    schedule = Schedule(
        day=day_steps,
        night=night_steps,
        night_offset_minutes=c.integer(sched, "schedule", "night_offset_minutes",
                                       ds.night_offset_minutes, -180, 180),
        quiet_hours=quiet,
    )

    s = c.section(root.get("tides", {}), "tides", ("station", "datum", "chart_days_behind", "chart_days_ahead",
                                                   "label_tides", "label_times", "label_heights",
                                                   "flip_arrows"))
    station = c.string(s, "tides", "station", d.tides.station)
    if not re.fullmatch(r"\d{7}", station):
        c.errors.append(f"tides.station: must be a 7-digit NOAA station id (got {_show(station)})")
        station = d.tides.station
    behind, ahead = _chart_span(c, s, "tides", d.tides.chart_days_behind, d.tides.chart_days_ahead)
    tides = Tides(
        station=station,
        datum=c.choice(s, "tides", "datum", d.tides.datum, TIDE_DATUMS),
        chart_days_behind=behind,
        chart_days_ahead=ahead,
        label_tides=c.choice(s, "tides", "label_tides", d.tides.label_tides, TIDE_LABELS),
        label_times=c.boolean(s, "tides", "label_times", d.tides.label_times),
        label_heights=c.choice(s, "tides", "label_heights", d.tides.label_heights, TIDE_HEIGHT_LABELS),
        flip_arrows=c.boolean(s, "tides", "flip_arrows", d.tides.flip_arrows),
    )

    s = c.section(root.get("weather", {}), "weather", ("enabled",))
    weather = Weather(enabled=c.boolean(s, "weather", "enabled", d.weather.enabled))
    s = c.section(root.get("surf", {}), "surf", ("enabled",))
    surf = Surf(enabled=c.boolean(s, "surf", "enabled", d.surf.enabled))
    s = c.section(root.get("gauge", {}), "gauge", ("enabled", "station"))
    gauge_station = c.string(s, "gauge", "station", d.gauge.station)
    if not re.fullmatch(r"\d{7}", gauge_station):
        c.errors.append(f"gauge.station: must be a 7-digit NOAA station id (got {_show(gauge_station)})")
        gauge_station = d.gauge.station
    gauge = Gauge(enabled=c.boolean(s, "gauge", "enabled", d.gauge.enabled), station=gauge_station)

    s = c.section(root.get("sky", {}), "sky", (
        "center_azimuth_deg", "field_of_view_deg", "max_altitude_deg", "magnitude_limit",
        "constellation_lines", "labels", "day_magnitude_limit", "day_constellation_lines", "sun_arc"))
    sky = Sky(
        center_azimuth_deg=c.number(s, "sky", "center_azimuth_deg", d.sky.center_azimuth_deg, 0, 360,
                                    hi_inclusive=False),
        field_of_view_deg=c.number(s, "sky", "field_of_view_deg", d.sky.field_of_view_deg, 10, 360),
        max_altitude_deg=c.number(s, "sky", "max_altitude_deg", d.sky.max_altitude_deg, 10, 90),
        magnitude_limit=c.number(s, "sky", "magnitude_limit", d.sky.magnitude_limit, -2, 8),
        constellation_lines=c.boolean(s, "sky", "constellation_lines", d.sky.constellation_lines),
        labels=c.boolean(s, "sky", "labels", d.sky.labels),
        day_magnitude_limit=c.number(s, "sky", "day_magnitude_limit", d.sky.day_magnitude_limit, -2, 8),
        day_constellation_lines=c.boolean(s, "sky", "day_constellation_lines", d.sky.day_constellation_lines),
        sun_arc=c.boolean(s, "sky", "sun_arc", d.sky.sun_arc),
    )

    s = c.section(root.get("harbor", {}), "harbor", ("south", "north", "center_lon", "chart_days_behind",
                                                     "chart_days_ahead", "label_tides", "label_times", "label_heights"))
    behind, ahead = _chart_span(c, s, "harbor", d.harbor.chart_days_behind, d.harbor.chart_days_ahead)
    harbor = Harbor(
        south=c.number(s, "harbor", "south", d.harbor.south, -90, 90),
        north=c.number(s, "harbor", "north", d.harbor.north, -90, 90),
        center_lon=c.number(s, "harbor", "center_lon", d.harbor.center_lon, -180, 180),
        chart_days_behind=behind,
        chart_days_ahead=ahead,
        label_tides=c.choice(s, "harbor", "label_tides", d.harbor.label_tides, TIDE_LABELS),
        label_times=c.boolean(s, "harbor", "label_times", d.harbor.label_times),
        label_heights=c.choice(s, "harbor", "label_heights", d.harbor.label_heights, TIDE_HEIGHT_LABELS),
    )
    if harbor.north <= harbor.south:
        c.errors.append("harbor.north: must be north of harbor.south")

    s = c.section(root.get("battery", {}), "battery", ("indicator_volts", "low_volts", "capacity_mah", "low_power",
                                                       "low_power_after_minutes", "data_every_hours"))
    battery = Battery(
        indicator_volts=c.number(s, "battery", "indicator_volts", d.battery.indicator_volts, 3.0, 4.2),
        low_volts=c.number(s, "battery", "low_volts", d.battery.low_volts, 3.0, 4.1),
        capacity_mah=c.integer(s, "battery", "capacity_mah", d.battery.capacity_mah, 100, 50000),
        low_power=c.boolean(s, "battery", "low_power", d.battery.low_power),
        low_power_after_minutes=c.integer(s, "battery", "low_power_after_minutes",
                                          d.battery.low_power_after_minutes, 0, 600),
        data_every_hours=c.number(s, "battery", "data_every_hours", d.battery.data_every_hours, 0.25, 24),
    )

    s = c.section(root.get("photos", {}), "photos", ("folder", "fit", "hidden", "show_now_minutes"))
    hidden = s.get("hidden", [])
    if not isinstance(hidden, list) or not all(_is_file_name(h) for h in hidden):
        c.errors.append("photos.hidden: must be a list of file names (no folders)")
        hidden = []
    photos = Photos(
        folder=c.string(s, "photos", "folder", d.photos.folder),
        fit=c.choice(s, "photos", "fit", d.photos.fit, FIT_MODES),
        hidden=tuple(dict.fromkeys(hidden)),  # drop duplicates, keep order
        show_now_minutes=c.integer(s, "photos", "show_now_minutes",
                                   d.photos.show_now_minutes if old_photo is None else old_photo.show,
                                   1, 1440),
    )

    s = c.section(root.get("wifi", {}), "wifi", ("help",))
    wifi = Wifi(help=c.string(s, "wifi", "help", d.wifi.help, allow_empty=True))

    if c.errors:
        raise ConfigError(c.errors)
    return Config(location=location, display=display, schedule=schedule, tides=tides, weather=weather,
                  surf=surf, gauge=gauge, sky=sky, harbor=harbor, battery=battery, photos=photos, wifi=wifi)


def _rotation(c: _Checker, sched: dict, key: str, old_key: str, old_screen: str,
              default: tuple[Step, ...]) -> tuple[Step, ...]:
    """`schedule.day` / `schedule.night`: a list of {"screen", "minutes"} steps. A file from
    before rotations (`tides_refresh_minutes` / `sky_refresh_minutes`, no list) loads as a
    one-step rotation of that screen."""
    path = f"schedule.{key}"
    if key not in sched:
        if old_key in sched:
            minutes = c.integer(sched, "schedule", old_key, default[0].minutes, MIN_REFRESH_MINUTES, 1440)
            return (Step(old_screen, minutes),)  # type: ignore[arg-type]
        return default
    value = sched[key]
    if not isinstance(value, list):
        c.errors.append(f"{path}: must be a list of screens (got {_show(value)})")
        return default
    if not value:
        c.errors.append(f"{path}: must have at least one screen")
        return default
    if len(value) > MAX_ROTATION_STEPS:
        c.errors.append(f"{path}: at most {MAX_ROTATION_STEPS} screens (got {len(value)})")
        return default
    steps = []
    for i, item in enumerate(value):
        where = f"{path}[{i}]"
        item = c.section(item, where, ("screen", "minutes"))
        if "screen" not in item:
            c.errors.append(f"{where}.screen: must be one of {', '.join(SCREEN_KINDS)} (missing)")
        if "minutes" not in item:
            c.errors.append(f"{where}.minutes: must be a whole number from {MIN_REFRESH_MINUTES} to 1440 (missing)")
        steps.append(Step(
            screen=c.choice(item, where, "screen", old_screen, SCREEN_KINDS),  # type: ignore[arg-type]
            minutes=c.integer(item, where, "minutes", 60, MIN_REFRESH_MINUTES, 1440),
        ))
    return tuple(steps)


class _OldPhoto(NamedTuple):
    """`schedule.photo`, the rule photos had before they were rotation steps."""

    enabled: bool
    every: int
    show: int
    during: str


def _read_old_photo(old: Any) -> _OldPhoto | None:
    """`schedule.photo` with the defaults it had for keys left out; None if there isn't one.
    Never raises: one that makes no sense is ignored, with a warning."""
    if old is None:
        return None
    if isinstance(old, dict):
        enabled, every = old.get("enabled", True), old.get("every_minutes", 60)
        show, during = old.get("show_minutes", 10), old.get("during", "day")
        every, show = (int(n) if isinstance(n, float) and n.is_integer() else n for n in (every, show))
        whole = all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 1440 for n in (every, show))
        if isinstance(enabled, bool) and whole and show < every and during in ("day", "night", "always"):
            return _OldPhoto(enabled, every, show, during)
    log.warning("ignoring schedule.photo, which can't be read: %r", old)
    return None


def _fold_old_photo(old: _OldPhoto, day: tuple[Step, ...],
                    night: tuple[Step, ...]) -> tuple[tuple[Step, ...], tuple[Step, ...]]:
    """The rotations with the old rule folded in: each period `during` allows gets a photo
    step. `enabled: false` leaves them alone, and so does a `show` under 3 minutes, which the
    old rule never drew. `at_minute` is ignored."""
    if not old.enabled or old.show < MIN_REFRESH_MINUTES:
        return day, night
    if old.during != "night":
        day = _with_photo_step(day, old.every, old.show)
    if old.during != "day":
        night = _with_photo_step(night, old.every, old.show)
    return day, night


def _with_photo_step(steps: tuple[Step, ...], every: int, show: int) -> tuple[Step, ...]:
    """A photo first in the cycle. When the cycle divides `every`, the steps repeat up to
    `every` and the photo comes round just as often: [tides 30] with 10 min every 60 is
    [photo 10, tides 20, tides 30]. Otherwise the cycle stays about as it was, and the first
    step is shortened, or dropped if that would leave it under 3 minutes. A rotation that
    already holds a photo step is left as it is."""
    if any(s.screen == "photo" for s in steps):
        return steps
    cycle = sum(s.minutes for s in steps)
    photo = Step("photo", show)
    if every % cycle == 0:
        return _fit_steps(_photo_first(photo, steps * (every // cycle)))
    first = steps[0].minutes - show
    rest = steps[1:] if first < MIN_REFRESH_MINUTES else (replace(steps[0], minutes=first), *steps[1:])
    return _fit_steps((photo, *rest))


def _photo_first(photo: Step, steps: tuple[Step, ...]) -> tuple[Step, ...]:
    """`photo`, then `steps` shortened from the front by its length, keeping the total. What's
    left of a step that would fall under 3 minutes goes to the step after it, as the schedule
    would start that one early (or to the photo, when no step is left)."""
    rest = list(steps)
    left = photo.minutes
    while left >= rest[0].minutes:  # the photo is shorter than the total: `rest` can't run out
        left -= rest.pop(0).minutes
    rest[0] = replace(rest[0], minutes=rest[0].minutes - left)
    if rest[0].minutes < MIN_REFRESH_MINUTES:
        spare = rest.pop(0).minutes
        if not rest:
            return (replace(photo, minutes=photo.minutes + spare),)
        rest[0] = replace(rest[0], minutes=rest[0].minutes + spare)
    return (photo, *rest)


def _fit_steps(steps: tuple[Step, ...]) -> tuple[Step, ...]:
    """At most MAX_ROTATION_STEPS steps: the last one kept runs on for the time of those left
    out, so the cycle stays the same (a step can't pass 1440 minutes, though)."""
    if len(steps) <= MAX_ROTATION_STEPS:
        return steps
    kept = steps[:MAX_ROTATION_STEPS]
    extra = sum(s.minutes for s in steps[MAX_ROTATION_STEPS:])
    return (*kept[:-1], replace(kept[-1], minutes=min(kept[-1].minutes + extra, 1440)))


def _without_photos(steps: tuple[Step, ...], fallback: ScreenKind) -> tuple[Step, ...]:
    """A rotation with its photo steps gone and the same cycle: the step before a photo runs
    on through it, and a photo at the start gives its time to the first screen after it. A
    rotation of only photos becomes one step of `fallback` for the whole cycle."""
    kept: list[Step] = []
    lead = 0
    for step in steps:
        if step.screen != "photo":
            kept.append(replace(step, minutes=step.minutes + lead))
            lead = 0
        elif kept:
            kept[-1] = replace(kept[-1], minutes=kept[-1].minutes + step.minutes)
        else:
            lead += step.minutes
    return tuple(kept) if kept else (Step(fallback, lead),)


def _is_file_name(name: Any) -> bool:
    return isinstance(name, str) and name not in ("", ".", "..") and "/" not in name and "\\" not in name \
        and "\0" not in name


# --- files -----------------------------------------------------------------------------


def load_config(path: Path | None = None) -> tuple[Config, str | None]:
    """(config, error). Never raises.

    No file: the defaults and no error. A file that can't be read, isn't JSON or doesn't
    validate: the defaults and a message saying what's wrong.
    """
    path = state.config_path() if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_config(), None
    except Exception as exc:  # noqa: BLE001 - unreadable: permissions, a directory, bad bytes…
        return default_config(), f"{path}: can't read it ({exc})"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return default_config(), f"{path}: not valid JSON ({exc})"
    try:
        return validate(data), None
    except ConfigError as exc:
        return default_config(), f"{path}: {exc}"
    except Exception as exc:  # noqa: BLE001 - load_config must never raise
        log.exception("unexpected error validating %s", path)
        return default_config(), f"{path}: {exc}"


def save_config(config: Config, path: Path | None = None) -> None:
    """Write `config` atomically (see state.write_json)."""
    state.write_json(state.config_path() if path is None else path, config.to_dict())


def edit_config(change: Callable[[Config], Config], path: Path | None = None) -> Config:
    """Read-modify-write the config file under its lock (the dashboard's Hide and Delete, a
    button). Raises ConfigError if the file is bad, so a change can't overwrite settings
    that could still be fixed."""
    path = state.config_path() if path is None else path
    with state.locked(path):
        config, error = load_config(path)
        if error:
            raise ConfigError(error)
        new = change(config)
        state.write_atomic(path, state.dumps(new.to_dict()).encode("utf-8"))
    return new

