"""Place data: what's particular to one spot on the coast and isn't a setting (PRD.md §9, A1).

Each place is a folder. The bundled ones are in `data/places/<id>/` (the Wedge's ships with the
repo); the ones built for a frame go in its places folder (`state.places_dir()`, which is
`~/.config/photopainter/places/<id>/` on the Pi, where a deploy doesn't reach), and those win.
A folder may hold any of these:

- `skyline.json` (built by scripts/build-skyline.py): land on the horizon as seen from its
  observer, in degrees of azimuth and height.
- `harbor.json` (built by scripts/build-harbor.py): the harbor chart's map, inside `bounds`.
- `place.json` (written by hand): what a person chose for that spot. `skyline.label` is what
  the screens write on the island; `harbor` holds the chart's title, its default view, the
  names and where they sit (`place_names`, `landmarks`, `island_nudge`, `island_lines`,
  `bold_islands`, `big_piers`) and `marsh_full_ft`; `sea` holds the sea screen's jetty, as
  drawn on the full-size picture, its beach's tide range, and the observer and bearing they
  were drawn for.

Built files are never edited by hand: hand choices live in `place.json`, so a rebuild keeps
them.

Each part is matched on its own, with no setting (Owner, 2026-11-08), and the first folder
that matches wins:

- the skyline when the frame is within 2 km of its observer;
- the harbor chart when the frame is inside its bounds;
- the sea's jetty and beach when the frame is within 2 km of their observer and the window
  faces within 45° of the bearing they were drawn for, since the jetty is drawn on the
  picture as seen from there, looking that way.

So a place nobody has built has an open horizon, no jetty and no harbor chart. A file that
can't be read is skipped (logged, and listed by `problems()` for the dashboard), never fatal.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from photopainter import state

log = logging.getLogger(__name__)

PLACES = "places"  # the bundled places, inside the data dir
PLACE_FILE = "place.json"
SKYLINE_FILE = "skyline.json"
HARBOR_FILE = "harbor.json"
SKYLINE_MAX_KM = 2.0
SEA_MAX_KM = 2.0
SEA_MAX_TURN_DEG = 45.0


class NoPlaceData(LookupError):
    """A screen needs place data this location doesn't have (for example a harbor chart)."""


@dataclass(frozen=True, eq=False)
class Skyline:
    """Land on the horizon, from `skyline.json`."""

    name: str
    az: np.ndarray  # degrees, increasing
    height: np.ndarray  # degrees above the sea horizon
    observer: tuple[float, float]  # (lat, lon) it was computed for
    label: str = ""  # what the screens write on it: place.json's `skyline.label`, else `name`


@dataclass(frozen=True)
class Jetty:
    """A jetty on the sea screen, on the full-size picture (800×480): its left and right
    edges, each from the tip down to the bottom of the picture."""

    left: tuple[tuple[float, float], ...]
    right: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class SeaPlace:
    """The sea screen's part of a place."""

    jetty: Jetty | None
    beach_ft: tuple[float, float] | None  # tides (low, high) for the beach's widest and narrowest


# --- where places are --------------------------------------------------------------------------


def roots(data_dir: Path | str) -> list[Path]:
    """Where places are looked for, in order: this frame's own, then the bundled ones."""
    return [state.places_dir(), Path(data_dir) / PLACES]


def folders(data_dir: Path | str) -> list[Path]:
    """Every place folder, this frame's own first, each root's in name order."""
    found: list[Path] = []
    for root in roots(data_dir):
        try:
            found += sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
    return found


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def turn_between(a: float, b: float) -> float:
    """Degrees between two bearings, 0-180."""
    return abs((a - b + 180) % 360 - 180)


# --- reading -----------------------------------------------------------------------------------


def _json(path: Path) -> Any:
    """A JSON file's contents, cached until it changes. Raises OSError or ValueError."""
    st = path.stat()
    return _json_cached(path, st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=16)
def _json_cached(path: Path, _mtime: int, _size: int) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _place(folder: Path) -> dict[str, Any]:
    """`place.json` in `folder`, or {} if there's none or it can't be read."""
    path = folder / PLACE_FILE
    if not path.is_file():
        return {}
    try:
        data = _json(path)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except (OSError, ValueError) as exc:
        log.warning("skipping %s: %s", path, exc)
        return {}


