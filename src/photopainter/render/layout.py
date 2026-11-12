"""Fit photos onto the frame's canvas."""

from __future__ import annotations

from typing import Literal

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

PANEL_SIZE = (800, 480)  # the panel's native landscape resolution
FitMode = Literal["auto", "fill", "fit"]
# auto mode crops (fill) only if that removes at most this fraction of the photo.
# 4:3 and 3:2 photos on the 5:3 panel lose 10-20% -> fill; portrait/square -> fit.
AUTO_MAX_CROP = 0.35


def open_image(source, draft_size: int = 1600) -> Image.Image:
    """Open an image file (path or binary file object), applying EXIF rotation.

    For JPEGs, decode at a reduced scale (at least `draft_size` on each side) to save
    time and memory on the Pi Zero.
    """
    _register_heif()
    img = Image.open(source)
    if img.format == "JPEG":
        img.draft("RGB", (draft_size, draft_size))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _register_heif() -> None:
    """Let Pillow open HEIC/HEIF if pillow-heif is installed (dev extra on the Mac)."""
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


def canvas_size(portrait: bool) -> tuple[int, int]:
    w, h = PANEL_SIZE
    return (h, w) if portrait else (w, h)


def crop_fraction(img_size: tuple[int, int], canvas: tuple[int, int]) -> float:
    """Fraction of the photo's area that a fill (cover + crop) would cut away."""
    a = img_size[0] / img_size[1]
    b = canvas[0] / canvas[1]
    return 1 - min(a, b) / max(a, b)


def fit_to_canvas(
    img: Image.Image,
    size: tuple[int, int],
    mode: FitMode = "auto",
    background: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """Resize `img` to exactly `size`.

    fill: scale to cover the canvas and crop the overflow (center crop).
    fit:  scale to fit inside the canvas and pad with `background`.
    auto: fill if that crops at most AUTO_MAX_CROP of the photo, otherwise fit.
    """
    if mode == "auto":
        mode = "fill" if crop_fraction(img.size, size) <= AUTO_MAX_CROP else "fit"
    if mode == "fill":
        return ImageOps.fit(img, size, Image.Resampling.LANCZOS)
    if mode == "fit":
        inner = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, background)
        canvas.paste(inner, ((size[0] - inner.width) // 2, (size[1] - inner.height) // 2))
        return canvas
    raise ValueError(f"unknown fit mode {mode!r}")


def vibrance(img: Image.Image, amount: float) -> Image.Image:
    """Boost muted colors more than already-saturated ones (amount 0 = no change).

    Each pixel's distance from its gray is scaled by 1 + amount * (1 - s), where s is
    the pixel's saturation (0..1), so skin tones and strong colors change little.
    """
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    gray = (a @ np.array([0.299, 0.587, 0.114], dtype=np.float32))[..., None]
    sat = (a.max(axis=2, keepdims=True) - a.min(axis=2, keepdims=True)) / 255.0
    out = gray + (a - gray) * (1.0 + amount * (1.0 - sat))
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


def enhance(
    img: Image.Image,
    saturation: float = 1.0,
    contrast: float = 1.0,
    gamma: float = 1.0,
    vibrance_amount: float = 0.0,
) -> Image.Image:
    """Tone and color adjustments before dithering (all defaults = no change).

    gamma > 1 brightens midtones without clipping highlights (out = in ** (1 / gamma)).
    vibrance_amount > 0 boosts muted colors (see `vibrance`); saturation scales all color.
    """
    if gamma != 1.0:
        lut = [round(255 * (i / 255) ** (1 / gamma)) for i in range(256)]
        img = img.point(lut * 3)
    if vibrance_amount != 0.0:
        img = vibrance(img, vibrance_amount)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    return img
