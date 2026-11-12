"""The sky screen (scene kind `sky`): the sky through the Viewer's south-facing window.

The night look was designed with the Owner over labeled previews and approved on
2026-11-04. At 800×480:

    ☁ 64° Cloudy                                          ◯ Moon rises 2:45 am
    High 74°  Low 58°                                      ☼ Sun rises 6:15 am
    NW 5–11 mph ↖                                               Sky at 9:30 pm
          · PEGASUS ·  ·          Saturn ⊖            ░░░ Milky Way (dots)
       ·  ╲__·___·          ·  Altair             ░░░
    ◂ Venus      ◐ the Moon               ·                 Jupiter ▸
    ─────────────────────── horizon ───────────── ⌒CATALINA⌒ ───────── (sea)
    ~~~~~~~~~~~~~~~~~~~~ surf ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
       SE                    S                    SW                    W  (sand)

- Black sky; stars sized by magnitude, the brightest colored by their B−V index (orange
  and orange-red dithered from the red and yellow inks; hot stars white in a blue ring);
  names of stars brighter than magnitude 1.5.
- Constellation lines and names in green, 1 px, for the main constellations (a star of
  magnitude 3 or brighter, or the zodiac: 55 of 88); lines stop short of their stars.
- The Milky Way as ordered white dots, denser where it's brighter (ESO's photograph).
- Planets as discs in yellow (Mars red, Saturn ringed), labeled in their ink; the Moon
  20 px, lit toward the Sun with its real phase, dark side black. Planets and the Moon
  that are up but beyond the sides of the view are named at that edge, with an arrow.
- Below the horizon: the Pacific (blue, a few white glints, and the Moon's path on the
  water when it's up), a white line of surf, dark brown sand (stippled yellow, red and
  black dots) with the compass points in white. Catalina on the horizon from its real
  skyline, 4× taller than life, grey (white ordered dots on half its pixels) with a white
  ridge line, labeled. Chosen on the panel on 2026-11-04 over solid white, solid green and
  black: black was hard to make out.
- The weather top left (added 2026-11-04, in place of the time and date): the condition
  with an icon, the temperature, today's high and low, and the wind with an arrow pointing
  the way it blows as seen from the window (left is east, like the sky). It comes from the
  day screen's weather cache and helpers (`scenes/tides.py`), with one change: a clear
  night gets stars for an icon, since the day screen's crescent would pass for the real
  Moon. Left out when the cache is missing, over 12 h old or weather is disabled; "as of"
  under it when it's over 2 h old.
- Top right: when the Sun next rises or sets and, while the Moon is down, when it rises,
  soonest first; under them, in small letters, the time the sky is drawn for.
- Both corners sit on solid blue boxes, the same day and night, in white bold letters (Owner,
  2026-11-06, over labeled previews of five ways). The boxes are flush with the screen's edges,
  with only the inner corner rounded and no line around them. Blue echoes the sea along the
  bottom. They replaced white boxes ruled in black by day, too stark on the dithered sky, and
  thin letters on plain black at night, hard to read.
Positions come from `astro/sky.py`.

By day and twilight (the Sun less than 15° below the horizon; deeper than that, everything
above is exactly what's drawn), the same view takes its colors from the Sun's altitude:
- The sky is ordered dots of blue and white (`sky_shares`): deepest overhead, paler toward
  the horizon and around the Sun, darkening through blue and black dots as it sets. With
  the Sun low, a sunset along the horizon, strongest toward the Sun: pale, then yellow,
  then orange (and purple above that, once the Sun is down).
- The Sun is 22 px with rays, on the water a wide path of glints, with today's path across
  the sky as a dashed arc with the hours marked, so the screen reads as a sundial. Off the
  sides of the view it gets an edge marker like the planets; planets next to it are lost
  in its glare. Top right: when it next sets (or rises, at dawn).
- Fewer stars: only magnitude 2 and brighter by day, more as the sky darkens (`star_limit`).
  No Milky Way until it's nearly dark. Constellation lines stay on, white while the sky
  is light, though most of the stars on them are too faint to draw. The Moon's dark side
  is blue.
- While the sky is light (the Sun above -2°), names are black on small white boxes, since white
  letters with a thin outline got lost in the pale dots (Owner, 2026-11-05, over labeled
  previews). In the deeper twilight they go back to white with a dark outline.
- The magnitude, the lines and the arc are settings (`sky.day_magnitude_limit`,
  `day_constellation_lines`, `sun_arc`), with the choices above as their defaults.
- Bright sand (`day_sand`) with black compass points, and a hazy Catalina, paler than the sea.
A Sky pin is the only way to see this outside dusk and dawn: by day the schedule shows the
day screen.

During an eclipse seen from here (`astro/eclipse.py`), to scale with the disc as drawn (Owner,
2026-11-07, over labeled previews: the Moon its usual size, rather than half as big again,
with or without the umbra's edge dashed around it):
- A lunar eclipse: each pixel of the Moon by where it falls in Earth's shadow. In the
  penumbra white with black dots, denser toward the umbra; in the umbra red, coppery with
  yellow along its edge and darkening with black toward its center, as the real thing does.
  Its path on the water fades as it's covered and goes red when it's total.
- A solar eclipse: black where the Moon crosses the Sun, the rays left as they are. It can
  only be seen by day, so only on a Sky pin or a rotation with the sky in it: an eclipse
  doesn't take over the schedule (Owner, 2026-11-07).
- Top right, first, every eclipse on Earth from 12 h before it begins, in two rows (Owner,
  2026-11-07): its name, by how deep it gets anywhere ("Total solar eclipse" though it's only
  partial here; "Faint lunar eclipse" for a penumbral one, which the eye can hardly see), then
  what's seen here: "Begins 2:04 am", "Total at 3:04 am", "Total until 4:02 am", "Ends 5:17 am";
  "Partial here from 10:05 am" and "Partial here until 12:22 pm"; or "11:11 am, not seen here".
  The icons are the day screen's.
- An ordinary new moon isn't drawn when it would touch the Sun, so it can't pass for an eclipse.

Projection: stereographic, centered on the horizon at `sky.center_azimuth_deg`.
- The horizon is a great circle through the center of the projection, so it comes out
  as a straight line along the bottom of the screen.
- It's conformal: constellations keep their shapes everywhere, so Orion looks like
  Orion at the edge too. A cylindrical map (azimuth across, altitude up) would stretch
  the sky sideways by 1/cos(altitude): twice as wide at 60°, where the Moon and planets
  often are.
- It's Stellarium's default, so the chart is easy to check against it, and the default
  170° across by 90° up (to the zenith, at the top center) fits the sky's 800×422 almost
  exactly.
- The costs: things grow toward the edges (1.8× at 85° from the center), and lines of
  equal azimuth lean in toward the zenith.
The scale is the largest at which both `field_of_view_deg` along the horizon and
`max_altitude_deg` above the center fit, so at least that much sky is shown.

Two halves, like the day screen: `sky_model()` works out what to show (pure data,
testable), and `draw()` draws it on an `InkCanvas` in a `SkyStyle`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from photopainter.astro.eclipse import Eclipse, LunarEclipse, SolarEclipse, find_eclipse
from photopainter.astro.moon import moon_events
from photopainter.astro.sky import (
    Body, Constellation, MoonView, SkyView, StarCatalog, load_constellations, load_milky_way,
    load_stars, offset, sky_view, sun_altaz,
)
from photopainter.astro.sun import sun_events
from photopainter.config import Config
from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.scenes.base import Scene, SceneContext
from photopainter.scenes.tides import (
    Weather, compass, condition, deg, flow_arrow, load_weather, weather_icon,
)

BLACK, WHITE, YELLOW, RED, BLUE, GREEN = (INK[n] for n in ("black", "white", "yellow", "red", "blue", "green"))

COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


# --- style ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkyStyle:
    sky: int = BLACK
    star: int = WHITE
    line: int = GREEN  # constellation lines
    line_width: int = 1
    con_label: int = GREEN  # constellation names
    con_label_size: int = 15
    star_label: int = WHITE  # names of the brightest stars
    label_halo: int | None = None  # around names; None: the sky's ink
    star_label_size: int = 15
    star_label_mag: float = 1.5  # name stars at least this bright
    beach: bool = True  # below the horizon: the ocean, a line of surf and the sand (else a plain band)
    sand_mix: tuple[tuple[int, float], ...] = ((YELLOW, 0.25), (RED, 0.30))  # stippled dots; the rest black
    sand_text: int = WHITE  # the compass points…
    sand_halo: int = BLACK  # …and the halo around them
    ocean_h: int = 22  # pixels of ocean between the horizon and the surf
    ground: int = BLUE  # the ocean (or the plain band)
    ground_text: int = WHITE
    horizon: int = WHITE
    horizon_width: int = 2
    text: int = WHITE  # the weather and the notes, on their boxes
    corner_box: int = BLUE  # the solid boxes behind them, day and night
    label_box: bool = False  # names on a solid box of `halo` instead of with a halo around each letter
    moon_radius: int = 20
    moon_lit: int = WHITE
    moon_dark: int = BLACK  # the unlit part
    moon_outline: int | None = None
    planet_radius: int = 6
    planet_label_size: int = 18
    planet_inks: dict[str, int] = field(default_factory=lambda: {
        "Mercury": YELLOW, "Venus": YELLOW, "Mars": RED, "Jupiter": YELLOW, "Saturn": YELLOW})
    planet_label_ink: int | None = None  # None: the planet's own ink
    star_min_d: int = 2  # pixel diameter of the faintest stars drawn…
    star_d_per_mag: float = 1.2  # …growing this much per magnitude brighter…
    star_max_d: int = 10  # …up to this
    constellations: str = "main"  # "main": a star of magnitude 3 or brighter, or the zodiac; "all": 88
    star_colors: str = "dither"  # "off": all white; "solid": white/yellow/red; "dither": adds orange
    color_mag: float = 2.5  # color only stars at least this bright (the eye sees faint stars as white)
    milky_way: int | None = WHITE  # ink for the Milky Way's dots; None: leave it out
    mw_pattern: str = "ordered"  # "ordered" (8×8 Bayer, like the day screen's night) or "stipple"
    mw_from: float = 0.30  # sqrt-brightness (data/milkyway.png) where the dots start…
    mw_to: float = 0.95  # …and where they reach full coverage
    mw_gamma: float = 1.4  # >1 keeps faint parts sparse, so the bright core and dark lanes stand out
    mw_density: float = 0.40  # fraction of pixels dotted at full coverage
    moon_path: bool = True  # the Moon's reflection on the water when it's up
    island: bool = True  # Catalina on the horizon (data/skyline.json)
    island_exaggeration: float = 4.0  # it's half a degree tall; drawn this many times taller
    island_fill: int = WHITE  # grey: half the pixels white, in ordered dots (Owner, on the panel)
    island_base: int = BLACK  # what island_fill's dots are on
    island_fill_density: float = 0.5  # below 1: that share of ordered dots of island_fill on island_base
    island_outline: int | None = WHITE  # the ridge line
    island_label: bool = True
    off_view: bool = True  # name planets and the Moon that are up but outside the view, at the edge
    footer_h: int = 58
    compass_size: int = 22  # the compass points on the sand
    hour_size: int = 14  # the hours along the Sun's path
    notes: bool = True  # the Sun's and Moon's next rise or set and the time, top right
    con_names: bool = True  # the constellations' names, with their lines
    reserved: tuple[tuple[float, float, float, float], ...] = ()  # boxes no name may be placed on
    # The sky by daylight and twilight (see the module docstring, `lit_style`, `sky_shares`).
    day_label: int = BLACK  # names over the sky by day…
    day_label_halo: int = WHITE  # …and their halo (or box)
    day_label_box: bool = True  # names on boxes by day, rather than with halos
    zenith_tone: tuple[float, ...] = (0.0, 0.1, 0.3, 0.6, 0.85, 1.03, 1.08, 1.15, 1.2)  # at each of SUN_ALTS
    horizon_tone: tuple[float, ...] = (0.0, 0.1, 0.25, 0.4, 0.55, 0.6, 0.65, 0.7, 0.7)  # added at the horizon
    glow_tone: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.1, 0.35, 0.55, 0.7, 0.7)  # added around the Sun
    haze_deg: float = 16.0  # how far up the horizon's brightness reaches
    glow_deg: float = 14.0  # how far the Sun's glow reaches
    warm: tuple[float, ...] = (0.0, 0.2, 0.5, 0.8, 1.0, 1.0, 0.8, 0.0, 0.0)  # the sunset's strength at each of SUN_ALTS
    warm_band_deg: float = 18.0  # how far up they reach
    warm_spread_deg: float = 80.0  # how far along the horizon from the Sun
    day_sand: tuple[tuple[int, float], ...] = ((YELLOW, 0.62), (RED, 0.03), (WHITE, 0.33))  # the rest black
    sun_radius: int = 22
    sun_rays: bool = True
    sun_ring: int | None = RED  # a thin ring, so the disc stands out on a pale sky
    sun_path: bool = True  # the Sun's reflection on the water

    @property
    def halo(self) -> int:
        """The ink around names over the sky."""
        return self.sky if self.label_halo is None else self.label_halo


# --- projection ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Stereographic:
    """Stereographic projection centered on the horizon at azimuth `az0`.

    (alt, az) in degrees → screen (x, y): the center goes to (cx, cy) and `k` is pixels
    per unit of 2·tan(angle from center / 2). Azimuth increases to the right (facing
    south, east is on the left).
    """

    az0: float
    cx: float
    cy: float  # the horizon's screen row
    k: float

    @classmethod
    def fit(cls, az0: float, fov_deg: float, max_alt_deg: float, width: int, horizon_y: int) -> Stereographic:
        fov = min(max(fov_deg, 1.0), 300.0)
        alt = min(max(max_alt_deg, 1.0), 90.0)
        k = min((width / 2) / (2 * math.tan(math.radians(fov / 4))),
                horizon_y / (2 * math.tan(math.radians(alt / 2))))
        return cls(float(az0), width / 2, float(horizon_y), k)

    def project(self, alt: np.ndarray | float, az: np.ndarray | float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(x, y, ok): ok is False for points too far behind the view (over 120° from
        its center) to project sensibly."""
        phi = np.radians(np.asarray(alt, dtype=float))
        dl = np.radians((np.asarray(az, dtype=float) - self.az0 + 180.0) % 360.0 - 180.0)
        cos_c = np.cos(phi) * np.cos(dl)  # cosine of the angle from the center
        ok = cos_c > -0.5
        denom = np.where(ok, 1.0 + cos_c, 1.0)
        x = self.cx + self.k * 2 * np.cos(phi) * np.sin(dl) / denom
        y = self.cy - self.k * 2 * np.sin(phi) / denom
        return x, y, ok

    def point(self, alt: float, az: float) -> tuple[float, float, bool]:
        x, y, ok = self.project(alt, az)
        return float(x), float(y), bool(ok)

    def unproject(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(alt, az) in degrees of screen points: the inverse of `project`."""
        X, Y = (x - self.cx) / self.k, (self.cy - y) / self.k
        rho = np.hypot(X, Y)
        ang = 2 * np.arctan(rho / 2)  # from the center of the view
        sin_c, cos_c = np.sin(ang), np.cos(ang)
        with np.errstate(invalid="ignore", divide="ignore"):
            alt = np.degrees(np.arcsin(np.where(rho > 0, Y * sin_c / rho, 0.0)))
        az = self.az0 + np.degrees(np.arctan2(X * sin_c, rho * cos_c))
        return alt, az

    def horizon_x(self, az: float) -> float:
        dl = math.radians((az - self.az0 + 180.0) % 360.0 - 180.0)
        return self.cx + 2 * self.k * math.tan(dl / 2)

    def half_fov(self, width: int) -> float:
        """Degrees of azimuth from the center to the left/right edge along the horizon."""
        return math.degrees(2 * math.atan((width / 2) / (2 * self.k)))


# --- model ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkyModel:
    place: str
    now: datetime  # local, the moment drawn
    view: SkyView
    catalog: StarCatalog
    constellations: tuple[Constellation, ...]
    moon_next: tuple[str, datetime] | None  # ("rises" | "sets", when), the next one within a day
    milky_way: np.ndarray | None = None  # data/milkyway.png, if it's there
    skyline: Skyline | None = None  # Catalina, when seen from about where it was computed
    sun_next: tuple[str, datetime] | None = None  # ("rises" | "sets", when)
    sun_track: tuple[tuple[datetime, float, float], ...] = ()  # today's (time, alt, az) every 10 min; lit only
    weather: Weather | None = None  # the day screen's weather, when the cache is fresh enough
    eclipse: Eclipse | None = None  # going on, or due soon, and seen from here


def sky_model(now: datetime, ctx: SceneContext) -> SkyModel:
    loc = ctx.config.location
    now = now.astimezone(loc.tz)
    catalog = load_stars(ctx.data_dir)
    view = sky_view(now, loc.lat, loc.lon, catalog)
    skyline = load_skyline(ctx.data_dir)
    if skyline is not None and _km_between((loc.lat, loc.lon), skyline.observer) > SKYLINE_MAX_KM:
        skyline = None  # computed for somewhere else
    lit = view.sun.alt > NIGHT_SUN_ALT
    weather = None
    if ctx.config.weather.enabled:
        from photopainter.sources import weather as weather_source
        weather = load_weather(weather_source.load(ctx.state_dir), now, any_age=ctx.any_age)
    return SkyModel(loc.name, now, view, catalog, load_constellations(ctx.data_dir),
                    next_moon_event(now, view.moon.alt > 0, ctx.config), load_milky_way(ctx.data_dir),
                    skyline, next_sun_event(now, ctx.config),
                    sun_track(now, ctx.config) if lit else (), weather,
                    find_eclipse(now, loc.lat, loc.lon, view.moon.phase.phase_deg if view.moon.phase else None))


# --- the skyline (Catalina) ----------------------------------------------------------------

SKYLINE_FILE = "skyline.json"
SKYLINE_MAX_KM = 2.0


@dataclass(frozen=True, eq=False)
class Skyline:
    """`data/skyline.json` (built by scripts/build-skyline.py): land on the horizon."""

    name: str
    az: np.ndarray  # degrees, increasing
    height: np.ndarray  # degrees above the sea horizon
    observer: tuple[float, float]  # (lat, lon) it was computed for


def load_skyline(data_dir) -> Skyline | None:
    """The skyline file, or None if it's missing or unreadable."""
    return _load_skyline(Path(data_dir) / SKYLINE_FILE)


@lru_cache(maxsize=2)
def _load_skyline(path: Path) -> Skyline | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pts = np.array(data["points"], dtype=float)
        obs = data["observer"]
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2:
            return None
        return Skyline(str(data.get("name", "")), pts[:, 0], pts[:, 1], (float(obs["lat"]), float(obs["lon"])))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def skyline_polygon(sk: Skyline, proj: Stereographic, exaggeration: float) -> list[tuple[float, float]]:
    """The island's outline on screen, closed along the horizon."""
    x, y, _ = proj.project(sk.height * exaggeration, sk.az)
    pts = list(zip(x.tolist(), y.tolist()))
    return [(pts[0][0], proj.cy)] + pts + [(pts[-1][0], proj.cy)]


def draw_skyline(c: InkCanvas, poly: list[tuple[float, float]], style: SkyStyle) -> None:
    """The island: filled, hiding the stars behind it, with its ridge line on top."""
    if style.island_fill_density >= 1.0:
        c.draw.polygon(poly, fill=style.island_fill)
    else:  # e.g. grey: white dots on black
        W, H = c.size
        c.draw.polygon(poly, fill=style.island_base)
        shape = InkCanvas((W, H), "black")
        shape.draw.polygon(poly, fill=WHITE)
        iy, ix = np.mgrid[0:H, 0:W]
        ys, xs = np.nonzero((np.asarray(shape.image) == WHITE) & (BAYER8[iy % 8, ix % 8] < style.island_fill_density))
        c.draw.point(np.column_stack((xs, ys)).ravel().tolist(), fill=style.island_fill)
    if style.island_outline is not None:
        c.draw.line(poly[1:-1], fill=style.island_outline, width=1)


def next_moon_event(now: datetime, up: bool, config: Config) -> tuple[str, datetime] | None:
    """When the Moon next sets (if it's up) or rises (if not), within the next 24 h."""
    loc = config.location
    for day in (now.date(), now.date() + timedelta(days=1)):
        rise, set_ = moon_events(day, loc.lat, loc.lon, loc.tz)
        t = set_ if up else rise
        if t is not None and now < t <= now + timedelta(hours=24):
            return ("sets" if up else "rises"), t
    return None


def next_sun_event(now: datetime, config: Config) -> tuple[str, datetime] | None:
    """The next sunrise or sunset, or None where the Sun doesn't rise and set today."""
    loc = config.location
    for day in (now.date(), now.date() + timedelta(days=1)):
        try:
            events = sun_events(day, loc.lat, loc.lon, loc.tz)
        except ValueError:
            return None
        for what, t in (("rises", events.sunrise), ("sets", events.sunset)):
            if t > now:
                return what, t
    return None


def sun_track(now: datetime, config: Config) -> tuple[tuple[datetime, float, float], ...]:
    """Where the Sun is every 10 minutes of the local day: (time, alt, az)."""
    loc = config.location
    start = datetime.combine(now.date(), time(0), tzinfo=loc.tz).astimezone(timezone.utc)
    times = [start + timedelta(minutes=10 * i) for i in range(145)]
    alt, az = sun_altaz(times, loc.lat, loc.lon)
    return tuple((t.astimezone(loc.tz), float(a), float(z)) for t, a, z in zip(times, alt, az))


# --- daylight ------------------------------------------------------------------------------

# With the Sun further below the horizon than this it's night: the black sky above.
NIGHT_SUN_ALT = -15.0
# With it higher than this it's light out: what the `day_` settings cover.
DAY_SUN_ALT = -6.0
# Sun altitudes for the tone and sunset tables in `SkyStyle`; between them, linear.
SUN_ALTS = (-15.0, -12.0, -9.0, -6.0, -3.0, 0.0, 5.0, 15.0, 40.0)
# The inks a lit sky is dithered from, darkest first (`sky_shares` gives one share of each).
SKY_INKS = np.array([BLACK, BLUE, WHITE, YELLOW, RED], dtype=np.uint8)
# A sunset's colors, as shares of SKY_INKS, from weak to strong (`sky_shares`).
SUNSET_AT = (0.0, 0.35, 0.7, 1.0)
SUNSET_PALE = np.array([0.0, 0.0, 0.75, 0.25, 0.0])
SUNSET_PURPLE = np.array([0.0, 0.5, 0.0, 0.0, 0.5])
SUNSET_YELLOW = np.array([0.0, 0.0, 0.1, 0.85, 0.05])
SUNSET_ORANGE = np.array([0.0, 0.0, 0.0, 0.6, 0.4])


def _smooth(a: float, b: float, x: float) -> float:
    t = min(max((x - a) / (b - a), 0.0), 1.0)
    return t * t * (3 - 2 * t)


def lit_style(style: SkyStyle, sun_alt: float) -> SkyStyle:
    """The style for the Sun at this altitude. At night it's `style` itself, unchanged."""
    if sun_alt <= NIGHT_SUN_ALT:
        return style
    day = _smooth(-6.0, 3.0, sun_alt)  # 0 still dark, 1 full daylight, on the ground
    halo = BLACK if sun_alt < -2.0 else BLUE
    night_sand, day_sand = dict(style.sand_mix), dict(style.day_sand)
    sand = tuple((ink, (1 - day) * night_sand.get(ink, 0.0) + day * day_sand.get(ink, 0.0))
                 for ink in (*night_sand, *(i for i in day_sand if i not in night_sand)))
    changes: dict = {"sky": halo, "moon_dark": halo, "sand_mix": sand,
                     "milky_way": style.milky_way if sun_alt < -12.0 else None,
                     "mw_density": style.mw_density * (1 - (sun_alt - NIGHT_SUN_ALT) / 3.0)}
    if day >= 0.5:
        changes |= {"sand_text": BLACK, "sand_halo": WHITE}
    if sun_alt > -10.0:  # green is hard to read on the dots
        changes |= {"con_label": WHITE}
    if sun_alt > DAY_SUN_ALT:
        changes |= {"line": WHITE}
    if sun_alt > -2.0:
        changes |= {"star_label": style.day_label, "con_label": style.day_label, "label_halo": style.day_label_halo,
                    "planet_label_ink": style.day_label, "label_box": style.day_label_box}
    if sun_alt > -2.0:  # hazy by day: paler than the sea
        changes |= {"island_base": BLUE, "island_fill": WHITE, "island_fill_density": 0.3, "island_outline": BLUE}
    return replace(style, **changes)


def star_limit(sun_alt: float, limit: float, day_limit: float) -> float:
    """The faintest stars to draw: `limit` at night, `day_limit` from about when the first
    stars come out at dusk, and more of them as the sky darkens between. Never more by
    day than at night."""
    day = min(limit, day_limit)
    t = min(max((sun_alt - NIGHT_SUN_ALT) / (-4.0 - NIGHT_SUN_ALT), 0.0), 1.0)
    return limit + (day - limit) * t


def sky_shares(alt: np.ndarray, az: np.ndarray, sun_alt: float, sun_az: float, style: SkyStyle) -> np.ndarray:
    """How much of each of `SKY_INKS` a part of the sky gets: shape (..., 5), summing to
    1 for each direction.

    A tone from 0 (black) through 1 (solid blue) to 2 (white): darkest straight up,
    brighter toward the horizon and around the Sun, all by the Sun's altitude (the tables
    in `SkyStyle`). With the Sun low, a sunset along the horizon replaces that, strongest
    toward the Sun: going down, the sky passes through a pale band (a purple one once the
    Sun has set), then yellow, then orange. Blue never mixes directly with yellow, which
    would make grey-green mud.
    """
    a, sa = np.radians(np.maximum(alt, 0.0)), math.radians(sun_alt)
    daz = (az - sun_az + 180.0) % 360.0 - 180.0
    cos_g = np.sin(a) * math.sin(sa) + np.cos(a) * math.cos(sa) * np.cos(np.radians(daz))
    from_sun = np.degrees(np.arccos(np.clip(cos_g, -1.0, 1.0)))
    up = np.degrees(a)
    tone = (np.interp(sun_alt, SUN_ALTS, style.zenith_tone)
            + np.interp(sun_alt, SUN_ALTS, style.horizon_tone) * np.exp(-up / style.haze_deg)
            + np.interp(sun_alt, SUN_ALTS, style.glow_tone) * np.exp(-from_sun / style.glow_deg))
    black = np.clip(1.0 - tone, 0.0, 1.0)
    white = np.clip(tone - 1.0, 0.0, 1.0)
    zeros = np.zeros_like(tone)
    sky = np.stack((black, 1.0 - black - white, white, zeros, zeros), axis=-1)
    spread = 0.25 + 0.75 * np.exp(-(daz / style.warm_spread_deg) ** 2)
    warm = np.interp(sun_alt, SUN_ALTS, style.warm) * np.exp(-up / style.warm_band_deg) * spread
    set_ = _smooth(-5.0, 1.0, sun_alt)  # 0: the Sun is well down
    band = set_ * SUNSET_PALE + (1.0 - set_) * SUNSET_PURPLE
    out = np.interp(warm, SUNSET_AT, (1.0, 0.0, 0.0, 0.0))[..., None] * sky
    for i, stop in enumerate((band, SUNSET_YELLOW, SUNSET_ORANGE), start=1):
        out = out + np.interp(warm, SUNSET_AT, np.eye(4)[i])[..., None] * stop
    return out


def paint_sky(c: InkCanvas, proj: Stereographic, horizon_y: int, sun: Body, style: SkyStyle) -> None:
    """A lit sky in ordered dots (`sky_shares`). The shares are worked out every other
    pixel each way, like the Milky Way's, since they change slowly."""
    W = c.size[0]
    ys, xs = np.mgrid[0:horizon_y + 1:2, 0:W:2].astype(float) + 1.0
    alt, az = proj.unproject(xs, ys)
    shares = sky_shares(alt, az, sun.alt, sun.az, style)
    cum = np.cumsum(np.repeat(np.repeat(shares, 2, axis=0), 2, axis=1)[:horizon_y + 1, :W], axis=-1)
    iy, ix = np.mgrid[0:horizon_y + 1, 0:W]
    which = np.minimum((BAYER8[iy % 8, ix % 8][..., None] >= cum).sum(axis=-1), len(SKY_INKS) - 1)
    c.image.paste(Image.frombytes("P", (W, horizon_y + 1), SKY_INKS[which].tobytes()), (0, 0))


def draw_sun(c: InkCanvas, x: float, y: float, sun_alt: float, style: SkyStyle) -> None:
    """The Sun: a disc with rays, yellow by day, orange and then red-orange when it's low."""
    R = style.sun_radius
    at_x, at_y = np.array([x]), np.array([y])
    color = "yellow" if sun_alt >= 3.0 else "orange" if sun_alt >= 0.5 else "red-orange"
    if style.sun_rays:
        for i in range(12):
            a = math.radians(i * 30.0)
            r0, r1 = R + 6, R + (15 if i % 2 == 0 else 11)
            ray = [(x + r0 * math.sin(a), y - r0 * math.cos(a)), (x + r1 * math.sin(a), y - r1 * math.cos(a))]
            c.draw.line(ray, fill=BLUE, width=5)  # so yellow rays show on a pale sky
            c.draw.line(ray, fill=YELLOW if sun_alt >= 2.0 else RED, width=3)
    if style.sun_ring is not None:
        stamp(c, at_x, at_y, 2 * R + 5, style.sun_ring)
    stamp_colored(c, x, y, 2 * R + 1, color)


def sun_icon(c: InkCanvas, x: float, y: float, r: int, ink: int, gap: int = 3, ray: int = 5) -> None:
    """A small Sun for the corners: a disc and eight rays `ray` px long, `gap` px clear of it."""
    for i in range(8):
        a = math.radians(i * 45.0)
        c.draw.line([(x + (r + gap) * math.sin(a), y - (r + gap) * math.cos(a)),
                     (x + (r + gap + ray) * math.sin(a), y - (r + gap + ray) * math.cos(a))], fill=ink, width=2)
    stamp(c, np.array([x]), np.array([y]), 2 * r + 1, ink)


def _track_points(proj: Stereographic, m: SkyModel, width: int):
    """Today's Sun while it's up: its times, then its screen x and y and whether they're near the view."""
    up = [(t, a, z) for t, a, z in m.sun_track if a > 0.0]
    x, y, ok = proj.project(np.array([p[1] for p in up]), np.array([p[2] for p in up]))
    return up, x, y, ok & (x > -30) & (x < width + 30) & (y > -30)


def draw_sun_track(c: InkCanvas, proj: Stereographic, m: SkyModel, width: int) -> None:
    """Today's path of the Sun: a dashed arc with a dot at each hour."""
    up, x, y, seen = _track_points(proj, m, width)
    for i in range(len(up) - 1):
        if seen[i] and seen[i + 1] and i % 2 == 0:
            c.draw.line([(x[i], y[i]), (x[i + 1], y[i + 1])], fill=BLUE, width=4)
    for i in range(len(up) - 1):
        if seen[i] and seen[i + 1] and i % 2 == 0:
            c.draw.line([(x[i], y[i]), (x[i + 1], y[i + 1])], fill=YELLOW, width=2)
    for i, (t, a, _) in enumerate(up):
        if t.minute == 0 and seen[i] and a >= 3.0:
            stamp(c, x[[i]], y[[i]], 9, BLUE)
            stamp(c, x[[i]], y[[i]], 5, YELLOW)


def sun_track_hours(c: InkCanvas, proj: Stereographic, placer: Placer, m: SkyModel, style: SkyStyle) -> None:
    """The hours along the Sun's path: small numbers above its dots, where there's room."""
    up, x, y, seen = _track_points(proj, m, c.size[0])
    for i, (t, a, _) in enumerate(up):
        if t.minute != 0 or not seen[i] or a < 3.0:
            continue
        name, spot = str(t.hour % 12 or 12), (float(x[i]), float(y[i]) - 7)
        b = text_box(c, spot, name, style.hour_size, "mb", True)
        if placer.free(b):
            placer.take(b)
            name_label(c, style, spot, name, style.hour_size, style.star_label, "mb", True)


# --- drawing helpers -----------------------------------------------------------------------


Box = tuple[float, float, float, float]


class Placer:
    """Keeps labels from overlapping each other and the things they label."""

    def __init__(self, bounds: Box) -> None:
        self.bounds = bounds
        self.taken: list[Box] = []

    def free(self, b: Box) -> bool:
        x0, y0, x1, y1 = self.bounds
        if b[0] < x0 or b[1] < y0 or b[2] > x1 or b[3] > y1:
            return False
        return not any(b[0] < t[2] and t[0] < b[2] and b[1] < t[3] and t[1] < b[3] for t in self.taken)

    def take(self, b: Box) -> None:
        self.taken.append(b)


def text_box(c: InkCanvas, xy: tuple[float, float], s: str, size: int, anchor: str,
             bold: bool = False, pad: int = 2) -> Box:
    l, t, r, b = c.draw.textbbox((round(xy[0]), round(xy[1])), s, font=font(size, bold), anchor=anchor)
    return (l - pad, t - pad, r + pad, b + pad)


def label(c: InkCanvas, xy: tuple[float, float], s: str, size: int, ink: int, bg: int | None,
          anchor: str = "lm", bold: bool = False) -> None:
    """Text with a 2-pixel halo of `bg` around each letter, so lines and the Milky Way's
    dots stop short of it."""
    xy = (round(xy[0]), round(xy[1]))
    if bg is not None:
        c.draw.text(xy, s, font=font(size, bold), fill=bg, anchor=anchor, stroke_width=2, stroke_fill=bg)
    c.draw.text(xy, s, font=font(size, bold), fill=ink, anchor=anchor)


def name_label(c: InkCanvas, style: SkyStyle, xy: tuple[float, float], s: str, size: int, ink: int,
               anchor: str = "lm", bold: bool = False) -> None:
    """A name over the sky, on a box of the halo's ink or with the halo around its letters."""
    if style.label_box:
        c.draw.rectangle(text_box(c, xy, s, size, anchor, bold), fill=style.halo)
    label(c, xy, s, size, ink, None if style.label_box else style.halo, anchor, bold)


def place_label(c: InkCanvas, placer: Placer, xy: tuple[float, float], gap: float, s: str, size: int,
                bold: bool = False) -> tuple[tuple[float, float], str] | None:
    """Try right, left, above, below and the diagonals of a point, near then farther;
    returns (xy, anchor) or None."""
    x, y = xy
    for g in (gap, gap + 12):
        d = g * 0.75
        for pos, anchor in (((x + g, y), "lm"), ((x - g, y), "rm"), ((x, y - g), "mb"), ((x, y + g), "mt"),
                            ((x + d, y - d), "lb"), ((x + d, y + d), "lt"), ((x - d, y - d), "rb"),
                            ((x - d, y + d), "rt")):
            b = text_box(c, pos, s, size, anchor, bold)
            if placer.free(b):
                placer.take(b)
                return pos, anchor
    return None


def star_diameter(mag: np.ndarray, style: SkyStyle, limit: float) -> np.ndarray:
    """Pixel diameter by magnitude: `star_min_d` at the magnitude limit, growing by
    `star_d_per_mag` for each magnitude brighter, up to `star_max_d`."""
    d = style.star_min_d + style.star_d_per_mag * (limit - mag)
    return np.clip(np.round(d), style.star_min_d, style.star_max_d).astype(int)


ZODIAC = frozenset({"Ari", "Tau", "Gem", "Cnc", "Leo", "Vir", "Lib", "Sco", "Sgr", "Cap", "Aqr", "Psc"})
MAIN_BRIGHTEST = 3.0  # "main" constellations have a star at least this bright (or are zodiacal)


def shown_constellations(m: SkyModel, which: str) -> list[Constellation]:
    if which == "all":
        return list(m.constellations)
    cat = m.catalog
    out = []
    for con in m.constellations:
        mags = [cat.mag[i] for pair in con.lines for h in pair if (i := cat.rows.get(h)) is not None]
        if con.abbr in ZODIAC or (mags and min(mags) <= MAIN_BRIGHTEST):
            out.append(con)
    return out


def disc_offsets(d: int) -> np.ndarray:
    """(n, 2) pixel offsets of a filled disc of diameter d around its center pixel."""
    if d <= 1:
        return np.zeros((1, 2), dtype=int)
    c = (d - 1) / 2
    yy, xx = np.mgrid[0:d, 0:d]
    keep = (xx - c) ** 2 + (yy - c) ** 2 <= (d / 2) ** 2 - 0.3
    off = np.column_stack((xx[keep], yy[keep])) - int(c)
    return off


def star_color(bv: float, scheme: str) -> str:
    """"blue-white", "white", "yellow", "orange", "red-orange" or "red" for a B−V color
    index.

    "solid" uses the panel's inks only: white, yellow, red. "dither" adds orange (red and
    yellow dots half and half) for K giants like Arcturus and Aldebaran, red-orange (three
    red dots to one yellow) for M giants like Betelgeuse and Antares rather than the
    panel's deep red, and blue-white (a white star in a blue ring) for the hot B stars such
    as Rigel, Spica and Regulus; blue ink alone would make them look faint on black."""
    if scheme == "off" or not math.isfinite(bv):
        return "white"
    if scheme == "solid":
        return "white" if bv < 0.45 else "yellow" if bv < 1.3 else "red"
    if bv < 0.0:
        return "blue-white"
    if bv < 0.45:
        return "white"
    if bv < 1.0:
        return "yellow"
    return "orange" if bv < 1.6 else "red-orange"


def stamp_colored(c: InkCanvas, x: float, y: float, d: int, color: str) -> None:
    """One star of diameter d in `color` (see `star_color`); dithered colors use a fixed
    screen pattern, so neighboring stars match."""
    off = disc_offsets(d) + np.array([int(round(x)), int(round(y))])
    px, py = off[:, 0], off[:, 1]
    if color == "blue-white":
        if d >= 4:
            ring = disc_offsets(d + 2) + np.array([int(round(x)), int(round(y))])
            c.draw.point(ring.ravel().tolist(), fill=BLUE)
        c.draw.point(off.ravel().tolist(), fill=WHITE)
        return
    if color == "orange":
        yellow = (px + py) % 2 == 0
    elif color == "red-orange":
        yellow = (px % 2 == 0) & (py % 2 == 0)
    else:
        ink = {"white": WHITE, "yellow": YELLOW, "red": RED}[color]
        c.draw.point(off.ravel().tolist(), fill=ink)
        return
    if d <= 3:  # too small to dither: the nearer solid ink
        c.draw.point(off.ravel().tolist(), fill=YELLOW if color == "orange" else RED)
        return
    c.draw.point(off[yellow].ravel().tolist(), fill=YELLOW)
    c.draw.point(off[~yellow].ravel().tolist(), fill=RED)


def _bayer(n: int) -> np.ndarray:
    """n × n ordered-dither thresholds, evenly spread in (0, 1) (as the day screen's)."""
    m = np.zeros((1, 1))
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


BAYER8 = _bayer(8)
# Fixed "random" thresholds for stippling: the same dots every night, so only the sky moves.
STIPPLE = np.random.default_rng(20261104).random((64, 64))


def milky_way(c: InkCanvas, m: SkyModel, proj: Stereographic, horizon_y: int, style: SkyStyle,
              grid: np.ndarray) -> None:
    """The Milky Way as ordered dots: denser where it's brighter. Its brightness is worked
    out every other pixel each way (it changes slowly; a quarter of the work on the Pi),
    the dots for every pixel."""
    from photopainter.astro.sky import galactic, milky_way_at

    W = c.size[0]
    ys, xs = np.mgrid[0:horizon_y:2, 0:W:2].astype(float) + 1.0  # centers of 2 × 2 blocks
    alt, az = proj.unproject(xs, ys)
    l, b = galactic(m.now, m.view.lat, m.view.lon, alt, az)
    s = milky_way_at(grid, l, b)
    t = np.clip((s - style.mw_from) / (style.mw_to - style.mw_from), 0.0, 1.0)
    density = np.repeat(np.repeat(t ** style.mw_gamma * style.mw_density, 2, axis=0), 2, axis=1)
    density = density[:horizon_y, :W]
    iy, ix = np.mgrid[0:horizon_y, 0:W]
    thresholds = STIPPLE[iy % 64, ix % 64] if style.mw_pattern == "stipple" else BAYER8[iy % 8, ix % 8]
    mask = thresholds < density
    if mask.any():
        yy, xx = np.nonzero(mask)
        c.draw.point(np.column_stack((xx, yy)).ravel().tolist(), fill=style.milky_way)


def stamp(c: InkCanvas, xs: np.ndarray, ys: np.ndarray, d: int, ink: int) -> None:
    if len(xs) == 0:
        return
    off = disc_offsets(d)
    pts = (np.column_stack((np.round(xs), np.round(ys))).astype(int)[:, None, :] + off[None, :, :]).reshape(-1, 2)
    c.draw.point(pts.ravel().tolist(), fill=ink)


def phase_disc(c: InkCanvas, cx: float, cy: float, r: float, illuminated: float, limb_screen_deg: float,
               lit: int, dark: int | None) -> None:
    """The Moon: lit side toward `limb_screen_deg` (screen angle, clockwise from up)."""
    n = int(math.ceil(r)) + 1
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1].astype(float)
    inside = xx ** 2 + yy ** 2 <= r ** 2
    a = math.radians(limb_screen_deg)
    ux, uy = math.sin(a), -math.cos(a)  # toward the bright limb, screen coordinates
    u = xx * ux + yy * uy
    v = -xx * uy + yy * ux
    half = np.sqrt(np.clip(r ** 2 - v ** 2, 0, None))
    lit_mask = inside & (u >= -(2 * illuminated - 1) * half)
    x0, y0 = int(round(cx)) - n, int(round(cy)) - n
    for mask, ink in ((inside & ~lit_mask, dark), (lit_mask, lit)):
        if ink is None or not mask.any():
            continue
        ys, xs = np.nonzero(mask)
        c.draw.point(np.column_stack((xs + x0, ys + y0)).ravel().tolist(), fill=ink)


