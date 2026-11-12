"""The harbor chart (scene kind `harbor`): lower Newport Bay from above as a nautical chart,
with today's tide, wind, swell, sun and moon on it.

Look approved by the Owner as a mockup on 2026-11-04: north up; land buff (yellow dots),
harbor water blue dots, the open ocean much paler blue dots (paler than the mockup: the
Owner found its text hard to read on the ocean, so words on water also get a white halo), salt marsh green dots, beaches
denser yellow, a 1-px black shoreline, black piers and jetties; red triangles and green
squares with their numbers for the channel beacons, the safe-water buoy off the entrance
(bottom right, Owner); names; a title block top right; a compass rose bottom left with
sunrise and sunset ticked on its rim and, when they're up, the Sun on the rim and the Moon (a
phase disc) just inside the inner circle, each at its azimuth now: apart, so a new moon beside
the Sun can't pass for an eclipse. A solar eclipse seen from here puts the tide band's eclipse
icon (a black disc in a yellow ring) in the Sun's place, with no Moon. When the Sun's and Moon's
words would collide they share one label ("Sun 39°, Moon 24° up"). A field of thin wind arrows over the ocean, with
tail feathers (one per 10 mph), longer the harder it blows and laid out in lines along the wind;
always two bigger wavy swell arrows offshore (Owner, 2026-11-05: the swell blue, the wind another
ink, green from previews, since red already means the measured water and the port-hand beacons); swell and wind
in words in the title block (Owner), keyed with small copies of their arrows, in two lines each so the words stay large; a one-nautical-mile scale bar; latitude and longitude
ticks on the border.

The view comes from config: `harbor.south` and `harbor.north` are the map's bottom and top,
above the tide band, and the width follows from its shape and `harbor.center_lon`. A view reaching past what the place's `harbor.json`
covers is shrunk, then moved, to fit. Marks, names and soundings follow the view; the title
block, rose and scale bar stay where they are on the screen, and depths under
them are left out.

Today's tide runs along the bottom in a band (Owner, 2026-11-05, over a box in the title block
and one over the ocean), by default from 24 h back to 4 days ahead: the day screen's own chart
(`scenes/tides.tide_chart`) at a smaller size, so the same rules apply. Its span and labels are
settings of its own (`harbor.chart_days_behind`, `chart_days_ahead`, `label_tides`, `label_times`,
`label_heights`), apart from the day screen's. Blue the tide table,
red the measured water, red dots where it's likely to go, night as dots, day names and
new and full moons (`scenes/tides.py`, PRD D11). Measured is an estimate for the Wedge from
the Los Angeles gauge, not a reading there.

Soundings are NOAA ENC depths in feet below MLLW, thinned on a grid, never typed by hand. On
the ocean grid they're coarser than in the harbor. Not for navigation: the dashboard says so.
The credits and what the depths mean are on the dashboard too, not the panel, where they were
too small to read (Owner, 2026-11-06).

The green fringe along the marsh widens as the tide drops, from the mockup; it's a picture
of mud showing, not surveyed data.

Missing caches leave out what they feed; the chart itself needs only the place's harbor chart
(`places.harbor_for`: `harbor.json` and `place.json` in its folder, `data/places/wedge/` for the
Wedge). Where there's none it raises `places.NoPlaceData`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np
from PIL import Image, ImageDraw

from photopainter import places
from photopainter.config import Harbor as HarborSettings
from photopainter.render.draw import INK, InkCanvas, font, line_height, text_width
from photopainter.scenes.base import Scene, SceneContext
from photopainter.scenes.tides import (
    BAYER8, ChartStyle, DayModel, clock, compass, day_model, disc, ft, moon_disc, paint, tide_chart, whole,
)

if TYPE_CHECKING:
    from photopainter.astro.eclipse import SolarEclipse

BLACK, WHITE, YELLOW, RED, BLUE, GREEN = (INK[n] for n in ("black", "white", "yellow", "red", "blue", "green"))
WIND_INK, SWELL_INK = GREEN, BLUE  # red is taken: the measured water and the port-hand beacons

W, H = 800, 480  # the chart is drawn at this size; a portrait canvas gets it cropped (PRD Q6)
BAND_H = 120  # the tide, along the bottom
MAP_H = H - BAND_H
MIN_LAT_SPAN = 0.005
FAR_SOUTH = 2.0  # degrees below the view the ocean polygon closes at

LAND_DOTS = 0.30
HARBOR_DOTS = 0.50
OCEAN_DOTS = 0.125  # one dot in eight: paler than the mockup's 22 %, so text reads over it
MARSH_DOTS = 0.50
FRINGE_DOTS = 0.30
BEACH_DOTS = 0.70
# The marsh's fringe starts below the place's `marsh_full_ft` (none without one)…
FRINGE_PX_PER_FT = 2.2  # …and widens this much per foot
HARBOR_SOUNDING_CELL = 40  # px between depths in the harbor…
OCEAN_SOUNDING_CELL = 88  # …and on the ocean
SOUNDING_SIZE = 11

# Names, where they sit and which are bold come from the place's data (places.py, place.json's
# `harbor`): place_names, landmarks, island_nudge, island_lines, bold_islands, big_piers.

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class View:
    """Which part of the Earth the map shows, and how."""

    west: float
    top: float  # the latitude at y = 0
    deg_per_px_lon: float
    deg_per_px_lat: float
    height: int  # of the map, in pixels from the top; the tide band takes the rest
    south: float  # the latitude at y = height

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.west) / self.deg_per_px_lon, (self.top - lat) / self.deg_per_px_lat

    def contains(self, lat: float, lon: float) -> bool:
        x, y = self.xy(lat, lon)
        return 0 <= x < W and 0 <= y < self.height

    @property
    def px_per_km(self) -> float:
        return 1.0 / (self.deg_per_px_lat * 110.95)


def make_view(s: HarborSettings, bounds: dict[str, float], fallback: HarborSettings | None = None) -> View:
    """The view for settings `s`, fitted inside what the data covers. A view that misses the
    data altogether (settings made for another place) takes `fallback`, the place's own view."""
    south_b, north_b, west_b, east_b = bounds["south"], bounds["north"], bounds["west"], bounds["east"]
    if fallback is not None and _misses(s, bounds):
        s = fallback
    south, north = sorted((s.south, s.north))
    span = max(north - south, MIN_LAT_SPAN)
    cos_lat = math.cos(math.radians((south + north) / 2))
    # Shrink until it fits, keeping the south bound's place as far as possible.
    span = min(span, north_b - south_b, (east_b - west_b) * cos_lat * MAP_H / W)
    lon_span = span * (W / MAP_H) / cos_lat
    south = min(max(south, south_b), north_b - span)
    center = min(max(s.center_lon, west_b + lon_span / 2), east_b - lon_span / 2)
    return View(west=center - lon_span / 2, top=south + span, deg_per_px_lon=lon_span / W,
                deg_per_px_lat=span / MAP_H, height=MAP_H, south=south)


