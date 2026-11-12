"""Shared plumbing for the web data sources: one JSON GET, and the cache rules.

Each source (`tides.py`, `weather.py`, `surf.py`, `gauge.py`) keeps one JSON cache in the state dir,
in the format PRD.md §5 gives, and has `ensure_fresh(config, now=None) -> Refresh`:

- It fetches only when the cache is stale by that source's rule: too old, not covering
  far enough ahead, or made for settings that no longer match the config.
- Each request has a 10 s network timeout and a User-Agent with no personal details.
- It never raises. When a fetch fails, the old cache stays exactly as it was and the
  error comes back in `Refresh.error` for `status.json`.
- After a failed fetch the source isn't tried again for `RETRY_AFTER`, so a tick every
  minute while the Wi-Fi is down doesn't wait on the network every time. The last attempt
  per source is kept in `sources.json` in the state dir (written only here).
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from photopainter import __version__, state

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
RETRY_AFTER = timedelta(minutes=5)
CLOCK_SLACK = timedelta(minutes=5)  # a cache "fetched" further in the future than this is stale
MAX_BYTES = 5_000_000  # the biggest response we expect is NOAA's curve, about 85 KB
# Identifies the software only: no email, name or address (PRD.md T1).
USER_AGENT = f"photopainter/{__version__} (e-ink photo frame)"
ATTEMPTS = "sources.json"  # {"tides": {"at": ISO, "error": str | None}, …}


class FetchError(Exception):
    """A fetch that didn't give usable data; the message is readable as is."""


@dataclass(frozen=True)
class Refresh:
    """What `ensure_fresh()` did, for the tick to put in `status.json`."""

    source: str  # "tides", "weather", "surf" or "gauge"
    fetched: bool  # a new cache was written by this call
    error: str | None  # the latest fetch problem, None if there is none; the old cache is kept
    fetched_at: datetime | None  # when the cache now on disk was fetched; None = no cache
    note: str = ""  # why it did or didn't fetch, for logs and the CLI
    cache: dict | None = field(default=None, compare=False, repr=False)  # the cache now on disk

    @property
    def where(self) -> str:
        """`status.json` `last_error.where` for this source's errors, e.g. "tides-fetch"."""
        return f"{self.source}-fetch"


# --- HTTP ------------------------------------------------------------------------------


def get_json(url: str, params: dict[str, Any], *, timeout: float = TIMEOUT_S) -> Any:
    """GET `url?params` and parse the JSON body. Raises FetchError with a readable message."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    host = urllib.parse.urlsplit(url).hostname or url
    request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT,
                                                    "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        raise FetchError(f"{host} answered HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise FetchError(f"{host} didn't answer within {timeout:g} s") from exc
        raise FetchError(f"can't reach {host} ({exc.reason})") from exc
    except TimeoutError as exc:  # socket.timeout is TimeoutError
        raise FetchError(f"{host} didn't answer within {timeout:g} s") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError(f"lost the connection to {host} ({exc!r})") from exc
    if len(body) > MAX_BYTES:
        raise FetchError(f"{host} sent more than {MAX_BYTES // 1_000_000} MB")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise FetchError(f"{host} sent something that isn't JSON") from exc


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The reason from an error body such as Open-Meteo's {"error": true, "reason": "…"}."""
    try:
        data = json.loads(exc.read(4096))
    except Exception:  # noqa: BLE001 - only decoration for the message
        return ""
    if not isinstance(data, dict):
        return ""
    reason = data.get("reason")
    if not reason and isinstance(data.get("error"), dict):  # NOAA: {"error": {"message": …}}
        reason = data["error"].get("message")
    return str(reason)[:200] if reason else ""


# --- times -----------------------------------------------------------------------------


def local_from_unix(seconds: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(seconds, tz)


def now_in(tz: ZoneInfo) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz)


def parse_time(text: Any) -> datetime | None:
    """An aware datetime from an ISO string with offset; None if it isn't one."""
    if not isinstance(text, str):
        return None
    try:
        return state.from_iso(text)
    except ValueError:
        return None


def fetched_at(cache: dict | None) -> datetime | None:
    return parse_time(cache.get("fetched_at")) if isinstance(cache, dict) else None


def hours(td: timedelta) -> str:
    return f"{td.total_seconds() / 3600:g} h"


def age_problem(cache: dict, now: datetime, max_age: timedelta) -> str | None:
    """Why `cache` is too old to keep (None if it isn't)."""
    at = fetched_at(cache)
    if at is None:
        return "no fetch time in the cache"
    if at > now + CLOCK_SLACK:
        return f"fetched in the future ({state.to_iso(at)}; the clock moved back)"
    if now - at > max_age:
        return f"older than {hours(max_age)}"
    return None


def last_time(rows: Any) -> datetime | None:
    """The `t` of the last row of a time-sorted list, or None."""
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
        return None
    return parse_time(rows[-1].get("t"))


