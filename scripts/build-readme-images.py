#!/usr/bin/env python3
"""Draw the README's pictures (docs/images/) with the frame's own code, on the Mac.

Every screen comes from `photopainter scene` at the panel's 800 x 480, with the recorded caches
in tests/fixtures (real data from 2026-11-03), so a picture can't show anything the frame doesn't
draw. The timeline asks `schedule.decide()` what's up at each minute of that day. The sample
photos in docs/images/samples/ are public domain (credits in README.md); personal photos stay
out of git.

    .venv/bin/python scripts/build-readme-images.py              # everything, about two minutes
    .venv/bin/python scripts/build-readme-images.py hero night   # some of it

Run it after a screen changes. Needs data/de421.bsp (scripts/fetch-ephemeris.sh), and Google
Chrome for the hero, which draws the dashboard's wood frame on a wall. The wall is a mockup; the
picture on it is the real output. The dashboard's own pictures come from
scripts/readme-dashboard-shots.mjs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
SCREENS = OUT / "screens"
SAMPLES = OUT / "samples"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PANEL = (800, 480)
PAPER = (246, 242, 234)
INK_TEXT = (28, 26, 24)
SOFT_TEXT = (104, 98, 90)
INK_RED = (181, 30, 22)

# name: (scene kind, --at, extra flags). All from the fixtures' day, Tuesday November 3, 2026.
DAY = "2026-11-03T14:10"
SCENES: dict[str, tuple[str, str | None, tuple[str, ...]]] = {
    "day": ("tides", DAY, ()),
    "sky-predawn": ("sky", "2026-11-04T04:30", ("--forecast",)),
    "sky-evening": ("sky", "2026-11-03T18:45", ()),
    "sky-noon": ("sky", "2026-11-03T12:00", ("--forecast",)),
    "sky-sunset": ("sky", "2026-11-03@sunset", ("--forecast",)),
    "sky-dusk": ("sky", "2026-11-03@dusk", ("--forecast",)),
    "sky-summer": ("sky", "2027-07-03T22:30", ()),
    "sky-moon": ("sky", "2026-11-22T20:00", ()),
    # The total lunar eclipse of March 3, 2026, and the solar eclipse of April 8, 2024 (partial here).
    "sky-eclipse-partial": ("sky", "2026-03-03T02:40", ()),
    "sky-eclipse-total": ("sky", "2026-03-03T03:30", ()),
    "sky-eclipse-solar": ("sky", "2024-04-08T11:12", ()),
    "sea-calm": ("sea", DAY, ("--forecast", "--waves", "calm", "--clouds", "clear")),
    "sea-big": ("sea", DAY, ("--forecast", "--waves", "big", "--clouds", "clear")),
    "sea-windy": ("sea", DAY, ("--forecast", "--waves", "windy", "--clouds", "partly")),
    "sea-gale": ("sea", DAY, ("--forecast", "--waves", "gale", "--clouds", "overcast")),
    "sea-sunset": ("sea", "2026-11-03@sunset", ("--forecast",)),
    "sea-night": ("sea", "2026-11-03T22:00", ("--forecast",)),
    "harbor": ("harbor", DAY, ()),
    "combo": ("combo", DAY, ()),
    "photo-garibaldi": ("photo", None, ("--photo", "garibaldi.jpg")),
    "photo-pelican": ("photo", None, ("--photo", "pelican.jpg")),
    "photo-sunset": ("photo", None, ("--photo", "sunset.jpg")),
    "photo-lighthouse": ("photo", None, ("--photo", "lighthouse.jpg")),
    "photo-otters": ("photo", None, ("--photo", "otters.jpg")),
    "day-nowifi": ("tides", DAY, ("--no-wifi-mark",)),
    "setup": ("setup", None, ()),
}
HERO_SCREENS = ["day", "photo-garibaldi", "sky-predawn", "sea-sunset", "harbor", "sky-eclipse-total",
                "photo-pelican", "combo"]


class Scratch:
    """A scratch state folder and config, so a build touches nothing but docs/images."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="readme-images-"))
        for name in ("tides", "weather", "surf", "gauge"):
            shutil.copy(ROOT / "tests" / "fixtures" / f"{name}.json", self.dir)
        config = self.dir / "config.json"
        config.write_text(json.dumps({"photos": {"folder": str(SAMPLES)}}))
        # The Wi-Fi setup screen names the frame after the computer's hostname: give it a neutral
        # one, so this Mac's name doesn't end up in the README.
        site = self.dir / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text("import socket\nsocket.gethostname = lambda: 'SeaAndSkyInkFrame'\n")
        self.env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(site),
            "PHOTOPAINTER_STATE_DIR": str(self.dir),
            "PHOTOPAINTER_CONFIG": str(config),
        }

    def scene(self, kind: str, at: str | None, flags: tuple[str, ...], out: Path) -> Path:
        command = [str(ROOT / ".venv" / "bin" / "photopainter"), "scene", kind, "-o", str(out), *flags]
        if at:
            command += ["--at", at]
        subprocess.run(command, env=self.env, check=True, capture_output=True, cwd=ROOT)
        return out