def eclipsed_moon(c: InkCanvas, cx: float, cy: float, r: float, e: LunarEclipse, toward_shadow_deg: float,
                  lit: int = WHITE) -> None:
    """The Moon in Earth's shadow, whose center lies `toward_shadow_deg` (screen angle, clockwise
    from up), to scale with the disc. Each pixel by where it is in the shadow, in ordered dots:
    in the penumbra `lit` with black, more toward the umbra as less of the Sun reaches it; in
    the umbra red, coppery with yellow along the edge, where sunlight bends in through Earth's
    air, and darkening with black toward the center."""
    n = int(math.ceil(r)) + 1
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1].astype(float)
    a = math.radians(toward_shadow_deg)
    away = np.hypot(xx - e.distance * r * math.sin(a), yy + e.distance * r * math.cos(a)) / r  # in Moon radii
    x0, y0 = int(round(cx)) - n, int(round(cy)) - n
    dots = BAYER8[(np.arange(2 * n + 1) + y0)[:, None] % 8, (np.arange(2 * n + 1) + x0)[None, :] % 8]

    hidden = np.clip((e.penumbra - away) / (e.penumbra - e.umbra), 0.0, 1.0)  # how much of the Sun
    ink = np.where(dots < 0.8 * hidden ** 3, BLACK, lit)
    depth = e.umbra - away  # in Moon radii, from the umbra's edge
    in_umbra = np.where(dots < 0.5 * np.clip(1.0 - depth / 0.6, 0.0, 1.0), YELLOW,
                        np.where(dots > 1.0 - 0.5 * np.clip((depth - 1.1) / 1.2, 0.0, 1.0), BLACK, RED))
    ink = np.where(depth > 0, in_umbra, ink)
    inside = xx ** 2 + yy ** 2 <= r ** 2
    for which in np.unique(ink[inside]).tolist():
        ys, xs = np.nonzero(inside & (ink == which))
        c.draw.point(np.column_stack((xs + x0, ys + y0)).ravel().tolist(), fill=which)



