"""Tide predictions for the configured NOAA CO-OPS station, cached in `tides.json`.

NOAA's data API (<https://api.tidesandcurrents.noaa.gov/api/prod/>) needs no key. The
default station, 9410580 (Newport Bay Entrance, by the Wedge), has predictions only: no
water level or temperature sensors (`gauge.py` has how far the water is from them). Predictions come from harmonic constants, so one fetch
covers weeks: the frame keeps showing tides through a long internet outage.

A fetch is two requests, both from local midnight yesterday (or as far back as
`Config.chart_days_behind` needs) to `FETCH_DAYS_AHEAD` days ahead: the highs and lows
(`interval=hilo`) and a curve every 30 minutes (`interval=30`).
They ask for GMT, which is unambiguous across daylight-saving changes, and the cache holds
the times in the configured time zone. Heights are feet above the configured datum.

Cache (PRD.md §5 "Tide cache"):

    {"station": "9410580", "datum": "MLLW", "units": "ft", "fetched_at": "…",
     "hilo":  [{"t": "2026-11-03T05:27:00-08:00", "v": 4.711, "type": "H"}, …],
     "curve": [{"t": "2026-11-03T00:00:00-08:00", "v": 2.31}, …]}

It's refetched when it's over 24 h old, covers less than 14 days ahead, doesn't reach back
as far as the chart starts, or is for another station, datum or time zone than the config.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from photopainter import state
from photopainter.config import Config
from photopainter.sources import common
from photopainter.sources.common import FetchError, Refresh

SOURCE = "tides"
FILE = state.TIDES
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MAX_AGE = timedelta(hours=24)
MIN_AHEAD = timedelta(days=14)
FETCH_DAYS_AHEAD = 42  # six weeks; NOAA serves up to a year of predictions per request
CURVE_MINUTES = 30  # the contract allows at most 30-minute spacing
UNITS = "ft"


def ensure_fresh(config: Config, now: datetime | None = None, *, force: bool = False,
                 state_dir: Path | None = None) -> Refresh:
    """Refetch `tides.json` if it's stale (or `force`). Never raises; see `common`."""
    now = common.now_in(config.location.tz) if now is None else now
    return common.refresh(SOURCE, FILE, now, state_dir=state_dir, force=force,
                          stale=lambda cache, at: stale_reason(cache, config, at),
                          fetch=lambda: fetch(config, now))


def load(state_dir: Path | None = None) -> dict | None:
    """The cached tides, or None."""
    return common.load(FILE, state_dir)


def covers_until(cache: dict | None) -> datetime | None:
    """The last moment both the curve and the highs/lows cover (status.json `tides_until`)."""
    if not isinstance(cache, dict):
        return None
    ends = [common.last_time(cache.get("curve")), common.last_time(cache.get("hilo"))]
    return None if None in ends else min(ends)  # type: ignore[type-var]


def stale_reason(cache: dict, config: Config, now: datetime) -> str | None:
    """Why `cache` must be refetched for `config` at `now`; None if it's still good."""
    if cache.get("station") != config.tides.station:
        return f"for station {cache.get('station')}, not {config.tides.station}"
    if cache.get("datum") != config.tides.datum:
        return f"for datum {cache.get('datum')}, not {config.tides.datum}"
    if cache.get("units") != UNITS:
        return f"in {cache.get('units')}, not {UNITS}"
    problem = common.age_problem(cache, now, MAX_AGE)
    if problem:
        return problem
    curve = cache.get("curve")
    first = common.parse_time(curve[0].get("t")) if isinstance(curve, list) and curve else None
    if first is None or first > _midnight(now, config, 0):
        return "missing the start of today"  # e.g. the time zone changed
    behind = config.chart_days_behind
    if first > now - timedelta(days=behind):
        return f"starting {first:%Y-%m-%d %H:%M} (the chart goes back {behind:g} days)"
    until = covers_until(cache)
    if until is None or until < now + MIN_AHEAD:
        shown = "nothing" if until is None else f"only until {until:%Y-%m-%d %H:%M}"
        return f"covering {shown} (needs {MIN_AHEAD.days} days ahead)"
    return None