def _section(folder: Path, key: str) -> dict[str, Any]:
    value = _place(folder).get(key)
    return value if isinstance(value, dict) else {}


def load_skyline(path: Path | str) -> Skyline | None:
    """A skyline file (or the one in a folder), or None if it's missing or unreadable."""
    p = Path(path)
    if p.is_dir():
        p = p / SKYLINE_FILE
    try:
        st = p.stat()
    except OSError:
        return None
    return _load_skyline(p, st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=4)
def _load_skyline(path: Path, _mtime: int, _size: int) -> Skyline | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pts = np.array(data["points"], dtype=float)
        obs = data["observer"]
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2:
            return None
        name = str(data.get("name", ""))
        return Skyline(name, pts[:, 0], pts[:, 1], (float(obs["lat"]), float(obs["lon"])), name)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("skipping %s: %s", path, exc)
        return None


def _points(value: Any) -> tuple[tuple[float, float], ...]:
    pts = tuple((float(x), float(y)) for x, y in value)
    if len(pts) < 2:
        raise ValueError("needs at least two points")
    return pts


# --- matching ----------------------------------------------------------------------------------


def skyline_for(lat: float, lon: float, data_dir: Path | str) -> Skyline | None:
    """The skyline seen from (lat, lon): the first within `SKYLINE_MAX_KM` of its observer."""
    for folder in folders(data_dir):
        if not (folder / SKYLINE_FILE).is_file():
            continue
        sk = load_skyline(folder / SKYLINE_FILE)
        if sk is None or km_between((lat, lon), sk.observer) > SKYLINE_MAX_KM:
            continue
        label = _section(folder, "skyline").get("label")
        return replace(sk, label=label) if isinstance(label, str) else sk
    return None


def harbor_for(lat: float, lon: float, data_dir: Path | str) -> dict[str, Any] | None:
    """The harbor chart around (lat, lon): `harbor.json` whose bounds hold it, with
    place.json's `harbor` section under "place" ({} if there's none)."""
    for folder in folders(data_dir):
        path = folder / HARBOR_FILE
        if not path.is_file():
            continue
        try:
            data = _json(path)
            b = data["bounds"]
            inside = b["south"] <= lat <= b["north"] and b["west"] <= lon <= b["east"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("skipping %s: %s", path, exc)
            continue
        if inside:
            return {**data, "place": _section(folder, "harbor")}
    return None


def sea_for(lat: float, lon: float, bearing: float, data_dir: Path | str) -> SeaPlace | None:
    """The sea screen's jetty and beach for a window at (lat, lon) facing `bearing`."""
    for folder in folders(data_dir):
        sea = _section(folder, "sea")
        if not sea:
            continue
        try:
            obs = sea["observer"]
            at = (float(obs["lat"]), float(obs["lon"]))
            drawn = float(sea["bearing_deg"])
            jetty = None
            if sea.get("jetty") is not None:
                jetty = Jetty(_points(sea["jetty"]["left"]), _points(sea["jetty"]["right"]))
            beach = None
            if sea.get("beach_ft") is not None:
                low, high = (float(v) for v in sea["beach_ft"])
                if not high > low:
                    raise ValueError("beach_ft: the high must be above the low")
                beach = (low, high)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping the sea in %s: %s", folder / PLACE_FILE, exc)
            continue
        if km_between((lat, lon), at) <= SEA_MAX_KM and turn_between(bearing, drawn) <= SEA_MAX_TURN_DEG:
            return SeaPlace(jetty, beach)
    return None


def problems(data_dir: Path | str) -> list[str]:
    """What can't be read in any place folder, for the dashboard."""
    found: list[str] = []
    for folder in folders(data_dir):
        for name in (PLACE_FILE, SKYLINE_FILE, HARBOR_FILE):
            path = folder / name
            if not path.is_file():
                continue
            try:
                data = _json(path)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                found.append(f"{path}: {exc}")
                continue
            if name == SKYLINE_FILE and load_skyline(path) is None:
                found.append(f"{path}: not a skyline (needs points and an observer)")
    return found
