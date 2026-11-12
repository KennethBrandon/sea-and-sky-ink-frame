#!/usr/bin/env python3
"""Build the sky screen's data files (PRD.md S1): stars, constellation lines, Milky Way.

Run on the Mac only, from the repo root, with the venv (it needs Pillow and NumPy):

    .venv/bin/python scripts/build-star-data.py

It downloads three pinned source files once to ~/.cache/photopainter-dev/star-sources/
(checked against their SHA-256), then writes:

    data/stars.json            {"source": …, "license": …, "stars": [[hip, ra_deg, dec_deg, mag, name, bv], …]}
    data/constellations.json   {"source": …, "license": …, "constellations":
                                  [{"abbr": "Ori", "name": "Orion", "lines": [[hip, hip], …]}, …]}
    data/milkyway.png          the Milky Way's brightness on a galactic-coordinate grid

The star rows are the PRD §5 format plus a sixth field, `bv`: the B−V color index (null if
unknown), which the sky screen uses to color the brightest stars. HYG's B−V is right for
the bright stars except Betelgeuse, a variable, where it has Hipparcos' 1.50; BV_FIXES
puts in the usual value. (HYG's spectral classes were considered instead, but they're
wrong for some bright doubles: Capella "M1", Dubhe "F".)

Sources (credited in data/README.md):

- Stars: the HYG database v4.1 (David Nash, astronexus), from Hipparcos, Yale Bright Star
  and Gliese. Positions are J2000. Kept: every Hipparcos star to magnitude 5.0, plus every
  star a constellation line uses (a few are fainter). Names are HYG's `proper` names
  (the IAU's official star names), for every star kept that has one.
- Constellation lines: Stellarium's "modern" sky culture (v26.3), all 88 IAU
  constellations. Its lines are polylines of Hipparcos numbers; here they become
  de-duplicated pairs. CC BY-SA 4.0, like HYG, so both JSON files are too.
- Milky Way: ESO's all-sky panorama by Serge Brunier (eso0932a, the 6000 × 3000 "large"
  JPEG), CC BY 4.0, credit "ESO/S. Brunier". It's a photograph in galactic coordinates,
  plate carrée: longitude 0 at the center increasing to the left, latitude up. Changes
  made here: converted to brightness, stars removed with a 10 × 10 pixel median (so each
  grid cell is 0.6°), the sky background subtracted, scaled to the brightest part (the
  galactic center), and stored as sqrt(brightness) × 255 in an 8-bit grayscale PNG,
  600 × 300, in the same layout.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

REPO = Path(__file__).resolve().parent.parent
CACHE = Path.home() / ".cache" / "photopainter-dev" / "star-sources"

HYG_COMMIT = "c7f7f883fe678cc7680169a50ccd7dcc49b060ce"  # astronexus/HYG-Database, 2025-02-14
STELLARIUM_TAG = "v26.3"  # Stellarium release, 2026-09-28

SOURCES = {
    "hyg": {
        "file": "hygdata_v41.csv",
        "url": f"https://raw.githubusercontent.com/astronexus/HYG-Database/{HYG_COMMIT}/hyg/CURRENT/hygdata_v41.csv",
        "sha256": "d9f69fd86bbf90a4e4d52b4c5c53eacfa6dfc0bfdef85bfd94f095e0bebe4ebd",
    },
    "stellarium": {
        "file": f"stellarium-{STELLARIUM_TAG}-modern-index.json",
        "url": f"https://raw.githubusercontent.com/Stellarium/stellarium/{STELLARIUM_TAG}/skycultures/modern/index.json",
        "sha256": "1f2f5ffd6c9e25a7d0dcfdbf1f756e2db03dd3b8ed4ec016a2839b09f6b0fe1e",
    },
    "eso": {
        "file": "eso0932a-large.jpg",
        "url": "https://cdn.eso.org/images/large/eso0932a.jpg",
        "sha256": "60400c92c54b7c1bd12299c69e83b16e5b6256e7dabacc478c021758ecd28179",
    },
}
# B−V corrections, by Hipparcos number: (value, why)
BV_FIXES = {
    27989: 1.85,  # Betelgeuse: SIMBAD B 2.27, V 0.42 (Johnson); HYG has Hipparcos' 1.50
}
MW_BLOCK = 10  # source pixels per grid cell, each way: 6000 × 3000 → 600 × 300 (0.6°)

MAG_LIMIT = 5.0
LICENSE = "CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
STARS_SOURCE = (f"HYG database v4.1 (astronexus/HYG-Database @ {HYG_COMMIT[:7]}): Hipparcos stars "
                f"to magnitude {MAG_LIMIT:g} plus every star a constellation line uses; J2000; "
                "names are the IAU star names in HYG's 'proper' field. Built by "
                "scripts/build-star-data.py")
LINES_SOURCE = (f"Stellarium {STELLARIUM_TAG}, skycultures/modern (Stellarium's team): "
                "constellation lines as pairs of Hipparcos numbers. Built by "
                "scripts/build-star-data.py")
USER_AGENT = "photopainter-build-star-data/1"


def fetch(name: str) -> Path:
    """The cached source file, downloading and checking it first if needed."""
    src = SOURCES[name]
    path = CACHE / src["file"]
    if not path.is_file():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"downloading {src['url']}", file=sys.stderr)
        req = urllib.request.Request(src["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != src["sha256"]:
        raise SystemExit(f"{path}: SHA-256 {digest} != expected {src['sha256']}; delete it to "
                         "download again, or update the pin if the source really changed")
    return path


def constellations(index: dict) -> list[dict]:
    """[{"abbr", "name", "lines": [[hip, hip], …]}] sorted by abbreviation."""
    out = []
    for c in index["constellations"]:
        abbr = c["id"].split()[-1]  # "CON modern Ori" -> "Ori"
        pairs: list[list[int]] = []
        seen: set[tuple[int, int]] = set()
        for polyline in c["lines"]:
            for a, b in zip(polyline, polyline[1:]):
                key = (min(a, b), max(a, b))
                if a != b and key not in seen:
                    seen.add(key)
                    pairs.append([int(a), int(b)])
        out.append({"abbr": abbr, "name": c["common_name"]["native"], "lines": pairs})
    return sorted(out, key=lambda c: c["abbr"])


def stars(rows: list[dict], needed: set[int]) -> list[list]:
    """[[hip, ra_deg, dec_deg, mag, name|None, bv|None]] sorted by Hipparcos number."""
    out = []
    for r in rows:
        if not r["hip"]:
            continue
        hip, mag = int(r["hip"]), float(r["mag"])
        if mag > MAG_LIMIT and hip not in needed:
            continue
        ra_deg = float(r["ra"]) * 15.0  # HYG gives RA in hours
        bv = BV_FIXES.get(hip, float(r["ci"]) if r["ci"].strip() else None)
        out.append([hip, round(ra_deg, 4), round(float(r["dec"]), 4), round(mag, 2),
                    r["proper"].strip() or None, None if bv is None else round(bv, 2)])
    return sorted(out, key=lambda s: s[0])


def milky_way(path: Path) -> Image.Image:
    """The ESO panorama → 600 × 300 grayscale: sqrt(Milky Way brightness) × 255."""
    Image.MAX_IMAGE_PIXELS = None
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    h, w = lum.shape
    gh, gw = h // MW_BLOCK, w // MW_BLOCK
    blocks = lum[:gh * MW_BLOCK, :gw * MW_BLOCK].reshape(gh, MW_BLOCK, gw, MW_BLOCK)
    grid = np.median(blocks.transpose(0, 2, 1, 3).reshape(gh, gw, -1), axis=2)  # drops stars
    b = 90.0 - (np.arange(gh) + 0.5) * 180.0 / gh  # galactic latitude of each row
    background = float(np.median(grid[np.abs(b) > 40.0]))
    grid = np.clip(grid - background, 0.0, None)
    grid = grid / float(np.percentile(grid, 99.9))
    img = Image.fromarray(np.round(np.sqrt(np.clip(grid, 0, 1)) * 255).astype(np.uint8), "L")
    return img.filter(ImageFilter.GaussianBlur(0.8))


def write_json(path: Path, head: dict, key: str, items: list) -> None:
    """One item per line: small, and diffs stay readable."""
    lines = [json.dumps(item, ensure_ascii=False, separators=(", ", ": ")) for item in items]
    body = ",\n  ".join(lines)
    head_json = json.dumps(head, ensure_ascii=False)[:-1]  # drop the closing brace
    path.write_text(f'{head_json}, "{key}": [\n  {body}\n]}}\n', encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=REPO / "data", help="output folder (default: data/)")
    args = parser.parse_args(argv)

    with fetch("hyg").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    index = json.loads(fetch("stellarium").read_text(encoding="utf-8"))

    cons = constellations(index)
    needed = {h for c in cons for pair in c["lines"] for h in pair}
    star_rows = stars(rows, needed)
    have = {s[0] for s in star_rows}
    missing = sorted(needed - have)
    if missing:
        raise SystemExit(f"constellation lines use stars missing from HYG: {missing}")

    args.out.mkdir(parents=True, exist_ok=True)
    write_json(args.out / "stars.json", {"source": STARS_SOURCE, "license": LICENSE}, "stars", star_rows)
    write_json(args.out / "constellations.json", {"source": LINES_SOURCE, "license": LICENSE},
               "constellations", cons)
    milky_way(fetch("eso")).save(args.out / "milkyway.png", optimize=True)
    n_named = sum(1 for s in star_rows if s[4])
    n_lines = sum(len(c["lines"]) for c in cons)
    for name in ("stars.json", "constellations.json", "milkyway.png"):
        print(f"{args.out / name}: {(args.out / name).stat().st_size / 1024:.0f} KB", file=sys.stderr)
    print(f"{len(star_rows)} stars ({n_named} named), {len(cons)} constellations, {n_lines} lines",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
