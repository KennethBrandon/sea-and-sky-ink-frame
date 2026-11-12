"""Eclipses for the sky screen: every one on Earth, and how it looks from one place.

`find_eclipse(at, lat, lon)` gives the eclipse going on anywhere on Earth at `at`, or due to
begin within `LOOK_AHEAD` (here, if it's seen here): a `LunarEclipse` or `SolarEclipse`. Its `kind` is what it's called,
how deep it gets anywhere (a total eclipse is called total though it's only partial here), and
the rest says what's seen from (lat, lon): whether it's seen at all, and how it looks at `at`.
Computed with Skyfield and the JPL ephemeris, no network.

The Moon goes dark in Earth's own shadow, which falls the same for everyone on the night
side, so a lunar eclipse is worked out from Earth's center: the same shadow as Skyfield's
`eclipselib` (Danjon's enlargement), centered opposite the Sun. Its contact times match the
published ones to about a minute, and they're the same everywhere; it's seen here if the Moon
is up for some of it.

A solar eclipse is the opposite. Anywhere on Earth: the Moon's shadow cones, from the Sun past
the Moon, against Earth as a sphere. It's an eclipse while the penumbra touches Earth, total
or annular if the umbra or its extension does at greatest eclipse (when the shadow's axis
passes closest to Earth's center; a hybrid comes out total, as it is there). From (lat, lon):
how much of the Sun the Moon hides, worked out where you stand. It's seen here if it's on while
the Sun is up. Only the horizon counts, not the weather.

Most nights this costs nothing: an eclipse can only be near a full or new moon, so anywhere
else the search doesn't run. Near one it's a few hundred positions in one vectorized call.

Directions are as in `astro/sky.py`: `bearing_deg` is measured on the sky in degrees clockwise
from straight up (toward the zenith), as seen by someone facing the body. Sizes are in units
of the eclipsed disc's radius, which is how they're drawn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

from photopainter.astro import ephemeris, timescale
from photopainter.astro.moon import moon_phase
from photopainter.astro.sky import REFRACTION, bearing

LOOK_AHEAD = timedelta(hours=12)  # how far ahead an eclipse is mentioned
LONGEST = timedelta(hours=7)  # no eclipse lasts longer, penumbra included
STEP = timedelta(minutes=4)  # the search's grid; contact times are interpolated from it
# The Moon gains about 0.5° an hour on the Sun, so over LOOK_AHEAD + LONGEST it can't get from
# further away than this to a full or new moon (and an eclipse is within 2° of one).
NEAR_SYZYGY_DEG = 13.0

EARTH_RADIUS_KM = 6378.137  # these are the values in Skyfield's eclipselib
SUN_RADIUS_KM = 696340.0
MOON_RADIUS_KM = 1737.1
DANJON = 1.01  # Earth's atmosphere widens its shadow by this much

LunarStage = Literal["penumbral", "partial", "total"]
SolarStage = Literal["partial", "annular", "total"]


@dataclass(frozen=True)
class LunarEclipse:
    """A lunar eclipse, the same wherever the Moon is up. In a penumbral eclipse the Moon never
    meets the dark core of the shadow: `begins` and `ends` are then the penumbra's, otherwise
    the umbra's, what the eye can see. `total_begins` and `total_ends` are None unless it goes
    total."""

    kind: LunarStage  # how deep it gets
    seen: bool  # whether the Moon is up here for some of it
    greatest: datetime
    stage: LunarStage | None  # how deep it is at this moment; None: not started, or over
    begins: datetime
    ends: datetime
    total_begins: datetime | None
    total_ends: datetime | None
    distance: float  # from the Moon's center to the shadow's, in Moon radii
    bearing_deg: float  # from the Moon's center toward the shadow's, seen from here
    umbra: float  # the dark core's radius, in Moon radii
    penumbra: float  # the whole shadow's radius, in Moon radii

    @property
    def covered(self) -> float:
        """How much of the Moon's disc is in the umbra at this moment, 0-1."""
        return disc_overlap(self.distance, self.umbra)