def moon_across_sun(c: InkCanvas, x: float, y: float, d: int, e: SolarEclipse, toward_moon_deg: float) -> None:
    """Black where the Moon is in front of the Sun, a disc of diameter `d` at (x, y), to scale with
    it. Off the Sun the Moon can't be seen, so nothing is drawn there."""
    off = disc_offsets(d)
    r = d / 2
    a = math.radians(toward_moon_deg)
    covered = np.hypot(off[:, 0] - e.distance * r * math.sin(a), off[:, 1] + e.distance * r * math.cos(a)) <= e.moon_size * r
    c.draw.point((off[covered] + np.array([int(round(x)), int(round(y))])).ravel().tolist(), fill=BLACK)


def screen_bearing(proj: Stereographic, alt: float, az: float, bearing_deg: float) -> float:
    """Which way `bearing_deg` on the sky at (alt, az) points on screen, clockwise from up:
    project a point 1° that way."""
    x, y, _ = proj.point(alt, az)
    x2, y2, _ = proj.point(*offset(alt, az, bearing_deg, 1.0))
    return math.degrees(math.atan2(x2 - x, -(y2 - y))) % 360


def clock(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _unit(alt: np.ndarray, az: np.ndarray) -> np.ndarray:
    a, z = np.radians(alt), np.radians(az)
    return np.stack((np.cos(a) * np.cos(z), np.cos(a) * np.sin(z), np.sin(a)), axis=-1)


def _altaz(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    return np.degrees(np.arcsin(np.clip(v[..., 2], -1, 1))), np.degrees(np.arctan2(v[..., 1], v[..., 0])) % 360


# --- the screen ----------------------------------------------------------------------------


def draw(c: InkCanvas, m: SkyModel, config: Config, style: SkyStyle = SkyStyle()) -> None:
    W, H = c.size
    sky_cfg = config.sky
    horizon_y = H - style.footer_h
    proj = Stereographic.fit(sky_cfg.center_azimuth_deg, sky_cfg.field_of_view_deg,
                             sky_cfg.max_altitude_deg, W, horizon_y)
    v, cat = m.view, m.catalog
    lit = v.sun.alt > NIGHT_SUN_ALT  # a sky that isn't black
    style = lit_style(style, v.sun.alt)
    if lit:
        paint_sky(c, proj, horizon_y, v.sun, style)
    else:
        c.draw.rectangle([0, 0, W, horizon_y], fill=style.sky)
    if style.milky_way is not None and m.milky_way is not None:
        milky_way(c, m, proj, horizon_y, style, m.milky_way)

    sx, sy, sok = proj.project(v.star_alt, v.star_az)
    on = sok & (v.star_alt > -1) & (sx > -20) & (sx < W + 20) & (sy > -20) & (sy < horizon_y + 20)
    limit = sky_cfg.magnitude_limit
    faintest = star_limit(v.sun.alt, limit, sky_cfg.day_magnitude_limit)
    show_lines = sky_cfg.constellation_lines and (sky_cfg.day_constellation_lines or v.sun.alt <= DAY_SUN_ALT)
    diam = star_diameter(cat.mag, style, limit)
    placer = Placer((0, 0, W, horizon_y - 2))
    for reserved in style.reserved:
        placer.take(reserved)
    island = None
    if style.island and m.skyline is not None:
        island = skyline_polygon(m.skyline, proj, style.island_exaggeration)
        xs_ = [p[0] for p in island]
        if max(xs_) < 0 or min(xs_) > W:
            island = None
        else:
            placer.take((min(xs_), min(p[1] for p in island) - 2, max(xs_), horizon_y))

    # The corners: reserve them first so that no name is placed under them.
    daytime = v.sun.alt > 0
    if m.weather is not None:
        x0, y0, x1, y1 = weather_block(c, m.weather, daytime, style, paint_it=False)
        placer.take((x0, y0, x1 + CORNER_GAP, y1 + CORNER_GAP))
    mv: MoonView = v.moon
    notes, notes_box = note_rows(m, W) if style.notes else ([], None)
    if notes_box is not None:
        placer.take((notes_box[0] - CORNER_GAP, 0, W, notes_box[3] + CORNER_GAP))

    track_on = v.sun.alt > -1.0 and sky_cfg.sun_arc
    if track_on:
        draw_sun_track(c, proj, m, W)

    # Constellation lines, each segment along its great circle.
    line_stars: set[int] = set()
    if show_lines:
        for con in shown_constellations(m, style.constellations):
            for a, b in con.lines:
                ia, ib = cat.rows.get(a), cat.rows.get(b)
                if ia is None or ib is None or not (sok[ia] and sok[ib]):
                    continue
                if v.star_alt[ia] < -2 and v.star_alt[ib] < -2:
                    continue
                if not (on[ia] or on[ib]):
                    continue
                pa, pb = _unit(v.star_alt[ia], v.star_az[ia]), _unit(v.star_alt[ib], v.star_az[ib])
                n = max(1, int(math.degrees(math.acos(float(np.clip(pa @ pb, -1, 1)))) // 4))
                ts = np.linspace(0, 1, n + 1)[:, None]
                alt, az = _altaz(pa[None, :] * (1 - ts) + pb[None, :] * ts)
                x, y_, _ = proj.project(alt, az)
                c.draw.line(list(zip(x.tolist(), y_.tolist())), fill=style.line, width=style.line_width)
                line_stars.update((ia, ib))

    # Stars: faint first; line stars get a ring of sky so lines stop short of them, and on
    # a lit sky every star does, to show up against the dots. A line's fainter stars are
    # for the night; as the sky lightens, they fade out with the rest.
    wanted = cat.mag <= faintest
    if not lit:
        wanted = wanted | np.isin(np.arange(len(cat)), list(line_stars))
    visible = np.nonzero(on & wanted)[0]
    visible = visible[np.argsort(-cat.mag[visible])]
    for d in sorted(set(diam[visible].tolist())):
        idx = visible[diam[visible] == d]
        ringed = idx if lit else idx[np.isin(idx, list(line_stars))]
        stamp(c, sx[ringed], sy[ringed], d + 4, style.sky)
    colors = {int(i): star_color(float(cat.bv[i]), style.star_colors) for i in visible
              if cat.mag[i] <= style.color_mag}
    for d in sorted(set(diam[visible].tolist())):
        idx = visible[diam[visible] == d]
        white = np.array([i for i in idx if colors.get(int(i), "white") == "white"], dtype=int)
        stamp(c, sx[white], sy[white], d, style.star)
        for i in idx:
            if colors.get(int(i), "white") != "white":
                stamp_colored(c, sx[i], sy[i], d, colors[int(i)])
        for i in idx:
            r = d / 2 + 1
            placer.take((sx[i] - r, sy[i] - r, sx[i] + r, sy[i] + r)) if d >= 4 else None

    # Where the Sun is, if it's drawn; planets too close to it are lost in its glare.
    sun_x, sun_y, _ = proj.point(v.sun.alt, v.sun.az)
    sun_up = disc_on_screen(proj, v.sun, W, horizon_y, style.sun_radius)

    # Planets.
    planets_drawn: list[tuple[Body, float, float]] = []
    for p in v.planets:
        x, y_, ok = proj.point(p.alt, p.az)
        if not ok or p.alt < 0 or not (0 <= x < W and 0 <= y_ < horizon_y):
            continue
        if sun_up and math.hypot(x - sun_x, y_ - sun_y) < style.sun_radius + 20:
            continue
        ink = style.planet_inks.get(p.name, YELLOW)
        r = style.planet_radius
        stamp(c, np.array([x]), np.array([y_]), 2 * r + 5, style.sky)
        stamp(c, np.array([x]), np.array([y_]), 2 * r + 1, ink)
        if p.name == "Saturn":
            c.draw.ellipse([x - r * 2, y_ - r * 0.7, x + r * 2, y_ + r * 0.7], outline=ink, width=2)
            stamp(c, np.array([x]), np.array([y_]), 2 * r + 1, ink)
        placer.take((x - 2 * r, y_ - r - 2, x + 2 * r, y_ + r + 2))
        planets_drawn.append((p, x, y_))

    solar = m.eclipse if isinstance(m.eclipse, SolarEclipse) and m.eclipse.stage else None
    lunar = m.eclipse if isinstance(m.eclipse, LunarEclipse) and m.eclipse.stage else None

    # The Sun.
    if sun_up:
        draw_sun(c, sun_x, sun_y, v.sun.alt, style)
        if solar is not None:
            moon_across_sun(c, sun_x, sun_y, 2 * style.sun_radius + 1, solar,
                            screen_bearing(proj, v.sun.alt, v.sun.az, solar.bearing_deg))
        reach = style.sun_radius + (15 if style.sun_rays else 3)
        placer.take((sun_x - reach, sun_y - reach, sun_x + reach, sun_y + reach))

    # The Moon.
    mx, my, mok = proj.point(mv.alt, mv.az)
    moon_up = disc_on_screen(proj, mv, W, horizon_y, style.moon_radius)
    if moon_up and sun_up and moon_lost_in_sun(mx, my, sun_x, sun_y, style):
        moon_up = False  # new, right beside the Sun: not to be seen, and not to pass for an eclipse
    if moon_up:
        R = style.moon_radius
        stamp(c, np.array([mx]), np.array([my]), 2 * R + 7, style.sky)
        if style.moon_outline is not None:
            stamp(c, np.array([mx]), np.array([my]), 2 * R + 3, style.moon_outline)
        if lunar is not None:
            eclipsed_moon(c, mx, my, R, lunar, screen_bearing(proj, mv.alt, mv.az, lunar.bearing_deg), style.moon_lit)
        else:
            illuminated = mv.phase.illuminated if mv.phase else 0.5
            phase_disc(c, mx, my, R, illuminated, screen_bearing(proj, mv.alt, mv.az, mv.bright_limb_deg), style.moon_lit,
                       style.moon_dark if style.moon_dark is not None else style.sky)
        placer.take((mx - R - 3, my - R - 3, mx + R + 3, my + R + 3))

    if island is not None:
        draw_skyline(c, island, style)

    if sky_cfg.labels:
        if track_on:
            sun_track_hours(c, proj, placer, m, style)
        # Planet names first, then Catalina, bright stars and constellations.
        for p, x, y_ in planets_drawn:
            ink = style.planet_label_ink if style.planet_label_ink is not None else style.planet_inks.get(p.name, YELLOW)
            gap = style.planet_radius * (2.4 if p.name == "Saturn" else 1) + 5
            spot = place_label(c, placer, (x, y_), gap, p.name, style.planet_label_size, True)
            if spot:
                name_label(c, style, spot[0], p.name, style.planet_label_size, ink, spot[1], True)
        if island is not None and style.island_label:
            top = min(island, key=lambda p: p[1])
            name = "CATALINA"
            for dx, dy in ((0, 0), (-40, 0), (40, 0), (0, -14), (-40, -14), (40, -14)):
                spot = (top[0] + dx, top[1] - 4 + dy)
                b = text_box(c, spot, name, style.con_label_size, "mb")
                if placer.free(b):
                    placer.take(b)
                    name_label(c, style, spot, name, style.con_label_size, style.con_label, "mb")
                    break
        named = [i for i in visible if cat.names[i] and cat.mag[i] <= style.star_label_mag]
        for i in sorted(named, key=lambda i: cat.mag[i]):
            spot = place_label(c, placer, (sx[i], sy[i]), diam[i] / 2 + 4, cat.names[i], style.star_label_size)
            if spot:
                name_label(c, style, spot[0], cat.names[i], style.star_label_size, style.star_label, spot[1])
        if show_lines and style.con_names:
            _constellation_names(c, m, placer, sx, sy, on, horizon_y, style)

    foreground(c, proj, horizon_y, style, m)

    # Planets and the Moon that are up but outside the view: their names at the edge.
    if style.off_view and sky_cfg.labels:
        _off_view(c, proj, m, placer, horizon_y, style, moon_up)

    # Top right: when the Sun and the Moon next rise or set, and the time.
    if notes_box is not None:
        corner_panel(c, notes_box, style.corner_box)
    for what, s, y_mid in notes:
        f = font(SMALL_NOTE if what == "time" else CORNER_TEXT, True)
        x1, r = W - CORNER_X, 9
        cx = x1 - text_width(s, f) - 9 - r
        c.draw.text((x1, y_mid), s, font=f, fill=style.text, anchor="rm")
        if what == "sun":
            sun_icon(c, cx, y_mid, 5, YELLOW)
        elif what == "eclipse":
            eclipse_icon(c, cx, y_mid, r, m.eclipse, style.text, style.corner_box)
        elif what == "moon":
            disc_x, disc_y = np.array([cx]), np.array([y_mid])
            stamp(c, disc_x, disc_y, 2 * r + 5, style.text)
            stamp(c, disc_x, disc_y, 2 * r + 1, style.corner_box)
            phase_disc(c, cx, y_mid, r, mv.phase.illuminated,
                       90.0 if mv.phase.waxing else 270.0, style.moon_lit, None)

    if m.weather is not None:
        weather_block(c, m.weather, daytime, style)


# --- the corners: the weather and the notes ------------------------------------------------

CORNER_X = 12  # from the screen's edge to the text
CORNER_PAD = 14  # from the text to a box's inner edge
CORNER_RADIUS = 12  # of a box's inner corner
CORNER_GAP = 8  # keeps names off a box
CORNER_TEXT = 18  # bold, like all the text here
SMALL_NOTE = 15  # "Sky at 9:30 pm" and "as of 3:10 pm"
WEATHER_TEMP = 36  # the temperature's size
WEATHER_ICON = 40
NOTE_ROW = 27


def corner_panel(c: InkCanvas, box: Box, ink: int, radius: int = CORNER_RADIUS) -> None:
    """A corner's solid box, flush with the screen's edges: only the corner that faces the
    middle of the screen is rounded."""
    x0, _, x1, _ = box
    c.draw.rounded_rectangle(box, radius=radius, fill=ink, corners=(False, False, x1 < c.size[0], x0 > 0))


def note_rows(m: SkyModel, width: int) -> tuple[list[tuple[str, str, int]], Box]:
    """Top right: an eclipse if there's one to mention, when the Sun next rises or sets and
    when the Moon rises (if it's down), soonest first, then the time the sky is drawn for.
    Returns the rows, as ("eclipse" | "eclipse_detail" | "sun" | "moon" | "time", text, row's
    middle), and the box they take."""
    rows, widest, y = [], 0, 20
    for what, s, _ in top_notes(m):
        rows.append((what, s, y))
        widest = max(widest, text_width(s, font(CORNER_TEXT, True)) + 31)  # room for the icon
        y += NOTE_ROW
    s = f"Sky at {clock(m.now)}"
    rows.append(("time", s, y - 3))
    widest = max(widest, text_width(s, font(SMALL_NOTE, True)))
    return rows, (width - CORNER_X - widest - CORNER_PAD, 0, width, y + 18)


def eclipse_notes(e: Eclipse, now: datetime) -> list[str]:
    """What to say about an eclipse at `now`, in two rows: its name, by how deep it gets
    anywhere (a total eclipse is called total though it's only partial here; a penumbral one
    is "faint", since the eye can hardly see it), then what's seen here and when. Nothing once
    it's over: over here if it's seen here, otherwise everywhere."""
    if now >= e.ends:
        return []
    body = "lunar" if isinstance(e, LunarEclipse) else "solar"
    name = f"{'Faint' if e.kind == 'penumbral' else e.kind.capitalize()} {body} eclipse"
    if not e.seen:
        return [name, f"{clock(e.greatest)}, not seen here"]
    here = e.here if isinstance(e, SolarEclipse) else e.kind
    if here == "partial" and e.kind != "partial":
        return [name, f"Partial here from {clock(e.begins)}" if now < e.begins else f"Partial here until {clock(e.ends)}"]
    deep = "Annular" if here == "annular" else "Total"
    if now < e.begins:
        return [name, f"Begins {clock(e.begins)}"]
    if e.total_begins is not None and now < e.total_begins:
        return [name, f"{deep} at {clock(e.total_begins)}"]
    if e.total_ends is not None and now < e.total_ends:
        return [name, f"{deep} until {clock(e.total_ends)}"]
    return [name, f"Ends {clock(e.ends)}"]


def eclipse_icon(c: InkCanvas, x: float, y: float, r: int, e: Eclipse, ring: int, box: int) -> None:
    """A small eclipse for the corners, like the day screen's: a red Moon, or a black Moon
    in a yellow ring."""
    at_x, at_y = np.array([x]), np.array([y])
    if isinstance(e, LunarEclipse):
        stamp(c, at_x, at_y, 2 * r + 5, ring)
        stamp(c, at_x, at_y, 2 * r + 1, box)
        stamp(c, at_x, at_y, 2 * r + 1, RED)
    else:
        stamp(c, at_x, at_y, 2 * r + 3, YELLOW)
        stamp(c, at_x, at_y, 2 * r - 3, BLACK)


def top_notes(m: SkyModel) -> list[tuple[str, str, datetime]]:
    """What goes top right: an eclipse first, if there's one to mention ("eclipse", its name,
    and "eclipse_detail", what's seen here), then soonest first ("sun" | "moon", text, when).
    The Sun's next rise or set, all day and all night; the Moon's rise while it's down."""
    first = []
    if m.eclipse is not None:
        first = list(zip(("eclipse", "eclipse_detail"), eclipse_notes(m.eclipse, m.now), (m.eclipse.greatest,) * 2))
    notes = []
    if m.sun_next is not None:
        notes.append(("sun", f"Sun {m.sun_next[0]} {clock(m.sun_next[1])}", m.sun_next[1]))
    mv = m.view.moon
    if mv.alt <= 0 and m.moon_next is not None and mv.phase is not None:
        notes.append(("moon", f"Moon {m.moon_next[0]} {clock(m.moon_next[1])}", m.moon_next[1]))
    return first + sorted(notes, key=lambda n: n[2])


def wind_words(w: Weather) -> str:
    """'NW 5–11 mph': the wind and, when they're higher, the gusts."""
    speed = f"{round(w.wind)}"
    if w.gust is not None and round(w.gust) > round(w.wind):
        speed += f"–{round(w.gust)}"
    return f"{compass(w.wind_dir)} {speed} mph"


def weather_block(c: InkCanvas, w: Weather, daytime: bool, style: SkyStyle, paint_it: bool = True) -> Box:
    """The weather in the top left corner: the icon, temperature and condition on one row,
    then the high and low, the wind (its arrow points the way it blows, as seen from the
    window) and "as of" when the data is old. Returns the box it takes; with `paint_it`
    false it only measures, so the box can be reserved before the sky is drawn."""
    words, kind = condition(w.code, daytime)
    x, y = CORNER_X, 30  # the first row's middle
    f_big, f_text = font(WEATHER_TEMP, True), font(CORNER_TEXT, True)
    temp = f"{round(w.temp)}°"
    rows: list[tuple[str, int, bool]] = [(f"High {deg(w.high)}  Low {deg(w.low)}", CORNER_TEXT, False)]
    if w.wind is not None:
        rows.append((wind_words(w), CORNER_TEXT, w.wind_dir is not None))  # (text, size, arrow after it)
    if w.stale:
        rows.append((f"as of {clock(w.fetched_at)}", SMALL_NOTE, False))
    width = max([WEATHER_ICON + 8 + text_width(temp, f_big) + 10 + text_width(words, f_text),
                 *(text_width(t, font(n, True)) + (30 if arrow else 0) for t, n, arrow in rows)])
    box = (0, 0, x + width + CORNER_PAD, y + 34 + 25 * (len(rows) - 1) + 15)
    if not paint_it:
        return box
    ink = style.text
    corner_panel(c, box, style.corner_box)
    sky_weather_icon(c, x + WEATHER_ICON / 2, y + 1, WEATHER_ICON, kind, ink)
    xx = x + WEATHER_ICON + 8
    c.draw.text((xx, y), temp, font=f_big, fill=ink, anchor="lm")
    c.draw.text((xx + text_width(temp, f_big) + 10, y + 4), words, font=f_text, fill=ink, anchor="lm")
    yy = y + 34
    for t, n, arrow in rows:
        c.draw.text((x, yy), t, font=font(n, True), fill=ink, anchor="lm")
        if arrow:
            flow_arrow(c, x + text_width(t, font(n, True)) + 16, yy, 18, w.wind_dir, flip=True, fill=ink)
        yy += 25
    return box


def sky_weather_icon(c: InkCanvas, cx: float, cy: float, s: float, kind: str, ink: int) -> None:
    """The weather's icon for `kind`, about `s` px across, for a corner's blue box. The day
    screen's icons, with two changes: the Sun's rays are yellow, since black ones would be
    lost on the blue, and at night there are stars instead of the crescent, which would
    pass for the real Moon here."""
    gap, ray = round(0.075 * s), round(0.125 * s)
    if kind == "sun":
        sun_icon(c, cx, cy, round(0.26 * s), YELLOW, gap, ray)
    elif kind == "sun_cloud":
        sun_icon(c, cx - 0.16 * s, cy - 0.14 * s, round(0.2 * s), YELLOW, gap, ray)
        weather_icon(c, cx + 0.08 * s, cy + 0.08 * s, s * 0.8, "cloud")
    elif kind == "moon":
        stars_icon(c, cx, cy, s, ink)
    elif kind == "moon_cloud":
        stars_icon(c, cx - 0.2 * s, cy - 0.2 * s, s * 0.55, ink)
        weather_icon(c, cx + 0.08 * s, cy + 0.1 * s, s * 0.8, "cloud")
    else:
        weather_icon(c, cx, cy, s, kind, on_dark=True)


def stars_icon(c: InkCanvas, cx: float, cy: float, s: float, ink: int) -> None:
    """Three four-pointed stars, about `s` px across."""
    for dx, dy, r in ((-0.22, 0.12, 0.30), (0.2, -0.2, 0.19), (0.26, 0.26, 0.12)):
        x, y, r = cx + dx * s, cy + dy * s, r * s
        c.draw.polygon([(x, y - r), (x + r * 0.3, y - r * 0.3), (x + r, y), (x + r * 0.3, y + r * 0.3),
                        (x, y + r), (x - r * 0.3, y + r * 0.3), (x - r, y), (x - r * 0.3, y - r * 0.3)], fill=ink)


def foreground(c: InkCanvas, proj: Stereographic, horizon_y: int, style: SkyStyle,
               m: SkyModel | None = None) -> None:
    """Below the horizon: the ocean with a few glints (and the Moon's path on the water
    when it's up), a line of surf, the sand, and the compass points (ticks on the horizon,
    letters on the sand). Or, without `beach`, a plain band with the compass points."""
    W, H = c.size
    points = [(proj.horizon_x(i * 22.5), i) for i in range(16)]
    points = [(x, i) for x, i in points if 0 <= x < W]
    if not style.beach:
        c.draw.rectangle([0, horizon_y, W, H], fill=style.ground)
        c.draw.line([(0, horizon_y), (W, horizon_y)], fill=style.horizon, width=style.horizon_width)
        for x, i in points:
            c.draw.line([(x, horizon_y), (x, horizon_y + (10 if i % 2 == 0 else 5))], fill=style.horizon, width=2)
            if i % 2 == 0:
                c.draw.text((round(x), horizon_y + 12), COMPASS_8[i // 2], font=font(22, True),
                            fill=style.ground_text, anchor="mt")
        return

    shore = horizon_y + style.ocean_h  # where the surf meets the sand
    c.draw.rectangle([0, horizon_y, W, H], fill=style.ground)
    # Glints on the water: short white dashes, fewer and shorter toward the horizon.
    rng = np.random.default_rng(11)
    for row in range(4, style.ocean_h - 3, 4):
        near = row / style.ocean_h
        for x in rng.uniform(-10, W, int(3 + 9 * near)):
            length = 2 + 6 * near * rng.random()
            c.draw.line([(x, horizon_y + row), (x + length, horizon_y + row)], fill=WHITE)
    if m is not None:
        sun_alt = m.view.sun.alt
        if style.moon_path and sun_alt < -6.0:  # by day, the Sun's outshines it
            moon_path(c, proj, horizon_y, style, m)
        if style.sun_path and sun_alt > -1.0:
            sun_path(c, proj, horizon_y, style, m)
    # The sand, behind a wavy line of surf.
    xs = np.arange(-2, W + 3, 2, dtype=float)
    edge = shore + 2.0 * np.sin(xs / 31.0) + 1.2 * np.sin(xs / 11.0 + 1.0)
    outline = [(0.0, float(H))] + list(zip(xs.tolist(), edge.tolist())) + [(float(W), float(H))]
    c.draw.polygon(outline, fill=BLACK)
    sand = InkCanvas((W, H), "black")
    sand.draw.polygon(outline, fill=WHITE)
    inside = np.asarray(sand.image) == WHITE
    iy, ix = np.arange(H)[:, None], np.arange(W)[None, :]
    t = STIPPLE[iy % 64, ix % 64]
    below = 0.0
    for ink, share in style.sand_mix:
        ys, xs_ = np.nonzero(inside & (t >= below) & (t < below + share))
        c.draw.point(np.column_stack((xs_, ys)).ravel().tolist(), fill=ink)
        below += share
    c.draw.line(list(zip(xs.tolist(), edge.tolist())), fill=WHITE, width=3)
    # Compass: ticks down from the horizon, letters on the sand.
    label_y = (shore + 3 + H) / 2 + 1
    for x, i in points:
        c.draw.line([(x, horizon_y), (x, horizon_y + (7 if i % 2 == 0 else 4))], fill=WHITE, width=2)
        if i % 2 == 0:
            label(c, (x, label_y), COMPASS_8[i // 2], style.compass_size, style.sand_text, style.sand_halo, "mm", True)


def moon_path(c: InkCanvas, proj: Stereographic, horizon_y: int, style: SkyStyle, m: SkyModel) -> None:
    """The Moon's reflection: white glints on the water below it, fuller for a fuller Moon,
    and less of them as it goes into an eclipse, red once it's total."""
    mv = m.view.moon
    if mv.alt <= 0 or mv.phase is None:
        return
    fullness, inks = mv.phase.illuminated, (WHITE,)
    if isinstance(m.eclipse, LunarEclipse) and m.eclipse.stage in ("partial", "total"):
        fullness *= 1.0 - m.eclipse.covered
        if m.eclipse.stage == "total":
            inks = (RED,)
    light_path(c, proj, horizon_y, style, mv.alt, mv.az, fullness, inks, 1969)


def sun_path(c: InkCanvas, proj: Stereographic, horizon_y: int, style: SkyStyle, m: SkyModel) -> None:
    """The Sun's reflection: glints like the Moon's, gold when it's low, less of them as the
    Moon covers it."""
    sun = m.view.sun
    inks = (YELLOW, YELLOW, RED) if sun.alt < 8.0 else (YELLOW, WHITE)
    fullness = 1.0 - m.eclipse.covered if isinstance(m.eclipse, SolarEclipse) and m.eclipse.stage else 1.0
    light_path(c, proj, horizon_y, style, sun.alt, sun.az, fullness, inks, 1970, wide=3.0)


def light_path(c: InkCanvas, proj: Stereographic, horizon_y: int, style: SkyStyle, alt: float, az: float,
               fullness: float, inks: tuple[int, ...], seed: int, wide: float = 1.0) -> None:
    """A column of glints on the water straight below a body at (alt, az), widening toward
    the shore, longer for a lower body (a high one's path starts closer to the shore).
    `wide` spreads it out, with that many more glints."""
    W = c.size[0]
    x0 = proj.horizon_x(az)
    if not -40 < x0 < W + 40:
        return
    rng = np.random.default_rng(seed)
    start = int(style.ocean_h * min(0.45, max(0.0, (alt - 20.0) / 100.0)))
    n = 0
    for row in range(start + 2, style.ocean_h - 2, 2):
        near = row / style.ocean_h  # 0 at the horizon, 1 at the shore
        spread = (3 + 14 * near) * wide
        for _ in range(max(1, round((3 + 7 * near) * (0.4 + 0.6 * fullness) * wide))):
            x = x0 + rng.normal(0.0, spread / 2.5)
            half = (2 + 6 * near * rng.random()) / 2
            c.draw.line([(x - half, horizon_y + row), (x + half, horizon_y + row)], fill=inks[n % len(inks)])
            n += 1


def moon_lost_in_sun(mx: float, my: float, sun_x: float, sun_y: float, style: SkyStyle) -> bool:
    """Whether the Moon, drawn at (mx, my), would touch the Sun's disc as drawn. They're drawn
    far bigger than life, so this is most of a day either side of every new moon."""
    return math.hypot(mx - sun_x, my - sun_y) < style.sun_radius + style.moon_radius + 3


def disc_on_screen(proj: Stereographic, body: Body, width: int, horizon_y: int, radius: float) -> bool:
    """Whether to draw the Moon or the Sun: its center inside the sides of the view (so at
    least half the disc shows; past an edge it gets an off-view marker instead of a cut-off
    sliver), and not more than its radius below the horizon (it may be setting into the sea)."""
    x, y, ok = proj.point(body.alt, body.az)
    return ok and body.alt > -1 and 0 <= x < width and y < horizon_y + radius


def off_view(proj: Stereographic, m: SkyModel, width: int, horizon_y: int,
             moon_up: bool) -> list[tuple[Body, bool]]:
    """The planets, the Sun (and the Moon, unless it's drawn) that are above the horizon but not
    on the screen, with True for those off the left (east) edge. A body is off the screen
    if its projected position is: in this projection the view reaches further sideways
    higher up, so a planet 50° up can be on screen beyond the horizon's edge azimuth.
    Bodies more than 150° from the view's center (behind the viewer) are left out."""
    bodies: list[Body] = [m.view.sun, *m.view.planets]
    if not moon_up:
        bodies.insert(0, m.view.moon)
    out = []
    for body in bodies:
        if body.alt <= 0:
            continue
        dl = (body.az - proj.az0 + 180.0) % 360.0 - 180.0
        if abs(dl) > 150:
            continue
        x, y, ok = proj.point(body.alt, body.az)
        if ok and 0 <= x < width and 0 <= y < horizon_y:
            continue  # drawn
        out.append((body, (x < 0) if ok else dl < 0))
    return out


def _off_view(c: InkCanvas, proj: Stereographic, m: SkyModel, placer: Placer, horizon_y: int,
              style: SkyStyle, moon_up: bool) -> None:
    """"◂ Venus" at the left edge (or "Venus ▸" at the right) for the bodies `off_view`
    finds, at about their height."""
    W = c.size[0]
    half = proj.half_fov(W)
    size = style.planet_label_size
    eclipsed = isinstance(m.eclipse, LunarEclipse) and m.eclipse.stage in ("partial", "total")
    for body, left in off_view(proj, m, W, horizon_y, moon_up):
        ink = (RED if eclipsed else style.moon_lit) if body.name == "Moon" else style.planet_inks.get(body.name, YELLOW)
        edge_az = proj.az0 + (-half if left else half)
        _, y, _ = proj.point(min(body.alt, 80.0), edge_az)
        y = min(max(y, 20.0), horizon_y - 20.0)
        s = f"\u25c2 {body.name}" if left else f"{body.name} \u25b8"
        x, anchor = (6, "lm") if left else (W - 6, "rm")
        for dy in (0, -22, 22, -44, 44):
            b = text_box(c, (x, y + dy), s, size, anchor, True)
            if placer.free(b):
                placer.take(b)
                ink_text = style.planet_label_ink if style.planet_label_ink is not None else ink
                name_label(c, style, (x, y + dy), s, size, ink_text, anchor, True)
                break


def _constellation_names(c: InkCanvas, m: SkyModel, placer: Placer, sx: np.ndarray, sy: np.ndarray,
                         on: np.ndarray, horizon_y: int, style: SkyStyle) -> None:
    cat = m.catalog
    for con in shown_constellations(m, style.constellations):
        idx = sorted({i for pair in con.lines for h in pair if (i := cat.rows.get(h)) is not None})
        pts = [(sx[i], sy[i]) for i in idx if on[i] and 0 <= sx[i] < c.size[0] and 0 <= sy[i] < horizon_y - 4]
        if len(pts) < 2:
            continue
        x = float(np.mean([p[0] for p in pts]))
        y = float(np.mean([p[1] for p in pts]))
        s = con.name.upper()
        for dy in (0, 14, -14, 28, -28, 42, -42):
            b = text_box(c, (x, y + dy), s, style.con_label_size, "mm")
            if placer.free(b):
                placer.take(b)
                name_label(c, style, (x, y + dy), s, style.con_label_size, style.con_label, "mm")
                break


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    m = sky_model(scene.draw_for, ctx)
    c = InkCanvas(ctx.canvas, "black")
    draw(c, m, ctx.config)
    return c.indices()
