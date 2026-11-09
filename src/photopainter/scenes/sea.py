"""The sea screen: today's ocean from the window, drawn woodblock-style in the six inks.

Not a chart of the swell numbers but a picture made from them. The Owner approved the look
on 2026-11-04 over mockups of a calm, a big and a windy day; `draw` is that mockup's drawing,
with the waves further apart and whitecaps that show, as the Owner then asked. `SeaStyle`
holds the drawing's numbers, and `scripts/sea-playground.py` puts them on sliders.

    +--------------------------------------------------------------------+
    | Thursday, November 5 · 2:10 pm · sunny, 71°                    sun |  sky: a third
    | 2 ft at 13 s from the SW                                           |
    | Wind 4 mph W · Water 62° · High tide 3:42 pm                       |
    |          ___/\\__ Catalina __                                       |
    |~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~|  horizon
    |  rows of crests, spaced t**1.75 from the horizon to the beach      |
    |#jetty                                                              |
    |###  sand, with a white wash line                                   |
    +--------------------------------------------------------------------+

What each number does:

- **Swell height:** the amplitude and peakedness of the crests, and when foam breaks off
  them (from about 2 ft on the nearest rows).
- **Period:** how far apart the rows are (`row_count`) and the wavelength along each crest.
- **Swell direction,** against the window's bearing (`sky.center_azimuth_deg`): tilts the
  crest lines.
- **Wind:** a chop on every crest and, from `whitecap_mph`, whitecaps as white dashes on
  the water, more of them and further out as it blows harder (`whitecaps_in_row`). Its direction is only in the words.
- **Tide** (the table plus the measured gap, as on the day screen): the width of the beach.
- **Weather code:** the cache has no cloud cover, so the WMO code decides the sky: clear
  (0, 1), clear with two clouds (2), overcast (everything else, rain and fog included).
- **Sun:** a disc at its real azimuth and altitude, with no reflection on the water. It sets
  behind the sea, and it isn't drawn when it's outside the picture's span of
  `center ± HALF_SPAN_DEG` or the sky is overcast. Above about 47° it stays at the top edge.

- **Jetty:** from the place's data (`places.sea_for`): the Wedge's runs out from the bottom
  left, where the Owner drew it. Left of it is the harbor mouth: flat water with no waves and
  no beach, whatever the swell. Without place data there's no jetty, and the island on the
  horizon is the place's skyline (none: an open horizon).
- **Beach:** its width follows the tide between the place's low and high (the Wedge: 0 to
  7 ft), or the station's own range in the tide cache without place data.

With the Sun below the horizon the same drawing goes dark (`Look`): a black sky, the Moon
in its phase, the lit faces as blue dots on black. On a moonless or
overcast night the crests are blue, not white. The day look is what was approved; the night
is a variant.

Each line of words leaves out what's missing, and a missing cache only takes away its part
of the picture (no swell data: a nearly flat sea). A forecast is used up to 12 h old, as on
the day screen, with no "as of" note: nothing else goes on the picture. The swell is the
offshore model's, not surf at the Wedge (PRD D9).

Drawn for `scene.draw_for`, the middle of its schedule bucket; all randomness is seeded
from that moment, so the same moment and caches give the same pixels.

A dashboard preview can ask for a wave look (`scene.waves`, from `WAVES`) or a cloud look
(`scene.clouds`) in place of the forecast's, to see the picture on a day that isn't today.
The words follow the look. The wall never gets one: the schedule's screens leave them unset,
since the picture is meant to show the real sea.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from photopainter import places
from photopainter.astro.moon import MoonPhase
from photopainter.places import Jetty, SeaPlace
from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.scenes.base import Scene, SceneContext
from photopainter.scenes.sky import Skyline, clock, phase_disc
from photopainter.scenes.tides import compass, condition, day_model, deg, flow_arrow, whole

BLACK, WHITE, YELLOW, BLUE = INK["black"], INK["white"], INK["yellow"], INK["blue"]

ARROW = "\u2192"  # in the words: draw the direction's arrow here
ARROW_GAP_PX = 5
HALF_SPAN_DEG = 55.0  # the picture spans this far either side of the window's bearing
SKY_PX_PER_DEG = 3.0  # how high the Sun and Moon stand above the horizon
ISLAND_PX_PER_DEG = 11.0
ISLAND_EXAGGERATION = 4.0  # as the sky screen draws land on the horizon
DISC_RADIUS = 17
DEFAULT_PERIOD_S = 10.0  # when the cache has no period
WHITECAP_ROWS = 8  # whitecaps_in_row() counts dashes for a sea of this many rows
MAX_DRAWN_SWELL_FT = 14.0  # the words say what it is; past this the drawing stops growing
FLAT_SEA_AMPLITUDE = 1.5  # px at the nearest row, with no swell data
BEACH_DEFAULT_PX = 44  # no tide data

CLEAR_CODES = (0, 1)
PARTLY_CODES = (2,)
Cloud = tuple[float, float, float, float]  # center x, center y, radius x, radius y


# A jetty and the beach's tide range come from the place's data (places.sea_for): the Wedge's
# jetty was drawn on the picture by the Owner over a preview.
BEACH_SPAN_FT = (0.0, 7.0)  # the beach's widest and narrowest tides when nothing says otherwise
MIN_BEACH_SPAN_FT = 1.0


@dataclass(frozen=True)
class SeaStyle:
    """The drawing's choices; the defaults are the look the Owner chose.
    `scripts/sea-playground.py` has a slider for each."""

    rows_at_10s: float = 8.5  # rows of crests at a 10 s swell; they go as 1 / period², clipped
    rows_min: int = 3
    rows_max: int = 13
    perspective: float = 1.75  # row k of n sits at (k/n)**this from the horizon to the beach
    nearness_power: float = 1.5  # how fast amplitude and wavelength grow toward the beach
    amp_base_px: float = 8.0  # the nearest row's amplitude = base + per_ft * swell
    amp_per_ft: float = 5.0
    peak_base: float = 1.4  # how sharp the crests are = base + per_ft * swell
    peak_per_ft: float = 0.2
    crest_px_per_s: float = 36.0  # wavelength along the nearest crest, per second of period
    crest_min_px: float = 60.0
    tilt: float = 0.33  # crest slope when the swell comes from 90° off the window
    trough_at: float = 0.26  # the solid trough starts this far down the gap to the next row
    chop_per_mph: float = 0.18  # px of chop on the nearest row
    foam_from_px: float = 16.0  # crests taller than this break into foam
    foam_chance: float = 0.6
    whitecap_mph: float = 10.0  # no whitecaps under this
    whitecap_reach: float = 0.4  # at that wind, only rows at least this near have them
    whitecap_reach_per_mph: float = 0.02  # how much further out each extra mph takes them
    whitecap_base: float = 2.0  # dashes on a row = (base + per_mph * extra mph) * (0.35 + nearness)
    whitecap_per_mph: float = 1.4
    whitecap_length: float = 1.0  # scales the dashes

    def scaled(self, k: float) -> SeaStyle:
        """The same drawing `k` times the size: its lengths in pixels change, its shapes don't."""
        return replace(self, amp_base_px=self.amp_base_px * k, amp_per_ft=self.amp_per_ft * k,
                       crest_px_per_s=self.crest_px_per_s * k, crest_min_px=self.crest_min_px * k,
                       chop_per_mph=self.chop_per_mph * k, foam_from_px=self.foam_from_px * k,
                       whitecap_length=self.whitecap_length * k)


