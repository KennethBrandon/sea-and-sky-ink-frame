"""A plain text screen: notices like "Clock not set" or "No Wi-Fi", and `scene message`.

The first line of `scene.message` is the heading (bold, as large as fits); any further
lines are body text, smaller. Everything is centered, wrapped to the canvas and shrunk
until it fits; if even the smallest size overflows, the body is cut with an ellipsis.
"""

from __future__ import annotations

import numpy as np

from photopainter.render.draw import INK, InkCanvas, font, line_height, wrap_text
from photopainter.scenes.base import Scene, SceneContext

MARGIN = 40
ACCENT_HEIGHT = 10  # a blue band along the top edge marks a frame notice
HEADING_SIZES = range(88, 23, -4)  # largest first
BODY_RATIO = 0.55
MIN_BODY_SIZE = 20
PARAGRAPH_GAP = 0.6  # gap between heading and body, as a fraction of the body line height


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    if scene.message is None:
        raise ValueError("a message scene needs a message")
    heading, _, body = scene.message.strip().partition("\n")
    heading, body = heading.strip(), body.strip()

    canvas = InkCanvas(ctx.canvas, "white")
    width, height = ctx.canvas
    if heading or body:
        canvas.draw.rectangle([0, 0, width - 1, ACCENT_HEIGHT - 1], fill=INK["blue"])
    box_w = width - 2 * MARGIN
    box_top = ACCENT_HEIGHT + MARGIN
    box_h = height - box_top - MARGIN

    lines = _fit(heading, body, box_w, box_h)
    total = sum(step for _, _, step in lines)
    y = box_top + max(0, (box_h - total) // 2)
    for text, fnt, step in lines:
        if text:
            canvas.draw.text((width // 2, y), text, font=fnt, fill=INK["black"], anchor="ma")
        y += step
    return canvas.indices()


def _fit(heading: str, body: str, box_w: int, box_h: int):
    """[(text, font, line step)] for the largest sizes at which everything fits."""
    for size in HEADING_SIZES:
        lines = _lines(heading, body, size, box_w)
        if sum(step for _, _, step in lines) <= box_h:
            return lines
    # Still too tall at the smallest size: keep what fits, end with an ellipsis.
    kept, used = [], 0
    for text, fnt, step in lines:
        if used + step > box_h:
            if kept:
                last_text, last_font, last_step = kept[-1]
                kept[-1] = (last_text.rstrip(" .,;:") + "…", last_font, last_step)
            break
        kept.append((text, fnt, step))
        used += step
    return kept


def _lines(heading: str, body: str, size: int, box_w: int):
    head_font = font(size, bold=True)
    body_font = font(max(MIN_BODY_SIZE, round(size * BODY_RATIO)))
    out = []
    if heading:
        out += [(t, head_font, line_height(head_font, 1.1)) for t in wrap_text(heading, head_font, box_w)]
    if body:
        step = line_height(body_font, 1.3)
        if heading:
            out.append(("", body_font, round(step * PARAGRAPH_GAP)))
        out += [(t, body_font, step) for t in wrap_text(body, body_font, box_w)]
    return out
