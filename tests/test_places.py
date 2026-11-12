"""Place data (places.py, PRD.md §9 L1): what's particular to a spot, matched by location."""

import ast
import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import places, state
from photopainter.config import Harbor, default_config
from photopainter.scenes import Scene, SceneContext, harbor, render_scene

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
FIXTURES = Path(__file__).parent / "fixtures"
WEDGE = (33.593, -117.882)
FAR = (33.2, -117.882)  # 44 km south of the Wedge, at sea off Oceanside
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def own(tmp_path, monkeypatch):
    """This frame's places folder, empty to start with."""
    folder = tmp_path / "places"
    folder.mkdir()
    monkeypatch.setenv(state.PLACES_DIR_ENV, str(folder))
    return folder


def write(folder: Path, name: str, data) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def skyline(lat, lon, name="Rock"):
    return {"name": name, "observer": {"lat": lat, "lon": lon}, "points": [[200, 0], [205, 0.4], [210, 0]]}


def at_wedge(**location):
    c = default_config()
    return replace(c, location=replace(c.location, **location))


def test_the_bundled_wedge_has_all_three_parts():
    assert places.skyline_for(*WEDGE, DATA).label == "CATALINA"
    chart = places.harbor_for(*WEDGE, DATA)
    assert chart["place"]["title"] == "NEWPORT BAY" and chart["bounds"]["south"] == 33.54
    sea = places.sea_for(*WEDGE, 190.0, DATA)
    assert sea.beach_ft == (0.0, 7.0) and sea.jetty.left[0] == (32.0, 207.0)


def test_far_from_the_wedge_there_is_none_of_it():
    assert places.skyline_for(*FAR, DATA) is None
    assert places.harbor_for(*FAR, DATA) is None
    assert places.sea_for(*FAR, 190.0, DATA) is None


def test_each_part_has_its_own_rule():
    # The skyline: within 2 km of its observer.
    assert places.skyline_for(33.593 + 0.017, -117.882, DATA) is not None  # 1.9 km north
    assert places.skyline_for(33.593 + 0.019, -117.882, DATA) is None  # 2.1 km
    # The harbor chart: anywhere inside its bounds, however far from the Wedge.
    assert places.harbor_for(33.69, -118.01, DATA) is not None
    assert places.harbor_for(33.71, -118.01, DATA) is None
    # The jetty: within 2 km, and the window within 45° of the way it was drawn.
    assert places.sea_for(33.593 + 0.017, -117.882, 190.0, DATA) is not None
    assert places.sea_for(33.593 + 0.019, -117.882, 190.0, DATA) is None
    assert places.sea_for(*WEDGE, 145.0, DATA) is not None and places.sea_for(*WEDGE, 144.0, DATA) is None


def test_this_frames_own_places_come_before_the_bundled_ones(own):
    write(own / "my-wedge", "skyline.json", skyline(*WEDGE, "A rock"))
    write(own / "my-wedge", "place.json", {"skyline": {"label": "ROCK"}})
    sk = places.skyline_for(*WEDGE, DATA)
    assert sk.name == "A rock" and sk.label == "ROCK"
    assert places.harbor_for(*WEDGE, DATA)["place"]["title"] == "NEWPORT BAY"  # the bundled one still


def test_a_skyline_without_a_label_is_labeled_with_its_name(own):
    write(own / "rock", "skyline.json", skyline(*FAR, "Rock"))
    assert places.skyline_for(*FAR, DATA).label == "Rock"
    write(own / "rock", "place.json", {"skyline": {"label": ""}})
    assert places.skyline_for(*FAR, DATA).label == ""  # no label on the screen


def test_a_broken_file_is_skipped_and_listed(own):
    write(own / "a", "skyline.json", "{not json")
    write(own / "a", "place.json", "[1, 2]")
    write(own / "b", "harbor.json", {"no": "bounds"})
    write(own / "c", "place.json", {"sea": {"observer": {"lat": 33.593}, "bearing_deg": 190}})
    assert places.skyline_for(*WEDGE, DATA).name == "Santa Catalina Island"  # the bundled one
    assert places.harbor_for(*WEDGE, DATA) is not None
    assert places.sea_for(*WEDGE, 190.0, DATA) is not None
    found = places.problems(DATA)
    assert any("a/skyline.json" in p for p in found) and any("a/place.json" in p for p in found)
    assert not any("wedge" in p for p in found)


def test_a_place_is_its_files(own):
    """The bundled Wedge copied into this frame's folder is found there with nothing bundled."""
    shutil.copytree(DATA / "places" / "wedge", own / "copy")
    bundled = json.loads((DATA / "places" / "wedge" / "place.json").read_text())
    nowhere = ROOT / "nowhere"
    assert places.harbor_for(*WEDGE, nowhere)["place"] == bundled["harbor"]
    assert places.skyline_for(*WEDGE, nowhere).label == "CATALINA"
    assert places.sea_for(*WEDGE, 190.0, nowhere) == places.sea_for(*WEDGE, 190.0, DATA)


def test_no_harbor_chart_far_away_says_why():
    ctx = SceneContext.from_config(at_wedge(lat=FAR[0], lon=FAR[1]), state_dir=FIXTURES)
    with pytest.raises(places.NoPlaceData, match="no harbor chart"):
        render_scene(Scene("harbor", "harbor/x", datetime(2026, 11, 3, 14, 10, tzinfo=LA)), ctx)


def test_a_harbor_view_that_misses_the_chart_takes_the_places_own():
    data = places.harbor_for(*WEDGE, DATA)
    own_view = harbor.place_view(data)
    elsewhere = Harbor(south=40.0, north=40.1, center_lon=-70.0)
    assert harbor.make_view(elsewhere, data["bounds"], own_view) == harbor.make_view(own_view, data["bounds"])
    nearby = Harbor(south=33.59, north=33.61, center_lon=-117.89)  # inside: kept
    assert harbor.make_view(nearby, data["bounds"], own_view) == harbor.make_view(nearby, data["bounds"])
    assert own_view == Harbor(south=33.5835, north=33.61575, center_lon=-117.9015)


def test_nothing_in_the_screens_names_a_place_in_newport():
    """No string a screen could draw names Newport's places: they come from the place's data.
    Docstrings and comments may still describe the bundled example."""
    words = ("wedge", "newport", "catalina", "balboa", "corona del mar")
    for path in (ROOT / "src" / "photopainter" / "scenes").glob("*.py"):
        for s in _strings(path.read_text()):
            assert not any(w in s.lower() for w in words), f"{path.name}: {s!r}"


def _strings(code: str) -> list[str]:
    """Every string literal in `code` but docstrings."""
    tree = ast.parse(code)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_the_skyline_is_drawn_only_near_its_observer():
    from photopainter.scenes import sky

    at = datetime(2027, 7, 3, 22, 30, tzinfo=LA)
    here = sky.sky_model(at, SceneContext.from_config(default_config(), state_dir=FIXTURES))
    there = sky.sky_model(at, SceneContext.from_config(at_wedge(lat=FAR[0], lon=FAR[1]), state_dir=FIXTURES))
    assert here.skyline is not None and there.skyline is None
    assert np.all(here.skyline.height >= 0)
