#!/usr/bin/env python3
"""Mac only: build data/harbor.json, the map behind the harbor chart (`scenes/harbor.py`).

From an Overpass (OpenStreetMap) response and NOAA ENC soundings: the ocean coastline
chained, the harbor multipolygon, other water, marsh and beach polygons, piers and
breakwaters, channel marks, named islands and thinned soundings, as lat/lon lists,
simplified so the file stays small.

    scripts/build-harbor.py --osm saved.json --soundings saved.json   # no network
    scripts/build-harbor.py --fetch --save-dir ~/.cache/photopainter-dev/harbor

Needs only the standard library. ENC Direct often answers "Could not find service" for a
moment, so requests are retried. Soundings there are in metres below chart datum (MLLW);
the file holds whole feet, rounded down as charts do.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Iterable

SOUTH, WEST, NORTH, EAST = 33.54, -118.02, 33.70, -117.78  # what the file covers
BBOX = f"{SOUTH},{WEST},{NORTH},{EAST}"
QUERY = f"""[out:json][timeout:120];
(
  way["natural"="coastline"]({BBOX});
  way["natural"="water"]({BBOX});
  relation["natural"="water"]({BBOX});
  way["natural"="wetland"]({BBOX});
  relation["natural"="wetland"]({BBOX});
  way["man_made"~"pier|breakwater|groyne"]({BBOX});
  node["seamark:type"~"buoy|beacon"]({BBOX});
  way["natural"="beach"]({BBOX});
);
out geom;
"""
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ENC_URL = "https://encdirect.noaa.gov/arcgis/rest/services/encdirect/"
ENC_LAYERS = ("enc_harbour/MapServer/76", "enc_approach/MapServer/80")  # Sounding_point, best first
BAY_NAME = "Newport Bay"
ISLAND_NAMES = {  # OSM way ids of the bay's inner rings
    40500961: "Lido Isle", 40500960: "Balboa Island", 55205318: "Little Balboa",
    40500956: "Bay Is.", 40500975: "Newport Is.",
}
SIMPLIFY_M = 3.0  # about a third of a pixel at the default view
MIN_WATER_M2 = 3000.0  # ponds smaller than this are left out
SOUNDING_SPACING_M = 90.0
M_TO_FT = 3.28084
MARSH_KINDS = {"saltmarsh", "marsh", "tidalflat"}
Point = tuple[float, float]  # (lat, lon)


def _meters(p: Point, lat0: float) -> tuple[float, float]:
    return (p[1] * 111_320.0 * math.cos(math.radians(lat0)), p[0] * 110_950.0)


def simplify(points: list[Point], tolerance_m: float = SIMPLIFY_M) -> list[Point]:
    """Douglas-Peucker, keeping the ends."""
    if len(points) < 3:
        return list(points)
    lat0 = points[0][0]
    xy = [_meters(p, lat0) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        dx, dy = xy[b][0] - ax, xy[b][1] - ay
        norm = dx * dx + dy * dy
        worst, worst_d = -1, tolerance_m
        for i in range(a + 1, b):
            px, py = xy[i][0] - ax, xy[i][1] - ay
            t = 0.0 if norm == 0 else max(0.0, min(1.0, (px * dx + py * dy) / norm))
            d = math.hypot(px - t * dx, py - t * dy)
            if d > worst_d:
                worst, worst_d = i, d
        if worst >= 0:
            keep[worst] = True
            stack += [(a, worst), (worst, b)]
    return [p for p, k in zip(points, keep) if k]


def _key(p: Point) -> tuple[float, float]:
    return (round(p[0], 6), round(p[1], 6))


def chain(ways: Iterable[list[Point]]) -> list[list[Point]]:
    """Join ways that share end points; returns every chain, longest first."""
    todo = [list(w) for w in ways if len(w) >= 2]
    done: list[list[Point]] = []
    while todo:
        line = todo.pop(0)
        grew = True
        while grew:
            grew = False
            for i, s in enumerate(todo):
                if _key(s[0]) == _key(line[-1]):
                    line += s[1:]
                elif _key(s[-1]) == _key(line[0]):
                    line = s[:-1] + line
                elif _key(s[-1]) == _key(line[-1]):
                    line += s[-2::-1]
                elif _key(s[0]) == _key(line[0]):
                    line = s[:0:-1] + line
                else:
                    continue
                todo.pop(i)
                grew = True
                break
        done.append(line)
    return sorted(done, key=len, reverse=True)


def area_m2(ring: list[Point]) -> float:
    lat0 = ring[0][0]
    xy = [_meters(p, lat0) for p in ring]
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(xy, xy[1:] + xy[:1]))) / 2


def _geom(e: dict) -> list[Point]:
    return [(p["lat"], p["lon"]) for p in e.get("geometry", [])]


def _touches(ring: list[Point]) -> bool:
    lats, lons = [p[0] for p in ring], [p[1] for p in ring]
    return min(lats) < NORTH and max(lats) > SOUTH and min(lons) < EAST and max(lons) > WEST


def _out(points: list[Point], tolerance_m: float = SIMPLIFY_M) -> list[list[float]]:
    return [[round(lat, 5), round(lon, 5)] for lat, lon in simplify(points, tolerance_m)]


def _poly(outer: list[Point], holes: list[list[Point]] = ()) -> dict:
    return {"outer": _out(outer), "holes": [_out(h) for h in holes if len(h) >= 3]}


def _relation_rings(rel: dict) -> tuple[list[list[Point]], list[list[Point]]]:
    members = [m for m in rel.get("members", []) if m.get("geometry")]
    outers = [[(p["lat"], p["lon"]) for p in m["geometry"]] for m in members if m.get("role") == "outer"]
    inners = [[(p["lat"], p["lon"]) for p in m["geometry"]] for m in members if m.get("role") == "inner"]
    return chain(outers), chain(inners)


def _centroid(ring: list[Point]) -> Point:
    return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))


def build_map(elements: list[dict]) -> dict[str, Any]:
    """The map's part of harbor.json from Overpass elements. Raises ValueError if the
    coastline or the bay isn't there."""
    tags = lambda e: e.get("tags", {})  # noqa: E731
    ways = [e for e in elements if e["type"] == "way"]
    relations = [e for e in elements if e["type"] == "relation"]

    coast = chain(_geom(e) for e in ways if tags(e).get("natural") == "coastline")
    if not coast:
        raise ValueError("no coastline in the response")
    ocean = coast[0]  # islands offshore would be shorter chains
    if ocean[0][1] > ocean[-1][1]:  # west to east, water on the south side
        ocean = ocean[::-1]

    bay = next((r for r in relations if tags(r).get("name") == BAY_NAME), None)
    if bay is None:
        raise ValueError(f"no relation named {BAY_NAME!r} in the response")
    outers, inners = _relation_rings(bay)
    in_bay = {m["ref"] for m in bay["members"]}

    marsh_ids = {e["id"] for e in elements if tags(e).get("wetland") in MARSH_KINDS}
    marsh: list[dict] = []
    for e in ways:
        if e["id"] in marsh_ids and len(_geom(e)) >= 3 and _touches(_geom(e)):
            marsh.append(_poly(_geom(e)))
    for r in relations:
        if tags(r).get("natural") == "wetland":
            o, i = _relation_rings(r)
            marsh += [_poly(ring, i) for ring in o if _touches(ring)]

    water: list[dict] = []
    for e in ways:
        ring = _geom(e)
        if (tags(e).get("natural") == "water" and e["id"] not in in_bay and len(ring) >= 3 and _touches(ring)
                and area_m2(ring) >= MIN_WATER_M2):
            water.append(_poly(ring))
    for r in relations:
        if tags(r).get("natural") == "water" and r is not bay:
            o, i = _relation_rings(r)
            water += [_poly(ring, i) for ring in o if _touches(ring) and area_m2(ring) >= MIN_WATER_M2]

    beaches = [_poly(_geom(e)) for e in ways
               if tags(e).get("natural") == "beach" and len(_geom(e)) >= 3 and _touches(_geom(e))]

    piers = []
    for e in ways:
        kind, name = tags(e).get("man_made"), tags(e).get("name")
        if kind in ("breakwater", "groyne") or (kind == "pier" and name):  # no little private docks
            if _touches(_geom(e)) and len(_geom(e)) >= 2:
                piers.append({"kind": kind, "name": name, "line": _out(_geom(e))})

    marks = []
    for e in elements:
        t = tags(e)
        if e["type"] != "node" or "seamark:type" not in t:
            continue
        colour = t.get("seamark:buoy_lateral:colour") or t.get("seamark:beacon_lateral:colour")
        kind = "safe_water" if t["seamark:type"] == "buoy_safe_water" else colour
        if kind in ("red", "green", "safe_water"):
            marks.append({"kind": kind, "name": t.get("name", ""), "lat": round(e["lat"], 5), "lon": round(e["lon"], 5)})
    marks.sort(key=lambda k: (k["lat"], k["lon"]))

    islands = []
    for m in bay["members"]:
        name = ISLAND_NAMES.get(m["ref"])
        if name and m.get("geometry"):
            lat, lon = _centroid([(p["lat"], p["lon"]) for p in m["geometry"]])
            islands.append({"name": name, "lat": round(lat, 5), "lon": round(lon, 5)})
    islands.sort(key=lambda k: k["name"])

    return {"ocean": _out(ocean), "harbor": [_poly(o, inners) for o in outers], "water": water,
            "marsh": marsh, "beaches": beaches, "piers": piers, "marks": marks, "islands": islands}