@dataclass(frozen=True)
class WordStyle:
    """Where the words go, and each line's (size, height): the date and weather, the swell,
    the wind, water and tide, and when it was updated."""

    left: int = 12
    top: int = 8
    pad: int = 10
    lines: tuple[tuple[int, int], ...] = ((15, 22), (30, 42), (17, 24), (11, 14))


@dataclass(frozen=True)
class WaveLook:
    """A day's swell and wind to preview, whatever the forecast says."""

    swell_ft: float
    period_s: float
    swell_from: float
    wind_mph: float
    wind_from: float = 270.0


WAVES: dict[str, WaveLook] = {  # the names are `base.WAVE_LOOKS`; a test keeps them equal
    "calm": WaveLook(1.5, 13, 225, 4),
    "big": WaveLook(8, 17, 190, 15),
    "windy": WaveLook(3, 9, 250, 19),
    "gale": WaveLook(6, 8, 270, 35),
}
CLOUD_CODES = {"clear": 0, "partly": 2, "overcast": 3}  # a WMO code for each of `base.CLOUD_LOOKS`


@dataclass(frozen=True)
class Light:
    """A disc in the sky over the water: the Sun, or the Moon when `phase` is set."""

    az: float  # degrees from north through east
    alt: float
    phase: MoonPhase | None = None
    bright_limb_deg: float = 0.0


