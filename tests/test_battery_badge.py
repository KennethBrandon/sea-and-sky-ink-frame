"""The battery indicator: size, place, fill, and the six inks on every screen."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import cli
from photopainter.config import default_config, save_config
from photopainter.render import battery_badge
from photopainter.render.draw import INK
from photopainter.render.palette import INKS
from photopainter.scenes import Scene, SceneContext, render_scene

LA = ZoneInfo("America/Los_Angeles")
FIXTURES = Path(__file__).parent / "fixtures"
W, H = battery_badge.box_size()


def changed(photo: np.ndarray, out: np.ndarray) -> tuple[int, int, int, int]:
    rows, cols = np.nonzero(out != photo)
    return int(rows.min()), int(rows.max()), int(cols.min()), int(cols.max())


@pytest.mark.parametrize("shape", [(480, 800), (800, 480)])
@pytest.mark.parametrize("corner", battery_badge.CORNERS)
def test_box_sits_in_its_corner_and_is_small(shape, corner):
    photo = np.full(shape, INK["blue"], np.uint8)
    out = battery_badge.draw(photo, 50, corner)
    assert (photo == INK["blue"]).all() and out.dtype == np.uint8 and out.shape == shape  # a copy
    top, bottom, left, right = changed(photo, out)
    m = battery_badge.MARGIN
    assert (bottom - top + 1, right - left + 1) == (H, W) and W < 50 and H < 30
    assert top == m if corner.startswith("top") else bottom == shape[0] - m - 1
    assert left == m if corner.endswith("left") else right == shape[1] - m - 1


def test_box_is_white_ruled_in_black():
    out = battery_badge.draw(np.full((480, 800), INK["blue"], np.uint8), 100)
    top, bottom, left, right = changed(np.full((480, 800), INK["blue"], np.uint8), out)
    box = out[top:bottom + 1, left:right + 1]
    assert (box[0] == INK["black"]).all() and (box[-1] == INK["black"]).all()
    assert (box[:, 0] == INK["black"]).all() and (box[:, -1] == INK["black"]).all()
    assert (box[1, 1:-1] == INK["white"]).all()
    assert INK["blue"] not in box and INK["red"] not in box


def _filled(percent: int) -> tuple[int, int]:
    """(red, black) pixels in the battery's inside for `percent`."""
    box = battery_badge._box(percent)
    x, y = 1 + battery_badge.PAD, 1 + battery_badge.PAD
    inside = box[y + 3:y + battery_badge.BODY[1] - 3, x + 3:x + battery_badge.BODY[0] - 3]
    return int((inside == INK["red"]).sum()), int((inside == INK["black"]).sum())


def test_fill_follows_the_percent():
    assert _filled(0) == (0, 0)
    red_low, _ = _filled(1)
    assert red_low > 0  # a sliver, not nothing
    assert _filled(10)[0] < _filled(25)[0]
    assert _filled(26)[0] == 0 and _filled(50)[1] < _filled(100)[1]  # black from 26 %
    assert _filled(120) == _filled(100)
    assert battery_badge._box(0).tolist() == battery_badge.draw(
        np.zeros((480, 800), np.uint8), None, "top-left")[8:8 + H, 8:8 + W].tolist()


def test_unknown_corner():
    with pytest.raises(ValueError, match="unknown corner"):
        battery_badge.draw(np.zeros((480, 800), np.uint8), 50, "middle")


@pytest.mark.parametrize("kind, at", [
    ("tides", "2026-11-03T14:10"), ("sky", "2026-11-03T22:30"), ("sky", "2026-11-03T14:10"),
    ("sea", "2026-11-03T14:10"), ("harbor", "2026-11-03T14:10"), ("combo", "2026-11-03T14:10"),
    ("message", "2026-11-03T14:10"), ("photo", "2026-11-03T14:10"),
])
def test_stays_on_palette_on_every_screen(kind, at, tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOPAINTER_STATE_DIR", str(FIXTURES))
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.effect_noise((160, 96), 80).convert("RGB").save(photos / "a.png")
    c = default_config()
    from dataclasses import replace
    config = replace(c, photos=replace(c.photos, folder=str(photos)))
    when = datetime.fromisoformat(at).replace(tzinfo=LA)
    ctx = SceneContext.from_config(config, state_dir=FIXTURES)
    scene = Scene(kind, f"{kind}/x", when, message="Hello" if kind == "message" else None)
    screen = render_scene(scene, ctx)
    for corner in battery_badge.CORNERS:
        out = battery_badge.draw(screen, 40, corner)
        assert out.shape == screen.shape and out.dtype == np.uint8 and out.max() < len(INKS)
        assert (out != screen).sum() <= W * H


def test_scene_command_draws_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHOTOPAINTER_STATE_DIR", str(tmp_path))
    save_config(default_config())
    plain, badged = tmp_path / "plain.png", tmp_path / "badged.png"
    assert cli.main(["scene", "message", "--message", "x", "--at", "2026-11-03T14:10", "-o", str(plain)]) == 0
    assert cli.main(["scene", "message", "--message", "x", "--at", "2026-11-03T14:10", "-o", str(badged),
                     "--battery-volts", "3.5", "--battery-corner", "top-left"]) == 0
    a, b = np.asarray(Image.open(plain).convert("RGB")), np.asarray(Image.open(badged).convert("RGB"))
    rows, cols = np.nonzero((a != b).any(axis=2))
    assert rows.max() < 40 and cols.max() < 50
