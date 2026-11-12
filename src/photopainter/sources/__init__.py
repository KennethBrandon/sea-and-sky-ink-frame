"""Data from the web for the day screen, each cached as JSON in the state dir.

| Module       | Source                                   | Cache          | Refetched when older than |
|--------------|------------------------------------------|----------------|---------------------------|
| `tides.py`   | NOAA CO-OPS predictions (station config) | `tides.json`   | 24 h (or < 14 days ahead) |
| `weather.py` | Open-Meteo forecast                      | `weather.json` | 1 h                       |
| `surf.py`    | Open-Meteo marine (offshore swell model) | `surf.json`    | 1 h                       |
| `gauge.py`   | NOAA CO-OPS water levels (gauge config)  | `gauge.json`   | 30 min                    |

Each has `ensure_fresh(config, now=None) -> Refresh` (never raises; see `common.py` for
the rules) and `load() -> dict | None`. The cache formats are PRD.md §5's contracts.

`python -m photopainter.sources [--force]` refreshes them all now and prints what happened.
"""

from __future__ import annotations

from datetime import datetime

from photopainter.config import Config
from photopainter.sources.common import FetchError, Refresh

__all__ = ["FetchError", "Refresh", "ensure_all_fresh"]


def ensure_all_fresh(config: Config, now: datetime | None = None, *,
                     force: bool = False) -> dict[str, Refresh]:
    """`ensure_fresh` for tides, weather, surf and the gauge, in that order. Never raises."""
    from photopainter.sources import gauge, surf, tides, weather

    return {m.SOURCE: m.ensure_fresh(config, now, force=force) for m in (tides, weather, surf, gauge)}