@dataclass(frozen=True)
class SeaModel:
    """Everything the picture is drawn from."""

    seed: int
    daylight: bool
    sky: str  # "clear", "partly", "overcast"
    light: Light | None  # the Sun by day, the Moon at night; None below the horizon
    swell_ft: float | None  # None: no swell data, a nearly flat sea
    period_s: float | None
    swell_from: float | None
    wind_mph: float
    beach_px: int
    words: tuple[str, str | None, str | None]  # the three lines; the first is always there
    wind_from: float | None = None
    updated: str | None = None  # the small last line


@dataclass(frozen=True)
class Look:
    """The inks that change between day and night."""

    sky: int
    face_base: int  # under a wave's lit face
    trough: int
    lip: int
    keyline: int
    island_base: int
    ridge: int
    sand_base: int
    sand_density: float
    wash: int
    wash_key: int
    rock_edge: int
    text: int
    halo: int


DAY_LOOK = Look(sky=WHITE, face_base=WHITE, trough=BLUE, lip=WHITE, keyline=BLACK, island_base=WHITE,
                ridge=BLACK, sand_base=WHITE, sand_density=0.7, wash=WHITE, wash_key=BLACK,
                rock_edge=WHITE, text=BLACK, halo=WHITE)
NIGHT_LOOK = Look(sky=BLACK, face_base=BLACK, trough=BLACK, lip=WHITE, keyline=BLACK, island_base=BLACK,
                  ridge=WHITE, sand_base=BLACK, sand_density=0.25, wash=BLUE, wash_key=BLACK,
                  rock_edge=BLUE, text=WHITE, halo=BLACK)


# --- the numbers -----------------------------------------------------------------------------


def row_count(period_s: float | None, style: SeaStyle = SeaStyle()) -> int:
    """Rows of crests: a swell's wavelength goes as its period squared, so a long period
    leaves few in view: 13 at 6 s, 8 at 10 s, 5 at 13 s, 3 at 17 s."""
    rows = style.rows_at_10s * (10.0 / (period_s or DEFAULT_PERIOD_S)) ** 2
    return round(min(max(rows, style.rows_min), style.rows_max))


def whitecaps_in_row(wind_mph: float, s: float, style: SeaStyle = SeaStyle()) -> int:
    """White dashes on a row of nearness `s` (0-1): none under `whitecap_mph`, a few near the
    beach just above it, reaching further out and crowding in as it blows harder."""
    extra = wind_mph - style.whitecap_mph
    if extra < 0 or s < style.whitecap_reach - extra * style.whitecap_reach_per_mph:
        return 0
    return int((style.whitecap_base + extra * style.whitecap_per_mph) * (0.35 + s))


def beach_px(tide_ft: float | None, span: tuple[float, float] = BEACH_SPAN_FT) -> int:
    """How much sand shows: about 60 px at the low end of `span`, 26 at its high end."""
    if tide_ft is None:
        return BEACH_DEFAULT_PX
    low, high = span
    return round(min(max(60 - (tide_ft - low) * 34 / (high - low), 22.0), 68.0))


def place_for(ctx: SceneContext) -> SeaPlace | None:
    """The jetty and beach for this window, from the place's data (places.sea_for)."""
    loc = ctx.config.location
    return places.sea_for(loc.lat, loc.lon, ctx.config.sky.center_azimuth_deg, ctx.data_dir)


def beach_span(place: SeaPlace | None, heights: np.ndarray | None) -> tuple[float, float]:
    """The tides the beach's width runs between: the place's, else the station's own range in
    the tide cache."""
    if place is not None and place.beach_ft is not None:
        return place.beach_ft
    if heights is None or len(heights) == 0:
        return BEACH_SPAN_FT
    low, high = float(np.min(heights)), float(np.max(heights))
    return low, max(high, low + MIN_BEACH_SPAN_FT)