def request_params(config: Config, now: datetime, interval: str) -> dict[str, str]:
    """Query parameters for one NOAA request (`interval` is "hilo" or "30")."""
    days_back = max(1, math.ceil(config.chart_days_behind))
    begin = _midnight(now, config, -days_back).astimezone(timezone.utc)
    end = _midnight(now, config, FETCH_DAYS_AHEAD + 1).astimezone(timezone.utc)
    return {
        "product": "predictions",
        "application": "photopainter",
        "station": config.tides.station,
        "datum": config.tides.datum,
        "units": "english",
        "time_zone": "gmt",
        "format": "json",
        "interval": interval,
        "begin_date": f"{begin:%Y%m%d %H:%M}",
        "end_date": f"{end:%Y%m%d %H:%M}",
    }


def fetch(config: Config, now: datetime) -> dict:
    """Download and parse; raises FetchError."""
    hilo = common.get_json(API, request_params(config, now, "hilo"))
    curve = common.get_json(API, request_params(config, now, str(CURVE_MINUTES)))
    return parse(hilo, curve, config, now)


def parse(hilo_raw: Any, curve_raw: Any, config: Config, fetched: datetime) -> dict:
    """The cache from NOAA's two responses; raises FetchError if they don't make sense."""
    # Sort and measure in UTC: Python compares two datetimes in the same ZoneInfo by wall
    # clock, so the repeated hour when the clocks go back would sort and subtract wrongly.
    hilo = []
    for p in _predictions(hilo_raw, "highs and lows"):
        kind = str(p.get("type", ""))[:1].upper()  # "H" / "L" ("HH"/"LL" on some products)
        if kind not in ("H", "L"):
            raise FetchError(f"NOAA high/low with type {p.get('type')!r}")
        hilo.append({"t": _utc(p), "v": _height(p), "type": kind})
    curve = [{"t": _utc(p), "v": _height(p)} for p in _predictions(curve_raw, "tide curve")]
    hilo.sort(key=lambda r: r["t"])
    curve.sort(key=lambda r: r["t"])
    for a, b in zip(curve, curve[1:]):
        if b["t"] - a["t"] > timedelta(minutes=CURVE_MINUTES):
            raise FetchError(f"NOAA tide curve has a gap from {a['t']:%Y-%m-%d %H:%M} to "
                             f"{b['t']:%Y-%m-%d %H:%M} UTC")
    tz = config.location.tz
    return {
        "station": config.tides.station,
        "datum": config.tides.datum,
        "units": UNITS,
        "fetched_at": state.to_iso(fetched.astimezone(tz)),
        "hilo": [{**r, "t": state.to_iso(r["t"].astimezone(tz))} for r in hilo],
        "curve": [{**r, "t": state.to_iso(r["t"].astimezone(tz))} for r in curve],
    }


def _midnight(now: datetime, config: Config, days: int) -> datetime:
    """Local midnight `days` after today's."""
    tz = config.location.tz
    return datetime.combine(now.astimezone(tz).date() + timedelta(days=days), time(0), tzinfo=tz)


def _predictions(raw: Any, what: str) -> list[dict]:
    if isinstance(raw, dict) and raw.get("error"):
        err = raw["error"]
        message = err.get("message") if isinstance(err, dict) else err
        raise FetchError(f"NOAA: {str(message).strip()}")
    rows = raw.get("predictions") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise FetchError(f"NOAA sent no {what}")
    return rows


def _utc(p: dict) -> datetime:
    try:
        return datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise FetchError(f"NOAA prediction with a bad time {p.get('t')!r}") from exc


def _height(p: dict) -> float:
    try:
        return float(p["v"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FetchError(f"NOAA prediction with a bad height {p.get('v')!r}") from exc
