"""Pick the next photo from a folder, remembering where we left off.

The folder's order is the Owner's: `.order.json` in the folder (the dashboard writes it) names
the photos in the order to show them. Photos it doesn't name come after those, by name, so with
no file the order is plain file names. It's a dot-file so it lives with the photos but stays out
of the rotation, and sync-photos.sh leaves it alone.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable
from pathlib import Path

from photopainter import state

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}
ORDER_FILE = ".order.json"


def load_order(folder: Path) -> list[str]:
    """The names in `.order.json` ([] if it's missing or unreadable: then it's by name)."""
    try:
        data = state.read_json(folder / ORDER_FILE, None)
    except OSError:
        return []
    order = data.get("order") if isinstance(data, dict) else None
    return [n for n in order if isinstance(n, str)] if isinstance(order, list) else []


def save_order(folder: Path, names: list[str]) -> None:
    state.write_json(folder / ORDER_FILE, {"order": names})


def add_to_end(folder: Path, name: str) -> None:
    """Put a photo after all the others."""
    with state.update_json(folder / ORDER_FILE) as data:
        data["order"] = [p.name for p in list_images(folder) if p.name != name] + [name]


def _sort_key(order: list[str]) -> Callable[[str], tuple[int, str]]:
    place = {name: i for i, name in enumerate(order)}
    return lambda name: (place.get(name, len(place)), name)


def list_images(folder: Path) -> list[Path]:
    """Image files directly in `folder`, in the folder's order (hidden files skipped)."""
    images = [
        p
        for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    key = _sort_key(load_order(folder))
    return sorted(images, key=lambda p: key(p.name))


def rotation_order(images: list[Path], last_name: str | None) -> list[Path]:
    """All images, starting with the one after `last_name` and wrapping around.

    `images` is `list_images`' list, or part of it. Works by place in the folder's order, so
    hiding or deleting photos between runs doesn't break the rotation: a photo that has gone
    keeps its place for as long as `.order.json` still names it.
    """
    if not images:
        return []
    start = 0
    if last_name is not None:
        key = _sort_key(load_order(images[0].parent))
        start = bisect.bisect_right([key(p.name) for p in images], key(last_name)) % len(images)
    return images[start:] + images[:start]


class SlideshowState:
    """Remembers the last photo shown for each folder, in a small JSON file."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def last_shown(self, folder: Path) -> str | None:
        return self._load().get(str(folder.resolve()))

    def record(self, folder: Path, name: str) -> None:
        data = self._load()
        data[str(folder.resolve())] = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