def _misses(s: HarborSettings, bounds: dict[str, float]) -> bool:
    """Whether the view `s` asks for lies wholly outside `bounds`."""
    south, north = sorted((s.south, s.north))
    span = max(north - south, MIN_LAT_SPAN)
    half_lon = span * (W / MAP_H) / math.cos(math.radians((south + north) / 2)) / 2
    return (north < bounds["south"] or south > bounds["north"]
            or s.center_lon + half_lon < bounds["west"] or s.center_lon - half_lon > bounds["east"])


# --- data ------------------------------------------------------------------------------------


def harbor_data(ctx: SceneContext) -> dict[str, Any]:
    """The place's harbor chart (places.harbor_for): `harbor.json` with place.json's choices
    under "place". Raises places.NoPlaceData where there's none."""
    loc = ctx.config.location
    data = places.harbor_for(loc.lat, loc.lon, ctx.data_dir)
    if data is None:
        raise places.NoPlaceData(f"no harbor chart for {loc.name} ({loc.lat}, {loc.lon})")
    return data


def place_view(data: dict[str, Any]) -> HarborSettings | None:
    """The place's own view (place.json's `harbor.view`), or None."""
    view = data.get("place", {}).get("view")
    try:
        return HarborSettings(south=float(view["south"]), north=float(view["north"]),
                              center_lon=float(view["center_lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def title(data: dict[str, Any]) -> str:
    """The chart's title: the place's, else the harbor file's name."""
    return str(data.get("place", {}).get("title") or data.get("name") or "")


# --- ink helpers -----------------------------------------------------------------------------


_YY, _XX = np.mgrid[0:H, 0:W]
THRESH = BAYER8[_YY % 8, _XX % 8]


def _pts(view: View, ring: Iterable[list[float]]) -> list[tuple[float, float]]:
    return [view.xy(p[0], p[1]) for p in ring]


def poly_mask(polys: Iterable[list[tuple[float, float]]], holes: Iterable[list[tuple[float, float]]] = ()) -> np.ndarray:
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    for p in polys:
        if len(p) >= 3:
            d.polygon(p, fill=255)
    for p in holes:
        if len(p) >= 3:
            d.polygon(p, fill=0)
    return np.asarray(mask) > 0


def polys_mask(view: View, polys: Iterable[dict]) -> np.ndarray:
    """Polygons with their own holes ({outer, holes}), each cut only from itself."""
    mask = np.zeros((H, W), bool)
    for poly in polys:
        mask |= poly_mask([_pts(view, poly["outer"])], [_pts(view, h) for h in poly.get("holes", [])])
    return mask


def dots(c: InkCanvas, mask: np.ndarray, ink: int, density: float = 1.0, base: int | None = None) -> None:
    """`ink` on `mask` at `density` (ordered dots); `base` fills it solid first."""
    if base is not None:
        paint(c, 0, 0, mask, base)
    paint(c, 0, 0, mask & (THRESH < density) if density < 1.0 else mask, ink)


def grow(mask: np.ndarray, px: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(px):
        out[1:] |= out[:-1].copy()
        out[:-1] |= out[1:].copy()
        out[:, 1:] |= out[:, :-1].copy()
        out[:, :-1] |= out[:, 1:].copy()
    return out


def label(c: InkCanvas, xy: tuple[float, float], s: str, size: int, ink: int = BLACK, bold: bool = False,
          anchor: str = "la", halo: int | None = None) -> None:
    f = font(size, bold)
    x, y = round(xy[0]), round(xy[1])
    if halo is not None:  # 2 px all round: 1 px still leaves dots touching the letters
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if (dx or dy) and abs(dx) + abs(dy) < 4:
                    c.draw.text((x + dx, y + dy), s, font=f, fill=halo, anchor=anchor)
    c.draw.text((x, y), s, font=f, fill=ink, anchor=anchor)


def label_rect(xy: tuple[float, float], s: str, size: int, anchor: str = "la", bold: bool = False) -> Rect:
    x0, y0, x1, y1 = font(size, bold).getbbox(s, anchor=anchor)
    return (xy[0] + x0, xy[1] + y0, xy[0] + x1, xy[1] + y1)


def rotated_label(c: InkCanvas, cx: float, cy: float, s: str, size: int, angle: float, ink: int = BLACK,
                  bold: bool = False) -> list[Rect]:
    """Text turned `angle` degrees about (cx, cy). Returns small boxes along it: its whole
    bounding box would count a lot of empty corners as taken."""
    f = font(size, bold)
    w, h = text_width(s, f) + 8, line_height(f) + 8
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    d.text((4, 4), s, font=f, fill=255)
    img = img.rotate(angle, expand=True, resample=Image.NEAREST)
    x0, y0 = round(cx - img.width / 2), round(cy - img.height / 2)
    c.image.paste(ink, (x0, y0, x0 + img.width, y0 + img.height), mask=img.point(lambda v: 255 if v > 127 else 0))
    return along(cx, cy, text_width(s, f) / 2, angle, size)


def along(cx: float, cy: float, half: float, angle: float, size: int) -> list[Rect]:
    ux, uy = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
    return [(cx + ux * t - size, cy + uy * t - size, cx + ux * t + size, cy + uy * t + size)
            for t in np.linspace(-half, half, max(2, round(2 * half / size)))]


def overlaps(rect: Rect, rects: Iterable[Rect], margin: float = 0.0) -> bool:
    return any(rect[0] - margin < o[2] and rect[2] + margin > o[0] and rect[1] - margin < o[3]
               and rect[3] + margin > o[1] for o in rects)


# --- the map ---------------------------------------------------------------------------------


@dataclass
class Map:
    ocean: np.ndarray
    harbor: np.ndarray
    land: np.ndarray


def draw_map(c: InkCanvas, data: dict[str, Any], view: View, tide_ft: float | None) -> Map:
    coast = _pts(view, data["ocean"])
    bottom = view.xy(view.south - FAR_SOUTH, 0.0)[1]
    ocean = poly_mask([coast + [(coast[-1][0], bottom), (coast[0][0], bottom)]])
    dots(c, ocean, BLUE, OCEAN_DOTS)
    dots(c, ~ocean, YELLOW, LAND_DOTS)

    harbor = polys_mask(view, data["harbor"])
    other_water = polys_mask(view, data.get("water", [])) & ~ocean
    dots(c, other_water & ~harbor, BLUE, HARBOR_DOTS, base=WHITE)
    dots(c, harbor, BLUE, HARBOR_DOTS, base=WHITE)

    marsh = polys_mask(view, data["marsh"])
    marsh_full_ft = data.get("place", {}).get("marsh_full_ft")
    if tide_ft is not None and marsh_full_ft is not None:
        spread = round(max(0.0, marsh_full_ft - tide_ft) * FRINGE_PX_PER_FT)
        dots(c, grow(marsh, spread) & harbor & ~marsh, GREEN, FRINGE_DOTS)
    dots(c, marsh, GREEN, MARSH_DOTS, base=WHITE)
    dots(c, polys_mask(view, data["beaches"]), YELLOW, BEACH_DOTS, base=WHITE)

    water = ocean | harbor | other_water
    inside = water.copy()
    inside[1:] &= water[:-1]
    inside[:-1] &= water[1:]
    inside[:, 1:] &= water[:, :-1]
    inside[:, :-1] &= water[:, 1:]
    dots(c, water & ~inside, BLACK)

    big_piers = set(data.get("place", {}).get("big_piers", ()))
    for pier in data["piers"]:
        width = 5 if pier["kind"] != "pier" else 4 if pier.get("name") in big_piers else 2
        c.draw.line(_pts(view, pier["line"]), fill=BLACK, width=width)
    return Map(ocean, harbor | other_water, ~water)


def draw_marks(c: InkCanvas, data: dict[str, Any], view: View) -> list[Rect]:
    """The beacons and the buoy. Returns where they went."""
    taken: list[Rect] = []
    for mark in data["marks"]:
        if not view.contains(mark["lat"], mark["lon"]):
            continue
        x, y = view.xy(mark["lat"], mark["lon"])
        if mark["kind"] == "safe_water":
            disc(c, x, y, 6, RED, BLACK, 1)
            disc(c, x, y, 2.5, WHITE)
            label(c, (x + 10, y), mark["name"], 11, bold=True, anchor="lm", halo=WHITE)
            taken.append(label_rect((x + 10, y), mark["name"], 11, "lm", True))
        else:
            if mark["kind"] == "red":
                c.draw.polygon([(x, y - 7), (x - 6, y + 5), (x + 6, y + 5)], fill=RED, outline=BLACK)
            else:
                c.draw.rectangle([x - 5, y - 5, x + 5, y + 5], fill=GREEN, outline=BLACK)
            label(c, (x + 8, y), mark["name"], 10, bold=True, anchor="lm", halo=WHITE)
            taken.append(label_rect((x + 8, y), mark["name"], 10, "lm", True))
        taken.append((x - 7, y - 8, x + 7, y + 7))
    return taken


def draw_names(c: InkCanvas, data: dict[str, Any], view: View, blocked: list[Rect]) -> list[Rect]:
    """Names, biggest first. On a map zoomed far out, one that would land on another (or on
    a fixed part of the screen) is left out."""
    taken: list[Rect] = []
    busy = lambda r: overlaps(r, blocked) or overlaps(r, taken)  # noqa: E731
    place = data.get("place", {})
    nudges = {k: tuple(v) for k, v in place.get("island_nudge", {}).items()}
    island_lines = {k: tuple(v) for k, v in place.get("island_lines", {}).items()}
    bold_islands = set(place.get("bold_islands", ()))
    for p in place.get("place_names", ()):
        name, lat, lon, size, bold, angle, ink = (p["name"], p["lat"], p["lon"], p["size"], p.get("bold", True),
                                                  p.get("angle", 0.0), p.get("ink", "black"))
        if not view.contains(lat, lon):
            continue
        x, y = view.xy(lat, lon)
        if angle:
            spots = along(x, y, text_width(name, font(size, bold)) / 2, angle, size)
            on_map = all(r[0] >= 0 and r[2] <= W and r[1] >= 0 and r[3] <= view.height for r in spots)
            if on_map and not any(busy(r) for r in spots):
                taken += rotated_label(c, x, y, name, size, angle, INK[ink], bold)
        elif not busy(label_rect((x, y), name, size, "mm", bold)):
            label(c, (x, y), name, size, INK[ink], bold, "mm", halo=WHITE if ink == "blue" else None)
            taken.append(label_rect((x, y), name, size, "mm", bold))
    for p in place.get("landmarks", ()):
        name, lat, lon, dx, dy, anchor, bold = (p["name"], p["lat"], p["lon"], p.get("dx", 0), p.get("dy", 0),
                                                p.get("anchor", "lm"), p.get("bold", False))
        if view.contains(lat, lon):
            x, y = view.xy(lat, lon)
            size = 12 if bold else 11
            r = label_rect((x + dx, y + dy), name, size, anchor, bold)
            if not busy(r):
                label(c, (x + dx, y + dy), name, size, bold=bold, anchor=anchor, halo=WHITE)
                taken.append(r)
    for island in data["islands"]:
        if view.contains(island["lat"], island["lon"]):
            name = island["name"]
            dx, dy = nudges.get(name, (0, 0))
            x, y = view.xy(island["lat"], island["lon"])
            size = 12 if name in bold_islands else 11
            lines = island_lines.get(name, (name,))
            spots = [(x + dx, y + dy + (i - (len(lines) - 1) / 2) * (size + 1)) for i in range(len(lines))]
            rects = [label_rect(at, line, size, "mm", name in bold_islands) for at, line in zip(spots, lines)]
            if any(busy(r) for r in rects):
                continue
            for at, line in zip(spots, lines):
                label(c, at, line, size, bold=name in bold_islands, anchor="mm")
            taken += rects
    return taken


def pick_soundings(data: dict[str, Any], view: View, the_map: Map, taken: list[Rect]) -> list[tuple[float, float, str]]:
    """One depth per grid cell, nearest the cell's center, well inside the water and clear
    of everything in `taken` and of each other: two side by side would read as one long number."""
    # In the water by a few pixels, so a number doesn't sit on the shore.
    clear_land = ~grow(~(the_map.ocean | the_map.harbor), 6)
    best: dict[tuple[bool, int, int], tuple[float, float, float, str]] = {}
    for lat, lon, depth in data.get("soundings", []):
        x, y = view.xy(lat, lon)
        ix, iy = int(x), int(y)
        if not (12 <= ix < W - 12 and 12 <= iy < view.height - 12) or not clear_land[iy, ix]:
            continue
        in_harbor = bool(the_map.harbor[iy, ix])
        s = str(depth)
        if overlaps(label_rect((x, y), s, SOUNDING_SIZE, "mm"), taken, 2):
            continue
        cell = HARBOR_SOUNDING_CELL if in_harbor else OCEAN_SOUNDING_CELL
        gx, gy = ix // cell, iy // cell
        away = math.hypot(x - (gx + 0.5) * cell, y - (gy + 0.5) * cell)
        key = (in_harbor, gx, gy)
        if key not in best or (away, x, y) < best[key][:3]:
            best[key] = (away, x, y, s)
    kept: list[tuple[float, float, str]] = []
    rects: list[Rect] = []
    for _, x, y, s in sorted(best.values(), key=lambda b: (b[1], b[2])):
        rect = label_rect((x, y), s, SOUNDING_SIZE, "mm")
        if not overlaps(rect, rects, 4):
            kept.append((x, y, s))
            rects.append(rect)
    return kept


# --- conditions ------------------------------------------------------------------------------


def nice_time(t: datetime) -> str:
    """2:10 pm."""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def tide_now(m: DayModel) -> float | None:
    if m.tide is None:
        return None
    return float(m.tide.height + m.gap_at(m.now.timestamp())[0])


def next_tide_line(m: DayModel) -> str | None:
    """'Low 0.9 ft at 6:48 pm': the soonest next high or low, at the height the water is
    likely to reach."""
    if m.tide is None:
        return None
    options = [e for e in (m.tide.next_high, m.tide.next_low) if e is not None]
    if not options:
        return None
    e = m.with_gap(min(options, key=lambda e: e.t))
    return f"{'High' if e.high else 'Low'} {ft(e.v)} ft at {nice_time(e.t)}"


WIND_SHORTEST, WIND_LONGEST = 14.0, 52.0  # px; the longest arrive at 20 mph


def wind_length(mph: float | None, scale: float = 1.0) -> float:
    """How long a wind arrow is: 5 mph is 19 px, 10 mph 30, 20 mph and up 52."""
    return scale * min(max(8.0 + 2.2 * (mph or 0.0), WIND_SHORTEST), WIND_LONGEST)


def _heading(from_deg: float) -> tuple[float, float, float, float]:
    """(forward x, y, sideways x, y) in pixels for something coming from `from_deg`, north up,
    the same way as `tides.flow_arrow`."""
    a = math.radians(from_deg + 180)
    fx, fy = math.sin(a), -math.cos(a)
    return fx, fy, -fy, fx


def _feathers(size: float) -> tuple[int, int, int]:
    """(px between feathers, how far each reaches back and out, line width) on a wind arrow:
    smaller on a shorter one, or they'd be most of it."""
    reach = round(min(max(size * 0.2, 4.0), 7.0))
    return max(3, reach - 2), reach, 2 if reach >= 5 else 1


def _wind_head(size: float) -> float:
    """How long a wind arrow's head is. A longer arrow doesn't get a bigger one: it stays thin."""
    return min(max(5.0, size * 0.28), 10.0)


def wind_arrow(c: InkCanvas, cx: float, cy: float, size: float, from_deg: float, mph: float, *,
               fill: int = WIND_INK) -> None:
    """Wind: a thin arrow with feathers on its tail, one for every 10 mph (at least one,
    at most four), like an arrow in flight and like the barbs on a weather map."""
    fx, fy, sx, sy = _heading(from_deg)
    half = size / 2
    tail = (cx - fx * half, cy - fy * half)
    tip = (cx + fx * half, cy + fy * half)
    c.draw.line([tail, tip], fill=fill, width=2)
    head = _wind_head(size)
    back = (tip[0] - fx * head, tip[1] - fy * head)
    wide = head * 0.6
    c.draw.polygon([tip, (back[0] + sx * wide, back[1] + sy * wide), (back[0] - sx * wide, back[1] - sy * wide)],
                   fill=fill)
    for i in range(min(4, max(1, int(mph // 10) + 1))):
        step, reach, width = _feathers(size)
        at = (tail[0] + fx * i * step, tail[1] + fy * i * step)
        for side in (1, -1):
            c.draw.line([at, (at[0] - fx * reach + side * sx * reach, at[1] - fy * reach + side * sy * reach)],
                        fill=fill, width=width)


def swell_arrow(c: InkCanvas, cx: float, cy: float, size: float, from_deg: float, *, fill: int = SWELL_INK) -> None:
    """Swell: a wavy shaft ending in a solid head."""
    fx, fy, sx, sy = _heading(from_deg)
    half = size / 2
    head = size * 0.34
    shaft_end = half - head * 0.8
    pts = []
    for t in np.linspace(-half, shaft_end, 24):
        wobble = math.sin((t + half) / (size * 0.62) * 2 * math.pi) * size * 0.09
        pts.append((cx + fx * t + sx * wobble, cy + fy * t + sy * wobble))
    c.draw.line(pts, fill=fill, width=max(2, round(size * 0.1)))
    base = (cx + fx * (half - head), cy + fy * (half - head))
    wide = size * 0.25
    c.draw.polygon([(cx + fx * half, cy + fy * half), (base[0] + sx * wide, base[1] + sy * wide),
                    (base[0] - sx * wide, base[1] - sy * wide)], fill=fill)


SWELL_SIZE = 56
WIND_ALONG = WIND_LONGEST + 14  # px between arrows in a line along the wind, so the longest nearly touch…
WIND_ACROSS = 30  # …and between lines, every other line shifted half a step
WIND_GAP = 3  # at least this between one arrow's tip and the next one's feathers…
WIND_LINES_APART = 20  # …unless they're at least this far apart across the wind
WIND_EDGE = 6  # px kept clear along the edges of the map
WIND_CLEAR = 3  # px kept clear around names, marks and the like: words have a halo
WIND_NUDGES = ((0, 0), (0, -8), (0, 8), (-14, 0), (14, 0), (-14, -8), (14, -8), (-14, 8), (14, 8))  # along, across


def _free_water(the_map: Map, view: View, blocked: Iterable[Rect]) -> np.ndarray:
    """Open water, away from the map's edges and everything in `blocked`, as a mask."""
    free = the_map.ocean.copy()
    free[max(0, view.height - WIND_EDGE):] = False
    free[:WIND_EDGE] = False
    free[:, :WIND_EDGE] = False
    free[:, W - WIND_EDGE:] = False
    for x0, y0, x1, y1 in blocked:
        m = WIND_CLEAR
        free[max(0, math.floor(y0) - m):max(0, math.ceil(y1) + m), max(0, math.floor(x0) - m):max(0, math.ceil(x1) + m)] = False
    return free


def _wind_wide(size: float) -> float:
    """How far a wind arrow reaches to either side of its shaft."""
    return max(_feathers(size)[1], _wind_head(size) * 0.6) + 1


def _wind_footprint(size: float, from_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Points at most 4 px apart, as offsets from an arrow's middle, covering all of it: feathers,
    shaft and head. A name or a depth is bigger than that, so it can't slip between them."""
    fx, fy, sx, sy = _heading(from_deg)
    half, wide = size / 2, _wind_wide(size)
    back = half + _feathers(size)[1]
    us, vs = np.meshgrid(np.linspace(-back, half, math.ceil((back + half) / 4) + 1),
                         np.linspace(-wide, wide, math.ceil(2 * wide / 4) + 1))
    return fx * us + sx * vs, fy * us + sy * vs


def _wind_fits(free: np.ndarray, x: float, y: float, footprint: tuple[np.ndarray, np.ndarray]) -> bool:
    """Does the whole arrow at (x, y) lie on `free`?"""
    xs = np.rint(x + footprint[0]).astype(int)
    ys = np.rint(y + footprint[1]).astype(int)
    if xs.min() < 0 or ys.min() < 0 or xs.max() >= free.shape[1] or ys.max() >= free.shape[0]:
        return False
    return bool(free[ys, xs].all())


def wind_rects(x: float, y: float, size: float, from_deg: float) -> list[Rect]:
    """An arrow as a row of small boxes along its shaft, so depths can sit between the lines
    of a field blowing on a diagonal, where one box around it would cover them too."""
    fx, fy, _, _ = _heading(from_deg)
    half, wide = size / 2, _wind_wide(size)
    centers = np.arange(-half - _feathers(size)[1] + wide, half + wide, 2 * wide)
    return [(x + fx * t - wide, y + fy * t - wide, x + fx * t + wide, y + fy * t + wide) for t in centers]


def wind_spots(m: DayModel, the_map: Map, view: View, blocked: list[Rect], scale: float = 1.0) -> list[tuple[int, int]]:
    """Where the wind arrows go: an even field over open water, in lines running the way the
    wind blows, so that a light day is a few short dashes and a windy one nearly unbroken
    streaks. Each arrow moves a little if that clears a name or the shore, but never to where
    it bunches up with another. No randomness: same picture, same arrows. `scale` shrinks the
    field and the arrows together, for a map drawn smaller."""
    w = m.weather
    if w is None or w.wind_dir is None or not w.wind or w.wind < 1:
        return []
    size = wind_length(w.wind, scale)
    footprint = _wind_footprint(size, w.wind_dir)
    fx, fy, sx, sy = _heading(w.wind_dir)
    free = _free_water(the_map, view, blocked)
    along, across = WIND_ALONG * scale, WIND_ACROSS * scale
    end_to_end = size + _feathers(size)[1] + WIND_GAP * scale
    cx, cy = W / 2, view.height / 2
    reach = math.hypot(W, view.height)
    placed: list[tuple[float, float]] = []  # along, across the wind from the middle of the map
    spots: list[tuple[int, int]] = []
    for line in range(-int(reach / across), int(reach / across) + 1):
        shift = along / 2 if line % 2 else 0.0
        for step in range(-int(reach / along), int(reach / along) + 1):
            for du, dv in WIND_NUDGES:
                u, v = step * along + shift + du * scale, line * across + dv * scale
                x, y = round(cx + fx * u + sx * v), round(cy + fy * u + sy * v)
                if not (0 <= x < W and 0 <= y < view.height and free[y, x]):
                    continue
                if not _wind_fits(free, x, y, footprint):
                    continue
                if any(abs(u - a) < end_to_end and abs(v - b) < WIND_LINES_APART * scale for a, b in placed):
                    continue
                placed.append((u, v))
                spots.append((x, y))
                break
    return spots


def conditions_lines(m: DayModel) -> tuple[list[str], list[str]]:
    """The title block's words: the swell's two lines, then the wind's and the water's. Old
    swell says "as of" on its second line, without "from" to make room; old wind on the water's."""
    swell: list[str] = []
    s = m.swell
    if s is not None and s.swell_height is not None:
        period = f" at {whole(s.swell_period)} s" if s.swell_period is not None else ""
        swell.append(f"Swell {ft(s.swell_height)} ft{period}")
        where = ""
        if s.swell_dir is not None:
            where = f"{compass(s.swell_dir)}, " if s.stale else f"from {compass(s.swell_dir)}, "
        swell.append(f"{where}offshore" + (f", as of {clock(s.fetched_at)}" if s.stale else ""))
    wind: list[str] = []
    below: list[str] = []
    w = m.weather
    if w is not None and w.wind is not None:
        if round(w.wind) == 0:
            wind.append("Calm")
        else:
            wind.append(f"Wind {whole(w.wind)} mph" + (f" from {compass(w.wind_dir)}" if w.wind_dir is not None else ""))
        if w.stale:
            below.append(f"as of {clock(w.fetched_at)}")
    if s is not None and s.water_temp is not None:
        below.append(f"Water {whole(s.water_temp)}°")
    if below:
        wind.append(" · ".join(below))
    return swell, wind


CONDITION_SIZE = 13
CONDITION_ROW = 15  # px from one line to the next
KEY_W = 20  # the little arrow in front of each group: the map's arrows, explained


def swell_rect(x: float, y: float, size: float) -> Rect:
    """The arrow on the map (its words are in the title block)."""
    half = size / 2
    return (x - half, y - half, x + half, y + half)


def swell_spot(wanted: tuple[float, float], the_map: Map, blocked: list[Rect], height: int,
               size: float = SWELL_SIZE) -> tuple[float, float]:
    """`wanted`, or the nearest place to it with all of the arrow on open water and clear of `blocked`; on a view with no such place, `wanted`."""
    def fits(x: float, y: float) -> bool:
        x0, y0, x1, y1 = swell_rect(x, y, size)
        if x0 < 4 or y0 < 4 or x1 >= W - 4 or y1 >= height - 4 or overlaps((x0, y0, x1, y1), blocked):
            return False
        return bool(the_map.ocean[int(y0):int(y1) + 1, int(x0):int(x1) + 1].all())

    spots = [(wanted[0] + dx, wanted[1] + dy) for dx in range(-345, 346, 15) for dy in range(-465, 466, 15)]
    spots.sort(key=lambda p: (math.hypot(p[0] - wanted[0], p[1] - wanted[1]), p))
    return next((p for p in spots if fits(*p)), wanted)


def swell_spots(m: DayModel, wanted: Iterable[tuple[float, float]], the_map: Map, blocked: list[Rect], height: int,
                size: float = SWELL_SIZE) -> list[tuple[float, float]]:
    """A place for each of the swell arrows, or none without a swell to draw."""
    s = m.swell
    if s is None or s.swell_dir is None or s.swell_height is None:
        return []
    spots: list[tuple[float, float]] = []
    for at in wanted:
        taken = blocked + [swell_rect(*spot, size) for spot in spots]
        spots.append(swell_spot(at, the_map, taken, height, size))
    return spots


def draw_on_water(c: InkCanvas, m: DayModel, data: dict[str, Any], view: View, the_map: Map, taken: list[Rect],
                  swell_at: Iterable[tuple[float, float]], *, swell_size: float = SWELL_SIZE,
                  sounding_size: int = SOUNDING_SIZE, wind_scale: float = 1.0) -> None:
    """What goes over the water, on the full chart and the combo's alike: the swell's arrows,
    the wind's around them, and depths in the gaps."""
    swells = swell_spots(m, swell_at, the_map, taken, view.height, swell_size)
    taken = taken + [swell_rect(x, y, swell_size) for x, y in swells]
    winds = wind_spots(m, the_map, view, taken, wind_scale)
    size = wind_length(m.weather.wind, wind_scale) if winds else 0.0
    taken += [rect for x, y in winds for rect in wind_rects(x, y, size, m.weather.wind_dir)]
    for x, y, s in pick_soundings(data, view, the_map, taken):
        label(c, (x, y), s, sounding_size, anchor="mm", halo=WHITE)
    for x, y in winds:
        wind_arrow(c, x, y, size, m.weather.wind_dir, m.weather.wind)
    for x, y in swells:
        swell_arrow(c, x, y, swell_size, m.swell.swell_dir)


def draw_conditions(c: InkCanvas, m: DayModel, x: int, y: int, right: int) -> None:
    """Swell and wind in the title block, two lines each so the words stay large in its width,
    after a small copy of the arrow they explain, pointing the same way. `y` is the middle of
    the first line. A line too long for the width (an old reading: "as of") gets smaller."""
    swell, wind = conditions_lines(m)
    s, w = m.swell, m.weather
    room = right - x - KEY_W
    for first_row, lines in ((0, swell), (2, wind)):
        for i, line in enumerate(lines):
            bold = first_row == 0 and i == 0
            size = next((z for z in range(CONDITION_SIZE, 9, -1) if text_width(line, font(z, bold)) <= room), 9)
            label(c, (x + KEY_W, y + CONDITION_ROW * (first_row + i)), line, size, bold=bold, anchor="lm")
    if swell and s.swell_dir is not None:
        swell_arrow(c, x + 7, y + CONDITION_ROW // 2, 20, s.swell_dir)
    if w is not None and w.wind_dir is not None and round(w.wind or 0):
        wind_arrow(c, x + 7, y + CONDITION_ROW * 2, 17, w.wind_dir, w.wind)


# --- rose ------------------------------------------------------------------------------------


RIM_WORD_SIZE = 11
ROSE_CLEAR = 5  # px of open water kept around the rim


def _rim(rx: float, ry: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return rx + math.sin(a) * r, ry - math.cos(a) * r


def _altaz(m: DayModel, times: list[datetime]) -> tuple[np.ndarray, np.ndarray] | None:
    from photopainter.astro import EphemerisMissing
    from photopainter.astro.sky import sun_altaz

    try:
        return sun_altaz(times, m.lat, m.lon)
    except EphemerisMissing:
        return None


def _keep_on(xy: tuple[float, float], s: str, size: int, height: int) -> tuple[float, float]:
    half = text_width(s, font(size)) / 2 + 3
    return (min(max(xy[0], half), W - half), min(max(xy[1], 8), height - 8))


@dataclass(frozen=True)
class Rim:
    """What's on the compass rose's rim today, worked out once: the same places draw it and
    keep the wind and the depths off it."""

    ticks: tuple[float, ...] = ()  # azimuths of sunrise and sunset
    sun_az: float | None = None
    moon_az: float | None = None
    words: tuple[tuple[tuple[float, float], str], ...] = ()  # the middle of each label, and its text
    eclipse: SolarEclipse | None = None  # going on here now: an eclipse in the Sun's place, no Moon


SUN_R, SUN_REACH, MOON_R = 8, 14, 8  # the Sun's disc and rays, and the Moon with its outline, in px
RING = 14  # the band of ticks between the rim and the inner circle


def moon_at(rx: float, ry: float, rr: float, az: float) -> tuple[float, float]:
    """Where the Moon goes: just inside the inner circle at its azimuth. The rim is the Sun's, so
    the two never meet, even at new moon, when the Moon sits right inside the Sun."""
    return _rim(rx, ry, rr - RING - MOON_R - 2, az)


def rim_marks(m: DayModel, rx: int, ry: int, rr: int, height: int) -> Rim:
    from photopainter.astro import EphemerisMissing
    from photopainter.astro.eclipse import SolarEclipse, find_eclipse
    from photopainter.astro.sky import moon_altaz

    ticks: list[float] = []
    words: list[tuple[tuple[float, float], str]] = []

    def place(radius: int, az: float, s: str) -> tuple[tuple[float, float], str]:
        return _keep_on(_rim(rx, ry, radius, az), s, RIM_WORD_SIZE, height), s

    def word(radius: int, az: float, s: str) -> None:
        words.append(place(radius, az, s))

    here = _altaz(m, [m.now])
    sun_az = float(here[1][0]) if here is not None and here[0][0] > 0 else None
    moon_az = moon_alt = None
    if m.moon is not None:
        try:
            alt, az = moon_altaz(m.now, m.lat, m.lon)
        except EphemerisMissing:
            alt, az = 0.0, 0.0
        if alt > 0:
            moon_az, moon_alt = float(az), alt
    eclipse = None
    if sun_az is not None and moon_az is not None:  # quick unless it's near new moon
        e = find_eclipse(m.now, m.lat, m.lon, m.moon.phase_deg)
        if isinstance(e, SolarEclipse) and e.stage:  # seen from here, now: the icon says it all
            eclipse, moon_az = e, None
    if m.sun is not None:
        where = _altaz(m, [m.sun.sunrise, m.sun.sunset])
        if where is not None:
            for az, at in zip(where[1], (m.sun.sunrise, m.sun.sunset)):
                ticks.append(float(az))
                if sun_az is None or abs((az - sun_az + 180) % 360 - 180) >= 20:  # else the Sun's own label is here
                    word(rr + 24, az, nice_time(at))
    sun_word = moon_word = None
    if sun_az is not None:
        up = round(float(here[0][0]))
        sun_word = place(rr + 26, sun_az, f"Eclipse, Sun {up}° up" if eclipse else f"Sun, {up}° up")
    if moon_az is not None:
        moon_word = place(rr + 24, moon_az, f"Moon, {round(moon_alt)}° up")
    if sun_word and moon_word and _words_touch(sun_word, moon_word):
        # Their marks are apart but their words would print over each other: one label for both,
        # between them.
        mid = sun_az + ((moon_az - sun_az + 180) % 360 - 180) / 2
        word(rr + 26, mid, f"Sun {up}°, Moon {round(moon_alt)}° up")
    else:
        words.extend(w for w in (sun_word, moon_word) if w)
    return Rim(tuple(ticks), sun_az, moon_az, tuple(words), eclipse)


def _words_touch(a: tuple[tuple[float, float], str], b: tuple[tuple[float, float], str]) -> bool:
    """Whether two rim labels, with their halos and a gap, would overlap."""
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = (label_rect(at, s, RIM_WORD_SIZE, "mm") for at, s in (a, b))
    gap = 4
    return ax0 < bx1 + gap and bx0 < ax1 + gap and ay0 < by1 + gap and by0 < ay1 + gap


def disc_rects(cx: float, cy: float, r: float, step: int = 6) -> list[Rect]:
    """A disc as a stack of boxes, since everything that keeps things apart here works in boxes.
    Each is as wide as the disc at its widest row, so none of the disc is left out."""
    rects: list[Rect] = []
    for top in np.arange(-r, r, step):
        bottom = min(top + step, r)
        widest = 0.0 if top < 0 < bottom else min(abs(top), abs(bottom))
        half = math.sqrt(r * r - widest * widest)
        rects.append((cx - half, cy + top, cx + half, cy + bottom))
    return rects


def rose_rects(rim: Rim, rx: int, ry: int, rr: int) -> list[Rect]:
    """The room the rose takes: its disc and what's on the rim today, not the square around them,
    so the wind and the depths can fill the water up to it."""
    rects = disc_rects(rx, ry, rr + ROSE_CLEAR)
    for az in rim.ticks:
        x, y = _rim(rx, ry, rr + 8, az)
        rects.append((x - 4, y - 4, x + 4, y + 4))
    if rim.sun_az is not None:
        x, y = _rim(rx, ry, rr + 1, rim.sun_az)
        rects.append((x - SUN_REACH - 1, y - SUN_REACH - 1, x + SUN_REACH + 1, y + SUN_REACH + 1))
    for at, s in rim.words:
        x0, y0, x1, y1 = label_rect(at, s, RIM_WORD_SIZE, "mm")
        rects.append((x0 - 2, y0 - 2, x1 + 2, y1 + 2))  # the halo
    return rects


def draw_eclipse(c: InkCanvas, x: float, y: float, e: SolarEclipse) -> None:
    """A solar eclipse in the Sun's place: a black disc in a yellow ring, the tide band's icon for
    one, the same at any depth so it can't be missed."""
    disc(c, x, y, SUN_R, YELLOW, BLACK, 1)
    disc(c, x, y, SUN_R - 3, BLACK)


def draw_rose(c: InkCanvas, m: DayModel, rim: Rim, rx: int, ry: int, rr: int) -> None:
    c.draw.ellipse([rx - rr, ry - rr, rx + rr, ry + rr], fill=WHITE, outline=BLACK)
    c.draw.ellipse([rx - rr + RING, ry - rr + RING, rx + rr - RING, ry + rr - RING], outline=BLACK)
    for deg in range(0, 360, 10):
        c.draw.line([_rim(rx, ry, rr - RING if deg % 30 == 0 else rr - 7, deg), _rim(rx, ry, rr, deg)], fill=BLACK)
    for name, deg in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        label(c, _rim(rx, ry, rr - 42, deg), name, 14, bold=True, anchor="mm")
    c.draw.polygon([_rim(rx, ry, 18 if k % 2 == 0 else 7, k * 36) for k in range(10)], fill=BLACK)
    for az in rim.ticks:
        c.draw.line([_rim(rx, ry, rr - 2, az), _rim(rx, ry, rr + 10, az)], fill=BLACK, width=2)
    if rim.moon_az is not None:
        x, y = moon_at(rx, ry, rr, rim.moon_az)
        moon_disc(c, x, y, MOON_R - 1, m.moon.phase_deg)
        c.draw.ellipse([x - MOON_R, y - MOON_R, x + MOON_R, y + MOON_R], outline=BLACK)
    if rim.sun_az is not None:
        x, y = _rim(rx, ry, rr + 1, rim.sun_az)
        for k in range(8):
            b = k * math.pi / 4
            c.draw.line([(x + math.cos(b) * (SUN_R + 2), y + math.sin(b) * (SUN_R + 2)),
                         (x + math.cos(b) * SUN_REACH, y + math.sin(b) * SUN_REACH)], fill=BLACK, width=2)
        if rim.eclipse is not None:
            draw_eclipse(c, x, y, rim.eclipse)
        else:
            disc(c, x, y, SUN_R, YELLOW, BLACK, 1)
    for at, s in rim.words:
        label(c, at, s, RIM_WORD_SIZE, anchor="mm", halo=WHITE)


# --- title block and the frame ----------------------------------------------------------------


def draw_block(c: InkCanvas, m: DayModel, rect: Rect, name: str = "") -> None:
    x0, y0, x1, y1 = (int(v) for v in rect)
    mid = (x0 + x1) / 2
    c.draw.rectangle([x0, y0, x1, y1], fill=WHITE, outline=BLACK)
    c.draw.rectangle([x0 + 3, y0 + 3, x1 - 3, y1 - 3], outline=BLACK)
    if name:
        label(c, (mid, y0 + 21), name, 22, bold=True, anchor="mm")
    when = f"{m.now:%A, %B} {m.now.day} · {nice_time(m.now)}"
    if text_width(when, font(12)) > x1 - x0 - 12:
        when = f"{m.now:%A, %b} {m.now.day} · {nice_time(m.now)}"
    label(c, (mid, y0 + 41), when, 12, anchor="mm")
    c.draw.line([(x0 + 12, y0 + 52), (x1 - 12, y0 + 52)], fill=BLACK)
    level = tide_now(m)
    trend = None if m.tide is None else ("rising" if m.tide.rising else "falling")
    next_line = next_tide_line(m)
    tx = x0 + 12
    if level is not None:
        label(c, (tx, y0 + 58), f"Tide {ft(level)} ft, {trend}", 14, bold=True)
    if next_line:
        label(c, (tx, y0 + 76), next_line, 11)
    draw_conditions(c, m, tx, y0 + 98, x1 - 7)


def draw_border(c: InkCanvas, view: View, height: int) -> None:
    c.draw.rectangle([0, 0, W - 1, height - 1], outline=BLACK)
    south = view.south
    first = math.ceil(south * 60)
    for mnt in range(first, math.floor(view.top * 60) + 1):
        lat = mnt / 60
        _, y = view.xy(lat, view.west)
        if not 14 < y < height - 14:
            continue
        for x0 in (0, W - 8):
            c.draw.line([(x0, y), (x0 + 8, y)], fill=BLACK)
        rotated_label(c, 7, y + 1, f"{mnt // 60}°{mnt % 60:02d}'", 8, 90)
    east = view.west + W * view.deg_per_px_lon
    for mnt in range(math.ceil(-east * 60), math.floor(-view.west * 60) + 1):
        lon = -mnt / 60
        x, _ = view.xy(view.top, lon)
        if not 22 < x < W - 22:
            continue
        for y0 in (0, height - 8):
            c.draw.line([(x, y0), (x, y0 + 8)], fill=BLACK)
        label(c, (x, 13), f"{mnt // 60}°{mnt % 60:02d}'", 8, anchor="mm", halo=WHITE)


def draw_scale(c: InkCanvas, view: View, x: float, y: float) -> Rect:
    """A one nautical mile bar, or half a mile on a map zoomed in far enough that a mile
    wouldn't fit."""
    miles, words = (1.0, "1 nautical mile") if 1.852 * view.px_per_km <= 260 else (0.5, "½ nautical mile")
    length = miles * 1.852 * view.px_per_km
    x = min(x, W - 12 - length)
    c.draw.rectangle([x, y, x + length, y + 5], outline=BLACK, fill=WHITE)
    c.draw.rectangle([x, y, x + length / 2, y + 5], fill=BLACK)
    label(c, (x, y - 4), "0", 10, anchor="mb", halo=WHITE)
    label(c, (x + length, y - 4), words, 10, anchor="rb", halo=WHITE)
    return (x - 4, y - 16, x + length + 4, y + 7)


# --- layout ----------------------------------------------------------------------------------


BLOCK: Rect = (590, 10, 790, 166)  # as wide as it was: taller, not wider, to keep off the bay
WAVE_BOX = (8, MAP_H + 2, W - 8, H - 3)
WAVE_STYLE = ChartStyle(axis_size=11, label_size=10, icon_radius=5, measured_width=2, dot_gap=7.0,
                        dot_radius=1.5, clear_margin=1.0, now_radius=4)
SWELL_AT = ((250, MAP_H - 60), (450, MAP_H - 60))
SCALE_AT = (300, MAP_H - 14)
ROSE = (98, MAP_H - 94, 70)


def draw_all(c: InkCanvas, m: DayModel, data: dict[str, Any], settings: HarborSettings) -> None:
    view = make_view(settings, data["bounds"], place_view(data))
    the_map = draw_map(c, data, view, tide_now(m))

    rx, ry, rr = ROSE
    rim = rim_marks(m, rx, ry, rr, MAP_H)
    fixed: list[Rect] = [BLOCK, *rose_rects(rim, rx, ry, rr), draw_scale(c, view, *SCALE_AT)]
    taken = fixed + draw_marks(c, data, view)
    taken += draw_names(c, data, view, taken)
    draw_on_water(c, m, data, view, the_map, taken, SWELL_AT)
    draw_border(c, view, MAP_H)
    draw_rose(c, m, rim, rx, ry, rr)
    draw_block(c, m, BLOCK, title(data))
    c.draw.rectangle([0, MAP_H, W - 1, H - 1], fill=WHITE, outline=BLACK)
    if m.tide is None:
        return
    tide_chart(c, WAVE_BOX, m, m.now - timedelta(days=settings.chart_days_behind),
               m.now + timedelta(days=settings.chart_days_ahead),
               replace(WAVE_STYLE, daily_labels=settings.label_tides == "daily", show_times=settings.label_times,
                       heights=settings.label_heights))


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    m = day_model(scene.draw_for, ctx)
    data = harbor_data(ctx)
    c = InkCanvas((W, H), "white")
    draw_all(c, m, data, ctx.config.harbor)
    full = c.indices()
    if ctx.canvas == (W, H):
        return full
    out = np.full((ctx.canvas[1], ctx.canvas[0]), WHITE, dtype=np.uint8)  # not designed yet: cropped
    rows, cols = min(out.shape[0], H), min(out.shape[1], W)
    out[:rows, :cols] = full[:rows, :cols]
    return out
