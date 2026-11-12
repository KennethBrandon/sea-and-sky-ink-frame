"""scripts/deploy.sh in a scratch checkout, with ssh and the Pi's commands faked.

rsync is real, aimed at a local folder, so the excludes are tested by what actually arrives.
The script's remote part really runs, against fake systemctl, pgrep, sudo, sleep and seq
(which records how long the script would wait, then lets it loop only 5 times).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
RESTART = "sudo systemctl restart photopainter-web.service"
RESTART_BUTTONS = "sudo systemctl restart photopainter-buttons.service"

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")

STUBS = {
    # Every argument but the last, then the fake Pi's folder in place of host:path.
    "rsync": '''echo "rsync $*" >> "$LOG"
args=("$@"); unset "args[${#args[@]}-1]"
exec "$REAL_RSYNC" "${args[@]}" "$PI_COPY/"''',
    # A script on stdin runs here, against the stubs below; a command line is only recorded.
    "ssh": '''shift
if [ "$1" = "bash -s" ]; then exec bash -s; fi
echo "ssh $*" >> "$LOG"''',
    "systemctl": '''case "$1" in
  is-active) if [ "$3" = photopainter-buttons.service ]; then exit "$BUTTONS_INACTIVE"; fi; exit "$WEB_INACTIVE" ;;
  show) echo 42 ;;
esac''',
    # Busy (has a child process) for the next $(cat "$BUSY") checks.
    "pgrep": '''[ "$2" = 42 ] || exit 2
n=$(cat "$BUSY")
[ "$n" -gt 0 ] || exit 1
echo $((n - 1)) > "$BUSY"''',
    "sleep": 'echo "sleep $*" >> "$LOG"',
    "seq": '''echo "seq $*" >> "$LOG"
echo 1 2 3 4 5''',
    "sudo": 'echo "sudo $*" >> "$LOG"',
}


@pytest.fixture
def deploy(tmp_path):
    """A function that runs the deploy; it returns (result, log lines)."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "src").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "deploy.sh")
    (repo / "data" / "de421.bsp").write_bytes(b"ephemeris")
    (repo / "src" / "code.py").write_text("x = 1")
    worktree = repo / ".claude" / "worktrees" / "some-task"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "code.py").write_text("x = 2")
    (tmp_path / "pi").mkdir()

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        path = stubs / name
        path.write_text(f"#!/bin/bash\n{body}\n")
        path.chmod(0o755)
    log = tmp_path / "log"
    busy = tmp_path / "busy"

    def run(busy_checks=0, web_running=True, buttons_running=False):
        log.write_text("")
        busy.write_text(str(busy_checks))
        env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "LOG": str(log),
               "BUSY": str(busy), "WEB_INACTIVE": "0" if web_running else "3",
               "BUTTONS_INACTIVE": "0" if buttons_running else "3",
               "REAL_RSYNC": shutil.which("rsync"), "PI_COPY": str(tmp_path / "pi"),
               "PHOTOPAINTER_DEPLOY_ANY": "1"}
        result = subprocess.run(["bash", str(repo / "scripts" / "deploy.sh")], capture_output=True,
                                text=True, env=env, timeout=60)
        return result, log.read_text().splitlines()

    run.pi = tmp_path / "pi"
    return run


def test_worktrees_stay_off_the_pi(deploy):
    result, _ = deploy()
    assert result.returncode == 0, result.stderr
    assert (deploy.pi / "src" / "code.py").read_text() == "x = 1"
    assert (deploy.pi / "scripts" / "deploy.sh").is_file()
    assert not (deploy.pi / ".claude").exists()


def test_idle_dashboard_restarts_after_the_code_is_on_the_pi(deploy):
    result, log = deploy()
    assert result.returncode == 0, result.stderr
    assert RESTART in log and "sleep 1" not in log
    assert log[0].startswith("rsync ")  # the new code first
    assert log.index(RESTART) > next(i for i, line in enumerate(log) if line.startswith("ssh "))
    assert "The dashboard restarted." in result.stdout and "Deployed to" in result.stdout
    assert RESTART_BUTTONS not in log and "The buttons service isn't running" in result.stdout


def test_waits_for_a_running_tick_before_restarting(deploy):
    result, log = deploy(busy_checks=3)
    assert result.returncode == 0, result.stderr
    assert log.count("sleep 1") == 3
    assert log[-1] == RESTART


def test_never_restarts_while_a_tick_is_still_running(deploy):
    result, log = deploy(busy_checks=100_000)
    assert result.returncode != 0
    assert RESTART not in log
    assert "seq 240" in log and log.count("sleep 1") == 5  # 240 s: longer than a refresh may take
    assert "still running the old code" in result.stderr and RESTART.removeprefix("sudo ") in result.stderr
    assert "Deployed to" not in result.stdout


def test_stopped_dashboard_stays_stopped(deploy):
    result, log = deploy(web_running=False)
    assert result.returncode == 0, result.stderr
    assert RESTART not in log
    assert "nothing to restart" in result.stdout and "Deployed to" in result.stdout


def test_the_buttons_restart_too_each_when_its_tick_has_finished(deploy):
    result, log = deploy(busy_checks=3, buttons_running=True)
    assert result.returncode == 0, result.stderr
    assert log.index(RESTART) < log.index(RESTART_BUTTONS)
    assert log.count("sleep 1") == 3 and log[log.index(RESTART) - 1] == "sleep 1"


def test_a_busy_dashboard_stops_the_deploy_before_the_buttons(deploy):
    result, log = deploy(busy_checks=100_000, buttons_running=True)
    assert result.returncode != 0
    assert RESTART not in log and RESTART_BUTTONS not in log
