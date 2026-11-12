"""The Wedge's screens, pixel for pixel (PRD.md §9, L0).

Every approved screen, drawn with the default config (the Wedge) from the fixture caches at
fixed moments, must give exactly the ink indices it gave when `golden_wedge.json` was
recorded. The any-coast work (PRD §9) moves place data out of the code and adds settings
whose defaults are today's behavior; this test is what holds it to that.

When a hash differs, the test saves the new picture as a PNG and names it, so you can look.

Re-record only for a change the Owner approved over previews, and never in the same commit as
a refactor: first record on the commit before the change, so the only difference is the one
meant. To record:

    .venv/bin/python tests/test_golden.py

The hashes depend on Pillow (its font rendering) and NumPy, so the file notes their versions.
If they differ from yours, a failure may be the libraries, not the code: record on the commit
before your change with your versions, then compare.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import PIL
import pytest

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden_wedge.json"
LA = ZoneInfo("America/Los_Angeles")

# name: (scene kind, moment drawn, extra Scene fields)
SCREENS: dict[str, tuple[str, str, dict]] = {
    "tides afternoon": ("tides", "2026-11-03T14:10", {}),
    "tides night": ("tides", "2026-11-03T22:30", {}),
    "tides stale weather": ("tides", "2026-11-04T20:00", {}),  # weather over 12 h old: Sun & moon box
    "sky night": ("sky", "2026-11-03T22:30", {}),
    "sky twilight": ("sky", "2026-11-03T17:15", {}),
    "sky day": ("sky", "2026-11-04T12:00", {}),
    "sky summer night": ("sky", "2027-07-03T22:30", {}),  # the Milky Way's core, Catalina, no weather
    "sky lunar eclipse": ("sky", "2026-03-03T03:30", {}),
    "sea afternoon": ("sea", "2026-11-03T14:10", {}),
    "sea night": ("sea", "2026-11-03T22:30", {}),
    "sea big swell look": ("sea", "2026-11-03T10:00", {"waves": "big", "clouds": "partly"}),
    "harbor afternoon": ("harbor", "2026-11-03T14:10", {}),
    "harbor night": ("harbor", "2026-11-03T22:30", {}),
    "combo afternoon": ("combo", "2026-11-03T14:10", {}),
    "combo night": ("combo", "2026-11-03T22:30", {}),
}

pytestmark = pytest.mark.skipif(not (ROOT / "data" / "de421.bsp").exists(), reason="no ephemeris")


def render(name: str) -> np.ndarray:
    from photopainter.config import default_config
    from photopainter.scenes import Scene, SceneContext, render_scene

    kind, at, extra = SCREENS[name]
    when = datetime.fromisoformat(at).replace(tzinfo=LA)
    ctx = SceneContext.from_config(default_config(), state_dir=FIXTURES, data_dir=ROOT / "data")
    return render_scene(Scene(kind, f"{kind}/golden", when, **extra), ctx)


def digest(ink: np.ndarray) -> str:
    h = hashlib.sha256(f"{ink.shape} {ink.dtype}".encode())
    h.update(np.ascontiguousarray(ink).tobytes())
    return h.hexdigest()


def versions() -> dict[str, str]:
    return {"pillow": PIL.__version__, "numpy": np.__version__}


def load() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_every_screen_has_a_recorded_hash():
    assert sorted(load()["screens"]) == sorted(SCREENS)


@pytest.mark.parametrize("name", sorted(SCREENS))
def test_the_wedge_draws_what_it_drew(name, tmp_path):
    golden = load()
    ink = render(name)
    if digest(ink) == golden["screens"][name]:
        return
    from photopainter.render.palette import preview_image

    out = tmp_path / (name.replace(" ", "-") + ".png")
    preview_image(ink).save(out)
    note = ""
    if golden["versions"] != versions():
        note = f" (recorded with {golden['versions']}, running {versions()}: see the module docstring)"
    pytest.fail(f"{name}: not the pixels recorded in {GOLDEN.name}{note}; the new picture is {out}")


def record() -> None:
    data = {
        "note": "sha256 of each screen's ink indices for the default config; tests/test_golden.py",
        "versions": versions(),
        "screens": {name: digest(render(name)) for name in sorted(SCREENS)},
    }
    GOLDEN.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(data['screens'])} screens in {GOLDEN}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    record()
