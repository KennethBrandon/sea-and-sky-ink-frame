"""The color test card: drawing it, pinning it from the dashboard, and reading it back
from a simulated photo of a panel with known inks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from photopainter.config import default_config
from photopainter.render.card import (
    CELL, MARKERS, MIXES, PATCHES, TARGETS, _marker, card_indices, ink_shares,
)
from photopainter.render.card_analysis import (
    P3_TO_SRGB, CardNotFound, build_report, delta_e, find_markers, load_photo, measure, report_text, solve,
    write_overlay,
)
from photopainter.render.draw import INK
from photopainter.render.palette import map_to_inks, palette_colors, srgb_to_linear
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.schedule import Overrides, Pin, decide

# A made-up panel: linear ink colors relative to a white of about 45% reflectance.
TRUE_INKS = np.array([
    (0.012, 0.012, 0.014),  # black
    (0.450, 0.470, 0.420),  # white
    (0.400, 0.380, 0.015),  # yellow
    (0.150, 0.012, 0.010),  # red
    (0.012, 0.050, 0.170),  # blue
    (0.020, 0.090, 0.030),  # green
])
# …and the palette that should come out of it: black 0, white 255.
EXPECTED = (TRUE_INKS - TRUE_INKS[0]) / (TRUE_INKS[1] - TRUE_INKS[0])


def camera_photo(path: Path, *, light=(1.0, 0.8), inks: np.ndarray = TRUE_INKS) -> None:
    """A photo of the card, as `camera_sensor` sees it, sRGB encoded and saved as a JPEG."""
    blurred = camera_sensor(light=light, inks=inks)
    img = Image.fromarray((np.clip(_encode(blurred), 0, 1) * 255 + 0.5).astype(np.uint8))
    img.save(path, quality=95)


def camera_sensor(*, light=(1.0, 0.8), inks: np.ndarray = TRUE_INKS) -> np.ndarray:
    """What a sensor sees of the card, in linear light: perspective, an uneven light, a
    wooden frame on a wall, and sensor blur."""
    indices = card_indices()
    h, w = 1500, 2200
    # Where the card's corners land in the photo: about 2 photo pixels per panel pixel.
    top_left, top_right, bottom_right, bottom_left = (290, 205), (1910, 245), (1865, 1225), (330, 1265)
    src = np.array([(0, 0), (800, 0), (800, 480), (0, 480)], dtype=np.float64)
    dst = np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float64)
    a = []
    for (x, y), (u, v) in zip(dst, src):
        a += [[x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]]
    a = np.array(a, dtype=np.float64)
    photo_to_card = np.append(np.linalg.solve(a[:, :8], a[:, 8]), 1).reshape(3, 3)

    yy, xx = np.mgrid[0:h, 0:w]
    points = np.stack([xx.ravel() + 0.5, yy.ravel() + 0.5, np.ones(h * w)], axis=1) @ photo_to_card.T
    cx = points[:, 0] / points[:, 2]
    cy = points[:, 1] / points[:, 2]
    inside = (cx >= 0) & (cx < 800) & (cy >= 0) & (cy < 480)

    wall = np.array([0.55, 0.53, 0.48])
    frame = np.array([0.05, 0.03, 0.015])
    out = np.tile(wall, (h * w, 1)).astype(np.float32)
    near = (cx >= -60) & (cx < 860) & (cy >= -60) & (cy < 540)
    out[near] = frame
    out[inside] = inks[indices[cy[inside].astype(int), cx[inside].astype(int)]]
    out = out.reshape(h, w, 3)
    # Light falls off from the left and a little toward the bottom.
    out *= (light[0] + (light[1] - light[0]) * xx[..., None] / w) * (1 - 0.05 * yy[..., None] / h)
    # Sensor and lens blur: a small box, in linear light.
    blurred = out.copy()
    for axis in (0, 1):
        blurred = (np.roll(blurred, -1, axis) + blurred + np.roll(blurred, 1, axis)) / 3
    return blurred


def _encode(linear: np.ndarray) -> np.ndarray:
    from photopainter.render.palette import linear_to_srgb

    return linear_to_srgb(np.clip(linear, 0, 1))


@pytest.fixture(scope="module")
def card_photo(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("card") / "photo.jpg"
    camera_photo(path)
    return path


# --- the card ---------------------------------------------------------------------------


def test_card_is_panel_sized_and_all_in_ink():
    indices = card_indices()
    assert indices.shape == (480, 800)
    assert indices.dtype == np.uint8
    assert indices.max() < 6


def test_solid_patches_are_solid_and_every_ink_repeats_across_the_panel():
    indices = card_indices()
    for patch in (p for p in PATCHES if p.ink):
        x0, y0, x1, y1 = patch.rect
        assert (indices[y0:y1, x0:x1] == INK[patch.ink]).all()
    for ink in INK:
        spots = [p for p in PATCHES if p.ink == ink]
        assert len(spots) >= 3, ink
        assert len({p.col for p in spots}) >= 3, f"{ink} doesn't spread across the panel"


def test_every_mix_and_target_is_on_the_card():
    assert {p.mix for p in PATCHES if p.mix} == set(MIXES)
    for method in ("today", "linear"):
        assert {p.target for p in PATCHES if p.method == method} == set(TARGETS)


def test_dot_mixes_have_their_exact_shares_where_measured():
    indices = card_indices()
    for patch in (p for p in PATCHES if p.mix):
        shares = ink_shares(indices, patch)
        assert shares[INK[patch.mix.second]] == pytest.approx(patch.mix.fraction)
        assert shares[INK[patch.mix.first]] == pytest.approx(1 - patch.mix.fraction)


def test_target_patches_are_dithered_each_way():
    gray = next(t for t in TARGETS if t.name == "gray 128")
    tile = Image.new("RGB", (CELL, CELL), gray.rgb)
    assert not np.array_equal(map_to_inks(tile), map_to_inks(tile, linear=True))


def test_linear_dither_of_a_gray_averages_to_it_in_linear_light():
    pal = srgb_to_linear(np.array(palette_colors("estimated")) / 255)
    img = Image.new("RGB", (200, 200), (128, 128, 128))
    share = np.bincount(map_to_inks(img, linear=True).ravel(), minlength=6) / 40000
    mixed = share @ pal
    assert mixed == pytest.approx([float(srgb_to_linear(128 / 255))] * 3, abs=0.01)


def test_map_to_inks_is_unchanged_by_default():
    img = Image.new("RGB", (64, 64), (128, 90, 60))
    assert np.array_equal(map_to_inks(img), map_to_inks(img, linear=False))


# --- as a screen and a pin ---------------------------------------------------------------


NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _context(portrait: bool = False) -> SceneContext:
    from dataclasses import replace

    config = default_config()
    config = replace(config, display=replace(config.display, portrait=portrait))
    return SceneContext.from_config(config)


def test_card_screen_landscape_and_portrait():
    scene = Scene("card", "card/x", NOW)
    landscape = render_scene(scene, _context())
    assert landscape.shape == (480, 800)
    portrait = render_scene(scene, _context(portrait=True))
    assert portrait.shape == (800, 480)
    assert np.array_equal(np.rot90(portrait), landscape)  # as `to_panel_codes` turns it back


def test_card_pin_holds_the_card_until_it_ends_and_then_the_schedule_returns():
    from photopainter.astro.sun import SunEvents

    config = default_config()
    tz = config.location.tz

    def sun(day):
        at = datetime(day.year, day.month, day.day, tzinfo=tz)
        return SunEvents(sunrise=at + timedelta(hours=6), sunset=at + timedelta(hours=18),
                         civil_dawn=at + timedelta(hours=5, minutes=30),
                         civil_dusk=at + timedelta(hours=18, minutes=30))

    start = datetime(2026, 11, 4, 10, 5, tzinfo=tz)
    until = start + timedelta(hours=1)
    overrides = Overrides(pin=Pin("card", until=until, set_at=start))
    first = decide(start, config, overrides, sun)
    later = decide(start + timedelta(minutes=55), config, overrides, sun)
    assert first.scene.kind == "card"
    assert later.scene.key == first.scene.key  # never redrawn while it's pinned
    assert first.next_change_at == until
    assert decide(until, config, overrides, sun).scene.kind != "card"


def test_card_pin_survives_overrides_json():
    pin = Pin("card", until=NOW + timedelta(hours=1), set_at=NOW)
    assert Overrides.from_dict(Overrides(pin=pin).to_dict()).pin.kind == "card"


def test_dashboard_pins_the_card_for_an_hour_and_lets_it_go(monkeypatch, tmp_path):
    from photopainter import state
    from photopainter.astro.sun import SunEvents
    from photopainter.web import server

    def sun(day):
        tz = default_config().location.tz
        at = datetime(day.year, day.month, day.day, tzinfo=tz)
        return SunEvents(at + timedelta(hours=6), at + timedelta(hours=18),
                         at + timedelta(hours=5, minutes=30), at + timedelta(hours=18, minutes=30))

    class Ticks:
        running, last_error, requests = False, None, 0

        def request(self):
            Ticks.requests += 1

        def snapshot(self):
            return {"running": False, "queued": False, "scheduled_at": None}

        def close(self, wait_s=0):
            return True

    state.state_dir().mkdir(parents=True, exist_ok=True)
    client = server.create_app(fake_panel=True, clock=lambda: NOW, ticks=Ticks(), sun=sun).test_client()
    res = client.post("/api/mode", json={"kind": "card", "until": "1h"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["mode"]["kind"] == "card"
    assert "Color test card until" in body["message"]
    assert Ticks.requests == 1
    assert client.get("/api/status").get_json()["screens"]["card"] is True
    assert client.post("/api/mode", json={"kind": "auto"}).get_json()["mode"]["kind"] == "auto"


def test_settings_page_offers_the_card():
    from photopainter.web import server

    page = (server.STATIC / "index.html").read_text()
    script = (server.STATIC / "app.js").read_text()
    assert 'id="card-show"' in page
    assert 'setMode("card", "1h")' in script


# --- reading a photo ----------------------------------------------------------------------


def test_recovers_the_inks_from_a_photo(card_photo):
    report = build_report([measure(load_photo(card_photo))])
    for i, name in enumerate(("black", "white", "yellow", "red", "blue", "green")):
        got = srgb_to_linear(np.array(report.inks[name]) / 255)
        assert delta_e(got, EXPECTED[i]) < 2.0, f"{name}: {report.inks[name]}"
    assert report.photos[0]["warnings"] == []
    assert report.photos[0]["rotation"] == 0


def test_light_falling_off_across_the_panel_is_removed(card_photo, tmp_path):
    report = build_report([measure(load_photo(card_photo))])
    assert report.photos[0]["evenness"] > 0.10  # the simulated light really is uneven
    even = tmp_path / "even.jpg"
    camera_photo(even, light=(1.0, 1.0))
    flat = build_report([measure(load_photo(even))])
    for name in report.inks:
        assert np.abs(np.array(report.inks[name]) - np.array(flat.inks[name])).max() <= 4, name


@pytest.mark.parametrize("turn", [1, 2, 3])
def test_finds_which_way_up_the_card_is(card_photo, tmp_path, turn):
    path = tmp_path / f"turned{turn}.png"
    Image.open(card_photo).rotate(90 * turn, expand=True).save(path)  # counter-clockwise
    result = measure(load_photo(path))
    report = build_report([result])
    assert result.rotation == (360 - 90 * turn) % 360
    assert delta_e(srgb_to_linear(np.array(report.inks["red"]) / 255), EXPECTED[3]) < 2.5


def test_physical_dots_read_as_linear_light_and_linear_dither_scores_better(card_photo):
    report = build_report([measure(load_photo(card_photo))])
    assert report.linear_mix_error < 1.5
    assert report.srgb_mix_error > report.linear_mix_error + 3
    assert report.target_error["linear"]["grays"] < report.target_error["today"]["grays"]
    assert report.model_error < 2.0


def test_dots_mixing_by_srgb_read_as_srgb():
    """Hand-made readings, as if the camera's curve or dot bleed made dots look like an sRGB mix."""
    indices = card_indices()
    inks = srgb_to_linear(np.array(palette_colors("estimated")) / 255) * 0.9 + 0.01
    values = {}
    for patch in PATCHES:
        shares = ink_shares(indices, patch)
        values[patch] = srgb_to_linear(shares @ (np.array(palette_colors("estimated")) / 255)) * 0.9 + 0.01
    assert solve(values).inks[INK["red"]] == pytest.approx(
        (inks[INK["red"]] - inks[INK["black"]]) / (inks[INK["white"]] - inks[INK["black"]]))

    class Fake:
        path = Path("fake.jpg")
        color_space = "sRGB"

    class FakeMeasured:
        photo = Fake()
        rotation = 0
        warnings: list[str] = []

    FakeMeasured.values = values
    report = build_report([FakeMeasured()], indices)
    assert report.srgb_mix_error < report.linear_mix_error
    assert "closer to sRGB" in report_text(report)


