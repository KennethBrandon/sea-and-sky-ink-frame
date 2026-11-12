"""Astronomy for the frame: the Sun (sun.py) and the night sky (sky.py), via Skyfield.

Skyfield and the JPL ephemeris are loaded lazily and once per process, so importing this
package is cheap and screens that don't need astronomy never pay for it. Nothing here
touches the network: the timescale uses Skyfield's built-in tables, and the ephemeris is
the local file `<data dir>/de421.bsp` (fetched by `scripts/fetch-ephemeris.sh`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from photopainter import state

if TYPE_CHECKING:
    from skyfield.jpllib import SpiceKernel
    from skyfield.timelib import Timescale

EPHEMERIS_FILE = "de421.bsp"  # JPL DE421: 1900-2050, about 17 MB


class EphemerisMissing(FileNotFoundError):
    """The JPL ephemeris isn't in the data folder."""


def ephemeris_path() -> Path:
    return state.data_dir() / EPHEMERIS_FILE


def ephemeris() -> SpiceKernel:
    """The JPL DE421 ephemeris (planets, Sun, Moon), loaded once per data folder."""
    return _load_ephemeris(ephemeris_path())


@lru_cache(maxsize=2)
def _load_ephemeris(path: Path) -> SpiceKernel:
    if not path.is_file():
        raise EphemerisMissing(
            f"JPL ephemeris not found at {path}. Run scripts/fetch-ephemeris.sh to download "
            f"it (once per machine) and link it into data/."
        )
    from skyfield.api import load_file

    return load_file(str(path))


@lru_cache(maxsize=1)
def timescale() -> Timescale:
    """Skyfield's timescale from its built-in leap-second and Delta T tables (no download)."""
    from skyfield.api import load

    return load.timescale(builtin=True)
