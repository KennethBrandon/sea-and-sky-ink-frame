"""The color test card: what to show on the panel to measure its inks.

A photo of this card, read by `render/card_analysis.py` (`scripts/analyze-card.py`), gives
the real ink colors, how ink dots mix, and how close a dithered color gets to its target.
The steps are in docs/hardware.md, "Measuring the ink colors".

Eleven columns by six rows of square cells on white:

    F      white  black  yellow red    white  blue   green  black  white  F
    white  . . dot mixes (MIXES) . .   black  . . dot mixes (MIXES) . .   white
    green  . . targets, today's  . .   white  . . targets, today's  . .   red
    white  . . same, linear      . .   yellow . . same, linear      . .   white
    blue   . . target pairs      . .   white  . . target pairs      . .   black
    F      white  yellow red    blue   white  green  black  yellow white  F

- F is a corner marker (QR-style), which the analysis finds in the photo. Reading the inks
  next to them tells it which way up the card is, so a flipped panel doesn't matter.
- Solid inks repeat across the panel, and whites sit between them, so uneven light can be
  measured and removed.
- Dot mixes are ordered patterns of two inks, to measure whether dots add up in linear
  light, and (the 1, 2 and 4 px blacks and whites) whether dots bleed into each other.
- Targets are known colors dithered from solid tiles, each by today's dither and by linear
  light (`map_to_inks`), without the photo color boost. Each is dithered alone, so error
  doesn't leak between patches. The bottom row's pairs read today, then linear.

Only the middle of each cell is measured (`Patch.sample`), a whole number of dot patterns
wide. The analysis redraws the card to learn each patch's dots, so it has to run on the
same palette as the code that showed the card.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from PIL import Image

from photopainter.render.draw import INK, InkCanvas, font, text_width
from photopainter.render.layout import PANEL_SIZE
from photopainter.render.palette import RGB, map_to_inks

CELL = 62
GUTTER = 8
PITCH = CELL + GUTTER
COLS, ROWS = 11, 6
LEFT = (PANEL_SIZE[0] - (COLS * PITCH - GUTTER)) // 2
TOP = 44  # room for the title above the grid
SAMPLE_INSET = 11  # leaves 40 px: whole periods of the 2, 4 and 8 px dot patterns
MODULE = 8  # a corner marker is 7 modules wide (1:1:3:1:1 across its middle)

Method = Literal["today", "linear"]


@dataclass(frozen=True)
class Mix:
    name: str
    first: str  # ink name
    second: str
    fraction: float  # share of `second`
    dot: int = 1  # dot size in pixels


@dataclass(frozen=True)
class Target:
    name: str
    rgb: RGB


MIXES: tuple[Mix, ...] = (
    Mix("black/white 25%", "black", "white", 0.25),
    Mix("black/white 50%", "black", "white", 0.50),
    Mix("black/white 75%", "black", "white", 0.75),
    Mix("black/white 50%, 2 px", "black", "white", 0.50, dot=2),
    Mix("black/white 50%, 4 px", "black", "white", 0.50, dot=4),
    Mix("red/yellow 50%", "red", "yellow", 0.50),
    Mix("red/white 50%", "red", "white", 0.50),
    Mix("blue/white 50%", "blue", "white", 0.50),
)

TARGETS: tuple[Target, ...] = (
    Target("gray 48", (48, 48, 48)),
    Target("gray 96", (96, 96, 96)),
    Target("gray 128", (128, 128, 128)),
    Target("gray 160", (160, 160, 160)),
    Target("gray 208", (208, 208, 208)),
    Target("light skin", (224, 172, 150)),
    Target("dark skin", (141, 85, 62)),
    Target("sky", (120, 165, 215)),
    Target("foliage", (90, 125, 60)),
    Target("sand", (210, 190, 150)),
    Target("sunset", (230, 120, 50)),
    Target("deep sea", (30, 80, 120)),
)

# F: corner marker. A letter: a solid ink. mN: MIXES[N]. tN, lN: TARGETS[N] by today's
# dither and by linear light.
LAYOUT: tuple[str, ...] = (
    "F  W  K  Y  R  W  B  G  K  W  F",
    "W  m0 m1 m2 m3 K  m4 m5 m6 m7 W",
    "G  t0 t1 t2 t3 W  t4 t5 t6 t7 R",
    "W  l0 l1 l2 l3 Y  l4 l5 l6 l7 W",
    "B  t8 l8 t9 l9 W  t10 l10 t11 l11 K",
    "F  W  Y  R  B  W  G  K  Y  W  F",
)
_SOLIDS = {"K": "black", "W": "white", "Y": "yellow", "R": "red", "B": "blue", "G": "green"}
_BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]])


def cell_rect(col: int, row: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a grid cell on the 800x480 card."""
    x, y = LEFT + col * PITCH, TOP + row * PITCH
    return x, y, x + CELL, y + CELL