def test_overexposed_and_glare_photos_are_flagged():
    indices = card_indices()
    values = {p: ink_shares(indices, p) @ (TRUE_INKS * 3.0) for p in PATCHES}  # white above 1
    assert any("overexposed" in w for w in solve(values).warnings)
    values = {p: ink_shares(indices, p) @ (TRUE_INKS + 0.3) for p in PATCHES}  # a veil of light
    assert any("glare" in w for w in solve(values).warnings)


def _draw_marker(gray: np.ndarray, x: int, y: int, module: int, black: int) -> None:
    pattern = _marker() != INK["white"]  # 7 modules, 8 px each
    tile = np.kron(pattern[::8, ::8], np.ones((module, module), dtype=bool))
    gray[y:y + tile.shape[0], x:x + tile.shape[1]][tile] = black


def test_a_marker_dimmed_by_glare_is_still_found_beside_a_false_one():
    """A real photo did this: glare hid the top left marker at the first darkness level,
    where something else passed, and the search stopped there with a wrong four."""
    gray = np.full((1000, 1600), 200, dtype=np.uint8)
    corners = [(200, 150), (1300, 150), (1300, 700), (200, 700)]
    for i, (x, y) in enumerate(corners):
        _draw_marker(gray, x, y, 12, black=150 if i == 0 else 40)  # the first is veiled
    _draw_marker(gray, 750, 450, 3, black=40)  # small and in the wrong place
    found = find_markers(Image.fromarray(gray))
    for x, y in corners:
        assert any(abs(fx - (x + 42)) < 4 and abs(fy - (y + 42)) < 4 for fx, fy, _ in found), (x, y)


