"""The combo screen (scene kind `combo`): the day screen, the sky, the harbor chart and the sea
on one panel, each in a quarter of it.

    +-----------------------------+-----------------------------+
    | The Wedge  Tue, Nov 3       | ☀ 74° Mostly sunny  ☼ sets  |
    | 4.6 ft ▲        High 4:49p  |      ·  sky through the     |
    | Rising +1.2     Low 11:23p  |         window   ·          |
    | tide chart                  | ~~~~~~~~~ sea, sand ~~~~~~~ |
    +-----------------------------+-----------------------------+
    | the harbor chart            | 3 ft at 11 s from the S ↓   |
    |   (the same north and south | Wind 9 mph W ← · Water 73°  |
    |    as the full chart, the   |  rows of crests             |
    |    middle of its width)     |  jetty, beach               |
    +-----------------------------+-----------------------------+

The right column is the view out of the window, with the sky over the sea; the left is the
tide and the chart. Each panel is its full screen's own drawing at a smaller size, not a
shrunken image: halving a dithered picture would turn its 50% dots into solid ink and its
text to mush.

A panel leaves out what's said in another, so that each word appears once: the weather and
the Sun and Moon's times are on the sky, the swell, wind and water on the sea, the date and
the tide on the tide panel.

Drawn for `scene.draw_for`, the middle of its schedule bucket, like the sky and the sea; the
tide panel and the chart show `scene.updated` (when the screen came up) if that's set, so
that their "now" isn't ahead of the clock.

Not designed yet: a portrait layout (PRD Q6). On a 480×800 canvas this is cropped.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np

from photopainter import places
from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.scenes import harbor as chart
from photopainter.scenes import photo as photo_scene
from photopainter.scenes import sea, sky, tides
from photopainter.scenes.base import Scene, SceneContext

BLACK, WHITE, BLUE, RED = INK["black"], INK["white"], INK["blue"], INK["red"]

log = logging.getLogger(__name__)
W, H = 800, 480
PANEL = (W // 2, H // 2)
DIVIDER = 2  # px, black, between the panels

# --- the tide -------------------------------------------------------------------------------

TIDE_CHART_TOP = 92
TIDE_STYLE = tides.ChartStyle(axis_size=12, label_size=10, icon_radius=5, measured_width=2, dot_gap=7.0,
                              dot_radius=1.5, clear_margin=1.0, now_radius=5)


def tide_panel(m: tides.DayModel, ctx: SceneContext) -> np.ndarray:
    """The place and date, the tide now and the next high and low, and the chart."""
    width, height = PANEL
    c = InkCanvas(PANEL, "white")
    settings = ctx.config.tides
    place = tides.text(c, (8, 19), m.place, 17, bold=True)
    tides.text(c, (8 + place + 10, 19), f"{m.now:%a, %b} {m.now.day}", 14)
    if m.moon is not None:
        tides.moon_disc(c, width - 16, 13, 8, m.moon.phase_deg)
        tides.text(c, (width - 30, 18), f"{m.moon.name} · {round(m.moon.illuminated * 100)}%", 11, anchor="rs")
    if m.tide is None:
        tides.text(c, (8, 58), "No tide data", 18)
        return c.indices()
    gap = float(m.gap_at(m.now.timestamp())[0])
    base = 62
    w = tides.text(c, (8, base), tides.ft(m.tide.height + gap), 40, bold=True)
    w += 4 + tides.text(c, (8 + w + 4, base), "ft", 16)
    tides.triangle(c, 8 + w + 13, base - 21, 15, m.tide.rising, BLUE)
    trend = tides.text(c, (8, base + 20), "Rising" if m.tide.rising else "Falling", 14)
    note = tides.gap_note(gap)
    if note:
        tides.text(c, (8 + trend + 8, base + 20), note, 12, bold=True, fill=RED)
    x = 216
    y = base - 18
    for e in (m.tide.next_high, m.tide.next_low):
        if e is None:
            continue
        e = m.with_gap(e)
        tides.text(c, (x, y), "High" if e.high else "Low", 14, bold=True)
        tides.text(c, (x + 40, y), f"{tides.clock(e.t)}  {tides.ft(e.v)} ft", 14)
        y += 20
    tides.tide_chart(c, (4, TIDE_CHART_TOP, width - 4, height - 2), m,
                     m.now - timedelta(days=settings.chart_days_behind),
                     m.now + timedelta(days=settings.chart_days_ahead),
                     replace(TIDE_STYLE, daily_labels=settings.label_tides == "daily",
                             show_times=settings.label_times, heights=settings.label_heights))
    return c.indices()


# --- the sky --------------------------------------------------------------------------------

SKY_STYLE = sky.SkyStyle(con_label_size=10, star_label_size=10, star_label_mag=1.0, planet_label_size=12,
                         moon_radius=12, planet_radius=4, star_min_d=1, star_d_per_mag=0.9, star_max_d=7,
                         footer_h=31, ocean_h=10, sun_radius=13, compass_size=12, hour_size=10, notes=False)
NOTE_SIZE = 11
NOTE_ICON = 22  # room for the small Sun or Moon before a note's text
CORNER_PAD = 5
CORNER_ROW = 16
CORNER_RADIUS = 6


def sky_corners(m: sky.SkyModel) -> tuple[tuple[float, float, float, float] | None, tuple[float, float, float, float] | None]:
    """The boxes the weather (top left) and the Sun and Moon's times (top right) will take."""
    f = font(NOTE_SIZE, True)
    left = None
    if m.weather is not None:
        words = tides.condition(m.weather.code, m.view.sun.alt > 0)[0]
        top_row = 22 + 4 + text_width(f"{round(m.weather.temp)}°", font(18, True)) + 5 + text_width(words, f)
        low_row = text_width(f"High {tides.deg(m.weather.high)}  Low {tides.deg(m.weather.low)}", f)
        rows = 2 + (1 if m.weather.stale else 0)
        left = (0.0, 0.0, float(CORNER_PAD * 2 + max(top_row, low_row)), float(26 + CORNER_ROW * (rows - 1) + 2))
    notes = sky.top_notes(m)
    right = None
    if notes:
        widest = max(text_width(s, f) for _, s, _ in notes)
        right = (float(PANEL[0] - CORNER_PAD - widest - NOTE_ICON), 0.0, float(PANEL[0]),
                 float(CORNER_ROW * len(notes) + 4))
    return left, right