def number(value: Any, digits: int | None = None) -> float | int | None:
    """A JSON number as is (rounded to `digits` if given); None stays None (a gap in a model)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FetchError(f"expected a number, got {value!r}")
    return round(value, digits) if digits is not None else value


def columns(block: Any, names: tuple[str, ...], what: str) -> list[tuple]:
    """Time-sorted rows from Open-Meteo's column arrays: [(unix time, a, b, …), …]."""
    if not isinstance(block, dict):
        raise FetchError(f"Open-Meteo sent no {what} data")
    cols = []
    for name in ("time", *names):
        col = block.get(name)
        if not isinstance(col, list):
            raise FetchError(f"Open-Meteo {what} data has no {name}")
        cols.append(col)
    if len({len(c) for c in cols}) != 1 or not cols[0]:
        raise FetchError(f"Open-Meteo {what} data is empty or ragged")
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in cols[0]):
        raise FetchError(f"Open-Meteo {what} times aren't Unix seconds")
    return sorted(zip(*cols), key=lambda row: row[0])


def open_meteo_error(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise FetchError("Open-Meteo sent something that isn't an object")
    if raw.get("error"):
        raise FetchError(f"Open-Meteo: {raw.get('reason') or 'error'}")


# --- cache files -----------------------------------------------------------------------


def cache_path(name: str, state_dir: Path | None = None) -> Path:
    return (state.state_dir() if state_dir is None else state_dir) / name


def load(name: str, state_dir: Path | None = None) -> dict | None:
    """The cache in the state dir, or None if it's missing or unreadable."""
    data = state.read_json(cache_path(name, state_dir), None)
    return data if isinstance(data, dict) else None


def _last_attempt(state_dir: Path, source: str) -> tuple[datetime, str | None] | None:
    attempts = state.read_json(state_dir / ATTEMPTS, {})
    entry = attempts.get(source) if isinstance(attempts, dict) else None
    if not isinstance(entry, dict):
        return None
    at = parse_time(entry.get("at"))
    error = entry.get("error")
    return (at, error if isinstance(error, str) else None) if at else None


def _record_attempt(state_dir: Path, source: str, now: datetime, error: str | None) -> None:
    try:
        with state.update_json(state_dir / ATTEMPTS, {}) as data:
            data[source] = {"at": state.to_iso(now), "error": error}
    except Exception:  # noqa: BLE001 - losing the record only means an earlier retry
        log.exception("couldn't record the %s fetch attempt", source)


def refresh(source: str, name: str, now: datetime, *, state_dir: Path | None,
            stale: Callable[[dict, datetime], str | None], fetch: Callable[[], dict],
            force: bool = False) -> Refresh:
    """Fetch `source` into `<state dir>/<name>` if its cache is stale. Never raises.

    `stale(cache, now)` says why a cache needs refetching (None = it's fine); `fetch()`
    returns the new cache or raises.
    """
    try:
        return _refresh(source, name, now, state_dir, stale, fetch, force)
    except Exception as exc:  # noqa: BLE001 - ensure_fresh never raises
        log.exception("%s refresh failed", source)
        return Refresh(source, False, f"{type(exc).__name__}: {exc}", None, "internal error")


def _refresh(source: str, name: str, now: datetime, state_dir: Path | None,
             stale: Callable[[dict, datetime], str | None], fetch: Callable[[], dict],
             force: bool) -> Refresh:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"naive datetime {now!r}: `now` must be timezone-aware")
    folder = state.state_dir() if state_dir is None else state_dir
    path = folder / name
    old = load(name, folder)
    reason = "forced" if force else "no cache" if old is None else _stale(stale, old, now)
    if reason is None:
        return Refresh(source, False, None, fetched_at(old), "fresh", old)

    last = _last_attempt(folder, source)
    if not force and last is not None and last[1] is not None and last[0] <= now < last[0] + RETRY_AFTER:
        retry = (last[0] + RETRY_AFTER).astimezone(now.tzinfo)
        return Refresh(source, False, last[1], fetched_at(old),
                       f"{reason}; last try failed, next try after {retry:%H:%M}", old)

    try:
        new = fetch()
        state.write_json(path, new)
    except Exception as exc:  # noqa: BLE001 - every failure keeps the old cache
        error = str(exc) if isinstance(exc, FetchError) else f"{type(exc).__name__}: {exc}"
        log.warning("%s fetch failed (%s): %s", source, reason, error)
        _record_attempt(folder, source, now, error)
        return Refresh(source, False, error, fetched_at(old), f"{reason}; fetch failed", old)

    # A cache that's stale straight away (say NOAA sent too few days) is kept, since it's
    # the newest there is, but counts as a failure so it isn't refetched every minute.
    problem = _stale(stale, new, now)
    error = None if problem is None else f"fetched, but still stale: {problem}"
    _record_attempt(folder, source, now, error)
    log.info("%s fetched (%s)%s", source, reason, f"; {error}" if error else "")
    return Refresh(source, True, error, fetched_at(new), reason, new)


def _stale(stale: Callable[[dict, datetime], str | None], cache: dict, now: datetime) -> str | None:
    try:
        return stale(cache, now)
    except Exception as exc:  # noqa: BLE001 - a cache we can't make sense of is stale
        return f"unreadable ({type(exc).__name__}: {exc})"