def test_locate_ignores_candidates_of_the_wrong_size_or_place(card_photo, monkeypatch):
    from photopainter.render import card_analysis

    photo = load_photo(card_photo)
    real = sorted(find_markers(photo.gray), key=lambda c: -c[2])[:4]  # dither can pass for tiny ones
    size = real[0][2]
    decoys = [(real[0][0] + 300, real[0][1] + 200, size / 5), (real[1][0] - 300, real[1][1] + 400, size)]
    expected = measure(photo).homography
    monkeypatch.setattr(card_analysis, "find_markers", lambda gray: [decoys[0], *real, decoys[1]])
    assert np.allclose(measure(photo).homography, expected, atol=1e-3)


def test_a_photo_without_the_card_says_what_to_do(tmp_path):
    path = tmp_path / "wall.jpg"
    Image.new("RGB", (1600, 1000), (180, 170, 160)).save(path)
    with pytest.raises(CardNotFound, match="--corners"):
        measure(load_photo(path))


def test_corners_can_be_given_by_hand(card_photo):
    photo = load_photo(card_photo)
    found = measure(photo)
    by_hand = measure(photo, list(reversed(found.corners)))
    assert np.allclose(by_hand.homography, found.homography, atol=1e-3)


def test_overlay_and_report_are_written(card_photo, tmp_path):
    result = measure(load_photo(card_photo))
    write_overlay(result, tmp_path / "check" / "overlay.png")
    assert (tmp_path / "check" / "overlay.png").is_file()
    text = report_text(build_report([result, result]))
    assert "Inks (black 0, white 255)" in text
    assert "Inks in each photo" in text
    assert "render/palette.py" in text


