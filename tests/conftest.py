from pathlib import Path

import pytest

from photopainter import state


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Point config and state at a temp folder so no test touches ~/.config or ~/.cache.

    The data dir stays the repo's data/ (fonts, ephemeris); tests that need another one
    set PHOTOPAINTER_DATA_DIR themselves.
    """
    monkeypatch.setenv(state.CONFIG_ENV, str(tmp_path / "config" / "config.json"))
    monkeypatch.setenv(state.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(state.DATA_DIR_ENV, raising=False)
    return tmp_path


@pytest.fixture
def ephemeris() -> Path:
    """The JPL ephemeris in data/; fails (not skips) with how to get it if it's missing."""
    from photopainter.astro import ephemeris_path

    path = ephemeris_path()
    if not path.is_file():
        pytest.fail(
            f"JPL ephemeris missing: {path}\n"
            f"Run scripts/fetch-ephemeris.sh once in this checkout (it downloads to "
            f"~/.cache/photopainter-dev/ the first time and links it into data/).",
            pytrace=False,
        )
    return path
