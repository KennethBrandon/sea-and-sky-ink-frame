"""The photo folder as the dashboard sees it: names, uploads, the trash and preview files.

Names
    A photo is named by its file name in `config.photos.folder`, never a path. Names from
    URLs must pass `check_name` (400 otherwise) and exist (404). Upload names are cleaned
    instead of refused: the folder part is dropped, odd characters become "_", and the
    extension is set from the actual format, so "IMG_1234.HEIC" sent as JPEG is saved as
    "IMG_1234.jpg". A new photo never replaces an old one: the same bytes under the same
    name count as already uploaded, and a different photo gets "-2", "-3"….

Uploads (PRD.md W1)
    The browser converts each photo to JPEG at ≤ 1600 px before sending (the Pi can't read
    HEIC). The server takes JPEG, PNG or WebP up to 25 MB, refuses anything else, and
    shrinks a photo over 1600 px on its long side (after applying its EXIF rotation).
    Smaller photos are saved byte for byte.

Order
    `PUT /api/photos/order` saves the order the grid is dragged into (`slideshow.py` keeps it in
    `.order.json`, and the rotation follows it). A new upload goes at the end. A photo the list
    doesn't name, such as one added while the list was on its way, stays after those it does.

Delete
    Moves the file to `<folder>/.trash/` (hidden files and folders aren't in the rotation).
    Nothing is erased; empty the trash over SSH.

Previews
    `GET /api/photos/<name>/preview.png` is the photo as the panel will show it, dithered.
    Each is drawn once and kept in `photo-previews/` in the state dir, under a name that
    changes when the file, the fit, the orientation or the render settings change.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import unicodedata
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from photopainter import state
from photopainter.config import Config
from photopainter.render.palette import palette_colors
from photopainter.render.pipeline import RenderOptions
from photopainter.slideshow import add_to_end, list_images, save_order

log = logging.getLogger(__name__)

FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_SIDE = 1600
MAX_PIXELS = 60_000_000  # bigger than any phone photo; refuses decompression bombs
MIN_FREE_BYTES = 200 * 1024 * 1024  # keep this much of the SD card free for state and logs
TRASH = ".trash"
MAX_NAME_CHARS = 80
PREVIEW_VERSION = 1  # bump to redraw every cached preview


class PhotoError(Exception):
    """A photo request that can't be done: an HTTP status and a readable message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# --- names -----------------------------------------------------------------------------


def check_name(name: str) -> str:
    """`name` if it's a plain file name in the folder; PhotoError(400) otherwise."""
    if (not isinstance(name, str) or name in ("", ".", "..") or name.startswith(".")
            or any(c in name for c in "/\\\0") or len(name) > 255):
        raise PhotoError(f"{name!r} isn't a photo name", 400)
    return name


def find(folder: Path, name: str) -> Path:
    """The photo `name` in `folder`; PhotoError 400 for a bad name, 404 if it isn't there."""
    path = folder / check_name(name)
    if not path.is_file():
        raise PhotoError(f"there's no photo called {name!r}", 404)
    return path


def listing(folder: Path) -> list[Path]:
    """The photos in `folder` in the rotation's order ([] if there's no folder)."""
    try:
        return list_images(folder)
    except OSError:
        return []


