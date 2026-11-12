"""The color test card as a screen (see `render/card.py`). It's shown by a card pin, which
the dashboard's Settings sets; the schedule never picks it."""

from __future__ import annotations

import numpy as np

from photopainter.render.card import card_indices
from photopainter.scenes.base import Scene, SceneContext


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    """The card, always landscape on the panel: on a portrait canvas it lies on its side,
    which `to_panel_codes` turns back."""
    indices = card_indices()
    return np.rot90(indices, -1).copy() if ctx.canvas == (480, 800) else indices