@dataclass(frozen=True)
class SolarEclipse:
    """A solar eclipse. `kind` and `greatest` are for the whole Earth; `here` and the rest are
    from (lat, lon). The times are here when it's seen here, otherwise anywhere on Earth;
    `total_begins` and `total_ends` are when the Moon is wholly inside the Sun or hides it
    here, None if it doesn't."""

    kind: SolarStage  # how deep it gets anywhere: what it's called
    here: SolarStage | None  # how deep it gets here, with the Sun up; None: not seen here
    greatest: datetime  # when the Moon's shadow passes closest to Earth's center
    stage: SolarStage | None  # how deep it is here at this moment; None: not started, over, or not here
    begins: datetime
    ends: datetime
    total_begins: datetime | None
    total_ends: datetime | None
    distance: float  # from the Sun's center to the Moon's, in Sun radii, seen from here
    bearing_deg: float  # from the Sun's center toward the Moon's, seen from here
    moon_size: float  # the Moon's radius, in Sun radii

    @property
    def seen(self) -> bool:
        return self.here is not None

    @property
    def covered(self) -> float:
        """How much of the Sun's disc the Moon hides here at this moment, 0-1."""
        return disc_overlap(self.distance, self.moon_size)


Eclipse = LunarEclipse | SolarEclipse


def find_eclipse(at: datetime, lat: float, lon: float, phase_deg: float | None = None) -> Eclipse | None:
    """The eclipse going on anywhere at `at` (timezone-aware), or due to begin within
    `LOOK_AHEAD`, as seen from (lat, lon); None if there's none. `phase_deg` is the Moon's
    phase at `at`, if already known."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"naive datetime {at!r}: `at` must be timezone-aware")
    if phase_deg is None:
        phase_deg = moon_phase(at).phase_deg
    from_full = abs(phase_deg % 360.0 - 180.0)
    if from_full <= NEAR_SYZYGY_DEG:
        return _lunar(at, lat, lon)
    if from_full >= 180.0 - NEAR_SYZYGY_DEG:
        return _solar(at, lat, lon)
    return None


def _grid(at: datetime) -> tuple[np.ndarray, object]:
    """Skyfield times around `at`, one of them `at` itself: seconds from `at`, and the times."""
    before = int(LONGEST / STEP) + 1
    after = int((LOOK_AHEAD + LONGEST) / STEP) + 1
    seconds = np.arange(-before, after + 1) * STEP.total_seconds()
    t = timescale().from_datetime(at)
    return seconds, timescale().tt_jd(t.tt + seconds / 86400.0)


def _span(seconds: np.ndarray, gap: np.ndarray) -> tuple[float, float] | None:
    """When `gap` (one contiguous dip below zero, on the grid `seconds`) goes below zero and
    comes back up, interpolated: seconds from `at`. None if it never does. A dip that runs
    off the grid starts or ends there."""
    inside = np.nonzero(gap < 0)[0]
    if inside.size == 0:
        return None
    i, j = int(inside[0]), int(inside[-1])
    start = float(seconds[i])
    if i > 0:
        start = float(np.interp(0.0, [gap[i], gap[i - 1]], [seconds[i], seconds[i - 1]]))
    end = float(seconds[j])
    if j < len(gap) - 1:
        end = float(np.interp(0.0, [gap[j], gap[j + 1]], [seconds[j], seconds[j + 1]]))
    return start, end


def _lowest(seconds: np.ndarray, y: np.ndarray) -> float:
    """When `y` is lowest, between grid points (a parabola through the lowest three)."""
    i = int(np.argmin(y))
    if 0 < i < len(y) - 1:
        a, b, c = float(y[i - 1]), float(y[i]), float(y[i + 1])
        if a - 2 * b + c > 0:
            return float(seconds[i]) + 0.5 * (a - c) / (a - 2 * b + c) * float(seconds[1] - seconds[0])
    return float(seconds[i])


def _when(at: datetime, span: tuple[float, float] | None) -> tuple[datetime | None, datetime | None]:
    if span is None:
        return None, None
    return at + timedelta(seconds=span[0]), at + timedelta(seconds=span[1])


def _angle(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Radians between unit vectors, arrays of shape (3, n); good for small angles too."""
    return 2.0 * np.arcsin(np.clip(np.linalg.norm(u - v, axis=0) / 2.0, 0.0, 1.0))