def sky_kind(code: int | None) -> str:
    """No forecast: the clear sky, with nothing on it that we don't know."""
    if code is None or code in CLEAR_CODES:
        return "clear"
    return "partly" if code in PARTLY_CODES else "overcast"


def clouds(kind: str, seed: int, width: int, scale: float = 1.0) -> list[Cloud]:
    """Flat, low and long; a few, placed from the seed."""
    if kind == "clear":
        return []
    rng = np.random.default_rng(seed + 1)
    spots = (0.21, 0.59, 0.87) if kind == "overcast" else (0.42, 0.8)
    return [(width * (f + rng.uniform(-0.04, 0.04)), rng.uniform(122, 134) * scale, rng.uniform(90, 150) * scale,
             rng.uniform(12, 18) * scale) for f in spots]


def _phrase_swell(height: float, period: float | None, direction: float | None) -> str:
    s = f"{whole(height)} ft"
    if period is not None:
        s += f" at {whole(period)} s"
    return s if direction is None else f"{s} from the {compass(direction)}{ARROW}"


def sea_model(now: datetime, ctx: SceneContext, updated: datetime | None = None,
              waves: str | None = None, clouds: str | None = None) -> SeaModel:
    """The picture's numbers and words for the moment `now`, from the day screen's model.
    `updated` is when the screen came up, for the small line; it's `now` if not given.
    `waves` and `clouds` (previews) are looks to draw instead of the forecast's."""
    m = day_model(now, ctx)
    now, w, s, tide = m.now, m.weather, m.swell, m.tide

    code = None if w is None else w.code
    if clouds is not None:
        code = CLOUD_CODES[clouds]
    if waves is not None:
        look = WAVES[waves]
        swell_ft, period_s, swell_from = look.swell_ft, look.period_s, look.swell_from
        wind_mph, wind_from = look.wind_mph, look.wind_from
    else:
        swell_ft, period_s, swell_from = (None, None, None) if s is None else (s.swell_height, s.swell_period, s.swell_dir)
        wind_mph, wind_from = (None, None) if w is None else (w.wind, w.wind_dir)

    sun_alt, sun_az, moon = _sky_bodies(now, m.lat, m.lon)
    daylight = m.daytime if sun_alt is None else sun_alt > 0
    if daylight:
        light = None if sun_alt is None else Light(sun_az, sun_alt)
    else:
        light = moon if moon is not None and moon.alt > 0 else None

    tide_ft = None
    next_tide = None
    if tide is not None:
        tide_ft = tide.height + float(m.gap_at(now.timestamp())[0])
        ahead = [e for e in (tide.next_high, tide.next_low) if e is not None]
        next_tide = min(ahead, key=lambda e: e.t, default=None)

    first = [f"{now:%A, %B} {now.day}"]
    word = "" if code is None else condition(code, daylight)[0].lower()
    if w is not None:
        first.append(f"{word}, {deg(w.temp)}" if word else deg(w.temp))
    elif word:
        first.append(word)
    second = None if swell_ft is None else _phrase_swell(swell_ft, period_s, swell_from)
    third = []
    if wind_mph is not None:
        third.append(f"Wind {whole(wind_mph)} mph" + ("" if wind_from is None else f" {compass(wind_from)}{ARROW}"))
    if s is not None and s.water_temp is not None:
        third.append(f"Water {deg(s.water_temp)}")
    if next_tide is not None:
        third.append(f"{'High' if next_tide.high else 'Low'} tide {clock(next_tide.t)}")

    return SeaModel(
        seed=int(now.timestamp()) // 60 % 100_000,
        daylight=daylight,
        sky=sky_kind(code),
        light=light,
        swell_ft=swell_ft,
        period_s=period_s,
        swell_from=swell_from,
        wind_mph=0.0 if wind_mph is None else wind_mph,
        beach_px=beach_px(tide_ft, beach_span(place_for(ctx), None if tide is None else tide.heights)),
        words=("  ·  ".join(first), second, "  ·  ".join(third) or None),
        wind_from=wind_from,
        updated=f"Updated {clock(updated or now)}",
    )


