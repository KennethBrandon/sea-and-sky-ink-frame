"""Draw charts and text directly in the panel's six inks.

Tides, sky and message screens are drawn on an `InkCanvas`: a Pillow "P" (palette) image
whose pixel values ARE ink indices (black 0, white 1, yellow 2, red 3, blue 4, green 5:
the order of `palette.INKS`). Pillow never anti-aliases text, lines or shapes on a "P"
image, so the output needs no dithering and stays crisp on the panel.

Always pass inks by index:

    canvas = InkCanvas((800, 480))
    canvas.draw.line([(0, 240), (800, 240)], fill=INK["blue"], width=3)
    canvas.draw.text((20, 20), "High 4.7 ft", font=font(32, bold=True), fill=INK["black"])
    indices = canvas.indices()

An RGB tuple or a color name (`fill="red"`) makes Pillow add a seventh palette entry;
`indices()` catches that and raises instead of sending the panel an ink it doesn't have.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from photopainter import state
from photopainter.render.palette import INKS, preview_image, palette_colors

INK: dict[str, int] = {ink.name: i for i, ink in enumerate(INKS)}

FONT_FILES = {False: "DejaVuSans.ttf", True: "DejaVuSans-Bold.ttf"}


def font_path(bold: bool = False) -> Path:
    return state.data_dir() / "fonts" / FONT_FILES[bold]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """The bundled font (DejaVu Sans) at `size` pixels, cached."""
    return _font(font_path(bold), int(size))


@lru_cache(maxsize=64)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise FileNotFoundError(f"bundled font missing: {path}")
    return ImageFont.truetype(str(path), size)


class InkCanvas:
    """A canvas whose pixels are ink indices.

    `image` is mode "P" with the six (estimated) ink colors as its palette, so saving it
    directly gives a rough preview; `indices()` is what goes to the panel.
    """

    def __init__(self, size: tuple[int, int], background: str = "white") -> None:
        if background not in INK:
            raise ValueError(f"unknown ink {background!r}; inks are {', '.join(INK)}")
        self.image: Image.Image = Image.new("P", size, INK[background])
        self.image.putpalette([c for rgb in palette_colors("estimated") for c in rgb])
        self.draw: ImageDraw.ImageDraw = ImageDraw.Draw(self.image)
        self.draw.fontmode = "1"  # Pillow's default for "P"; set explicitly: never anti-alias

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def indices(self) -> np.ndarray:
        """(height, width) uint8 ink indices, 0-5. Raises if anything was drawn off-palette."""
        out = np.asarray(self.image, dtype=np.uint8).copy()
        if out.max(initial=0) >= len(INKS):
            raise ValueError(
                "the canvas has a color that isn't one of the six inks: pass fill=INK['red'] "
                "(an ink index), not an RGB tuple or a color name"
            )
        return out

    def preview(self) -> Image.Image:
        """RGB image of how the panel will look (same as `palette.preview_image`)."""
        return preview_image(self.indices())


def text_width(text: str, fnt: ImageFont.FreeTypeFont) -> int:
    return round(fnt.getlength(text))


def line_height(fnt: ImageFont.FreeTypeFont, spacing: float = 1.2) -> int:
    """Distance between baselines for `fnt`."""
    ascent, descent = fnt.getmetrics()
    return round((ascent + descent) * spacing)


def wrap_text(text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Break `text` into lines no wider than `max_width` pixels.

    Breaks at spaces; keeps explicit newlines; splits a word only if it can't fit on a
    line by itself.
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, fnt) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while len(word) > 1 and text_width(word, fnt) > max_width:  # split a long word
                cut = 1
                while cut < len(word) and text_width(word[:cut + 1], fnt) <= max_width:
                    cut += 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines
