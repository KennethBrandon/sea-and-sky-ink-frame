"""Work the dashboard hands to child processes: ticks and preview renders.

The server never drives the panel (PRD.md §4). After an action it starts `photopainter
tick`, which decides, draws and refreshes exactly as the timer's ticks do; the tick's own
lock keeps two of them from overlapping. Previews (`photopainter scene … -o FILE`) run in
child processes too, so the server stays small (PRD budget: < 80 MB resident) and memory
a render used goes back to the system when it exits.

`TickRunner` keeps at most one tick running and one queued: tapping three buttons quickly
starts one tick and then one more, not three.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

TICK_MAX_RUNTIME_S = 6 * 60  # a tick gives up on the panel after 180 s; kill one stuck longer
RENDER_TIMEOUT_S = 120


def photopainter_command(*args: str) -> list[str]:
    """`photopainter ARGS` with this interpreter. On the Pi the launcher's PYTHONPATH is
    inherited, so the child runs the same code as the server."""
    return [sys.executable, "-m", "photopainter", *args]


class TickRunner:
    """Runs `command` (a tick) in the background: one at a time, at most one queued."""

    def __init__(self, command: Sequence[str], *, max_runtime_s: float = TICK_MAX_RUNTIME_S):
        self.command = list(command)
        self.max_runtime_s = max_runtime_s
        self.started = 0  # ticks started so far
        self.last_exit: int | None = None
        self.last_error: str | None = None  # couldn't start one
        self._lock = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._again = False
        self._closed = False

    # --- asking for ticks --------------------------------------------------------------

    def request(self) -> None:
        """Start a tick now, or once more after the running one if there is one."""
        with self._lock:
            if self._closed:
                return
            if self._proc is not None:
                self._again = True
            else:
                self._start()

    # --- state ---------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None

    def snapshot(self) -> dict:
        """For `GET /api/status`: {"running", "queued"}."""
        with self._lock:
            return {"running": self._proc is not None, "queued": self._again}

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no tick is running or queued. True if idle."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._proc is not None or self._again:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._lock.wait(left)
            return True

    def close(self, wait_s: float = 0) -> bool:
        """Start nothing new; give a running tick up to `wait_s` to finish (so a restart
        of the server doesn't cut a panel refresh short). True if none is left running."""
        with self._lock:
            self._closed = True
            self._again = False
        return self.wait_idle(wait_s)

    # --- the child process ---------------------------------------------------------------

    def _start(self) -> None:  # with the lock held
        try:
            proc = subprocess.Popen(self.command, stdin=subprocess.DEVNULL)
        except OSError as exc:
            log.error("couldn't start a tick (%s): %s", " ".join(self.command), exc)
            self.last_error = f"couldn't start a tick: {exc}"
            self._lock.notify_all()
            return
        self._proc = proc
        self.started += 1
        self.last_error = None
        threading.Thread(target=self._watch, args=(proc,), name="tick-watch", daemon=True).start()

    def _watch(self, proc: subprocess.Popen) -> None:
        try:
            code = proc.wait(timeout=self.max_runtime_s)
        except subprocess.TimeoutExpired:
            log.error("a tick ran for over %.0f s; stopping it", self.max_runtime_s)
            proc.kill()
            code = proc.wait()
        if code:
            log.warning("tick exited with status %s (see status.json last_error)", code)
        with self._lock:
            self.last_exit = code
            self._proc = None
            if self._again and not self._closed:
                self._again = False
                self._start()
            self._lock.notify_all()


class RenderError(RuntimeError):
    """A preview couldn't be drawn; the message is readable as is."""


class Renderer:
    """Draws screens to PNG files with `photopainter scene`, one at a time, at low priority
    so the tick (which drives the panel) keeps the CPU it needs."""

    def __init__(self, command: Sequence[str] | None = None, timeout_s: float = RENDER_TIMEOUT_S):
        base = list(command) if command is not None else photopainter_command("scene")
        nice = shutil.which("nice")
        self.command = ([nice, "-n", "10"] if nice else []) + base
        self.timeout_s = timeout_s
        self._lock = threading.Lock()

    def __call__(self, args: Sequence[str], out: Path) -> None:
        """`photopainter scene ARGS -o out`. Raises RenderError with the CLI's message."""
        tmp = out.with_name(f".{out.stem}.rendering.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                proc = subprocess.run([*self.command, *args, "-o", str(tmp)], stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired:
                tmp.unlink(missing_ok=True)
                raise RenderError(f"drawing took over {self.timeout_s:.0f} s") from None
            except OSError as exc:
                raise RenderError(f"couldn't start the renderer: {exc}") from exc
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise RenderError(cli_error(proc.stderr) or f"the renderer exited with status {proc.returncode}")
        tmp.replace(out)


_LOG_LINE = re.compile(r"^\S+ \S+ (?:ERROR|CRITICAL) [\w.]+: (.*)$")


def cli_error(stderr: str) -> str:
    """The last error the CLI logged ("… ERROR photopainter: <message>"), or its last line."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        m = _LOG_LINE.match(line)
        if m:
            return m[1]
    return lines[-1] if lines else ""