def small_png(path: Path, out: Path | None = None) -> None:
    """Keep a screen in as few bytes as its six inks need."""
    Image.open(path).convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=8).save(
        out or path, optimize=True
    )


def build_screens(scratch: Scratch) -> None:
    SCREENS.mkdir(parents=True, exist_ok=True)

    def one(item: tuple[str, tuple[str, str | None, tuple[str, ...]]]) -> None:
        name, (kind, at, flags) = item
        small_png(scratch.scene(kind, at, flags, SCREENS / f"{name}.png"))

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, SCENES.items()))
    print(f"screens: {len(SCENES)}")


def font(size: int, bold: bool = False):
    sys.path.insert(0, str(ROOT / "src"))
    from photopainter.render.draw import font as bundled

    return bundled(size, bold)


def screen(name: str) -> Image.Image:
    return Image.open(SCREENS / f"{name}.png").convert("RGB")


def rounded(image: Image.Image, radius: int) -> Image.Image:
    """The image with transparent rounded corners, drawn smoothly."""
    scale = 4
    mask = Image.new("L", (image.width * scale, image.height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius * scale, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask.resize(image.size, Image.Resampling.LANCZOS))
    return out


# --- the time-lapse ---------------------------------------------------------------------------


def build_night(scratch: Scratch) -> None:
    """A night sped up: on the wall the sky redraws each hour, here every 20 minutes."""
    start = datetime.fromisoformat("2026-11-03T15:20")
    moments = [start + timedelta(minutes=20 * i) for i in range(47)]
    with tempfile.TemporaryDirectory() as tmp:

        def one(at: datetime) -> Image.Image:
            path = scratch.scene("sky", at.isoformat(timespec="minutes"), ("--forecast",), Path(tmp) / f"{at:%d%H%M}.png")
            return Image.open(path).convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=32)

        with ThreadPoolExecutor(4) as pool:
            frames = list(pool.map(one, moments))
    durations = [260] * len(frames)
    durations[0] = durations[-1] = 1200
    frames[0].save(OUT / "night.gif", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=1)
    print(f"night: {(OUT / 'night.gif').stat().st_size // 1024} KB")


# --- the hero ---------------------------------------------------------------------------------

