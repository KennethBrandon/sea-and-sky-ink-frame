import numpy as np
import pytest
from PIL import Image

from photopainter import cli
from photopainter.hardware.panel import PanelDisplay, pack_4bpp


def test_pack_4bpp_left_pixel_in_high_nibble():
    codes = np.zeros((480, 800), dtype=np.uint8)
    codes[0, 0], codes[0, 1] = 0x3, 0x5  # red, blue
    codes[1, 798], codes[1, 799] = 0x6, 0x2  # green, yellow (last pair of row 2)
    buf = pack_4bpp(codes)
    assert len(buf) == 192_000
    assert buf[0] == 0x35
    assert buf[400 + 399] == 0x62


def test_pack_4bpp_matches_waveshare_getbuffer_loop():
    rng = np.random.default_rng(1)
    codes = rng.choice(np.array([0, 1, 2, 3, 5, 6], dtype=np.uint8), size=(480, 800))
    flat = codes.ravel()
    expected = bytes((flat[i] << 4) + flat[i + 1] for i in range(0, flat.size, 2))
    assert pack_4bpp(codes) == expected


def test_pack_4bpp_rejects_bad_input():
    with pytest.raises(ValueError):
        pack_4bpp(np.zeros((800, 480), dtype=np.uint8))
    with pytest.raises(ValueError):
        pack_4bpp(np.full((480, 800), 0x10, dtype=np.uint16))


def test_panel_lock_is_exclusive(tmp_path):
    panel = PanelDisplay(state_dir=tmp_path)
    with panel._lock():
        with pytest.raises(RuntimeError, match="another photopainter process"):
            with PanelDisplay(state_dir=tmp_path)._lock():
                pass


def test_cli_preview_writes_png(tmp_path):
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (1600, 1200), (200, 120, 40)).save(src)
    out = tmp_path / "out.png"
    assert cli.main(["preview", str(src), "-o", str(out)]) == 0
    assert Image.open(out).size == (800, 480)
    assert cli.main(["preview", str(src), "-o", str(out), "--portrait", "--fit", "fit"]) == 0
    assert Image.open(out).size == (480, 800)


def test_cli_defaults_match_render_options():
    from photopainter.render.pipeline import RenderOptions

    args = cli.build_parser().parse_args(["preview", "x.jpg"])
    assert cli._options(args) == RenderOptions()


def test_next_defaults_to_home_photos_folder():
    from pathlib import Path

    args = cli.build_parser().parse_args(["next"])
    assert args.image == str(Path.home() / "photos")
    assert cli.build_parser().parse_args(["next", "/tmp/x"]).image == "/tmp/x"


def test_cli_reports_missing_file_cleanly(tmp_path, caplog):
    assert cli.main(["preview", str(tmp_path / "nope.jpg")]) == 1
    assert "nope.jpg" in caplog.text
