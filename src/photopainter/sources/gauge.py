"""How far the water is running from the tide table, measured at a NOAA gauge, cached in
`gauge.json`.

Tide tables only know the Sun and Moon. Kelvin waves, storms and the warming of the sea can
hold the water over a foot above them for weeks, and nothing measures the water at the
Wedge: NOAA's Newport gauge (9410580) stopped reporting in January 1994. These changes reach
along the whole coast, though, so the frame measures them at the nearest working gauge,
Los Angeles (9410660, 39 km away), and adds that to Newport's own table.

What's cached is the gap, measured minus predicted at the gauge. It doesn't depend on the
datum, so the requests always use MLLW, whatever `tides.datum` says.

Why this is good enough (checked 2026-11-04; PRD.md D11): against Newport's old gauge, in
five sample years from 1982 to 1993, Newport's table plus LA's gap was off by 0.06-0.11 ft
RMS, where the table alone was off by 0.23-0.34 ft. On the day of the check, the Los Angeles,
Santa Monica, La Jolla and San Diego gauges all read 1.29-1.35 ft over their tables.

A fetch is two requests from far enough back for the chart (and at least `MIN_BACK`, for the
day screen's 25-hour mean) until now: `product=water_level`, the six-minute readings (the
recent ones are preliminary), and `product=predictions` at `interval=6`. They ask for GMT,
like `tides.py`, and the cache holds the times in the configured time zone.

Cache (PRD.md §5 "Gauge cache"):

    {"station": "9410660", "units": "ft", "fetched_at": "…",
     "gap": [{"t": "2026-11-03T14:06:00-08:00", "v": 1.292}, …]}

It's refetched when it's over 30 minutes old or for another station than the config. How the
day screen uses it, and when it stops trusting it, is in `scenes/tides.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config
from photopainter.sources import common
from photopainter.sources.common import FetchError, Refresh

SOURCE = "gauge"
FILE = "gauge.json"
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MAX_AGE = timedelta(minutes=30)
MIN_BACK = timedelta(hours=27)  # the day screen's mean over 25 h, with room for a late reading
SMOOTHING_ROOM = timedelta(hours=1)  # so that the chart's first hour is smoothed like the rest
MAX_GAP_FT = 6.0  # more than this is a broken sensor; nothing on this coast has come close
UNITS = "ft"
DATUM = "MLLW"  # a gap comes out the same on any datum the gauge has


def ensure_fresh(config: Config, now: datetime | None = None, *, force: bool = False,
                 state_dir: Path | None = None) -> Refresh:
    """Refetch `gauge.json` if it's stale (or `force`). Never raises; see `common`.

    When `config.gauge.enabled` is false nothing is fetched and the cache is left alone
    (the day screen shows the tide table alone).
    """
    now = common.now_in(config.location.tz) if now is None else now
    if not config.gauge.enabled:
        return Refresh(SOURCE, False, None, None, "disabled in config")
    return common.refresh(SOURCE, FILE, now, state_dir=state_dir, force=force,
                          stale=lambda cache, at: stale_reason(cache, config, at),
                          fetch=lambda: fetch(config, now))


def load(state_dir: Path | None = None) -> dict | None:
    """The cached gap, or None."""
    return common.load(FILE, state_dir)


def stale_reason(cache: dict, config: Config, now: datetime) -> str | None:
    """Why `cache` must be refetched for `config` at `now`; None if it's still good."""
    if cache.get("station") != config.gauge.station:
        return f"for station {cache.get('station')}, not {config.gauge.station}"
    if cache.get("units") != UNITS:
        return f"in {cache.get('units')}, not {UNITS}"
    return common.age_problem(cache, now, MAX_AGE)


def request_params(config: Config, now: datetime, product: str) -> dict[str, str]:
    """Query parameters for one NOAA request (`product` is "water_level" or "predictions")."""
    back = max(timedelta(days=config.chart_days_behind), MIN_BACK) + SMOOTHING_ROOM
    end = now.astimezone(timezone.utc)  # in UTC: across a clock change, local time subtracts wrongly
    params = {
        "product": product,
        "application": "photopainter",
        "station": config.gauge.station,
        "datum": DATUM,
        "units": "english",
        "time_zone": "gmt",
        "format": "json",
        "begin_date": f"{end - back:%Y%m%d %H:%M}",
        "end_date": f"{end:%Y%m%d %H:%M}",
    }
    if product == "predictions":
        params["interval"] = "6"
    return params


def fetch(config: Config, now: datetime) -> dict:
    """Download and parse; raises FetchError."""
    measured = common.get_json(API, request_params(config, now, "water_level"))
    predicted = common.get_json(API, request_params(config, now, "predictions"))
    return parse(measured, predicted, config, now)


def parse(measured_raw: Any, predicted_raw: Any, config: Config, fetched: datetime) -> dict:
    """The cache from NOAA's two responses; raises FetchError if they don't make sense."""
    predicted = {_utc(r): _height(r) for r in _rows(predicted_raw, "predictions", "predictions")}
    gaps = []
    for r in _rows(measured_raw, "data", "water levels"):
        at = _utc(r)
        if r.get("v") in ("", None) or at not in predicted:  # a gap in the gauge's record
            continue
        gap = _height(r) - predicted[at]
        if abs(gap) <= MAX_GAP_FT:
            gaps.append((at, round(gap, 3)))
    if not gaps:
        raise FetchError(f"NOAA sent no usable water levels for {config.gauge.station}")
    gaps.sort()
    tz = config.location.tz
    return {
        "station": config.gauge.station,
        "units": UNITS,
        "fetched_at": state.to_iso(fetched.astimezone(tz)),
        "gap": [{"t": state.to_iso(at.astimezone(tz)), "v": v} for at, v in gaps],
    }


def _rows(raw: Any, key: str, what: str) -> list[dict]:
    if isinstance(raw, dict) and raw.get("error"):
        err = raw["error"]
        message = err.get("message") if isinstance(err, dict) else err
        raise FetchError(f"NOAA: {str(message).strip()}")
    rows = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise FetchError(f"NOAA sent no {what}")
    return rows


def _utc(row: dict) -> datetime:
    try:
        return datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise FetchError(f"NOAA reading with a bad time {row.get('t')!r}") from exc


def _height(row: dict) -> float:
    try:
        return float(row["v"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FetchError(f"NOAA reading with a bad height {row.get('v')!r}") from exc