HERO_HTML = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{css}">
<style>
  :root {{ color-scheme: light; }}
  html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; background: #e7dfd2; }}
  .room {{ position: absolute; inset: 0; overflow: hidden;
    background: radial-gradient(120% 110% at 26% 20%, #f7f2e8 0%, #eadfce 46%, #cfc1ad 100%); }}
  .beam {{ position: absolute; background: rgba(255, 244, 218, .62); filter: blur({blur}px);
    transform: skewX(-24deg); }}
  .floorlight {{ position: absolute; left: 0; right: 0; bottom: 0; height: 22%;
    background: linear-gradient(transparent, rgba(92, 70, 44, .13)); }}
  .wall {{ position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: {frame}px;
    padding: 0; background: none; margin: 0; border-radius: 0; overflow: visible; }}
  .wall > * {{ max-width: none; }}
  .frame {{ box-shadow: 0 {s1}px {s2}px -{s3}px rgba(45, 22, 6, .55), 0 {s4}px {s5}px rgba(45, 22, 6, .24); }}
  .panel {{ background: #ff00ff !important; box-shadow: none; }}
</style>
<div class="room">
  <div class="beam" style="left:{b1x}px; top:{b1y}px; width:{bw}px; height:{bh}px"></div>
  <div class="beam" style="left:{b2x}px; top:{b1y}px; width:{bw}px; height:{bh}px"></div>
  <div class="floorlight"></div>
</div>
<section class="wall">
  <div class="frame">
    <span class="wood top"></span><span class="wood right"></span><span class="wood bottom"></span><span class="wood left"></span>
    <span class="mat"><span class="step"><span class="panel"></span></span></span>
  </div>
</section>
"""


def hero_base(width: int, height: int, frame: int) -> Image.Image:
    """The wall and the frame, with a magenta panel to find and fill."""
    u = frame / 100
    html = HERO_HTML.format(
        css=(ROOT / "src/photopainter/web/static/style.css").as_uri(), w=width, h=height, frame=frame,
        blur=round(2.2 * u), s1=round(3 * u), s2=round(3.5 * u), s3=round(2 * u), s4=round(0.8 * u), s5=round(1.6 * u),
        b1x=round(width * 0.10), b1y=round(-height * 0.15), b2x=round(width * 0.31), bw=round(width * 0.17), bh=round(height * 1.3),
    )
    with tempfile.TemporaryDirectory() as tmp:
        page, shot = Path(tmp) / "hero.html", Path(tmp) / "hero.png"
        page.write_text(html)
        subprocess.run(
            [CHROME, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1", "--disable-gpu",
             f"--window-size={width},{height}", f"--screenshot={shot}", page.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
        return Image.open(shot).convert("RGB").copy()


def magenta_box(image: Image.Image) -> tuple[int, int, int, int]:
    """Where the panel is, its pixels painted over so no magenta edge is left around it."""
    a = np.asarray(image).astype(int)
    hits = np.argwhere((a[..., 0] > 245) & (a[..., 1] < 10) & (a[..., 2] > 245))
    (y0, x0), (y1, x1) = hits.min(0), hits.max(0)
    tinted = (a[..., 0] - a[..., 1] > 40) & (a[..., 2] - a[..., 1] > 40)
    out = np.asarray(image).copy()
    out[tinted] = (170, 170, 164)
    image.paste(Image.fromarray(out))
    return int(x0), int(y0), int(x1) + 1, int(y1) + 1


def build_hero(_scratch: Scratch) -> None:
    """The frame on a wall, changing screens with an e-ink blink. An animated PNG."""
    scale = 2  # panel pixels stay square, and a browser's downscale blends the dots as the eye does
    frame = round(PANEL[0] * scale / 0.735)  # the display is 73.5 % of the frame in the dashboard's CSS
    width, height = round(frame * 1.22), round(frame * 0.80)
    base = hero_base(width, height, frame)
    box = magenta_box(base)
    panel_size = (box[2] - box[0], box[3] - box[1])
    # E-ink "white" is a light gray next to a white mat, as the dashboard draws it.
    paper = Image.new("RGB", panel_size, (228, 229, 224))

    def on_wall(picture: Image.Image) -> Image.Image:
        out = base.copy()
        out.paste(ImageChops.multiply(picture.resize(panel_size, Image.Resampling.NEAREST), paper), box[:2])
        return out

    black, white = (Image.new("RGB", PANEL, c) for c in ((20, 20, 24), (255, 255, 255)))
    frames, durations = [], []
    for name in HERO_SCREENS:
        frames += [on_wall(screen(name)), on_wall(black), on_wall(white)]
        durations += [2600, 110, 110]
    frames[0].save(OUT / "hero.png", save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"hero: {width}x{height}, panel box {box}, {(OUT / 'hero.png').stat().st_size // 1024} KB")


# --- pictures with numbered pins --------------------------------------------------------------

# Where each number goes, in panel pixels.
PINS = {
    "day": [(196, 96), (262, 185), (258, 235), (478, 156), (706, 176), (776, 268), (590, 13), (505, 358), (108, 314)],
    "sky-predawn": [(216, 62), (122, 208), (305, 122), (470, 183), (540, 96), (240, 236), (702, 402), (86, 432), (547, 34)],
}


def build_pins(_scratch: Scratch) -> None:
    for name, spots in PINS.items():
        scale, smooth = 2, 4
        picture = screen(name).resize((PANEL[0] * scale, PANEL[1] * scale), Image.Resampling.NEAREST)
        layer = Image.new("RGBA", (picture.width * smooth, picture.height * smooth), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        number_font = font(21 * scale * smooth // 2, bold=True)
        for number, (x, y) in enumerate(spots, 1):
            cx, cy = x * scale * smooth, y * scale * smooth
            r = 13 * scale * smooth
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))
            r -= 2 * scale * smooth
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*INK_RED, 255))
            draw.text((cx, cy), str(number), font=number_font, fill=(255, 255, 255, 255), anchor="mm")
        picture.paste(layer.resize(picture.size, Image.Resampling.LANCZOS), (0, 0), layer.resize(picture.size, Image.Resampling.LANCZOS))
        out = OUT / f"anatomy-{name.split('-')[0]}.png"
        picture.convert("P", palette=Image.Palette.ADAPTIVE, colors=64).save(out, optimize=True)
        print(f"{out.name}: {out.stat().st_size // 1024} KB")


# --- photo to ink ----------------------------------------------------------------------------


def build_pipeline(_scratch: Scratch) -> None:
    """The photo, the same photo in six inks, and a close-up where every dot is one ink."""
    sys.path.insert(0, str(ROOT / "src"))
    from photopainter.render.palette import INKS, palette_colors

    pad, gap, label = 40, 40, 62
    width = pad * 2 + gap + PANEL[0] * 2
    zoom_h = 480
    height = pad + label + PANEL[1] + gap + label + zoom_h + pad
    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)

    photo = Image.open(SAMPLES / "garibaldi.jpg").convert("RGB")
    scale = max(PANEL[0] / photo.width, PANEL[1] / photo.height)
    fitted = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.Resampling.LANCZOS)
    left, top = (fitted.width - PANEL[0]) // 2, (fitted.height - PANEL[1]) // 2
    fitted = fitted.crop((left, top, left + PANEL[0], top + PANEL[1]))
    inked = screen("photo-garibaldi")

    heading = font(32, bold=True)
    x2 = pad + PANEL[0] + gap
    y = pad
    draw.text((pad, y), "The photo: millions of colors", font=heading, fill=INK_TEXT)
    draw.text((x2, y), "On the panel: six inks", font=heading, fill=INK_TEXT)
    y += label
    sheet.paste(fitted, (pad, y))
    sheet.paste(inked, (x2, y))

    # The close-up: the fish's eye at 8x.
    cx, cy, zw, zh = 385, 272, 100, 60
    crop = (cx - zw // 2, cy - zh // 2, cx + zw // 2, cy + zh // 2)
    draw.rectangle((x2 + crop[0] - 2, y + crop[1] - 2, x2 + crop[2] + 1, y + crop[3] + 1), outline=(255, 255, 255), width=2)
    y2 = y + PANEL[1] + gap
    draw.text((pad, y2), "Up close: every dot is one ink", font=heading, fill=INK_TEXT)
    draw.text((x2, y2), "The whole palette", font=heading, fill=INK_TEXT)
    y2 += label
    sheet.paste(inked.crop(crop).resize((PANEL[0], zoom_h), Image.Resampling.NEAREST), (pad, y2))

    colors = palette_colors("estimated")
    cols, rows, swatch_gap = 3, 2, 16
    sw = (PANEL[0] - swatch_gap * (cols - 1)) // cols
    sh = (zoom_h - swatch_gap * (rows - 1)) // rows
    name_font = font(30, bold=True)
    for i, (ink, color) in enumerate(zip(INKS, colors)):
        sx = x2 + (i % cols) * (sw + swatch_gap)
        sy = y2 + (i // cols) * (sh + swatch_gap)
        draw.rounded_rectangle((sx, sy, sx + sw, sy + sh), 18, fill=color, outline=(190, 184, 172), width=2)
        on_dark = sum(color) < 380
        draw.text((sx + 24, sy + sh - 56), ink.name.capitalize(), font=name_font, fill=(255, 255, 255) if on_dark else INK_TEXT)
    out = OUT / "pipeline.png"
    rounded(sheet, 28).save(out, optimize=True)
    print(f"pipeline: {sheet.size}, {out.stat().st_size // 1024} KB")


# --- a day on the wall ------------------------------------------------------------------------


def build_timeline(scratch: Scratch) -> None:
    """One default day, minute by minute, from `schedule.decide()`, with one daytime hour opened
    out above it: each step's screen as the frame draws it at that time."""
    sys.path.insert(0, str(ROOT / "src"))
    from photopainter.astro.sun import sun_events
    from photopainter.config import default_config
    from photopainter.schedule import Overrides, decide

    config = default_config()
    loc = config.location
    day = date(2026, 11, 3)
    sun = lambda d: sun_events(d, loc.lat, loc.lon, loc.tz)  # noqa: E731
    midnight = datetime(day.year, day.month, day.day, tzinfo=loc.tz)
    decisions = [decide(midnight + timedelta(minutes=m), config, Overrides(), sun) for m in range(1440)]
    kinds = [d.scene.kind for d in decisions]
    colors = {"photo": (205, 40, 28), "tides": (16, 60, 170), "harbor": (38, 132, 64), "sky": (24, 24, 30),
              "sea": (40, 150, 175), "combo": (226, 172, 24)}
    names = {"photo": "A photo", "tides": "Tides and weather", "harbor": "The harbor chart", "sky": "The sky",
             "sea": "The sea", "combo": "All four at once"}

    # The hour opened out: 2 pm to 3 pm, each step drawn for the moment the schedule draws it for.
    hour = 14 * 60
    steps = []
    for m in range(hour, hour + 60):
        if m == hour or kinds[m] != kinds[m - 1]:
            steps.append((m, decisions[m]))
    thumbs = []
    for m, d in steps:
        if d.scene.kind == "photo":
            picture = screen("photo-garibaldi")
        else:
            at = d.scene.draw_for.strftime("%Y-%m-%dT%H:%M")
            flags = ("--forecast",) if d.scene.kind in ("sky", "sea") else ()
            picture = Image.open(scratch.scene(d.scene.kind, at, flags, scratch.dir / f"timeline-{m}.png")).convert("RGB")
        thumbs.append((m, d.scene.kind, picture))

    pad, gap = 40, 20
    width = 1720
    tw = (width - pad * 2 - gap * (len(thumbs) - 1)) // len(thumbs)
    th = tw * 3 // 5
    hour_y = pad + 78 + th + 70  # the opened-out hour
    hour_h = 40
    bar_y = hour_y + hour_h + 110  # the whole day
    bar_h = 64
    height = bar_y + bar_h + 100
    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)
    bar_w = width - pad * 2

    def to_x(minute: float) -> int:
        return pad + round(bar_w * minute / 1440)

    def hour_x(minute: float) -> int:
        return pad + round(bar_w * (minute - hour) / 60)

    def clock(minute: int) -> str:
        h, m = divmod(minute, 60)
        return f"{(h - 1) % 12 + 1}:{m:02d} {'am' if h < 12 else 'pm'}"

    time_font, small = font(30, bold=True), font(24)
    for i, (minute, kind, picture) in enumerate(thumbs):
        x = pad + i * (tw + gap)
        draw.text((x, pad), clock(minute), font=time_font, fill=INK_TEXT)
        draw.text((x, pad + 38), names[kind], font=small, fill=SOFT_TEXT)
        y = pad + 78
        sheet.paste(picture.resize((tw, th), Image.Resampling.LANCZOS), (x, y))
        # A line from each picture down to its ten minutes in the hour.
        mid = (hour_x(minute) + hour_x(minute + 10)) // 2
        draw.line([(x + tw // 2, y + th), (x + tw // 2, y + th + 30), (mid, hour_y - 16), (mid, hour_y)],
                  fill=INK_TEXT, width=3)

    def bands(first: int, last: int, x_of, y: int, h: int) -> None:
        start = first
        for m in range(first + 1, last + 1):
            if m == last or kinds[m] != kinds[start]:
                draw.rectangle((x_of(start), y, x_of(m) - 1, y + h), fill=colors[kinds[start]])
                start = m

    bands(hour, hour + 60, hour_x, hour_y, hour_h)
    bands(0, 1440, to_x, bar_y, bar_h)
    # The hour above, joined to where it sits in the day.
    for edge_hour, edge_day in ((hour_x(hour), to_x(hour)), (hour_x(hour + 60) - 1, to_x(hour + 60) - 1)):
        draw.line([(edge_hour, hour_y + hour_h), (edge_day, bar_y)], fill=SOFT_TEXT, width=2)
    draw.rectangle((to_x(hour) - 2, bar_y - 2, to_x(hour + 60) + 1, bar_y + bar_h + 2), outline=INK_TEXT, width=3)

    label_y = bar_y + bar_h + 14
    for h, text in ((0, "Midnight"), (6, "6 am"), (12, "Noon"), (18, "6 pm"), (24, "Midnight")):
        x = to_x(h * 60)
        draw.line((x, bar_y + bar_h, x, bar_y + bar_h + 10), fill=SOFT_TEXT, width=2)
        anchor = "la" if h == 0 else "ra" if h == 24 else "ma"
        draw.text((x, label_y), text, font=small, fill=SOFT_TEXT, anchor=anchor)
    # The key, under the bar.
    key_x, key_y = pad, label_y + 36
    for text, kind in (("Photo", "photo"), ("Tides", "tides"), ("Harbor", "harbor"), ("Sky, by day and all night", "sky"),
                       ("Sea", "sea"), ("Combo", "combo")):
        draw.rectangle((key_x, key_y + 2, key_x + 22, key_y + 24), fill=colors[kind])
        draw.text((key_x + 32, key_y), text, font=small, fill=INK_TEXT)
        key_x += 32 + round(draw.textlength(text, font=small)) + 40
    out = OUT / "timeline.png"
    rounded(sheet, 28).save(out, optimize=True)
    print(f"timeline: {sheet.size}, {out.stat().st_size // 1024} KB")


# --- no Wi-Fi ---------------------------------------------------------------------------------


def build_wifi(_scratch: Scratch) -> None:
    """No Wi-Fi in two steps: the mark an outage leaves, under a magnifier, and the code a held Back shows."""
    plain, marked, setup = screen("day"), screen("day-nowifi"), screen("setup")
    left, top, right, bottom = ImageChops.difference(plain, marked).getbbox()  # where the mark is
    mark = ((left + right) // 2, (top + bottom) // 2)

    pad, gap, label = 40, 40, 62
    width = pad * 2 + gap + PANEL[0] * 2
    height = pad + label + PANEL[1] + pad
    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)
    heading = font(32, bold=True)
    x1, x2, y = pad, pad + PANEL[0] + gap, pad
    draw.text((x1, y), "No Wi-Fi for 10 minutes: a small mark", font=heading, fill=INK_TEXT)
    draw.text((x2, y), "Hold Back for a second: a code to scan", font=heading, fill=INK_TEXT)
    y += label
    sheet.paste(marked, (x1, y))
    sheet.paste(setup, (x2, y))

    # The magnifier: the mark at 6x in a circle over the chart, with a line down to the real one.
    zoom, r = 6, 96
    half = r // zoom
    cx = min(max(mark[0], half), PANEL[0] - half)
    cy = min(max(mark[1], half), PANEL[1] - half)
    lens = marked.crop((cx - half, cy - half, cx + half, cy + half)).resize((2 * r, 2 * r), Image.Resampling.NEAREST)
    centre = (x1 + 640, y + 352)
    draw.line([centre, (x1 + mark[0], y + mark[1])], fill=INK_TEXT, width=3)
    smooth = 4
    disc = Image.new("L", (2 * r * smooth, 2 * r * smooth), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, disc.width - 1, disc.height - 1), fill=255)
    disc = disc.resize((2 * r, 2 * r), Image.Resampling.LANCZOS)
    ring = r + 7
    shadow = Image.new("L", sheet.size, 0)
    ImageDraw.Draw(shadow).ellipse((centre[0] - ring, centre[1] - ring + 8, centre[0] + ring, centre[1] + ring + 8), fill=110)
    sheet.paste(Image.new("RGB", sheet.size, (60, 44, 28)), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14)))
    draw.ellipse((centre[0] - ring, centre[1] - ring, centre[0] + ring, centre[1] + ring), fill=(255, 255, 255))
    sheet.paste(lens, (centre[0] - r, centre[1] - r), disc)
    out = OUT / "wifi.png"
    rounded(sheet, 28).save(out, optimize=True)
    print(f"wifi: {sheet.size}, mark at {mark}, {out.stat().st_size // 1024} KB")


# --- the dashboard, from scripts/readme-dashboard-shots.mjs --------------------------------------


def shadowed(sheet: Image.Image, picture: Image.Image, at: tuple[int, int], blur: int, offset: int) -> None:
    """Paste a picture with rounded corners onto the sheet, with a soft shadow under it."""
    shadow = Image.new("L", sheet.size, 0)
    shadow.paste(picture.getchannel("A").point(lambda a: a * 0.42), (at[0], at[1] + offset))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    sheet.paste(Image.new("RGB", sheet.size, (60, 44, 28)), (0, 0), shadow)
    sheet.paste(picture, at, picture)


def build_dashboard(_scratch: Scratch) -> None:
    """The dashboard at a desk and in a hand: light on a laptop, dark on a phone."""
    desktop = Image.open(OUT / "dashboard-desktop.png").convert("RGB")
    phone = Image.open(OUT / "dashboard-phone.png").convert("RGB")
    width, height, pad = 1720, 1176, 44
    sheet = Image.new("RGB", (width, height), (226, 218, 204))

    desk_w = 1330
    desk = desktop.resize((desk_w, desktop.height * desk_w // desktop.width), Image.Resampling.LANCZOS)
    shadowed(sheet, rounded(desk, 18), (pad, pad), 30, 16)

    bezel = 12
    phone_h = height - pad * 2 - bezel * 2 - 20
    shown = phone.resize((phone.width * phone_h // phone.height, phone_h), Image.Resampling.LANCZOS)
    body = Image.new("RGB", (shown.width + bezel * 2, shown.height + bezel * 2), (14, 14, 16))
    body.paste(rounded(shown, 44), (bezel, bezel), rounded(shown, 44))
    shadowed(sheet, rounded(body, 54), (width - body.width - pad, pad + 10), 34, 18)

    out = OUT / "dashboard.png"
    rounded(sheet, 28).save(out, optimize=True)
    print(f"dashboard: {sheet.size}, {out.stat().st_size // 1024} KB")

    # The boxes, in fewer bytes: flat colors and small dithered tiles.
    for name in ("photos", "schedule", "battery", "help", "setup"):
        path = OUT / f"dashboard-{name}.png"
        image = Image.open(path)
        if image.mode != "P":
            image.convert("RGB").quantize(colors=200, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)
        print(f"{path.name}: {path.stat().st_size // 1024} KB")


BUILDERS = {
    "screens": build_screens,
    "night": build_night,
    "hero": build_hero,
    "pins": build_pins,
    "pipeline": build_pipeline,
    "timeline": build_timeline,
    "wifi": build_wifi,
    "dashboard": build_dashboard,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw the README's pictures with the frame's own code.")
    parser.add_argument("parts", nargs="*", choices=[*BUILDERS, []], help="what to build (default: everything)")
    args = parser.parse_args()
    if not (ROOT / "data" / "de421.bsp").exists():
        print("data/de421.bsp is missing: run scripts/fetch-ephemeris.sh first.", file=sys.stderr)
        return 1
    scratch = Scratch()
    try:
        for name, build in BUILDERS.items():
            if not args.parts or name in args.parts:
                build(scratch)
    finally:
        shutil.rmtree(scratch.dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
