"""The panel's six inks and mapping RGB images onto them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from PIL import Image

RGB = tuple[int, int, int]
PaletteName = Literal["estimated", "nominal"]


@dataclass(frozen=True)
class Ink:
    name: str
    code: int  # 4-bit value the panel expects for this ink
    nominal: RGB  # pure RGB the Waveshare driver assumes
    estimated: RGB  # rough guess at how the ink really looks (to be calibrated)


# Order matters: an ink's position here is its index in mapped images.
# `estimated` values are rough starting guesses, NOT measurements. The real
# Spectra 6 inks are much duller than pure RGB; replace these once we've
# photographed a swatch card on our panel (see docs/hardware.md).
INKS: tuple[Ink, ...] = (
    Ink("black", 0x0, (0, 0, 0), (30, 30, 35)),
    Ink("white", 0x1, (255, 255, 255), (210, 210, 200)),
    Ink("yellow", 0x2, (255, 255, 0), (220, 200, 40)),
    Ink("red", 0x3, (255, 0, 0), (160, 35, 30)),
    Ink("blue", 0x5, (0, 0, 255), (40, 70, 150)),
    Ink("green", 0x6, (0, 255, 0), (50, 110, 70)),
)

# Lookup: ink index -> panel code.
CODES = np.array([ink.code for ink in INKS], dtype=np.uint8)


def srgb_to_linear(values) -> np.ndarray:
    """sRGB values in 0..1 to linear light, where ink dots add up the way light does."""
    v = np.asarray(values, dtype=np.float64)
    return np.where(v <= 0.04045, v / 12.92, ((np.maximum(v, 0.04045) + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(values) -> np.ndarray:
    """Linear light in 0..1 to sRGB values in 0..1 (the inverse of `srgb_to_linear`)."""
    v = np.asarray(values, dtype=np.float64)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.maximum(v, 0.0031308) ** (1 / 2.4) - 0.055)


# 8-bit sRGB -> 8-bit linear light.
_LINEAR_LUT: list[int] = np.round(srgb_to_linear(np.arange(256) / 255) * 255).astype(int).tolist()


def _white_balanced(colors: list[RGB], black: RGB, white: RGB) -> list[RGB]:
    """Stretch each channel so the black ink becomes (0,0,0) and the white ink (255,255,255).

    The eye adapts to the panel's own white, so a photo's white should land on the white
    ink rather than be dithered with blue to match an "absolute" grayish white. Colored
    inks keep their estimated darkness/saturation relative to black and white.
    """
    out = []
    for color in colors:
        out.append(tuple(
            max(0, min(255, round((c - b) / (w - b) * 255))) for c, b, w in zip(color, black, white)
        ))
    return out


def palette_colors(name: PaletteName) -> list[RGB]:
    """Ink colors to dither against (and to draw previews with), in INKS order."""
    if name == "estimated":
        est = [ink.estimated for ink in INKS]
        return _white_balanced(est, INKS[0].estimated, INKS[1].estimated)
    if name == "nominal":
        return [ink.nominal for ink in INKS]
    raise ValueError(f"unknown palette {name!r}")


def map_to_inks(image: Image.Image, palette: PaletteName = "estimated", dither: bool = True,
                linear: bool = False) -> np.ndarray:
    """Map an RGB image to ink indices (0..5), with Floyd-Steinberg dithering by default.

    `linear` matches colors and spreads the dither error in linear light instead of sRGB.
    Ink dots mix in linear light, so this should get midtones right, at the price of more
    colored dots. Only the color test card uses it so far (`render/card.py`).

    Returns a (height, width) uint8 array of indices into INKS.
    """
    colors = palette_colors(palette)
    rgb = image.convert("RGB")
    if linear:
        rgb = rgb.point(_LINEAR_LUT * 3)
        colors = [tuple(_LINEAR_LUT[c] for c in color) for color in colors]
    pal = Image.new("P", (1, 1))
    pal.putpalette([c for color in colors for c in color])
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    indices = np.asarray(rgb.quantize(palette=pal, dither=mode), dtype=np.uint8)
    if indices.max(initial=0) >= len(INKS):  # guard against Pillow padding the palette
        raise RuntimeError("palette mapping produced an index outside the six inks")
    return indices


def preview_image(indices: np.ndarray, palette: PaletteName = "estimated") -> Image.Image:
    """Render ink indices as RGB, approximating what the panel will show."""
    lut = np.array(palette_colors(palette), dtype=np.uint8)
    return Image.fromarray(lut[indices])  # (h, w, 3) uint8 -> RGB