def build_soundings(layers: list[list[dict]], spacing_m: float = SOUNDING_SPACING_M) -> list[list[float]]:
    """[lat, lon, feet] from ArcGIS features ({attributes: {Z}, geometry: {x, y}}), layers
    best first. Thinned so that none is nearer than `spacing_m` to one already kept, a
    better layer's and a shoaler one's first. Depths are whole feet, rounded down; drying
    heights (negative) are left out."""
    rows = []
    for rank, features in enumerate(layers):
        for f in features:
            z = f["attributes"].get("Z")
            if z is None or z < 0:
                continue
            rows.append((rank, z, f["geometry"]["y"], f["geometry"]["x"]))
    rows.sort()
    cell = spacing_m / 111_000.0
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    kept = []
    cos_lat = math.cos(math.radians((SOUTH + NORTH) / 2))
    for _, z, lat, lon in rows:
        gx, gy = int(lon * cos_lat / cell), int(lat / cell)
        near = (q for i in (-1, 0, 1) for j in (-1, 0, 1) for q in grid.get((gx + i, gy + j), ()))
        if any(math.hypot((lon - x) * cos_lat, lat - y) < cell for y, x in near):
            continue
        grid.setdefault((gx, gy), []).append((lat, lon))
        kept.append([round(lat, 5), round(lon, 5), int(z * M_TO_FT)])
    kept.sort()
    return kept