def sky_panel(m: sky.SkyModel, ctx: SceneContext) -> np.ndarray:
    """The sky through the window, with the weather and the Sun and Moon's next times on top."""
    left, right = sky_corners(m)
    style = replace(SKY_STYLE, reserved=tuple(b for b in (left, right) if b is not None),
                    con_names=m.view.sun.alt <= sky.DAY_SUN_ALT)  # by day their boxes crowd a small sky
    c = InkCanvas(PANEL, "black")
    sky.draw(c, replace(m, weather=None), ctx.config, style)
    style = sky.lit_style(style, m.view.sun.alt)
    if left is not None and m.weather is not None:
        w = m.weather
        daytime = m.view.sun.alt > 0
        words, kind = tides.condition(w.code, daytime)
        sky.corner_panel(c, left, style.corner_box, CORNER_RADIUS)
        ink = style.text
        sky.sky_weather_icon(c, CORNER_PAD + 10, 13, 20, kind, ink)
        x = CORNER_PAD + 22 + 4
        temp = f"{round(w.temp)}°"
        c.draw.text((x, 13), temp, font=font(18, True), fill=ink, anchor="lm")
        c.draw.text((x + text_width(temp, font(18, True)) + 5, 14), words, font=font(NOTE_SIZE, True), fill=ink, anchor="lm")
        y = 26 + CORNER_ROW // 2 - 2
        c.draw.text((CORNER_PAD, y), f"High {tides.deg(w.high)}  Low {tides.deg(w.low)}", font=font(NOTE_SIZE, True), fill=ink,
                    anchor="lm")
        if w.stale:
            c.draw.text((CORNER_PAD, y + CORNER_ROW), f"as of {tides.clock(w.fetched_at)}", font=font(NOTE_SIZE, True), fill=ink,
                        anchor="lm")
    if right is not None:
        sky.corner_panel(c, right, style.corner_box, CORNER_RADIUS)
        y = 2 + CORNER_ROW // 2
        for what, s, _ in sky.top_notes(m):
            x1 = PANEL[0] - CORNER_PAD
            c.draw.text((x1, y), s, font=font(NOTE_SIZE, True), fill=style.text, anchor="rm")
            cx = x1 - text_width(s, font(NOTE_SIZE, True)) - NOTE_ICON / 2 - 1
            if what == "sun":
                sky.sun_icon(c, cx, y, 3, INK["yellow"], gap=2, ray=3)
            elif what == "eclipse":
                sky.eclipse_icon(c, cx, y, 5, m.eclipse, style.text, style.corner_box)
            elif what == "moon":
                moon = m.view.moon
                sky.stamp(c, np.array([cx]), np.array([y]), 13, style.text)
                sky.stamp(c, np.array([cx]), np.array([y]), 11, style.corner_box)
                sky.phase_disc(c, cx, y, 5, moon.phase.illuminated, 90.0 if moon.phase.waxing else 270.0, style.moon_lit, None)
            y += CORNER_ROW
    return c.indices()


# --- the harbor -----------------------------------------------------------------------------

SWELL_SIZE = 40
SOUNDING_SIZE = 9
WIND_SCALE = 0.7  # the map is half the size, but an arrow half as long would be hard to read


def harbor_view(ctx: SceneContext, data: dict) -> chart.View:
    """The full chart's north and south in this panel's height, keeping the map's shape, so
    the middle of its width shows."""
    full = chart.make_view(ctx.config.harbor, data["bounds"], chart.place_view(data))
    lat_per_px = (full.top - full.south) / PANEL[1]
    lon_per_px = lat_per_px * full.deg_per_px_lon / full.deg_per_px_lat
    center = full.west + chart.W * full.deg_per_px_lon / 2
    return chart.View(west=center - PANEL[0] * lon_per_px / 2, top=full.top, deg_per_px_lon=lon_per_px,
                      deg_per_px_lat=lat_per_px, height=PANEL[1], south=full.south)


