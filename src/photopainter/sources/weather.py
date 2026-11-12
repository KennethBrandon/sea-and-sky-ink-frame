"""The weather forecast for the configured location from Open-Meteo, cached in `weather.json`.

Open-Meteo's forecast API (<https://open-meteo.com/en/docs>) needs no key. Its data is
CC BY 4.0 and free for non-commercial use: credit it wherever it's shown (`data/README.md`
and the dashboard). One request gives the current conditions, an hourly forecast and a
daily forecast for `FORECAST_DAYS` days from local midnight today, in °F and mph. Times
come as Unix seconds and are stored in the configured time zone; the daily rows are local
calendar days.

Cache (PRD.md §5 "Weather cache"):

    {"source": "open-meteo", "fetched_at": "…", "lat": 33.593, "lon": -117.882,
     "units": {"temp": "F", "wind": "mph"},
     "current": {"t": "…", "temp": 64.5, "code": 3, "wind": 4.7, "gust": 10.7, "wind_dir": 316},
     "hourly": [{"t": "…", "temp": 63.1, "code": 3, "precip_chance": 0, "wind": 5.2,
                 "wind_dir": 300}, …],
     "daily":  [{"date": "2026-11-03", "high": 74.3, "low": 57.8, "code": 3,
                 "precip_chance": 0}, …]}

`code` is the WMO weather code; `wind_dir` is where the wind comes from, degrees true.
`lat`/`lon` are the configured location (Open-Meteo snaps it to its own grid). A value
the model doesn't have is null. It's refetched when over 1 h old, when the hourly rows
cover less than 48 h ahead or the daily rows less than 3 days, or when the location moved.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config
from photopainter.sources import common
from photopainter.sources.common import FetchError, Refresh

SOURCE = "weather"
FILE = "weather.json"
API = "https://api.open-meteo.com/v1/forecast"
MAX_AGE = timedelta(hours=1)
MIN_HOURLY_AHEAD = timedelta(hours=48)
MIN_DAILY_DAYS = 3
FORECAST_DAYS = 7
UNITS = {"temp": "F", "wind": "mph"}

CURRENT = ("temperature_2m", "weather_code", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m")
HOURLY = ("temperature_2m", "weather_code", "precipitation_probability", "wind_speed_10m",
          "wind_direction_10m")
DAILY = ("temperature_2m_max", "temperature_2m_min", "weather_code", "precipitation_probability_max")


def ensure_fresh(config: Config, now: datetime | None = None, *, force: bool = False,
                 state_dir: Path | None = None) -> Refresh:
    """Refetch `weather.json` if it's stale (or `force`). Never raises; see `common`.

    When `config.weather.enabled` is false nothing is fetched and the cache is left alone
    (the day screen leaves the section out).
    """
    now = common.now_in(config.location.tz) if now is None else now
    if not config.weather.enabled:
        return Refresh(SOURCE, False, None, None, "disabled in config")
    return common.refresh(SOURCE, FILE, now, state_dir=state_dir, force=force,
                          stale=lambda cache, at: stale_reason(cache, config, at),
                          fetch=lambda: fetch(config, now))


def load(state_dir: Path | None = None) -> dict | None:
    """The cached forecast, or None."""
    return common.load(FILE, state_dir)


def stale_reason(cache: dict, config: Config, now: datetime) -> str | None:
    """Why `cache` must be refetched for `config` at `now`; None if it's still good."""
    loc = config.location
    if (cache.get("lat"), cache.get("lon")) != (loc.lat, loc.lon):
        return f"for {cache.get('lat')},{cache.get('lon')}, not {loc.lat},{loc.lon}"
    if cache.get("units") != UNITS:
        return f"in units {cache.get('units')}, not {UNITS}"
    problem = common.age_problem(cache, now, MAX_AGE)
    if problem:
        return problem
    last = common.last_time(cache.get("hourly"))
    if last is None or last < now + MIN_HOURLY_AHEAD:
        return f"hourly forecast shorter than {common.hours(MIN_HOURLY_AHEAD)}"
    today = now.astimezone(loc.tz).date().isoformat()
    days = [d.get("date") for d in cache.get("daily") or [] if isinstance(d, dict)]
    if sum(1 for d in days if isinstance(d, str) and d >= today) < MIN_DAILY_DAYS:
        return f"daily forecast shorter than {MIN_DAILY_DAYS} days"
    return None


def request_params(config: Config) -> dict[str, Any]:
    loc = config.location
    return {
        "latitude": loc.lat,
        "longitude": loc.lon,
        "current": ",".join(CURRENT),
        "hourly": ",".join(HOURLY),
        "daily": ",".join(DAILY),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": loc.timezone,
        "timeformat": "unixtime",
        "forecast_days": FORECAST_DAYS,
    }


def fetch(config: Config, now: datetime) -> dict:
    """Download and parse; raises FetchError."""
    return parse(common.get_json(API, request_params(config)), config, now)


def parse(raw: Any, config: Config, fetched: datetime) -> dict:
    """The cache from Open-Meteo's response; raises FetchError if it doesn't make sense."""
    common.open_meteo_error(raw)
    tz = config.location.tz
    _check_unit(raw, "current_units", "temperature_2m", "°F")
    _check_unit(raw, "hourly_units", "temperature_2m", "°F")
    _check_unit(raw, "daily_units", "temperature_2m_max", "°F")
    _check_unit(raw, "current_units", "wind_speed_10m", "mp/h")
    _check_unit(raw, "hourly_units", "wind_speed_10m", "mp/h")

    cur = raw.get("current")
    if not isinstance(cur, dict) or not isinstance(cur.get("time"), (int, float)):
        raise FetchError("Open-Meteo sent no current conditions")
    n = common.number
    current = {
        "t": state.to_iso(common.local_from_unix(cur["time"], tz)),
        "temp": n(cur.get("temperature_2m")),
        "code": n(cur.get("weather_code")),
        "wind": n(cur.get("wind_speed_10m")),
        "gust": n(cur.get("wind_gusts_10m")),
        "wind_dir": n(cur.get("wind_direction_10m")),
    }
    hourly = [
        {"t": state.to_iso(common.local_from_unix(t, tz)), "temp": n(temp), "code": n(code),
         "precip_chance": n(precip), "wind": n(wind), "wind_dir": n(wind_dir)}
        for t, temp, code, precip, wind, wind_dir in common.columns(raw.get("hourly"), HOURLY, "hourly")
    ]
    daily = [
        {"date": common.local_from_unix(t, tz).date().isoformat(), "high": n(high), "low": n(low),
         "code": n(code), "precip_chance": n(precip)}
        for t, high, low, code, precip in common.columns(raw.get("daily"), DAILY, "daily")
    ]
    return {
        "source": "open-meteo",
        "fetched_at": state.to_iso(fetched.astimezone(tz)),
        "lat": config.location.lat,
        "lon": config.location.lon,
        "units": dict(UNITS),
        "current": current,
        "hourly": hourly,
        "daily": daily,
    }


def _check_unit(raw: dict, block: str, name: str, expected: str) -> None:
    units = raw.get(block)
    got = units.get(name) if isinstance(units, dict) else None
    if got != expected:
        raise FetchError(f"Open-Meteo {block}.{name} is {got!r}, expected {expected!r}")
