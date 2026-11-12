"""The screens the frame shows: tides, sky, sea, harbor, combo, photo, message, the color test card and Wi-Fi setup.

`render_scene` imports `scenes/<kind>.py` only when that kind is drawn, so a tide tick
never loads Skyfield, and checks that the result is a canvas-sized array of ink indices.
"""

from __future__ import annotations

import importlib

import numpy as np

from photopainter.render.palette import INKS
from photopainter.scenes.base import SCENE_KINDS, Scene, SceneContext, SceneKind

__all__ = ["SCENE_KINDS", "Scene", "SceneContext", "SceneKind", "render_scene"]


def render_scene(scene: Scene, ctx: SceneContext) -> np.ndarray:
    """Draw `scene`: (height, width) uint8 ink indices at `ctx.canvas` size.

    Raises NotImplementedError if that kind's module doesn't exist yet, and ValueError if
    the module returns the wrong size, type or a value outside the six inks.
    """
    name = f"{__name__}.{scene.kind}"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            raise NotImplementedError(f"the {scene.kind} screen isn't built yet ({name})") from None
        raise
    indices = module.render(scene, ctx)
    width, height = ctx.canvas
    if not isinstance(indices, np.ndarray) or indices.dtype != np.uint8:
        raise ValueError(f"{name}.render must return a uint8 numpy array, got {type(indices).__name__}"
                         f"{'/' + str(indices.dtype) if isinstance(indices, np.ndarray) else ''}")
    if indices.shape != (height, width):
        raise ValueError(f"{name}.render returned {indices.shape}, expected (height, width) = "
                         f"{(height, width)}")
    if indices.max(initial=0) >= len(INKS):
        raise ValueError(f"{name}.render returned values outside the six ink indices 0-5")
    return indices
