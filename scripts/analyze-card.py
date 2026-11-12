#!/usr/bin/env python3
"""Measure the panel's inks from photos of the color test card (Mac).

Show the card from the dashboard (Settings > Color test card), photograph it, then:

    .venv/bin/python scripts/analyze-card.py PXL_0001.RAW-02.ORIGINAL.dng IMG_0002.HEIC

Prints the inks, how dots mix and how close dithered colors get, averaged over the photos,
and saves each photo with the measured areas outlined to previews/card-check-<name>.png.
Steps and what to look for: docs/hardware.md, "Measuring the ink colors".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from photopainter.render.card_analysis import (
    CardNotFound, build_report, load_photo, measure, report_text, write_overlay,
)


def parse_corners(text: str) -> list[tuple[float, float]]:
    """"x,y x,y x,y x,y" -> four points."""
    try:
        return [(float(a), float(b)) for a, b in (point.split(",") for point in text.split())]
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: expected \"x,y x,y x,y x,y\"") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("photos", nargs="+", type=Path, help="photos of the card (DNG, HEIC, JPEG, PNG)")
    parser.add_argument("--corners", type=parse_corners, metavar='"x,y x,y x,y x,y"',
                        help="the corner markers' centers in photo pixels, if they aren't found "
                             "on their own (one photo at a time)")
    parser.add_argument("--json", type=Path, help="also save the full results as JSON here")
    parser.add_argument("--check-dir", type=Path, default=Path("previews"),
                        help="where to save the outlined photos (default: previews/)")
    args = parser.parse_args()
    if args.corners and len(args.photos) > 1:
        parser.error("--corners works with one photo at a time")

    measured = []
    for path in args.photos:
        try:
            result = measure(load_photo(path), args.corners)
        except (CardNotFound, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        write_overlay(result, args.check_dir / f"card-check-{path.stem}.png")
        measured.append(result)

    report = build_report(measured)
    print(report_text(report))
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    print(f"\nCheck the outlines in {args.check_dir}/card-check-*.png sit inside their patches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