def clean_name(filename: str | None, ext: str) -> str:
    """A safe file name for an upload called `filename`, with extension `ext`."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(base).stem if "." in base else base
    stem = unicodedata.normalize("NFC", stem)
    stem = "".join(c if c.isprintable() else "_" for c in stem)
    stem = re.sub(r"[^\w .,'()&+\-]", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._") or "photo"
    return stem[:MAX_NAME_CHARS] + ext


def _unique(folder: Path, name: str, data: bytes) -> tuple[str, bool]:
    """(name to save as, True if this exact photo is already there under it)."""
    stem, ext = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        candidate = name if n == 1 else f"{stem}-{n}{ext}"
        path = folder / candidate
        if not path.exists():
            return candidate, False
        if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
            return candidate, True
        n += 1


# --- uploads ---------------------------------------------------------------------------


def prepare(data: bytes) -> tuple[bytes, str, dict]:
    """Check an upload and shrink it if needed: (bytes to save, extension, info)."""
    if not data:
        raise PhotoError("the file is empty", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError(f"the file is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
    except Exception:  # noqa: BLE001 - anything Pillow can't identify
        raise PhotoError("that isn't a photo this frame can read (use JPEG, PNG or WebP)", 415) from None
    if fmt not in FORMATS:
        raise PhotoError(f"{fmt or 'that'} files aren't supported; use JPEG, PNG or WebP", 415)
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise PhotoError(f"the photo is too big ({width}×{height}); send it at {MAX_SIDE} px or less", 413)
    info = {"format": fmt, "width": width, "height": height, "resized": False}
    try:
        if fmt == "JPEG":
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # decode at a reduced scale: less memory
        img.load()  # a truncated or corrupt file fails here, not on the frame later
    except Exception as exc:  # noqa: BLE001
        raise PhotoError(f"the photo couldn't be read ({exc})", 415) from None
    if max(width, height) <= MAX_SIDE:
        return data, FORMATS[fmt], info
    img = ImageOps.exif_transpose(img)
    if fmt == "JPEG":
        img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")  # keeps any transparency of palette and gray images
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "JPEG":
        img.save(out, "JPEG", quality=90, optimize=True)
    elif fmt == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "WEBP", quality=90)
    info.update(width=img.width, height=img.height, resized=True)
    return out.getvalue(), FORMATS[fmt], info


def save_upload(folder: Path, filename: str | None, data: bytes) -> dict:
    """Check, shrink and save one uploaded photo. Returns {"name", "duplicate", …info}."""
    body, ext, info = prepare(data)
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < len(body) + MIN_FREE_BYTES:
        raise PhotoError("the frame's SD card is nearly full; delete some photos first", 507)
    name, duplicate = _unique(folder, clean_name(filename, ext), body)
    if not duplicate:
        state.write_atomic(folder / name, body)  # its temp file is hidden from the rotation
        (folder / name).chmod(0o644)  # like photos copied in by sync-photos.sh (temp files are 0600)
        try:
            add_to_end(folder, name)
        except OSError as exc:  # the photo is safe; it just sorts by name among the unnamed
            log.warning("couldn't put %s at the end of the order: %s", name, exc)
    return {"name": name, "duplicate": duplicate, "size": len(body), **info}


# --- order -----------------------------------------------------------------------------


def set_order(folder: Path, names: list[str]) -> list[str]:
    """Save `names` as the photos' order; returns every photo's name in the order now."""
    for name in names:
        check_name(name)
    there = {p.name for p in listing(folder)}
    if there:  # names that have gone since the page loaded are left out
        save_order(folder, [n for n in dict.fromkeys(names) if n in there])
    return [p.name for p in listing(folder)]


# --- trash -----------------------------------------------------------------------------


def move_to_trash(folder: Path, name: str) -> Path:
    """Move a photo to `<folder>/.trash/`; returns where it went."""
    path = find(folder, name)
    trash = folder / TRASH
    trash.mkdir(exist_ok=True)
    dest = trash / name
    if dest.exists():
        dest = trash / f"{path.stem} ({datetime.now():%Y-%m-%d %H%M%S}){path.suffix}"
    path.replace(dest)
    return dest


# --- preview cache ---------------------------------------------------------------------


def _render_settings(config: Config) -> str:
    """Everything besides the file that changes how a photo is drawn."""
    return repr((PREVIEW_VERSION, config.photos.fit, config.display.portrait,
                 asdict(RenderOptions()), palette_colors("estimated")))


def _name_part(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def preview_file(state_dir: Path, photo: Path, config: Config) -> Path:
    """Where the preview of `photo` for these settings is (or will be) cached."""
    st = photo.stat()
    key = f"{st.st_size}:{st.st_mtime_ns}:{_render_settings(config)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return state_dir / state.PHOTO_PREVIEWS / f"{_name_part(photo.name)}-{digest}.png"


def drop_previews(state_dir: Path, name: str, keep: Path | None = None) -> None:
    """Delete cached previews of `name` (except `keep`)."""
    folder = state_dir / state.PHOTO_PREVIEWS
    for old in folder.glob(f"{_name_part(name)}-*.png"):
        if old != keep:
            old.unlink(missing_ok=True)


def version(photo: Path) -> str:
    """Changes whenever the file does: for cache-busting preview URLs."""
    st = photo.stat()
    return f"{st.st_mtime_ns:x}{st.st_size:x}"
