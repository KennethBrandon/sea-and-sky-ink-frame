#!/usr/bin/env python3
"""Build data/places/wedge/skyline.json: Santa Catalina Island's skyline as seen from the Wedge.

Run on the Mac only, from the repo root:

    SSL_CERT_FILE=/etc/ssl/cert.pem .venv/bin/python scripts/build-skyline.py

It samples the island's terrain on a 0.005° grid (Copernicus DEM GLO-90 elevations,
through Open-Meteo's elevation API, 100 points per request; cached in
~/.cache/photopainter-dev/ so a rerun fetches nothing), interpolates it 5× finer so no
ridge falls between samples, then for every point works out its direction and its angle
above the sea horizon as seen from the observer: eye 10 m above the sea, Earth's
curvature with standard refraction (k = 0.13). The skyline is the highest angle in each
0.25° of azimuth, smoothed over about 1° (distant haze softens the ridge; compared with a
photo of the island from the coast, 2026-11-04). The sky screen draws it on the horizon,
a little exaggerated (its real height is half a degree).

Output:
    {"source": …, "license": …, "name": "Santa Catalina Island",
     "observer": {"lat": 33.593, "lon": -117.882, "eye_m": 10},
     "points": [[az_deg, height_deg], …]}   # height above the sea horizon; 0 at both ends
"""

from __future__ import annotations

import json
import math
import sys

import numpy as np
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OBSERVER = (33.593, -117.882)  # the Wedge (PRD D7), never the Viewer's address
EYE_M = 10.0  # a balcony above the beach
LAT_RANGE = (33.29, 33.49)  # the island's bounding box
LON_RANGE = (-118.61, -118.29)
STEP = 0.005
AZ_BIN = 0.25
FINE = 5  # interpolate the elevation grid this many times finer
EARTH_R = 6_371_000.0
REFRACTION_K = 0.13
API = "https://api.open-meteo.com/v1/elevation"
USER_AGENT = "photopainter-build-skyline/1"


CACHE = Path.home() / ".cache" / "photopainter-dev" / "catalina-elevations.json"


def elevations(points: list[tuple[float, float]]) -> list[float]:
    """Elevations (m) of `points`, cached on disk so a rerun fetches nothing; waits and
    retries when the API says to slow down (HTTP 429)."""
    cache: dict[str, float] = json.loads(CACHE.read_text()) if CACHE.is_file() else {}
    key = lambda p: f"{p[0]:.4f},{p[1]:.4f}"  # noqa: E731
    todo = [p for p in points if key(p) not in cache]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        query = urllib.parse.urlencode({
            "latitude": ",".join(f"{p[0]:.4f}" for p in chunk),
            "longitude": ",".join(f"{p[1]:.4f}" for p in chunk)})
        req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": USER_AGENT})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    heights = json.load(resp)["elevation"]
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 5:
                    raise
                print("rate limited; waiting 65 s", file=sys.stderr)
                time.sleep(65)
        cache.update({key(p): float(h) for p, h in zip(chunk, heights)})
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
        print(f"{min(i + 100, len(todo))}/{len(todo)}", file=sys.stderr)
        time.sleep(2)
    return [cache[key(p)] for p in points]


def view(lat: float, lon: float, h: float) -> tuple[float, float]:
    """(azimuth, degrees above the sea horizon) of a point at height h (m)."""
    p1, p2 = math.radians(OBSERVER[0]), math.radians(lat)
    dl = math.radians(lon - OBSERVER[1])
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * EARTH_R * math.asin(math.sqrt(a))
    az = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                 math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    r_eff = EARTH_R / (1 - REFRACTION_K)
    drop = d * d / (2 * r_eff)
    angle = math.atan2(h - EYE_M - drop, d)
    horizon = -math.sqrt(2 * EYE_M / r_eff)  # the sea horizon's dip
    return az, math.degrees(angle - horizon)


def main() -> int:
    n_lat = round((LAT_RANGE[1] - LAT_RANGE[0]) / STEP) + 1
    n_lon = round((LON_RANGE[1] - LON_RANGE[0]) / STEP) + 1
    grid = [(LAT_RANGE[0] + i * STEP, LON_RANGE[0] + j * STEP) for i in range(n_lat) for j in range(n_lon)]
    print(f"fetching {len(grid)} elevations", file=sys.stderr)
    heights = np.array(elevations(grid)).reshape(n_lat, n_lon)
    # Bilinear interpolation onto a grid FINE times finer.
    fine_lat = np.linspace(LAT_RANGE[0], LAT_RANGE[1], (n_lat - 1) * FINE + 1)
    fine_lon = np.linspace(LON_RANGE[0], LON_RANGE[1], (n_lon - 1) * FINE + 1)
    fi = (fine_lat - LAT_RANGE[0]) / STEP
    fj = (fine_lon - LON_RANGE[0]) / STEP
    i0 = np.clip(np.floor(fi).astype(int), 0, n_lat - 2)
    j0 = np.clip(np.floor(fj).astype(int), 0, n_lon - 2)
    ti, tj = (fi - i0)[:, None], (fj - j0)[None, :]
    h = (heights[i0][:, j0] * (1 - ti) * (1 - tj) + heights[i0 + 1][:, j0] * ti * (1 - tj)
         + heights[i0][:, j0 + 1] * (1 - ti) * tj + heights[i0 + 1][:, j0 + 1] * ti * tj)
    best: dict[int, float] = {}
    for a, lat in enumerate(fine_lat):
        for b, lon in enumerate(fine_lon):
            if h[a, b] <= 0:
                continue
            az, up = view(lat, lon, float(h[a, b]))
            if up > 0:
                k = math.floor(az / AZ_BIN)
                best[k] = max(best.get(k, 0.0), up)
    if not best:
        raise SystemExit("no part of the island is above the horizon")
    lo, hi = min(best), max(best)
    raw = np.array([best.get(k, 0.0) for k in range(lo - 3, hi + 4)])
    weights = np.array([1, 2, 3, 2, 1], dtype=float)  # about 1° wide
    smooth = np.convolve(raw, weights / weights.sum(), mode="same")
    points = [[round((k + 0.5) * AZ_BIN, 2), round(float(v), 3)]
              for k, v in zip(range(lo - 3, hi + 4), smooth)]
    while points and points[0][1] < 0.005:  # trim to the island, closed with zeros
        points.pop(0)
    while points and points[-1][1] < 0.005:
        points.pop()
    points = [[round(points[0][0] - AZ_BIN, 2), 0.0]] + points + [[round(points[-1][0] + AZ_BIN, 2), 0.0]]
    out = {
        "source": ("Copernicus DEM GLO-90 elevations via the Open-Meteo elevation API "
                   "(https://open-meteo.com/en/docs/elevation-api), sampled every 0.005° and "
                   "interpolated; skyline computed by scripts/build-skyline.py"),
        "license": "Open-Meteo data CC BY 4.0; Copernicus DEM © DLR e.V. 2010-2014 and © Airbus "
                   "Defence and Space GmbH 2014-2018, provided under COPERNICUS by the EU and ESA",
        "name": "Santa Catalina Island",
        "observer": {"lat": OBSERVER[0], "lon": OBSERVER[1], "eye_m": EYE_M},
        "points": points,
    }
    path = REPO / "data" / "places" / "wedge" / "skyline.json"
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    top = max(points, key=lambda p: p[1])
    print(f"{path}: {len(points)} points, az {points[0][0]}-{points[-1][0]}, "
          f"highest {top[1]:.2f}° at az {top[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
