from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photopainter.render.layout import crop_fraction, fit_to_canvas
from photopainter.slideshow import (
    ORDER_FILE, SlideshowState, add_to_end, list_images, load_order, rotation_order, save_order,
)

CANVAS = (800, 480)


@pytest.mark.parametrize(
    "size, expect_fill",
    [
        ((4032, 3024), True),  # 4:3 landscape: loses 20%
        ((4899, 3266), True),  # 3:2 landscape: loses 10%
        ((800, 480), True),  # exact
        ((3072, 4080), False),  # portrait
        ((1000, 1000), False),  # square: would lose 40%
    ],
)
def test_auto_fit_chooses_fill_or_fit(size, expect_fill):
    img = Image.new("RGB", size, (255, 0, 0))
    out = np.asarray(fit_to_canvas(img, CANVAS, "auto"))
    corner_is_photo = tuple(out[2, 2]) == (255, 0, 0)
    assert corner_is_photo == expect_fill
    assert (crop_fraction(size, CANVAS) <= 0.35) == expect_fill


def touch(folder: Path, *names: str) -> None:
    for name in names:
        (folder / name).write_bytes(b"")


def test_list_images_filters_and_sorts(tmp_path):
    touch(tmp_path, "b.JPG", "a.png", ".hidden.jpg", "notes.txt", "c.heic")
    (tmp_path / "sub.jpg").mkdir()
    assert [p.name for p in list_images(tmp_path)] == ["a.png", "b.JPG", "c.heic"]


def test_rotation_order_wraps_and_survives_deleted_photo(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg", "d.jpg")
    images = list_images(tmp_path)
    names = lambda ps: [p.name for p in ps]  # noqa: E731
    assert names(rotation_order(images, None)) == ["a.jpg", "b.jpg", "d.jpg"]
    assert names(rotation_order(images, "b.jpg")) == ["d.jpg", "a.jpg", "b.jpg"]
    assert names(rotation_order(images, "d.jpg")) == ["a.jpg", "b.jpg", "d.jpg"]  # wraps
    assert names(rotation_order(images, "c.jpg")) == ["d.jpg", "a.jpg", "b.jpg"]  # c was deleted
    assert rotation_order([], "a.jpg") == []


def test_slideshow_state_per_folder(tmp_path):
    state = SlideshowState(tmp_path / "state" / "slideshow.json")
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    assert state.last_shown(one) is None
    state.record(one, "x.jpg")
    state.record(two, "y.jpg")
    assert state.last_shown(one) == "x.jpg"
    assert state.last_shown(two) == "y.jpg"


def test_saved_order_comes_first_and_the_rest_follow_by_name(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg", "c.jpg", "d.jpg")
    save_order(tmp_path, ["c.jpg", "gone.jpg", "a.jpg"])
    assert [p.name for p in list_images(tmp_path)] == ["c.jpg", "a.jpg", "b.jpg", "d.jpg"]


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"order": "a.jpg"}', '{"order": [1, null]}'])
def test_a_bad_order_file_falls_back_to_names(tmp_path, text):
    touch(tmp_path, "b.jpg", "a.jpg")
    (tmp_path / ORDER_FILE).write_text(text)
    assert load_order(tmp_path) == []
    assert [p.name for p in list_images(tmp_path)] == ["a.jpg", "b.jpg"]


def test_rotation_follows_the_saved_order(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg", "c.jpg")
    save_order(tmp_path, ["c.jpg", "a.jpg", "b.jpg"])
    images = list_images(tmp_path)
    names = lambda ps: [p.name for p in ps]  # noqa: E731
    assert names(rotation_order(images, None)) == ["c.jpg", "a.jpg", "b.jpg"]
    assert names(rotation_order(images, "c.jpg")) == ["a.jpg", "b.jpg", "c.jpg"]
    assert names(rotation_order(images, "b.jpg")) == ["c.jpg", "a.jpg", "b.jpg"]  # wraps


def test_rotation_carries_on_from_where_a_gone_photo_was(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg", "c.jpg", "d.jpg")
    save_order(tmp_path, ["d.jpg", "c.jpg", "b.jpg", "a.jpg"])
    (tmp_path / "c.jpg").unlink()  # the order still names it
    images = list_images(tmp_path)
    assert [p.name for p in images] == ["d.jpg", "b.jpg", "a.jpg"]
    assert rotation_order(images, "c.jpg")[0].name == "b.jpg"
    shown = [p for p in images if p.name != "b.jpg"]  # hidden: left out of the list, still in the order
    assert rotation_order(shown, "b.jpg")[0].name == "a.jpg"


def test_rotation_starts_over_after_a_photo_the_order_no_longer_names(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg")
    save_order(tmp_path, ["b.jpg", "a.jpg"])
    assert rotation_order(list_images(tmp_path), "gone.jpg")[0].name == "b.jpg"


def test_add_to_end_keeps_what_was_there_in_its_order(tmp_path):
    touch(tmp_path, "a.jpg", "b.jpg", "m.jpg")
    add_to_end(tmp_path, "a.jpg")  # by name until now: a stays behind the later names
    assert [p.name for p in list_images(tmp_path)] == ["b.jpg", "m.jpg", "a.jpg"]
    add_to_end(tmp_path, "b.jpg")  # already named: it moves
    assert load_order(tmp_path) == ["m.jpg", "a.jpg", "b.jpg"]


def test_the_order_file_is_not_a_photo(tmp_path):
    touch(tmp_path, "a.jpg")
    save_order(tmp_path, ["a.jpg"])
    assert [p.name for p in list_images(tmp_path)] == ["a.jpg"]
