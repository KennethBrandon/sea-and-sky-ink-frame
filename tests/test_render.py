import io
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from photopainter.render.layout import canvas_size, fit_to_canvas, open_image
from photopainter.render.palette import CODES, INKS, map_to_inks, palette_colors, preview_image
from photopainter.render.pipeline import RenderOptions, render, to_panel_codes


def solid(color, size=(800, 480)):
    return Image.new("RGB", size, color)


def noise(size=(800, 480), seed=0):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))


# --- palette -----------------------------------------------------------------


def test_codes_match_waveshare_driver():
    # black 0, white 1, yellow 2, red 3, (4 unused), blue 5, green 6
    assert {ink.name: ink.code for ink in INKS} == {
        "black": 0, "white": 1, "yellow": 2, "red": 3, "blue": 5, "green": 6,
    }
    assert list(CODES) == [0, 1, 2, 3, 5, 6]


@pytest.mark.parametrize("palette", ["estimated", "nominal"])
def test_each_ink_color_maps_to_itself(palette):
    for i, rgb in enumerate(palette_colors(palette)):
        indices = map_to_inks(solid(rgb, (16, 8)), palette, dither=False)
        assert (indices == i).all(), INKS[i].name


@pytest.mark.parametrize("palette", ["estimated", "nominal"])
@pytest.mark.parametrize("dither", [True, False])
def test_indices_stay_within_six_inks(palette, dither):
    indices = map_to_inks(noise(), palette, dither)
    assert indices.shape == (480, 800)
    assert indices.dtype == np.uint8
    assert indices.max() < len(INKS)


def test_dithering_mixes_inks_for_in_between_colors():
    gray = map_to_inks(solid((128, 128, 128), (64, 64)), "nominal", dither=True)
    assert set(np.unique(gray)) >= {0, 1}  # black and white dots


def test_preview_uses_only_palette_colors():
    indices = map_to_inks(noise(), "estimated")
    pixels = np.asarray(preview_image(indices)).reshape(-1, 3)
    assert {tuple(p) for p in np.unique(pixels, axis=0)} <= set(palette_colors("estimated"))


# --- layout ------------------------------------------------------------------


@pytest.mark.parametrize("src_size", [(4000, 3000), (3000, 4000), (800, 480), (100, 100)])
@pytest.mark.parametrize("mode", ["fill", "fit"])
def test_fit_to_canvas_always_exact_size(src_size, mode):
    assert fit_to_canvas(solid((10, 20, 30), src_size), (800, 480), mode).size == (800, 480)


def test_fit_pads_with_background_and_keeps_whole_image():
    out = fit_to_canvas(solid((255, 0, 0), (480, 480)), (800, 480), "fit", background=(0, 0, 0))
    px = np.asarray(out)
    assert tuple(px[240, 400]) == (255, 0, 0)  # image in the middle
    assert tuple(px[240, 5]) == (0, 0, 0)  # border on the left


def test_canvas_size_orientation():
    assert canvas_size(portrait=False) == (800, 480)
    assert canvas_size(portrait=True) == (480, 800)


def test_open_image_applies_exif_rotation():
    img = solid((0, 0, 255), (40, 20))  # stored landscape
    exif = img.getexif()
    exif[0x0112] = 6  # "rotate 90 CW to display" -> displayed portrait
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    assert open_image(buf).size == (20, 40)


def test_gamma_brightens_midtones_but_keeps_black_and_white():
    from photopainter.render.layout import enhance

    img = Image.new("RGB", (3, 1))
    img.putdata([(0, 0, 0), (128, 128, 128), (255, 255, 255)])
    out = np.asarray(enhance(img, gamma=1.5))[0]
    assert tuple(out[0]) == (0, 0, 0)
    assert tuple(out[2]) == (255, 255, 255)
    assert out[1][0] > 160  # 128 -> ~161


def test_vibrance_boosts_muted_colors_more_than_saturated_ones():
    from photopainter.render.layout import vibrance

    def chroma(rgb):
        return max(rgb) - min(rgb)

    img = Image.new("RGB", (2, 1))
    muted, strong = (150, 130, 115), (220, 40, 30)
    img.putpixel((0, 0), muted)
    img.putpixel((1, 0), strong)
    out = np.asarray(vibrance(img, 1.0))[0].astype(int)
    muted_gain = chroma(out[0]) / chroma(muted)
    strong_gain = chroma(out[1]) / chroma(strong)
    assert muted_gain > 1.7
    assert strong_gain < 1.3
    assert (np.asarray(vibrance(img, 0.0)) == np.asarray(img)).all()


# --- pipeline ----------------------------------------------------------------


def test_render_landscape_and_portrait_shapes():
    assert render(noise((1200, 900))).shape == (480, 800)
    assert render(noise((900, 1200)), RenderOptions(portrait=True)).shape == (800, 480)


def test_portrait_rotation_matches_waveshare_driver():
    # The driver rotates 480x800 portrait images with PIL rotate(90, expand=True).
    opts = RenderOptions(portrait=True, palette="nominal", dither=False)
    canvas = Image.new("RGB", (480, 800), (255, 255, 255))
    canvas.paste((255, 0, 0), (0, 0, 40, 40))  # red block in the viewer's top-left
    indices = map_to_inks(canvas, "nominal", dither=False)
    codes = to_panel_codes(indices, opts)
    expected = map_to_inks(canvas.rotate(90, expand=True), "nominal", dither=False)
    assert (codes == CODES[expected]).all()


def test_flip_rotates_180():
    indices = np.zeros((480, 800), dtype=np.uint8)
    indices[0, 0] = 3
    codes = to_panel_codes(indices, RenderOptions(flip=True))
    assert codes[479, 799] == CODES[3]


def test_to_panel_codes_rejects_wrong_shape():
    with pytest.raises(ValueError):
        to_panel_codes(np.zeros((800, 480), dtype=np.uint8), RenderOptions(portrait=False))


# --- import hygiene ----------------------------------------------------------


def test_cli_and_panel_import_without_hardware_libraries():
    code = (
        "import sys, photopainter.cli, photopainter.hardware.panel; "
        "bad = {'spidev', 'gpiozero', 'lgpio', 'smbus', 'smbus2'} & set(sys.modules); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
