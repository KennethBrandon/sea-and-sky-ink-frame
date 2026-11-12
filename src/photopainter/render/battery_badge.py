"""The battery indicator: a small battery on a white box, drawn over any finished screen.

The tick draws it after `render_scene` and before the panel and `current.png`, whenever the
frame runs on the battery below `battery.indicator_volts`. It has to read over a dithered
photo as well as a chart, so it's a black battery on solid white ruled in black, and drawn in
ink indices with no anti-aliasing like everything else. The fill follows the percent the
voltage curve gives (`hardware/battery.py`), black, and red from 25 % down.

The panel shows about 127 pixels an inch, so the box is about 8 mm wide.

The low-power mark sits beside it: a crescent on the same kind of box, drawn while
`battery.low_power` is on, plugged in or not. Holding both buttons turns that setting on or
off and changes nothing else you can see, so this is how you know which way it's set. It
keeps its place whether the battery is up or not, so it doesn't move when the battery appears.

The no-Wi-Fi mark (`hotspot.py`) is the last of them: a Wi-Fi fan with a small red X. Alone it
sits in the corner, in the battery's place, since one place further in it would cover the day
screen's last day name. With the battery up it moves left of the crescent's place. Low power
and this mark don't come together, as low power's Wi-Fi is off on purpose.
"""

from __future__ import annotations

import math

import numpy as np

from photopainter.render.draw import INK, InkCanvas

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_CORNER = "bottom-right"
MARGIN = 8  # from the canvas's edges to the box
BODY = (26, 14)  # the battery's width and height, outline included
NUB = (3, 6)
PAD = 4  # white between the box's outline and the battery
RED_AT_PERCENT = 25  # and below
MARK_GAP = 4  # between the battery's place and the low-power mark


def box_size() -> tuple[int, int]:
    """(width, height) of the box, outline included."""
    return 2 + 2 * PAD + BODY[0] + NUB[0], 2 + 2 * PAD + BODY[1]


def draw(indices: np.ndarray, percent: int | None, corner: str = DEFAULT_CORNER) -> np.ndarray:
    """A copy of `indices` (ink indices, any canvas size) with the indicator in `corner`.

    `percent` None or 0 draws an empty battery.
    """
    if corner not in CORNERS:
        raise ValueError(f"unknown corner {corner!r}; corners are {', '.join(CORNERS)}")
    width, height = box_size()
    top = MARGIN if corner.startswith("top") else indices.shape[0] - MARGIN - height
    left = MARGIN if corner.endswith("left") else indices.shape[1] - MARGIN - width
    out = indices.copy()
    out[top:top + height, left:left + width] = _box(percent or 0)
    return out


def draw_low_power(indices: np.ndarray, corner: str = DEFAULT_CORNER) -> np.ndarray:
    """A copy of `indices` with the low-power mark beside the battery's place in `corner`."""
    if corner not in CORNERS:
        raise ValueError(f"unknown corner {corner!r}; corners are {', '.join(CORNERS)}")
    battery_width, height = box_size()
    width = height  # square, as tall as the battery's box
    top = MARGIN if corner.startswith("top") else indices.shape[0] - MARGIN - height
    beside = MARGIN + battery_width + MARK_GAP
    left = beside if corner.endswith("left") else indices.shape[1] - beside - width
    out = indices.copy()
    out[top:top + height, left:left + width] = _mark(width)
    return out


def draw_no_wifi(indices: np.ndarray, corner: str = DEFAULT_CORNER, *, battery_up: bool = False) -> np.ndarray:
    """A copy of `indices` with the no-Wi-Fi mark in `corner`, or left of the crescent's place
    while the battery is up."""
    if corner not in CORNERS:
        raise ValueError(f"unknown corner {corner!r}; corners are {', '.join(CORNERS)}")
    battery_width, height = box_size()
    top = MARGIN if corner.startswith("top") else indices.shape[0] - MARGIN - height
    beside = MARGIN + (battery_width + MARK_GAP + height + MARK_GAP if battery_up else 0)
    left = beside if corner.endswith("left") else indices.shape[1] - beside - height
    out = indices.copy()
    out[top:top + height, left:left + height] = _no_wifi(height)
    return out


def _no_wifi(size: int) -> np.ndarray:
    """A Wi-Fi fan with a red X in its lower right corner, chosen over a slash through it,
    which took the fan with it at this size."""
    black, white, red = INK["black"], INK["white"], INK["red"]
    canvas = InkCanvas((size, size))
    canvas.draw.rectangle([0, 0, size - 1, size - 1], outline=black)
    cx, cy = size // 2, size - 3
    for r in (15, 10, 5):
        canvas.draw.arc([cx - r, cy - r, cx + r - 1, cy + r - 1], 225, 315, fill=black, width=2)
    canvas.draw.rectangle([cx - 1, cy - 2, cx, cy - 1], fill=black)
    canvas.draw.rectangle([12, 12, 22, 22], fill=white)
    canvas.draw.line([14, 14, 20, 20], fill=red, width=2)
    canvas.draw.line([14, 20, 20, 14], fill=red, width=2)
    canvas.draw.rectangle([0, 0, size - 1, size - 1], outline=black)
    return canvas.indices()


def _mark(size: int) -> np.ndarray:
    """A crescent, for asleep: a black disc with a white one taking a bite out of it."""
    black, white = INK["black"], INK["white"]
    canvas = InkCanvas((size, size))
    canvas.draw.rectangle([0, 0, size - 1, size - 1], outline=black)
    canvas.draw.ellipse([4, 4, size - 5, size - 5], fill=black)
    canvas.draw.ellipse([9, 2, size - 2, size - 8], fill=white)
    canvas.draw.rectangle([0, 0, size - 1, size - 1], outline=black)
    return canvas.indices()


def _box(percent: int) -> np.ndarray:
    width, height = box_size()
    black = INK["black"]
    canvas = InkCanvas((width, height))
    canvas.draw.rectangle([0, 0, width - 1, height - 1], outline=black)
    x, y = 1 + PAD, 1 + PAD
    (body_w, body_h), (nub_w, nub_h) = BODY, NUB
    canvas.draw.rectangle([x, y, x + body_w - 1, y + body_h - 1], outline=black, width=2)
    nub_y = y + (body_h - nub_h) // 2
    canvas.draw.rectangle([x + body_w, nub_y, x + body_w + nub_w - 1, nub_y + nub_h - 1], fill=black)
    inner = body_w - 6  # inside the outline, with a pixel of white around the fill
    fill = math.ceil(inner * min(max(percent, 0), 100) / 100)
    if fill > 0:
        ink = INK["red"] if percent <= RED_AT_PERCENT else black
        canvas.draw.rectangle([x + 3, y + 3, x + 3 + fill - 1, y + body_h - 4], fill=ink)
    return canvas.indices()
