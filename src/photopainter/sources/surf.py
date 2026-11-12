"""Swell and sea temperature off Newport from Open-Meteo's marine model, cached in `surf.json`.

Open-Meteo's marine API (<https://open-meteo.com/en/docs/marine-weather-api>) needs no key;
its data is CC BY 4.0, non-commercial (credit it where it's shown). The model is offshore:
it snaps the Wedge to the cell at 33.625, −117.875, so it gives the swell off Newport, not
the breaking height at the Wedge. Label it as swell (PRD.md D9). The water temperature is
the model's too: NOAA 9410580 has no sensors.

With `length_unit=imperial` heights come in feet, but the sea surface temperature still
comes in °C; the cache stores °F. Heights are rounded to 0.01 ft and the water
temperature to 0.1 °F.

Cache (PRD.md §5 "Surf cache"):

    {"source": "open-meteo-marine", "fetched_at": "…", "lat": 33.625, "lon": -117.875,
     "units": {"height": "ft", "period": "s", "water_temp": "F"},
     "hourly": [{"t": "…", "wave_height": 3.74, "wave_period": 10.95, "wave_dir": 240,
                 "swell_height": 2.30, "swell_period": 11.75, "swell_dir": 190,
                 "water_temp": 73.2}, …]}

`lat`/`lon` are the model cell the API used. Directions are where the waves come from,
degrees true. A value the model doesn't have is null. It's refetched when over 1 h old (well
before the day screen starts printing its age, at 2 h) or when the hourly rows cover less
than 48 h ahead. (A changed location reaches this cache within that hour: the cell isn't the
configured point, so there's nothing exact to compare.)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config
from photopainter.sources import common
from photopainter.sources.common import FetchError, Refresh

SOURCE = "surf"
FILE = "surf.json"
API = "https://marine-api.open-meteo.com/v1/marine"
MAX_AGE = timedelta(hours=1)
MIN_HOURLY_AHEAD = timedelta(hours=48)
FORECAST_DAYS = 5
UNITS = {"height": "ft", "period": "s", "water_temp": "F"}
FT_PER_M = 1 / 0.3048

HOURLY = ("wave_height", "wave_period", "wave_direction", "swell_wave_height", "swell_wave_period",
          "swell_wave_direction", "sea_surface_temperature")


def ensure_fresh(config: Config, now: datetime | None = None, *, force: bool = False,
                 state_dir: Path | None = None) -> Refresh:
    """Refetch `surf.json` if it's stale (or `force`). Never raises; see `common`.

    When `config.surf.enabled` is false nothing is fetched and the cache is left alone
    (the day screen leaves the section out).
    """
    now = common.now_in(config.location.tz) if now is None else now
    if not config.surf.enabled:
        return Refresh(SOURCE, False, None, None, "disabled in config")
    return common.refresh(SOURCE, FILE, now, state_dir=state_dir, force=force,
                          stale=stale_reason, fetch=lambda: fetch(config, now))


def load(state_dir: Path | None = None) -> dict | None:
    """The cached marine forecast, or None."""
    return common.load(FILE, state_dir)


def stale_reason(cache: dict, now: datetime) -> str | None:
    """Why `cache` must be refetched at `now`; None if it's still good."""
    if cache.get("units") != UNITS:
        return f"in units {cache.get('units')}, not {UNITS}"
    problem = common.age_problem(cache, now, MAX_AGE)
    if problem:
        return problem
    last = common.last_time(cache.get("hourly"))
    if last is None or last < now + MIN_HOURLY_AHEAD:
        return f"hourly forecast shorter than {common.hours(MIN_HOURLY_AHEAD)}"
    return None


def request_params(config: Config) -> dict[str, Any]:
    loc = config.location
    return {
        "latitude": loc.lat,
        "longitude": loc.lon,
        "hourly": ",".join(HOURLY),
        "length_unit": "imperial",
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
    units = raw.get("hourly_units") if isinstance(raw.get("hourly_units"), dict) else {}
    height = _converter(units, ("wave_height", "swell_wave_height"), {"ft": 1.0, "m": FT_PER_M})
    period = _converter(units, ("wave_period", "swell_wave_period"), {"s": 1.0})
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
        raise FetchError("Open-Meteo marine sent no model cell position")
    sst_unit = units.get("sea_surface_temperature")
    if sst_unit not in ("°C", "°F"):
        raise FetchError(f"Open-Meteo sea_surface_temperature is in {sst_unit!r}, expected °C or °F")

    hourly = []
    for t, wh, wp, wd, sh, sp, sd, sst in common.columns(raw.get("hourly"), HOURLY, "marine hourly"):
        hourly.append({
            "t": state.to_iso(common.local_from_unix(t, tz)),
            "wave_height": _scaled(wh, height, 2),
            "wave_period": _scaled(wp, period, 2),
            "wave_dir": common.number(wd),
            "swell_height": _scaled(sh, height, 2),
            "swell_period": _scaled(sp, period, 2),
            "swell_dir": common.number(sd),
            "water_temp": water_temp_f(sst, sst_unit),
        })
    return {
        "source": "open-meteo-marine",
        "fetched_at": state.to_iso(fetched.astimezone(tz)),
        "lat": round(lat, 3),  # the API says -117.87499 for the -117.875 cell
        "lon": round(lon, 3),
        "units": dict(UNITS),
        "hourly": hourly,
    }


def water_temp_f(value: Any, unit: str = "°C") -> float | None:
    """A sea surface temperature in °F, to 0.1 °F (None stays None)."""
    v = common.number(value)
    if v is None:
        return None
    return round(v * 9 / 5 + 32 if unit == "°C" else v, 1)


def _converter(units: dict, names: tuple[str, ...], factors: dict[str, float]) -> float:
    """The factor to our units for fields that must all share one unit."""
    found = sorted({str(units.get(n)) for n in names})
    if len(found) != 1 or found[0] not in factors:
        raise FetchError(f"Open-Meteo marine {'/'.join(names)} in {', '.join(found)}, "
                         f"expected {' or '.join(factors)}")
    return factors[found[0]]


def _scaled(value: Any, factor: float, digits: int) -> float | None:
    v = common.number(value)
    return None if v is None else round(v * factor, digits)
