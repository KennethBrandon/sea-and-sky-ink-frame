"""Which screens the frame can show where it is (PRD.md §9, L2).

A screen needs data this place may not have: the harbor chart needs place data whose chart
area holds the location (`places.harbor_for`). Where it's missing, the screen gives way the
way a photo step does with no photos (`Schedule.without`): it leaves both rotations and its
time goes to the steps around it. The dashboard refuses a pin of it and says why, and the
tick lets a pin set before a move lapse rather than draw an error.

The tick, the buttons and the dashboard's "coming up" all decide with `schedule_here`, so a
rotation the buttons move has the same steps the tick runs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from photopainter import places, state
from photopainter.config import Config

HARBOR_MISSING = ("No harbor chart for this place: build one on a computer with scripts/build-harbor.py "
                  "and upload it")


def missing(config: Config, data_dir: Path | str | None = None) -> dict[str, str]:
    """Screens with no data for this location, each with why."""
    data_dir = state.data_dir() if data_dir is None else data_dir
    loc = config.location
    out: dict[str, str] = {}
    if places.harbor_for(loc.lat, loc.lon, data_dir) is None:
        out["harbor"] = HARBOR_MISSING
    return out


def schedule_here(config: Config, *, photos: bool, data_dir: Path | str | None = None,
                  gone: dict[str, str] | None = None) -> Config:
    """`config` with the rotations' steps of screens that can't be shown here given to their
    neighbors: missing place data (`gone`, else worked out), and photo steps when there's no
    photo (`photos` false)."""
    drop = set(missing(config, data_dir) if gone is None else gone)
    if not photos:
        drop.add("photo")
    steps = config.schedule.day + config.schedule.night
    if not any(s.screen in drop for s in steps):
        return config
    return replace(config, schedule=config.schedule.without(drop))
