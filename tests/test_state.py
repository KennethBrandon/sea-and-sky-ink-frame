import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from photopainter import state

REPO = Path(__file__).resolve().parents[1]


def test_default_paths(monkeypatch):
    for name in (state.CONFIG_ENV, state.STATE_DIR_ENV, state.DATA_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    assert state.config_path() == Path.home() / ".config" / "photopainter" / "config.json"
    assert state.state_dir() == Path.home() / ".cache" / "photopainter"
    assert state.data_dir() == REPO / "data"
    assert (state.data_dir() / "fonts").is_dir()


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(state.CONFIG_ENV, str(tmp_path / "c.json"))
    monkeypatch.setenv(state.STATE_DIR_ENV, str(tmp_path / "s"))
    monkeypatch.setenv(state.DATA_DIR_ENV, "~/somewhere")
    assert state.config_path() == tmp_path / "c.json"
    assert state.state_dir() == tmp_path / "s"
    assert state.data_dir() == Path.home() / "somewhere"
    monkeypatch.setenv(state.STATE_DIR_ENV, "")  # empty = unset
    assert state.state_dir() == Path.home() / ".cache" / "photopainter"


def test_write_and_read_json(tmp_path):
    path = tmp_path / "deep" / "status.json"
    state.write_json(path, {"a": 1, "b": [1, 2]})
    assert state.read_json(path) == {"a": 1, "b": [1, 2]}
    assert path.read_text().endswith("\n")
    assert [p.name for p in path.parent.iterdir() if not p.name.startswith(".")] == ["status.json"]
    assert not list(path.parent.glob("*.tmp")) and not list(path.parent.glob(".*.tmp"))


def test_read_json_missing_or_corrupt_gives_default_copy(tmp_path):
    default = {"pin": None, "up_next": []}
    got = state.read_json(tmp_path / "nope.json", default)
    assert got == default and got is not default
    got["up_next"].append("x")
    assert default["up_next"] == []
    bad = tmp_path / "bad.json"
    bad.write_text('{"pin": ')
    assert state.read_json(bad, default) == default
    assert state.read_json(bad) is None


def test_failed_serialization_leaves_file_untouched(tmp_path):
    path = tmp_path / "x.json"
    state.write_json(path, {"ok": True})
    with pytest.raises(TypeError):
        state.write_json(path, {"bad": object()})
    assert state.read_json(path) == {"ok": True}


def test_datetimes_are_written_with_offsets(tmp_path):
    path = tmp_path / "t.json"
    la = ZoneInfo("America/Los_Angeles")
    state.write_json(path, {"at": datetime(2026, 11, 3, 14, 10, 5, 123, tzinfo=la),
                            "utc": datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc),
                            "day": date(2026, 12, 15)})
    data = state.read_json(path)
    assert data == {"at": "2026-11-03T14:10:05-08:00", "utc": "2026-11-03T22:00:00+00:00",
                    "day": "2026-12-15"}
    assert state.from_iso(data["at"]) == datetime(2026, 11, 3, 14, 10, 5, tzinfo=la)


def test_naive_datetimes_are_refused(tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        state.write_json(tmp_path / "t.json", {"at": datetime(2026, 11, 3, 14, 10)})
    with pytest.raises(ValueError, match="no UTC offset"):
        state.from_iso("2026-11-03T14:10:00")
    assert state.from_iso("2026-11-03T22:10:00Z").utcoffset() == timedelta(0)


def test_update_json_read_modify_write(tmp_path):
    path = tmp_path / "overrides.json"
    with state.update_json(path, {"pin": None, "up_next": []}) as data:
        data["up_next"].append("beach.jpg")
    with state.update_json(path) as data:
        data["up_next"].append("dog.jpg")
    assert state.read_json(path) == {"pin": None, "up_next": ["beach.jpg", "dog.jpg"]}


def test_update_json_writes_nothing_on_error(tmp_path):
    path = tmp_path / "overrides.json"
    state.write_json(path, {"n": 1})
    with pytest.raises(RuntimeError):
        with state.update_json(path) as data:
            data["n"] = 2
            raise RuntimeError("boom")
    assert state.read_json(path) == {"n": 1}


def test_update_json_replaces_non_object_with_default(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("[1, 2]")
    with state.update_json(path, {"n": 0}) as data:
        data["n"] += 1
    assert state.read_json(path) == {"n": 1}


def test_lock_times_out(tmp_path):
    path = tmp_path / "x.json"
    with state.locked(path):
        # flock locks belong to the open file, so a second open in this process conflicts.
        with pytest.raises(TimeoutError):
            with state.locked(path, timeout=0.1):
                pass


def test_concurrent_updates_lose_nothing(tmp_path):
    """Several processes incrementing one counter: every increment survives."""
    path = tmp_path / "counter.json"
    worker = (
        "import sys; from pathlib import Path; from photopainter import state\n"
        "for _ in range(25):\n"
        "    with state.update_json(Path(sys.argv[1]), {'n': 0}) as d:\n"
        "        d['n'] += 1\n"
    )
    env = {"PYTHONPATH": str(REPO / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", worker, str(path)], env=env) for _ in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert json.loads(path.read_text()) == {"n": 100}


def test_write_atomic_bytes(tmp_path):
    path = tmp_path / "current.png"
    state.write_atomic(path, b"one")
    state.write_atomic(path, b"two")
    assert path.read_bytes() == b"two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["current.png"]
