"""Where the stars, the Moon, the planets and the Sun are in the sky, for a time and place.

`sky_view(at, lat, lon, catalog)` gives altitude and azimuth (degrees; azimuth from north
through east) for every star in the catalog, the Moon and the naked-eye planets, as an
observer at (lat, lon) sees them: topocentric, apparent (precession, nutation,
aberration, light time), with standard atmospheric refraction, so positions near the
horizon match the real sky. Computed with Skyfield and the JPL ephemeris (`de421.bsp`);
no network.

Stars come from `data/stars.json` and constellation lines from `data/constellations.json`
(PRD.md §5 "Star data", plus a sixth field per star, its B−V color index; built by
`scripts/build-star-data.py`). Star positions are J2000 without proper motion, which is
good to well under 0.1° for every star in the file. All the stars are computed in one
vectorized Skyfield call. `data/milkyway.png` is the Milky Way's brightness on a galactic
grid (see the build script); `galactic()` turns sky directions into galactic coordinates
to look it up.

The Moon's phase comes from `astro/moon.py`. `MoonView.bright_limb_deg` says which way
its lit side faces, for drawing: the direction from the Moon's center toward the Sun,
measured on the sky in degrees clockwise from straight up (toward the zenith), as seen by
someone facing the Moon. 0 = lit at the top, 90 = lit on the right, 270 = lit on the left.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

from photopainter import state
from photopainter.astro import ephemeris, timescale
from photopainter.astro.moon import MoonPhase, moon_phase

STARS_FILE = "stars.json"
CONSTELLATIONS_FILE = "constellations.json"
MILKY_WAY_FILE = "milkyway.png"

# The naked-eye planets: (name, key in de421.bsp). For Jupiter and Saturn the system
# barycenter is within a few thousandths of a degree of the planet as seen from Earth.
PLANETS = (
    ("Mercury", "mercury"),
    ("Venus", "venus"),
    ("Mars", "mars"),
    ("Jupiter", "jupiter barycenter"),
    ("Saturn", "saturn barycenter"),
)

# Standard atmosphere for refraction (Skyfield applies none below -1° altitude).
REFRACTION = {"temperature_C": 10.0, "pressure_mbar": 1010.0}


# --- star data -----------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class StarCatalog:
    """`data/stars.json` as arrays, one row per star."""

    hip: np.ndarray  # int Hipparcos numbers
    ra_deg: np.ndarray  # J2000
    dec_deg: np.ndarray
    mag: np.ndarray  # visual magnitude (smaller is brighter)
    names: tuple[str | None, ...]
    source: str
    bv: np.ndarray  # B−V color index (NaN if unknown): below 0 blue-white, 0.6 yellow, 1.2 orange, 1.8 red
    rows: dict[int, int] = field(init=False, repr=False)  # Hipparcos number -> row

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", {int(h): i for i, h in enumerate(self.hip)})

    def __len__(self) -> int:
        return len(self.hip)


@dataclass(frozen=True)
class Constellation:
    abbr: str  # IAU abbreviation, "Ori"
    name: str  # "Orion"
    lines: tuple[tuple[int, int], ...]  # pairs of Hipparcos numbers


def load_stars(data_dir: Path | None = None) -> StarCatalog:
    """The star catalog from `<data dir>/stars.json`, read once per path.

    Raises FileNotFoundError if it's missing and ValueError if it isn't in the PRD §5
    format.
    """
    return _load_stars(Path(data_dir or state.data_dir()) / STARS_FILE)


@lru_cache(maxsize=4)
def _load_stars(path: Path) -> StarCatalog:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        rows = data["stars"]
        hip = np.array([int(r[0]) for r in rows], dtype=np.int64)
        ra = np.array([float(r[1]) for r in rows])
        dec = np.array([float(r[2]) for r in rows])
        mag = np.array([float(r[3]) for r in rows])
        names = tuple((r[4] or None) if len(r) > 4 else None for r in rows)
        bv = np.array([float(r[5]) if len(r) > 5 and r[5] is not None else np.nan for r in rows])
    except (TypeError, KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"{path}: not a star list ([hip, ra_deg, dec_deg, mag, name, bv], …): {exc}") from None
    return StarCatalog(hip, ra, dec, mag, names, str(data.get("source", "")), bv)


def load_constellations(data_dir: Path | None = None) -> tuple[Constellation, ...]:
    """Constellation lines from `<data dir>/constellations.json`, read once per path."""
    return _load_constellations(Path(data_dir or state.data_dir()) / CONSTELLATIONS_FILE)


@lru_cache(maxsize=4)
def _load_constellations(path: Path) -> tuple[Constellation, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        return tuple(
            Constellation(str(c["abbr"]), str(c["name"]),
                          tuple((int(a), int(b)) for a, b in c["lines"]))
            for c in data["constellations"])
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError(f"{path}: not a constellation list: {exc}") from None


def load_milky_way(data_dir: Path | None = None) -> np.ndarray | None:
    """`<data dir>/milkyway.png` as a (rows, cols) float array of sqrt(brightness), 0-1:
    row 0 is galactic latitude +90°, the middle column longitude 0, longitude increasing
    to the left. None if the file is missing."""
    path = Path(data_dir or state.data_dir()) / MILKY_WAY_FILE
    return _load_milky_way(path) if path.is_file() else None


@lru_cache(maxsize=2)
def _load_milky_way(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("L"), dtype=np.float32) / 255.0


def milky_way_at(grid: np.ndarray, l_deg: np.ndarray, b_deg: np.ndarray) -> np.ndarray:
    """Bilinear lookup of `grid` (see `load_milky_way`) at galactic (l, b) in degrees."""
    rows, cols = grid.shape
    lw = (np.asarray(l_deg) + 180.0) % 360.0 - 180.0
    x = cols / 2 - lw * cols / 360.0 - 0.5
    y = rows / 2 - np.asarray(b_deg) * rows / 180.0 - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.clip(np.floor(y).astype(int), 0, rows - 2)
    fx, fy = x - x0, np.clip(y - y0, 0.0, 1.0)
    x0, x1 = x0 % cols, (x0 + 1) % cols
    top = grid[y0, x0] * (1 - fx) + grid[y0, x1] * fx
    bottom = grid[y0 + 1, x0] * (1 - fx) + grid[y0 + 1, x1] * fx
    return top * (1 - fy) + bottom * fy


# --- positions -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """A planet, the Moon or the Sun, as seen from the observer."""

    name: str
    alt: float  # degrees, refracted
    az: float  # degrees from north through east


@dataclass(frozen=True)
class MoonView(Body):
    phase: MoonPhase | None = None
    bright_limb_deg: float = 0.0  # see the module docstring


@dataclass(frozen=True)
class SkyView:
    at: datetime
    lat: float
    lon: float
    star_alt: np.ndarray  # degrees, refracted; one per catalog row
    star_az: np.ndarray
    planets: tuple[Body, ...]  # PLANETS order, above the horizon or not
    moon: MoonView
    sun: Body


def sky_view(at: datetime, lat: float, lon: float, catalog: StarCatalog) -> SkyView:
    """Where everything is at the instant `at` (timezone-aware), seen from (lat, lon).

    Raises ValueError for a naive datetime, and EphemerisMissing if `de421.bsp` hasn't
    been fetched.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"naive datetime {at!r}: `at` must be timezone-aware")
    from skyfield.api import Star, wgs84

    eph = ephemeris()
    t = timescale().from_datetime(at)
    here = (eph["earth"] + wgs84.latlon(lat, lon)).at(t)

    def altaz(target) -> tuple[np.ndarray, np.ndarray]:
        alt, az, _ = here.observe(target).apparent().altaz(**REFRACTION)
        return alt.degrees, az.degrees

    star_alt, star_az = altaz(Star(ra_hours=catalog.ra_deg / 15.0, dec_degrees=catalog.dec_deg))
    planets = tuple(Body(name, *map(float, altaz(eph[key]))) for name, key in PLANETS)
    moon_alt, moon_az = map(float, altaz(eph["moon"]))
    sun_alt, sun_az = map(float, altaz(eph["sun"]))
    moon = MoonView("Moon", moon_alt, moon_az, phase=moon_phase(at),
                    bright_limb_deg=bearing(moon_alt, moon_az, sun_alt, sun_az))
    return SkyView(at, float(lat), float(lon), np.atleast_1d(star_alt), np.atleast_1d(star_az),
                   planets, moon, Body("Sun", sun_alt, sun_az))


