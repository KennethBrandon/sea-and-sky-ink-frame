"""Where things live, and safe reads and writes of the small JSON files the frame keeps.

Paths (PRD.md §5 "Paths"); each can be overridden with an environment variable, which
tests and the Mac use:

| What      | Default                               | Override                  |
|-----------|---------------------------------------|---------------------------|
| config    | ~/.config/photopainter/config.json    | PHOTOPAINTER_CONFIG       |
| state dir | ~/.cache/photopainter/                | PHOTOPAINTER_STATE_DIR    |
| data dir  | <repo>/data/                          | PHOTOPAINTER_DATA_DIR     |

All JSON writes go through `write_json` / `update_json`: the data is written to a temp file
in the same folder, flushed to disk, then renamed over the old file, so a reader (or a
power cut) never sees half a file. Both hold an exclusive `fcntl` lock on a sidecar lock
file while they work, so a read-modify-write by the dashboard and one by `tick` can't
interleave and lose an update.

Timestamps in JSON are ISO 8601 with a UTC offset: `write_json` serializes aware
`datetime`s that way and refuses naive ones; `from_iso` refuses strings without an offset.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parents[2]  # src/photopainter/state.py -> repo root

CONFIG_ENV = "PHOTOPAINTER_CONFIG"
STATE_DIR_ENV = "PHOTOPAINTER_STATE_DIR"
DATA_DIR_ENV = "PHOTOPAINTER_DATA_DIR"

# File names inside the state dir (PRD.md §5). The first two already exist and are
# written by hardware/panel.py and slideshow.py.
PANEL_LOCK = "panel.lock"
SLIDESHOW = "slideshow.json"
OVERRIDES = "overrides.json"
STATUS = "status.json"
CURRENT_PNG = "current.png"
INCOMING_PNG = "incoming.png"  # the picture a refresh under way is bringing in
TIDES = "tides.json"
PHOTO_PREVIEWS = "photo-previews"

LOCK_TIMEOUT_S = 10.0


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def config_path() -> Path:
    """The config file: $PHOTOPAINTER_CONFIG, or ~/.config/photopainter/config.json."""
    return _env_path(CONFIG_ENV) or Path.home() / ".config" / "photopainter" / "config.json"


def state_dir() -> Path:
    """Runtime state: $PHOTOPAINTER_STATE_DIR, or ~/.cache/photopainter/. Not created here."""
    return _env_path(STATE_DIR_ENV) or Path.home() / ".cache" / "photopainter"


def data_dir() -> Path:
    """Bundled data (fonts, stars, ephemeris): $PHOTOPAINTER_DATA_DIR, or <repo>/data/."""
    return _env_path(DATA_DIR_ENV) or REPO_DIR / "data"


# --- timestamps ----------------------------------------------------------------


def to_iso(dt: datetime) -> str:
    """ISO 8601 with a UTC offset, to the second. Refuses naive datetimes."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime {dt!r}: every datetime must be timezone-aware")
    return dt.isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries a UTC offset (`Z` is accepted)."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {text!r} has no UTC offset")
    return dt


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return to_iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def dumps(data: Any) -> str:
    """JSON text as `write_json` writes it (aware datetimes become ISO 8601 strings)."""
    return json.dumps(data, indent=2, ensure_ascii=False, default=_json_default) + "\n"


# --- locking and atomic writes ---------------------------------------------------------


def _lock_path(path: Path) -> Path:
    # A sidecar file: the data file itself is replaced on every write, so a lock taken on
    # it would be on an inode that no longer has the name.
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked(path: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold the exclusive lock for `path` (raises TimeoutError after `timeout` seconds).

    Not reentrant: don't call `write_json(path)` or `update_json(path)` while holding it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(_lock_path(path), "a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{path.name} stayed locked for {timeout:.0f} s") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step (temp file + fsync + rename). No lock taken.

    Use it directly for non-JSON files such as `current.png`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:  # make the rename itself survive a power cut
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json(path: Path, default: Any = None) -> Any:
    """The parsed file, or a copy of `default` if it's missing or isn't valid JSON.

    A corrupt file is logged and treated as missing: every state file can be rebuilt, and
    the frame should keep running rather than stop on it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return copy.deepcopy(default)


def write_json(path: Path, data: Any) -> None:
    """Atomically replace `path` with `data` as JSON, under the file's lock."""
    text = dumps(data).encode("utf-8")  # serialize first: a TypeError leaves the file alone
    with locked(path):
        write_atomic(path, text)


@contextmanager
def update_json(path: Path, default: dict | None = None) -> Iterator[dict]:
    """Read-modify-write a JSON object under the file's lock.

        with update_json(state_dir() / OVERRIDES, {"pin": None, "up_next": []}) as data:
            data["up_next"].append("beach.jpg")

    Yields the current object (or a copy of `default` if the file is missing, corrupt or
    not an object); it's written back when the block exits without an exception.
    """
    with locked(path):
        data = read_json(path, None)
        if not isinstance(data, dict):
            data = copy.deepcopy(default) if default is not None else {}
        yield data
        write_atomic(path, dumps(data).encode("utf-8"))