def _sky_bodies(now: datetime, lat: float, lon: float) -> tuple[float | None, float | None, Light | None]:
    """(Sun altitude, Sun azimuth, the Moon), all None without the ephemeris."""
    from photopainter.astro import EphemerisMissing
    from photopainter.astro.sky import moon_view, sun_altaz

    try:
        alt, az = sun_altaz([now], lat, lon)
        moon = moon_view(now, lat, lon)
    except (EphemerisMissing, ValueError):
        return None, None, None
    return float(alt[0]), float(az[0]), Light(moon.az, moon.alt, moon.phase, moon.bright_limb_deg)


# --- ink on the canvas -----------------------------------------------------------------------


def _bayer(n: int = 8) -> np.ndarray:
    m = np.zeros((1, 1))
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


@lru_cache(maxsize=2)
def _grids(width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(row of each pixel, column of each pixel, ordered dots, stipple), the same each time."""
    iy, ix = np.mgrid[0:height, 0:width]
    return iy, ix, _bayer(8)[iy % 8, ix % 8], np.random.default_rng(5).random((height, width))


def _poly_mask(size: tuple[int, int], points: list[tuple[float, float]]) -> np.ndarray:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return np.asarray(mask) > 0


def _paste(c: InkCanvas, mask: np.ndarray, ink: int) -> None:
    if mask.any():
        c.image.paste(ink, (0, 0, *c.size), mask=Image.fromarray(mask.astype(np.uint8) * 255, "L"))


def _fill(c: InkCanvas, mask: np.ndarray, ink: int, density: float = 1.0, base: int | None = None) -> None:
    """`ink` over `mask`, in ordered dots if `density` is under 1, on a solid `base` if given."""
    if base is not None:
        _paste(c, mask, base)
    if density < 1.0:
        mask = mask & (_grids(*c.size)[2] < density)
    _paste(c, mask, ink)


def _profile(phi: np.ndarray, peak: float) -> np.ndarray:
    return ((1 - np.cos(phi)) / 2) ** peak


def _line_width(s: str, size: int, bold: bool) -> float:
    parts = s.split(ARROW)
    return sum(text_width(p, font(size, bold)) for p in parts) + (len(parts) - 1) * (size + ARROW_GAP_PX)


def _line(c: InkCanvas, x: float, y: float, s: str, size: int, look: Look, from_deg: float | None,
          bold: bool = False) -> None:
    """One line of words, with an arrow wherever it has an `ARROW`: pointing the way the swell
    or wind is moving, as seen facing the window (from the west points left)."""
    f = font(size, bold)
    parts = s.split(ARROW)
    for i, part in enumerate(parts):
        c.draw.text((x, y), part, font=f, fill=look.text, anchor="la")
        x += text_width(part, f)
        if i < len(parts) - 1:
            if from_deg is not None:
                flow_arrow(c, x + ARROW_GAP_PX + size * 0.45, y + size * 0.6, size * 0.9, from_deg, flip=True,
                           fill=look.text)
            x += size + ARROW_GAP_PX


def _words(c: InkCanvas, m: SeaModel, look: Look, words: WordStyle = WordStyle()) -> None:
    """The words in a plain box top left, so the sky's dots don't get in their way; lines shrink
    if the canvas (a portrait frame) is too narrow for them."""
    left, top, pad = words.left, words.top, words.pad
    line1, line2, line3 = m.words
    lines = [(line1, *words.lines[0], False, None), (line2, *words.lines[1], True, m.swell_from),
             (line3, *words.lines[2], False, m.wind_from), (m.updated, *words.lines[3], False, None)]
    room = c.size[0] - 2 * (left + pad)
    fitted = []
    for text, size, step, bold, from_deg in lines:
        if text is None:
            continue
        while size > 9 and _line_width(text, size, bold) > room:
            size -= 1
        fitted.append((text, size, bold, from_deg, step))
    if not fitted:
        return
    right = left + 2 * pad + max(_line_width(t, size, bold) for t, size, bold, _, _ in fitted)
    bottom = top + pad + sum(step for *_, step in fitted) + 2
    c.draw.rectangle([left, top, right, bottom], fill=look.halo, outline=look.text)
    y = top + pad
    for text, size, bold, from_deg, step in fitted:
        _line(c, left + pad, y, text, size, look, from_deg, bold)
        y += step


# --- the picture -----------------------------------------------------------------------------


def draw(c: InkCanvas, m: SeaModel, center_az: float, skyline: Skyline | None,
         style: SeaStyle = SeaStyle(), scale: float = 1.0, words: WordStyle = WordStyle(),
         jetty: Jetty | None = None, half_span: float = HALF_SPAN_DEG) -> None:
    """The picture on `c`. `scale` draws it smaller (the combo screen's panel: 0.5 on a canvas
    half the size); 1.0 is the screen as approved. `skyline` and `jetty` are the place's
    (`places.skyline_for`, `place_for`); None leaves them out. `half_span` is how far either side
    of `center_az` the picture reaches (wider for the combo's sea across its bottom row)."""
    width, height = c.size
    look = DAY_LOOK if m.daylight else NIGHT_LOOK
    style = style.scaled(scale) if scale != 1.0 else style
    horizon = height // 3
    az0, span = center_az - half_span, 2 * half_span
    az_x = lambda az: ((az - az0 + 180) % 360 - 180) / span * width  # noqa: E731

    overcast = m.sky == "overcast"
    light = None if m.light is None or overcast else m.light
    light_x = None if light is None else az_x(light.az)
    on_view = light_x is not None and 0 < light_x < width

    c.draw.rectangle([0, 0, width, height], fill=look.sky)
    _sky(c, m, look, horizon, clouds(m.sky, m.seed, width, scale))
    if on_view:
        _disc(c, light, light_x, horizon, look, scale)
    if skyline is not None:
        _island(c, skyline, az_x, horizon, look, scale)
    sea_bottom = height - round(m.beach_px * scale)
    _sea(c, m, look, horizon, sea_bottom, center_az, m.daylight or light is not None, style, scale)
    _beach(c, look, sea_bottom, scale)
    if jetty is not None:
        _calm(c, jetty, look, scale)
        _jetty(c, jetty, look, scale)

    _words(c, m, look, words)


def _sky(c: InkCanvas, m: SeaModel, look: Look, horizon: int, cloud_list: list[Cloud]) -> None:
    width, height = c.size
    iy, _, dots, _ = _grids(width, height)
    above = iy < horizon
    if m.sky == "overcast":
        _fill(c, above, BLUE, 0.28 if m.daylight else 0.08)
    elif m.daylight:
        _paste(c, above & (dots < 0.2 * np.clip(1 - iy / horizon, 0, 1) ** 1.5), BLUE)
    # White clouds with a keyline; at night, dim ones in blue dots.
    for cx, cy, rx, ry in cloud_list:
        layers = ((2, BLACK), (0, WHITE)) if m.daylight else ((2, BLUE), (0, BLACK))
        for grow, ink in layers:
            c.draw.ellipse([cx - rx - grow, cy - ry - grow, cx + rx + grow, cy + ry + grow], fill=ink)
            c.draw.ellipse([cx - rx * 0.55 - grow, cy - ry * 1.9 - grow,
                            cx + rx * 0.35 + grow, cy + ry * 0.4 + grow], fill=ink)


def _disc(c: InkCanvas, light: Light, x: float, horizon: int, look: Look, scale: float = 1.0) -> None:
    """The Sun or the Moon. High ones stop at the top edge rather than leave the picture."""
    r = DISC_RADIUS * scale
    y = max(horizon - light.alt * SKY_PX_PER_DEG * scale, r + 2.0)
    if light.phase is None:
        c.draw.ellipse([x - r, y - r, x + r, y + r], fill=YELLOW, outline=BLACK)
        return
    # The bright limb faces the Sun: on this flat picture, bearing 0 is straight up.
    phase_disc(c, x, y, r, light.phase.illuminated, light.bright_limb_deg, WHITE, None)
    c.draw.ellipse([x - r, y - r, x + r, y + r], outline=look.text)


def _island(c: InkCanvas, sk: Skyline, az_x, horizon: int, look: Look, scale: float = 1.0) -> None:
    pts = [(az_x(az), horizon - h * ISLAND_EXAGGERATION * ISLAND_PX_PER_DEG * scale)
           for az, h in zip(sk.az.tolist(), sk.height.tolist())]
    poly = [(pts[0][0], horizon)] + pts + [(pts[-1][0], horizon)]
    _fill(c, _poly_mask(c.size, poly), BLUE, 0.45, base=look.island_base)
    c.draw.line(pts, fill=look.ridge, width=1)


def _sea(c: InkCanvas, m: SeaModel, look: Look, horizon: int, sea_bottom: int, center_az: float,
         bright: bool, style: SeaStyle, scale: float = 1.0) -> None:
    """Rows of crests from the horizon to the beach, each hiding what's behind it."""
    width, height = c.size
    rng = np.random.default_rng(m.seed)
    size = (width, height)
    rows = row_count(m.period_s, style)
    swell = min(m.swell_ft or 0.0, MAX_DRAWN_SWELL_FT)
    amp_near = FLAT_SEA_AMPLITUDE * scale if m.swell_ft is None else style.amp_base_px + swell * style.amp_per_ft
    lam_near = (m.period_s or DEFAULT_PERIOD_S) * style.crest_px_per_s
    delta = 0.0 if m.swell_from is None else math.radians(m.swell_from - center_az)
    peak = style.peak_base + swell * style.peak_per_ft
    wind = m.wind_mph
    lip = look.lip if bright else BLUE

    xs = np.arange(-20, width + 21, 2).astype(float)
    ys_row = [horizon + (sea_bottom - horizon) * ((k / rows) ** style.perspective) for k in range(rows + 2)]
    _fill(c, _grids(width, height)[0] > horizon, look.trough)  # the water beyond the first crest
    c.draw.line([(0, horizon), (width, horizon)], fill=BLUE, width=1)
    for k in range(1, rows + 1):
        t = k / rows
        s = max(0.06, t ** style.nearness_power)
        amp = amp_near * s
        lam = max(style.crest_min_px, lam_near * s)
        phi = 2 * np.pi * xs / lam + k * 2.3 + m.seed
        tilt = math.sin(delta) * style.tilt * s
        chop = wind * style.chop_per_mph * s
        shape = _profile(phi, peak)
        y = ys_row[k] - amp * shape + tilt * (xs - width / 2) + chop * np.sin(xs / ((9 + 26 * s) * scale) + k * 1.7)
        y = np.minimum(y, sea_bottom + 6 * scale)
        pts = list(zip(xs.tolist(), y.tolist()))
        gap = ys_row[k + 1] - ys_row[k]

        # The lit face in dots down from the crest; the trough solid from its middle down.
        closing = [(width + 20, height), (-20, height)]
        _fill(c, _poly_mask(size, pts + closing), BLUE, 0.5, base=look.face_base)
        mid = [(x, yy + gap * style.trough_at) for x, yy in pts]
        _fill(c, _poly_mask(size, mid + closing), look.trough)

        # The crest: a lip with a dark keyline under it, on the near rows.
        w = 1 + int(round(amp / 8))
        if s > 0.12:
            c.draw.line([(x, yy + w / 2 + 1) for x, yy in pts], fill=look.keyline, width=1)
        c.draw.line(pts, fill=lip, width=w)

        # Foam breaking off the big crests.
        if amp > style.foam_from_px:
            for i in np.nonzero(shape > 0.82)[0][::3]:
                if rng.random() < style.foam_chance:
                    r = rng.integers(2, 3 + int(amp / (10 * scale))) * scale
                    dy = rng.uniform(-amp * 0.25, amp * 0.35)
                    c.draw.ellipse([xs[i] - r, y[i] + dy - r, xs[i] + r, y[i] + dy + r], fill=WHITE)

        # Whitecaps from the wind: dashes out on the water below the crest, following its
        # line, so they read against the blue and don't vanish into the white lip. The wind
        # makes them, not the swell, so fewer rows each carry more.
        for _ in range(round(whitecaps_in_row(wind, s, style) * WHITECAP_ROWS / rows)):
            i = rng.integers(0, len(xs) - 12)
            dash = max(1, int(rng.integers(3 + int(s * 3), 6 + int(s * 8)) * style.whitecap_length))
            drop = gap * rng.uniform(0.15, 0.85)
            c.draw.line([(x, yy + drop) for x, yy in pts[i:i + dash]], fill=WHITE,
                        width=max(round((2 + int(s * 2)) * scale), 1))


def _beach(c: InkCanvas, look: Look, sea_bottom: int, scale: float = 1.0) -> None:
    width, height = c.size
    _fill(c, _grids(width, height)[0] >= sea_bottom, YELLOW, look.sand_density, base=look.sand_base)
    wash = [(x, sea_bottom + 4 * scale * math.sin(x / (37.0 * scale)) + 3 * scale * math.sin(x / (11.0 * scale)))
            for x in range(0, width + 1, 4)]
    c.draw.line(wash, fill=look.wash, width=max(round(9 * scale), 1))
    c.draw.line([(x, y + 6 * scale) for x, y in wash], fill=look.wash_key, width=1)


def _jetty_edges(jetty: Jetty, scale: float) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    """The jetty's left and right edges, at this size."""
    return (tuple((x * scale, y * scale) for x, y in jetty.left), tuple((x * scale, y * scale) for x, y in jetty.right))


def _calm_mask(size: tuple[int, int], jetty: Jetty, scale: float = 1.0) -> np.ndarray:
    """The sheltered water left of the jetty, running under the rocks so no wave shows between."""
    left = _jetty_edges(jetty, scale)[0]
    return _poly_mask(size, [(0, left[0][1]), *((x + 5 * scale, y) for x, y in left), (0, left[-1][1])])


def _calm(c: InkCanvas, jetty: Jetty, look: Look, scale: float = 1.0) -> None:
    """The harbor mouth behind the jetty: flat water, no waves and no beach."""
    _paste(c, _calm_mask(c.size, jetty, scale), look.trough)


def _edge_x(edge: tuple[tuple[float, float], ...], y: float) -> float:
    return float(np.interp(y, [p[1] for p in edge], [p[0] for p in edge]))


def _jetty(c: InkCanvas, jetty: Jetty, look: Look, scale: float = 1.0) -> None:
    """The jetty running out from the bottom of the picture, its boulders smaller toward the tip."""
    left, right = _jetty_edges(jetty, scale)
    top, bottom = left[0][1], left[-1][1]
    rng = np.random.default_rng(21)

    def nearness(y: float) -> float:
        return float(np.clip((y - top) / (bottom - top), 0.0, 1.0))

    def rough(edge: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
        ys = np.arange(top, bottom + 1, 7.0 * scale)
        return [(_edge_x(edge, y) + rng.uniform(-1, 1) * (1 + 4 * nearness(y)) * scale, float(y)) for y in ys]

    shape = _poly_mask(c.size, rough(right) + rough(left)[::-1])
    layer = Image.new("L", c.size, 0)  # between the boulders, black
    pen = ImageDraw.Draw(layer)
    for _ in range(round(260 * scale)):  # at half the size, half as many, so they still read as boulders
        y = top - 5 * scale + (bottom - top + 15 * scale) * rng.uniform() ** 1.6
        x = rng.uniform(_edge_x(left, y) - 8 * scale, _edge_x(right, y) + 8 * scale)
        size = (0.25 + 0.75 * nearness(y)) * scale
        a, b = rng.uniform(9, 22) * size, rng.uniform(6, 15) * size
        ang = rng.uniform(0, math.pi)
        pts = [(x + a * math.cos(t) * math.cos(ang) - b * math.sin(t) * math.sin(ang),
                y + a * math.cos(t) * math.sin(ang) + b * math.sin(t) * math.cos(ang))
               for t in np.linspace(0, 2 * math.pi, 7)]
        pen.polygon(pts, fill=0, outline=255)
    _paste(c, shape, BLACK)
    _paste(c, shape & (np.asarray(layer) == 255), look.rock_edge)


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    m = sea_model(scene.draw_for, ctx, scene.updated, scene.waves, scene.clouds)
    c = InkCanvas(ctx.canvas, "white")
    loc, place = ctx.config.location, place_for(ctx)
    draw(c, m, ctx.config.sky.center_azimuth_deg, places.skyline_for(loc.lat, loc.lon, ctx.data_dir),
         jetty=None if place is None else place.jetty)
    return c.indices()