def harbor_panel(m: tides.DayModel, ctx: SceneContext, data: dict | None = None) -> np.ndarray:
    """The map with its marks, names, wind and swell. The tide, the rose and the title block
    are left out: the tide panel beside it has them. Its helpers draw on the full chart's
    canvas, so this draws there and keeps the top left corner."""
    width, height = PANEL
    data = chart.harbor_data(ctx) if data is None else data
    view = harbor_view(ctx, data)
    c = InkCanvas((chart.W, chart.H), "white")
    the_map = chart.draw_map(c, data, view, chart.tide_now(m))
    outside: list[chart.Rect] = [(-99.0, -99.0, 0.0, chart.H), (0.0, -99.0, chart.W, 0.0),
                                 (width, 0.0, chart.W, chart.H), (0.0, height, chart.W, chart.H)]
    title_at, title = (width - 8, 12), chart.title(data)
    taken = outside + [chart.label_rect(title_at, title, 13, "ra", True)] + chart.draw_marks(c, data, view)
    taken += chart.draw_names(c, data, view, taken)
    chart.draw_on_water(c, m, data, view, the_map, taken, ((60, height - 44), (200, height - 44)),
                        swell_size=SWELL_SIZE, sounding_size=SOUNDING_SIZE, wind_scale=WIND_SCALE)
    chart.label(c, title_at, title, 13, bold=True, anchor="ra", halo=WHITE)
    return np.asarray(c.image, dtype=np.uint8)[:height, :width].copy()


# --- the sea --------------------------------------------------------------------------------

SEA_WORDS = sea.WordStyle(left=5, top=4, pad=5, lines=((11, 14), (15, 19), (11, 14), (9, 11)))


def _no_tide(line: str | None) -> str | None:
    """The sea's third line without the next tide: the tide panel gives it."""
    if line is None:
        return None
    return "  ·  ".join(part for part in line.split("  ·  ") if " tide " not in part) or None


def sea_panel(at: datetime, ctx: SceneContext, wide: bool = False) -> np.ndarray:
    """The picture at half size, with only the swell, the wind and the water in words.
    `wide`: across the whole bottom row, reaching twice as far either side at the same size."""
    m = sea.sea_model(at, ctx)
    m = replace(m, words=(None, m.words[1], _no_tide(m.words[2])), updated=None)
    c = InkCanvas((W, PANEL[1]) if wide else PANEL, "white")
    loc, place = ctx.config.location, sea.place_for(ctx)
    sea.draw(c, m, ctx.config.sky.center_azimuth_deg, places.skyline_for(loc.lat, loc.lon, ctx.data_dir), scale=0.5,
             words=SEA_WORDS, jetty=None if place is None else place.jetty,
             half_span=sea.HALF_SPAN_DEG * (2 if wide else 1))
    return c.indices()


# --- no harbor chart here: the photo ---------------------------------------------------------


def photo_panel(ctx: SceneContext) -> np.ndarray | None:
    """The photo the rotation is on, dithered for a quarter of the panel (Owner, 2026-11-08), or
    None with no photo that can be read."""
    for path in photo_scene.current(ctx):
        try:
            return photo_scene.render_at(path, PANEL, ctx.config)
        except Exception as exc:  # noqa: BLE001 - an unreadable file: try the next, as photos do
            log.warning("combo: skipping %s: %s", path.name, exc)
    return None


# --- the screen -----------------------------------------------------------------------------


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    now = scene.updated or scene.draw_for
    day = tides.day_model(now, ctx)
    width, height = PANEL
    full = np.full((H, W), WHITE, dtype=np.uint8)
    full[:height, :width] = tide_panel(day, ctx)
    full[:height, width:] = sky_panel(sky.sky_model(scene.draw_for, ctx), ctx)
    loc = ctx.config.location
    chart_data = places.harbor_for(loc.lat, loc.lon, ctx.data_dir)
    lower = None if chart_data is not None else photo_panel(ctx)
    if chart_data is not None:
        full[height:, :width] = harbor_panel(day, ctx, chart_data)
    elif lower is not None:  # no harbor chart here: the photo in its place
        full[height:, :width] = lower
    if chart_data is None and lower is None:  # nor any photo: the sea across the bottom
        full[height:, :] = sea_panel(scene.draw_for, ctx, wide=True)
    else:
        full[height:, width:] = sea_panel(scene.draw_for, ctx)
    half = DIVIDER // 2
    full[:height if chart_data is None and lower is None else H, width - half:width + half] = BLACK
    full[height - half:height + half, :] = BLACK
    out = np.full((ctx.canvas[1], ctx.canvas[0]), WHITE, dtype=np.uint8)  # not designed yet: cropped
    rows, cols = min(out.shape[0], H), min(out.shape[1], W)
    out[:rows, :cols] = full[:rows, :cols]
    return out
