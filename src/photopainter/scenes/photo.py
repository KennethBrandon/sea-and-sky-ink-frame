"""A photo from the photo folder (scene kind `photo`), dithered to the six inks.

`scene.photo` names a file in `config.photos.folder`; None means the next one in the
rotation. The rotation is `slideshow.py`'s, in the folder's order from the last one shown, and
its position (`slideshow.json` in the state dir) is shared with `photopainter next`. Hidden
photos (`config.photos.hidden`) are left out of the rotation.

Rendering never moves the rotation on: the tick calls `record_shown` once the panel has
shown a rotation photo. Photos from Up next or Show now don't move it either, since they
come ahead of the rotation rather than from it.

Color settings are `RenderOptions`' defaults (CLAUDE.md "Display rules"); the fit comes from
`config.photos.fit` and the canvas from the scene context.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import numpy as np

from photopainter import state
from photopainter.config import Config
from photopainter.render.layout import open_image
from photopainter.render.pipeline import RenderOptions
from photopainter.render.pipeline import render as render_photo
from photopainter.scenes.base import Scene, SceneContext
from photopainter.slideshow import SlideshowState, list_images, rotation_order

log = logging.getLogger(__name__)


class NoPhoto(RuntimeError):
    """There's no photo that can be shown."""


def options(config: Config) -> RenderOptions:
    """How photos are rendered for this config (the panel's flip included)."""
    return RenderOptions(fit=config.photos.fit, portrait=config.display.portrait, flip=config.display.flip)


def folder(ctx: SceneContext) -> Path:
    return ctx.config.photos.path


def available(config: Config) -> bool:
    """Whether the rotation has any photo to show (hidden ones don't count)."""
    hidden = set(config.photos.hidden)
    return any(p.name not in hidden for p in _images(config.photos.path))


def rotation(ctx: SceneContext) -> list[Path]:
    """Every photo in the rotation, starting with the one after the last one shown."""
    hidden = set(ctx.config.photos.hidden)
    images = [p for p in _images(folder(ctx)) if p.name not in hidden]
    return rotation_order(images, _slideshow(ctx).last_shown(folder(ctx)))


def candidates(scene: Scene, ctx: SceneContext) -> list[Path]:
    """Files to try for `scene`, best first: the named photo if it's there, then the
    rotation. The tick renders the first that works."""
    named: list[Path] = []
    if scene.photo is not None:
        path = _named(scene.photo, ctx)
        if path is not None:
            named.append(path)
        else:
            log.warning("photo %r isn't in %s; showing the next in the rotation", scene.photo, folder(ctx))
    return named + [p for p in rotation(ctx) if p not in named]


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    """The named photo, or the first photo in the rotation that can be read."""
    if scene.photo is not None:
        path = _named(scene.photo, ctx)
        if path is None:
            raise NoPhoto(f"photo {scene.photo!r} isn't in {folder(ctx)}")
        return _render_file(path, ctx)
    problem: Exception | None = None
    for path in rotation(ctx):
        try:
            return _render_file(path, ctx)
        except Exception as exc:  # noqa: BLE001 - skip unreadable files, like `photopainter next`
            log.warning("skipping %s: %s", path.name, exc)
            problem = exc
    if problem is not None:
        raise NoPhoto(f"none of the photos in {folder(ctx)} could be read (last: {problem})")
    raise NoPhoto(f"no photos to show in {folder(ctx)}")


def current(ctx: SceneContext) -> list[Path]:
    """The photo the rotation is on, then the rest of it: the last one shown if it's still
    there and not hidden, else the next. Empty with no photos to show."""
    hidden = set(ctx.config.photos.hidden)
    images = [p for p in _images(folder(ctx)) if p.name not in hidden]
    last = _slideshow(ctx).last_shown(folder(ctx))
    here = [p for p in images if p.name == last]
    return here + [p for p in rotation_order(images, last) if p not in here]


def render_at(path: Path, size: tuple[int, int], config: Config) -> np.ndarray:
    """A photo dithered for a canvas of `size` (width, height), as photos are for the panel."""
    return render_photo(open_image(path), options(config), size=size)


def record_shown(ctx: SceneContext, name: str) -> None:
    """Move the rotation on: `name` was the last photo shown."""
    _slideshow(ctx).record(folder(ctx), name)


def _render_file(path: Path, ctx: SceneContext) -> np.ndarray:
    width, height = ctx.canvas
    opts = replace(options(ctx.config), portrait=height > width)
    return render_photo(open_image(path), opts)


def _named(name: str, ctx: SceneContext) -> Path | None:
    if not name or Path(name).name != name or name in (".", ".."):
        return None  # a path, not a file name in the folder
    path = folder(ctx) / name
    return path if path.is_file() else None


def _images(path: Path) -> list[Path]:
    try:
        return list_images(path)
    except OSError:  # no folder (yet), or unreadable
        return []


def _slideshow(ctx: SceneContext) -> SlideshowState:
    return SlideshowState(ctx.state_dir / state.SLIDESHOW)
