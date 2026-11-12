"""scripts/sync-photos.sh, run into a local folder (PHOTOPAINTER_SYNC_DEST) instead of the Pi.

By default it only adds and updates, so photos uploaded from the dashboard survive;
--mirror deletes the rest. Neither ever touches dot-files such as the dashboard's .trash/.
The script converts with macOS's `sips`, so these tests run on the Mac only.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-photos.sh"

pytestmark = pytest.mark.skipif(shutil.which("sips") is None or shutil.which("rsync") is None,
                                reason="needs macOS's sips and rsync")


@pytest.fixture
def frame(tmp_path):
    """Photos on the Mac, and the Pi's ~/photos with a dashboard upload and its trash."""
    mac = tmp_path / "mac"
    mac.mkdir()
    Image.new("RGB", (2400, 1200), (200, 120, 40)).save(mac / "beach.png")
    Image.new("RGB", (640, 480), (20, 120, 200)).save(mac / "IMG_0001.jpg")
    pi = tmp_path / "pi-photos"
    (pi / ".trash").mkdir(parents=True)
    Image.new("RGB", (100, 60)).save(pi / "upload.jpg")  # from the dashboard
    Image.new("RGB", (100, 60)).save(pi / ".trash" / "deleted.jpg")
    (pi / ".upload.jpg.abc.tmp").write_bytes(b"half written")
    return mac, pi


def sync(*args: str, dest: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PHOTOPAINTER_SYNC_DEST": str(dest), "PHOTOPAINTER_HOST": "no-such-host.invalid"}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def files(folder: Path) -> set[str]:
    return {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}


def test_default_adds_and_keeps_everything_else(frame):
    mac, pi = frame
    r = sync(str(mac), dest=pi)
    assert r.returncode == 0, r.stderr
    assert files(pi) == {"beach.jpg", "IMG_0001.jpg", "upload.jpg", ".trash/deleted.jpg", ".upload.jpg.abc.tmp"}
    with Image.open(pi / "beach.jpg") as img:
        assert img.format == "JPEG" and max(img.size) == 1600
    assert "added or updated" in r.stdout


def test_mirror_deletes_other_photos_but_never_dot_files(frame):
    mac, pi = frame
    r = sync("--mirror", str(mac / "IMG_0001.jpg"), dest=pi)
    assert r.returncode == 0, r.stderr
    assert files(pi) == {"IMG_0001.jpg", ".trash/deleted.jpg", ".upload.jpg.abc.tmp"}


def test_dry_run_shows_what_a_mirror_would_delete(frame):
    mac, pi = frame
    before = files(pi)
    r = sync("--mirror", "--dry-run", str(mac), dest=pi)
    assert r.returncode == 0, r.stderr
    assert files(pi) == before
    assert "deleting upload.jpg" in r.stdout
    assert ".trash" not in r.stdout and "Dry run" in r.stdout


@pytest.mark.parametrize("args", [(), ("--mirror",), ("--bogus", "x.jpg")])
def test_bad_arguments(frame, args):
    _, pi = frame
    r = sync(*args, dest=pi)
    assert r.returncode == 1 and "usage" in r.stderr