def _altaz_of(rotation: np.ndarray, v: np.ndarray) -> tuple[float, float]:
    """(alt, az) in degrees of the direction `v`, turned into the local horizon frame."""
    n, e, u = rotation @ v
    return math.degrees(math.asin(u)), math.degrees(math.atan2(e, n)) % 360.0


def _lunar(at: datetime, lat: float, lon: float) -> LunarEclipse | None:
    from skyfield.api import wgs84

    eph = ephemeris()
    seconds, times = _grid(at)
    now = int(np.argmin(np.abs(seconds)))
    earth = eph["earth"].at(times)
    sun, moon = earth.observe(eph["sun"]).apparent().position.km, earth.observe(eph["moon"]).apparent().position.km
    d_sun, d_moon = np.linalg.norm(sun, axis=0), np.linalg.norm(moon, axis=0)
    u_moon, u_shadow = moon / d_moon, -sun / d_sun
    separation = _angle(u_moon, u_shadow)

    moon_r = np.arcsin(MOON_RADIUS_KM / d_moon)
    enlarged = DANJON * EARTH_RADIUS_KM / d_moon + EARTH_RADIUS_KM / d_sun
    sun_r = SUN_RADIUS_KM / d_sun
    umbra, penumbra = enlarged - sun_r, enlarged + sun_r

    in_penumbra = _span(seconds, separation - penumbra - moon_r)
    if in_penumbra is None or not (in_penumbra[1] > 0.0):
        return None
    in_umbra = _span(seconds, separation - umbra - moon_r)
    total = _span(seconds, separation - umbra + moon_r)
    visible = in_umbra or in_penumbra
    if visible[0] > LOOK_AHEAD.total_seconds():
        return None
    moon_alt = (eph["earth"] + wgs84.latlon(lat, lon)).at(times).observe(eph["moon"]).apparent().altaz(**REFRACTION)[0].degrees
    during = (seconds >= visible[0]) & (seconds <= visible[1])

    kind: LunarStage = "total" if total else "partial" if in_umbra else "penumbral"
    sep, r = float(separation[now]), float(moon_r[now])
    stage: LunarStage | None = None
    if sep < float(umbra[now]) - r:
        stage = "total"
    elif sep < float(umbra[now]) + r:
        stage = "partial"
    elif sep < float(penumbra[now]) + r:
        stage = "penumbral"
    rotation = wgs84.latlon(lat, lon).rotation_at(timescale().from_datetime(at))
    alt1, az1 = _altaz_of(rotation, u_moon[:, now])
    alt2, az2 = _altaz_of(rotation, u_shadow[:, now])
    begins, ends = _when(at, visible)
    total_begins, total_ends = _when(at, total)
    return LunarEclipse(kind, bool((moon_alt[during] > 0.0).any()), at + timedelta(seconds=_lowest(seconds, separation)),
                        stage, begins, ends, total_begins, total_ends, sep / r, bearing(alt1, az1, alt2, az2),
                        float(umbra[now]) / r, float(penumbra[now]) / r)


