"""`python -m photopainter.sources [--force]`: refresh the tide, weather, surf and gauge caches now.

Uses the config and state dir the frame uses (PHOTOPAINTER_CONFIG / PHOTOPAINTER_STATE_DIR
override them). Without `--force` it fetches only what's stale, like the tick does. Exits
1 if any source reports an error.
"""

from __future__ import annotations

import argparse
import logging

from photopainter import state
from photopainter.config import load_config
from photopainter.sources import ensure_all_fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m photopainter.sources", description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="fetch even if the caches are fresh")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    config, config_error = load_config()
    if config_error:
        print(f"config: {config_error} (using the defaults)")
    print(f"state dir: {state.state_dir()}")
    failed = False
    for name, r in ensure_all_fresh(config, force=args.force).items():
        when = state.to_iso(r.fetched_at) if r.fetched_at else "no cache"
        action = "fetched" if r.fetched else "kept"
        print(f"{name:8} {action:8} {when:26} {r.note}")
        if r.error:
            failed = True
            print(f"{'':8} error: {r.error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
