"""Photo -> ink indices -> panel codes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from photopainter.render.layout import FitMode, canvas_size, enhance, fit_to_canvas
from photopainter.render.palette import CODES, PaletteName, map_to_inks


@dataclass(frozen=True)
class RenderOptions:
    fit: FitMode = "auto"
    portrait: bool = False  # frame hung in portrait: canvas is 480x800
    flip: bool = False  # frame mounted upside down: rotate 180 degrees
    palette: PaletteName = "estimated"
    dither: bool = True
    # Color defaults picked by eye on the panel (2026-11-04): with two photos side by side
    # under each setting, this was the most consistent. One setting has to suit every photo.
    saturation: float = 1.3
    contrast: float = 1.0
    gamma: float = 1.0
    vibrance: float = 0.5


def render(img: Image.Image, opts: RenderOptions = RenderOptions(), size: tuple[int, int] | None = None) -> np.ndarray:
    """Return ink indices for the canvas as the viewer sees it (480x800 if portrait), or for
    a canvas of `size` (width, height), such as the combo screen's quarter."""
    canvas = fit_to_canvas(img, size or canvas_size(opts.portrait), opts.fit)
    canvas = enhance(canvas, opts.saturation, opts.contrast, opts.gamma, opts.vibrance)
    return map_to_inks(canvas, opts.palette, opts.dither)


def to_panel_codes(indices: np.ndarray, opts: RenderOptions = RenderOptions()) -> np.ndarray:
    """Rotate canvas indices into the panel's 480 rows x 800 columns and map to ink codes."""
    if opts.portrait:
        indices = np.rot90(indices)  # counter-clockwise, same as the Waveshare driver
    if opts.flip:
        indices = np.rot90(indices, 2)
    if indices.shape != (480, 800):
        raise ValueError(f"expected 480x800 panel image, got {indices.shape[0]}x{indices.shape[1]}")
    return CODES[indices]
