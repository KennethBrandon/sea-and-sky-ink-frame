import numpy as np
import pytest
from PIL import ImageFont

from photopainter.render.draw import INK, InkCanvas, font, line_height, text_width, wrap_text
from photopainter.render.palette import INKS, palette_colors


def test_ink_indices_follow_palette_order():
    assert INK == {"black": 0, "white": 1, "yellow": 2, "red": 3, "blue": 4, "green": 5}
    assert [INKS[i].name for i in INK.values()] == list(INK)


def test_canvas_basics():
    c = InkCanvas((800, 480))
    assert c.image.mode == "P" and c.image.size == (800, 480) and c.size == (800, 480)
    idx = c.indices()
    assert idx.shape == (480, 800) and idx.dtype == np.uint8
    assert (idx == INK["white"]).all()
    assert (InkCanvas((480, 800), "black").indices() == INK["black"]).all()
    with pytest.raises(ValueError, match="unknown ink"):
        InkCanvas((10, 10), "orange")


def test_text_and_lines_use_only_the_inks_drawn():
    c = InkCanvas((800, 480))
    for size in (12, 18, 31, 64):
        c.draw.text((10, size * 3), "Tide 4.7 ft @ 5:27 AM — Sirius", font=font(size), fill=INK["red"])
        c.draw.text((400, size * 3), "Bold label", font=font(size, bold=True), fill=INK["blue"])
    c.draw.line([(0, 0), (799, 479)], fill=INK["green"], width=3)
    c.draw.line([(0, 479), (799, 0)], fill=INK["black"], width=1)
    c.draw.ellipse([300, 200, 380, 260], outline=INK["yellow"], width=2)
    c.draw.arc([500, 300, 700, 460], 10, 200, fill=INK["red"], width=4)
    c.draw.polygon([(10, 400), (100, 470), (200, 410)], fill=INK["blue"], outline=INK["black"])
    idx = c.indices()
    # No anti-aliasing: exactly the inks used, nothing in between.
    assert set(np.unique(idx).tolist()) == {INK[n] for n in ("white", "red", "blue", "green", "black", "yellow")}
    assert idx.max() < len(INKS)


def test_red_text_is_only_red_on_white():
    c = InkCanvas((200, 60))
    c.draw.text((5, 5), "Agj 0O1l", font=font(36), fill=INK["red"])
    assert set(np.unique(c.indices()).tolist()) == {INK["white"], INK["red"]}


def test_off_palette_color_is_caught():
    c = InkCanvas((50, 50))
    c.draw.rectangle([0, 0, 10, 10], fill=(0, 128, 0))  # an RGB color, not an ink index
    with pytest.raises(ValueError, match="isn't one of the six inks"):
        c.indices()


def test_preview_uses_ink_colors():
    c = InkCanvas((40, 20))
    c.draw.rectangle([0, 0, 19, 19], fill=INK["blue"])
    rgb = np.asarray(c.preview())
    assert rgb.shape == (20, 40, 3)
    assert tuple(rgb[0, 0]) == palette_colors("estimated")[INK["blue"]]
    assert tuple(rgb[0, 39]) == palette_colors("estimated")[INK["white"]]


def test_font_is_bundled_and_cached():
    regular, bold = font(24), font(24, bold=True)
    assert isinstance(regular, ImageFont.FreeTypeFont)
    assert font(24) is regular
    assert regular.getname()[0] == "DejaVu Sans" and bold.getname()[1] == "Bold"
    assert font(48).size == 48


def test_wrap_text():
    f = font(20)
    lines = wrap_text("the quick brown fox jumps over the lazy dog", f, 150)
    assert len(lines) > 1
    assert all(text_width(line, f) <= 150 for line in lines)
    assert " ".join(lines) == "the quick brown fox jumps over the lazy dog"
    assert wrap_text("one\n\ntwo", f, 500) == ["one", "", "two"]
    long = wrap_text("Supercalifragilisticexpialidocious", f, 80)
    assert "".join(long) == "Supercalifragilisticexpialidocious"
    assert all(text_width(line, f) <= 80 for line in long)
    assert line_height(f) > f.size
