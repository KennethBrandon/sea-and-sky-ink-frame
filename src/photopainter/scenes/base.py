"""What a screen shows (`Scene`) and what it needs to draw it (`SceneContext`).

Every `scenes/<kind>.py` module provides

    def render(scene: Scene, ctx: SceneContext) -> np.ndarray   # (height, width) uint8 ink indices

drawn at `ctx.canvas` size, using only ink indices 0-5 (`render.draw.INK`). Call it through
`scenes.render_scene`, which checks the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from photopainter import state
from photopainter.config import Config
from photopainter.render.layout import canvas_size

SceneKind = Literal["tides", "sky", "sea", "photo", "message", "card", "harbor", "combo", "setup"]
SCENE_KINDS: tuple[SceneKind, ...] = ("tides", "sky", "sea", "photo", "message", "card", "harbor", "combo", "setup")

# A dashboard preview of the sea can ask for these instead of the forecast (`scenes/sea.py`).
WAVE_LOOKS: tuple[str, ...] = ("calm", "big", "windy", "gale")
CLOUD_LOOKS: tuple[str, ...] = ("clear", "partly", "overcast")


@dataclass(frozen=True)
class Scene:
    kind: SceneKind
    key: str  # the panel refreshes iff this differs from what's showing
    draw_for: datetime  # the moment the image depicts (timezone-aware)
    photo: str | None = None  # photo scenes: file name, or None = next in rotation
    message: str | None = None  # message scenes
    updated: datetime | None = None  # sea and combo scenes: when the screen came up, if not `draw_for`
    waves: str | None = None  # sea previews: a look from WAVE_LOOKS instead of the forecast's swell and wind
    clouds: str | None = None  # sea previews: a look from CLOUD_LOOKS instead of the forecast's sky

    def __post_init__(self) -> None:
        if self.kind not in SCENE_KINDS:
            raise ValueError(f"unknown scene kind {self.kind!r}; kinds are {', '.join(SCENE_KINDS)}")
        if self.draw_for.tzinfo is None or self.draw_for.utcoffset() is None:
            raise ValueError(f"Scene.draw_for must be timezone-aware, got {self.draw_for!r}")


@dataclass(frozen=True)
class SceneContext:
    config: Config
    canvas: tuple[int, int]  # (width, height): (800, 480), or (480, 800) when portrait
    state_dir: Path
    data_dir: Path
    any_age: bool = False  # a preview of a chosen moment: use the forecast for it, however old the fetch

    @classmethod
    def from_config(cls, config: Config, state_dir: Path | None = None,
                    data_dir: Path | None = None) -> SceneContext:
        """Canvas from `config.display.portrait`; folders from `state` unless given."""
        return cls(
            config=config,
            canvas=canvas_size(config.display.portrait),
            state_dir=state.state_dir() if state_dir is None else state_dir,
            data_dir=state.data_dir() if data_dir is None else data_dir,
        )