@dataclass(frozen=True)
class Patch:
    col: int
    row: int
    ink: str | None = None  # solid patches
    mix: Mix | None = None  # dot mixes
    target: Target | None = None  # dithered targets
    method: Method | None = None

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return cell_rect(self.col, self.row)

    @property
    def sample(self) -> tuple[int, int, int, int]:
        """The part of the cell to measure, away from its edges."""
        x0, y0, x1, y1 = self.rect
        return x0 + SAMPLE_INSET, y0 + SAMPLE_INSET, x1 - SAMPLE_INSET, y1 - SAMPLE_INSET

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.rect
        return (x0 + x1) / 2, (y0 + y1) / 2


def _parse_layout() -> tuple[tuple[Patch, ...], tuple[tuple[float, float], ...]]:
    patches: list[Patch] = []
    corners: dict[tuple[int, int], tuple[float, float]] = {}
    for row, line in enumerate(LAYOUT):
        for col, token in enumerate(line.split()):
            if token == "F":
                x0, y0, x1, y1 = cell_rect(col, row)
                corners[col, row] = ((x0 + x1) / 2, (y0 + y1) / 2)
            elif token in _SOLIDS:
                patches.append(Patch(col, row, ink=_SOLIDS[token]))
            elif token[0] == "m":
                patches.append(Patch(col, row, mix=MIXES[int(token[1:])]))
            else:
                method: Method = "today" if token[0] == "t" else "linear"
                patches.append(Patch(col, row, target=TARGETS[int(token[1:])], method=method))
    last_col, last_row = COLS - 1, ROWS - 1
    clockwise = (corners[0, 0], corners[last_col, 0], corners[last_col, last_row], corners[0, last_row])
    return tuple(patches), clockwise


PATCHES, MARKERS = _parse_layout()  # MARKERS: corner marker centers, clockwise from top left


def _mix_tile(mix: Mix, size: int = CELL) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    second = _BAYER[(yy // mix.dot) % 4, (xx // mix.dot) % 4] < mix.fraction * 16
    return np.where(second, INK[mix.second], INK[mix.first]).astype(np.uint8)


def _target_tile(target: Target, method: Method, size: int = CELL) -> np.ndarray:
    return map_to_inks(Image.new("RGB", (size, size), target.rgb), linear=method == "linear")


def _marker(size: int = 7 * MODULE) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    ring = np.maximum(np.abs(yy // MODULE - 3), np.abs(xx // MODULE - 3))  # 3: edge, 2: white, 0-1: middle
    return np.where(ring == 2, INK["white"], INK["black"]).astype(np.uint8)


def card_indices() -> np.ndarray:
    """The card as (480, 800) ink indices, landscape."""
    canvas = InkCanvas(PANEL_SIZE)
    width, height = PANEL_SIZE
    for text, size, y in (("Color test card: photograph it square-on, without glare", 18, 12),
                          ("Rows 3 and 4: the same colors by today's dither and by linear light. "
                           "Bottom pairs: today, then linear.", 11, height - 19)):
        fnt = font(size, bold=size > 12)
        canvas.draw.text(((width - text_width(text, fnt)) // 2, y), text, font=fnt, fill=INK["black"])
    out = canvas.indices()
    for patch in PATCHES:
        x0, y0, x1, y1 = patch.rect
        if patch.ink is not None:
            out[y0:y1, x0:x1] = INK[patch.ink]
        elif patch.mix is not None:
            out[y0:y1, x0:x1] = _mix_tile(patch.mix)
        else:
            out[y0:y1, x0:x1] = _target_tile(patch.target, patch.method)
    marker = _marker()
    half = marker.shape[0] // 2
    for cx, cy in MARKERS:
        x, y = round(cx) - half, round(cy) - half
        out[y:y + marker.shape[0], x:x + marker.shape[1]] = marker
    return out


def ink_shares(indices: np.ndarray, patch: Patch) -> np.ndarray:
    """How much of `patch`'s measured area each of the six inks covers (sums to 1)."""
    x0, y0, x1, y1 = patch.sample
    counts = np.bincount(indices[y0:y1, x0:x1].ravel(), minlength=6)
    return counts / counts.sum()