class FakeRaw:
    """Stands in for rawpy: a dim 16-bit linear image, whatever file is asked for."""

    class ColorSpace:
        sRGB = "srgb"

    def __init__(self, linear: np.ndarray):
        self.linear = linear
        self.kwargs: dict = {}

    def imread(self, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def sizes(self):
        height, width = self.linear.shape[:2]
        return SimpleNamespace(width=width, height=height)

    def postprocess(self, **kwargs):
        self.kwargs = kwargs
        return (np.clip(self.linear, 0, 1) * 65535).astype(np.uint16)


def test_raw_files_are_read_as_linear_light_whatever_their_exposure(monkeypatch, tmp_path):
    import sys

    fake = FakeRaw(camera_sensor() * 0.3)  # underexposed, as RAW files often are
    monkeypatch.setitem(sys.modules, "rawpy", fake)
    path = tmp_path / "shot.RAW-02.ORIGINAL.DNG"
    path.write_bytes(b"not read: rawpy is fake")
    report = build_report([measure(load_photo(path))])
    assert fake.kwargs["gamma"] == (1, 1)
    assert fake.kwargs["no_auto_bright"] is True
    assert fake.kwargs["output_color"] == "srgb"
    assert report.photos[0]["color_space"] == "RAW, linear"
    for i, name in enumerate(("black", "white", "yellow", "red", "blue", "green")):
        assert delta_e(srgb_to_linear(np.array(report.inks[name]) / 255), EXPECTED[i]) < 2.0, name


def test_raw_without_rawpy_says_how_to_get_it(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "rawpy", None)
    path = tmp_path / "shot.dng"
    path.write_bytes(b"x")
    with pytest.raises(OSError, match=r"\.\[raw\]"):
        load_photo(path)


def test_p3_matrix_keeps_white_and_widens_red():
    assert P3_TO_SRGB.sum(axis=1) == pytest.approx([1, 1, 1], abs=1e-3)
    assert (P3_TO_SRGB @ [1, 0, 0])[0] > 1  # P3 red is beyond sRGB red


P3_PROFILE = Path("/System/Library/ColorSync/Profiles/Display P3.icc")


@pytest.mark.skipif(not P3_PROFILE.is_file(), reason="needs macOS's Display P3 profile")
def test_display_p3_photos_are_converted(tmp_path):
    path = tmp_path / "p3.png"
    Image.new("RGB", (32, 32), (200, 100, 50)).save(path, icc_profile=P3_PROFILE.read_bytes())
    photo = load_photo(path)
    assert "P3" in photo.color_space
    expected = srgb_to_linear(np.array([200, 100, 50]) / 255) @ P3_TO_SRGB.T
    assert photo.linear[0, 0] == pytest.approx(expected, abs=1e-3)


def test_corner_markers_sit_at_the_four_corners():
    xs, ys = zip(*MARKERS)
    assert min(xs) < 80 and max(xs) > 720
    assert min(ys) < 90 and max(ys) > 390