def moon_view(at: datetime, lat: float, lon: float) -> MoonView:
    """The Moon alone, as `sky_view` computes it, for a screen with no stars to place."""
    from skyfield.api import wgs84

    eph = ephemeris()
    here = (eph["earth"] + wgs84.latlon(lat, lon)).at(timescale().from_datetime(at))

    def altaz(target) -> tuple[float, float]:
        alt, az, _ = here.observe(target).apparent().altaz(**REFRACTION)
        return float(alt.degrees), float(az.degrees)

    moon_alt, moon_az = altaz(eph["moon"])
    sun_alt, sun_az = altaz(eph["sun"])
    return MoonView("Moon", moon_alt, moon_az, phase=moon_phase(at),
                    bright_limb_deg=bearing(moon_alt, moon_az, sun_alt, sun_az))


def sun_altaz(times: list[datetime], lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    """(alt, az) in degrees of the Sun at each of `times` (timezone-aware), the way
    `sky_view` computes it, in one vectorized call."""
    from skyfield.api import wgs84

    eph = ephemeris()
    t = timescale().from_datetimes(times)
    here = (eph["earth"] + wgs84.latlon(lat, lon)).at(t)
    alt, az, _ = here.observe(eph["sun"]).apparent().altaz(**REFRACTION)
    return np.atleast_1d(alt.degrees), np.atleast_1d(az.degrees)


def moon_altaz(at: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(alt, az) in degrees of the Moon at `at`, the way `sky_view` computes it."""
    from skyfield.api import wgs84

    eph = ephemeris()
    here = (eph["earth"] + wgs84.latlon(lat, lon)).at(timescale().from_datetime(at))
    alt, az, _ = here.observe(eph["moon"]).apparent().altaz(**REFRACTION)
    return float(alt.degrees), float(az.degrees)


def star_altaz(at: datetime, lat: float, lon: float, ra_deg: np.ndarray,
               dec_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(alt, az) in degrees for J2000 positions, the way `sky_view` computes stars."""
    from skyfield.api import Star, wgs84

    eph = ephemeris()
    t = timescale().from_datetime(at)
    here = (eph["earth"] + wgs84.latlon(lat, lon)).at(t)
    star = Star(ra_hours=np.asarray(ra_deg, dtype=float) / 15.0, dec_degrees=np.asarray(dec_deg, dtype=float))
    alt, az, _ = here.observe(star).apparent().altaz(**REFRACTION)
    return np.atleast_1d(alt.degrees), np.atleast_1d(az.degrees)


def galactic(at: datetime, lat: float, lon: float, alt_deg: np.ndarray,
             az_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Galactic (l, b) in degrees of the sky directions (alt, az) seen from (lat, lon) at
    `at`. Geometric (no refraction or aberration): for the Milky Way, not for pointing."""
    from skyfield.api import wgs84
    from skyfield.framelib import galactic_frame

    t = timescale().from_datetime(at)
    horizon_to_gal = galactic_frame.rotation_at(t) @ wgs84.latlon(lat, lon).rotation_at(t).T
    a, z = np.radians(np.asarray(alt_deg, dtype=float)), np.radians(np.asarray(az_deg, dtype=float))
    h = np.stack((np.cos(a) * np.cos(z), np.cos(a) * np.sin(z), np.sin(a)))
    g = np.tensordot(horizon_to_gal, h, axes=1)
    return np.degrees(np.arctan2(g[1], g[0])) % 360.0, np.degrees(np.arcsin(np.clip(g[2], -1.0, 1.0)))


def bearing(alt1: float, az1: float, alt2: float, az2: float) -> float:
    """Direction from point 1 toward point 2 on the sky, in degrees clockwise from up
    (toward the zenith) as seen by an observer facing point 1. 0-360.

    Facing any point of the sky, azimuth increases to the right, so this is the
    great-circle initial bearing with altitude as latitude and azimuth as longitude.
    """
    p1, p2 = math.radians(alt1), math.radians(alt2)
    dl = math.radians(az2 - az1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def offset(alt: float, az: float, bearing_deg: float, distance_deg: float) -> tuple[float, float]:
    """The point `distance_deg` from (alt, az) in direction `bearing_deg` (as `bearing`):
    (alt, az) in degrees."""
    p, l = math.radians(alt), math.radians(az)
    b, d = math.radians(bearing_deg), math.radians(distance_deg)
    p2 = math.asin(math.sin(p) * math.cos(d) + math.cos(p) * math.sin(d) * math.cos(b))
    l2 = l + math.atan2(math.sin(b) * math.sin(d) * math.cos(p), math.cos(d) - math.sin(p) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2) % 360.0