def build(osm: dict, enc_layers: list[list[dict]], fetched: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": "OpenStreetMap via Overpass (shoreline, marks, piers); NOAA ENC Direct to GIS (soundings)",
        "license": "OpenStreetMap data: ODbL, (c) OpenStreetMap contributors. NOAA ENC soundings: public domain.",
        "fetched": fetched,
        "bounds": {"south": SOUTH, "north": NORTH, "west": WEST, "east": EAST},
    }
    out.update(build_map(osm["elements"]))
    out["soundings"] = build_soundings(enc_layers)
    out["soundings_note"] = "[lat, lon, feet below MLLW, rounded down]; NOAA ENC, not for navigation"
    return out


def _curl(url: str, data: str | None = None) -> str:
    # curl, not urllib: the python.org Python has no CA certificates on the Mac.
    args = ["curl", "-s", "-m", "150", "-A", "photopainter-build/1.0", url]
    if data is not None:
        args += ["--data-urlencode", f"data={data}"]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def fetch_osm() -> dict:
    return json.loads(_curl(OVERPASS_URL, QUERY))


def fetch_soundings(tries: int = 15) -> list[list[dict]]:
    envelope = json.dumps({"xmin": WEST, "ymin": SOUTH, "xmax": EAST, "ymax": NORTH, "spatialReference": {"wkid": 4326}})
    layers = []
    for path in ENC_LAYERS:
        features: list[dict] = []
        while True:
            q = urllib.parse.urlencode({
                "geometry": envelope, "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
                "spatialRel": "esriSpatialRelIntersects", "outFields": "Z", "returnGeometry": "true",
                "resultOffset": len(features), "resultRecordCount": 500, "f": "json"})
            for _ in range(tries):
                try:
                    page = json.loads(_curl(f"{ENC_URL}{path}/query?{q}"))
                except json.JSONDecodeError:
                    page = {}
                if "features" in page:
                    break
                time.sleep(2)  # the service comes and goes
            else:
                raise RuntimeError(f"ENC Direct didn't answer for {path}")
            features += page["features"]
            if not page.get("exceededTransferLimit") or not page["features"]:
                break
        layers.append(features)
    return layers


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--osm", type=Path, help="a saved Overpass response")
    p.add_argument("--soundings", type=Path, help="a saved list of ArcGIS feature lists, best first")
    p.add_argument("--fetch", action="store_true", help="download whatever isn't given")
    p.add_argument("--save-dir", type=Path, help="keep the downloads here")
    p.add_argument("--fetched", default=date.today().isoformat(), help="the date for the `fetched` field")
    p.add_argument("-o", "--output", type=Path, default=Path(__file__).parent.parent / "data" / "harbor.json")
    args = p.parse_args(argv)

    if args.osm:
        osm = json.loads(args.osm.read_text())
    elif args.fetch:
        osm = fetch_osm()
    else:
        p.error("give --osm or --fetch")
    if args.soundings:
        layers = json.loads(args.soundings.read_text())
    elif args.fetch:
        layers = fetch_soundings()
    else:
        layers = []
        print("no soundings: the chart will have none", file=sys.stderr)
    if args.save_dir:
        args.save_dir.mkdir(parents=True, exist_ok=True)
        if not args.osm:
            (args.save_dir / "osm.json").write_text(json.dumps(osm))
        if not args.soundings and layers:
            (args.save_dir / "soundings.json").write_text(json.dumps(layers))

    data = build(osm, layers, args.fetched)
    args.output.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"{args.output}: {args.output.stat().st_size / 1024:.0f} KB, ocean {len(data['ocean'])} points, "
          f"{len(data['beaches'])} beaches, {len(data['marsh'])} marshes, {len(data['water'])} other waters, "
          f"{len(data['piers'])} piers, {len(data['marks'])} marks, {len(data['soundings'])} soundings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