def _solar(at: datetime, lat: float, lon: float) -> SolarEclipse | None:
    from skyfield.api import wgs84

    eph = ephemeris()
    seconds, times = _grid(at)
    now = int(np.argmin(np.abs(seconds)))

    # Anywhere on Earth: the shadow's axis runs from the Sun through the Moon. `along` is how
    # far past the Moon it comes closest to Earth's center, `miss` how close that is (km).
    earth = eph["earth"].at(times)
    sun_c, moon_c = earth.observe(eph["sun"]).position.km, earth.observe(eph["moon"]).position.km
    axis = moon_c - sun_c
    sun_to_moon = np.linalg.norm(axis, axis=0)
    axis = axis / sun_to_moon
    along = -(moon_c * axis).sum(axis=0)
    miss = np.linalg.norm(moon_c + axis * along, axis=0)
    penumbra_r = MOON_RADIUS_KM + along * (SUN_RADIUS_KM + MOON_RADIUS_KM) / sun_to_moon
    anywhere = _span(seconds, miss - EARTH_RADIUS_KM - penumbra_r)
    if anywhere is None or not (anywhere[1] > 0.0) or anywhere[0] > LOOK_AHEAD.total_seconds():
        return None
    g = int(np.argmin(miss))
    to_surface = along[g] - math.sqrt(max(0.0, EARTH_RADIUS_KM ** 2 - float(miss[g]) ** 2))
    umbra_r = MOON_RADIUS_KM - to_surface * (SUN_RADIUS_KM - MOON_RADIUS_KM) / float(sun_to_moon[g])  # < 0: annular
    kind: SolarStage = "partial"
    if float(miss[g]) - abs(umbra_r) < EARTH_RADIUS_KM:
        kind = "total" if umbra_r > 0 else "annular"
    greatest = at + timedelta(seconds=_lowest(seconds, miss))

    # From here.
    here_at = (eph["earth"] + wgs84.latlon(lat, lon)).at(times)
    sun_seen, moon_seen = here_at.observe(eph["sun"]).apparent(), here_at.observe(eph["moon"]).apparent()
    sun, moon = sun_seen.position.km, moon_seen.position.km
    d_sun, d_moon = np.linalg.norm(sun, axis=0), np.linalg.norm(moon, axis=0)
    separation = _angle(moon / d_moon, sun / d_sun)
    sun_r, moon_r = np.arcsin(SUN_RADIUS_KM / d_sun), np.arcsin(MOON_RADIUS_KM / d_moon)
    sun_alt, sun_az, _ = sun_seen.altaz(**REFRACTION)
    moon_alt, moon_az, _ = moon_seen.altaz(**REFRACTION)
    on = _span(seconds, separation - sun_r - moon_r)
    seen = on is not None and bool((sun_alt.degrees[(seconds >= on[0]) & (seconds <= on[1])] > 0.0).any())
    if seen and on[0] > LOOK_AHEAD.total_seconds():
        return None  # it begins here later than it does elsewhere: mentioned from 12 h before that

    sep, a, b = float(separation[now]), float(sun_r[now]), float(moon_r[now])
    distance, toward = sep / a, bearing(float(sun_alt.degrees[now]), float(sun_az.degrees[now]),
                                        float(moon_alt.degrees[now]), float(moon_az.degrees[now]))
    if not seen:
        begins, ends = _when(at, anywhere)
        return SolarEclipse(kind, None, greatest, None, begins, ends, None, None, distance, toward, b / a)
    central = _span(seconds, separation - np.abs(sun_r - moon_r))
    deepest = int(np.argmin(separation))
    here: SolarStage = "partial" if central is None else "total" if moon_r[deepest] >= sun_r[deepest] else "annular"
    stage: SolarStage | None = None
    if sep < abs(a - b):
        stage = "total" if b >= a else "annular"
    elif sep < a + b:
        stage = "partial"
    begins, ends = _when(at, on)
    total_begins, total_ends = _when(at, central)
    return SolarEclipse(kind, here, greatest, stage, begins, ends, total_begins, total_ends, distance, toward, b / a)


def disc_overlap(distance: float, radius: float) -> float:
    """How much of a disc of radius 1 is covered by a disc of `radius` whose center is
    `distance` from its own, 0-1."""
    d, r = abs(distance), radius
    if d >= 1.0 + r:
        return 0.0
    if d <= abs(1.0 - r):
        return 1.0 if r >= 1.0 else r * r
    a = math.acos(max(-1.0, min(1.0, (d * d + 1.0 - r * r) / (2.0 * d))))
    b = math.acos(max(-1.0, min(1.0, (d * d + r * r - 1.0) / (2.0 * d * r))))
    area = a + r * r * b - 0.5 * math.sqrt(max(0.0, (-d + 1.0 + r) * (d + 1.0 - r) * (d - 1.0 + r) * (d + 1.0 + r)))
    return min(1.0, area / math.pi)
