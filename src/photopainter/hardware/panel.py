"""Drive the 7.3" Spectra 6 panel through the vendored Waveshare driver.

Importing this module is safe on a Mac: the driver (which grabs GPIO pins at import
time) is only imported inside PanelDisplay methods.
"""

from __future__ import annotations

import fcntl
import logging
import signal
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

PANEL_ROWS, PANEL_COLS = 480, 800
WHITE_CODE = 0x1
STATE_DIR = Path.home() / ".cache" / "photopainter"
REFRESH_TIMEOUT_S = 180  # a healthy refresh takes ~30 s; never hang forever on BUSY


def seconds_since_boot() -> float:
    """Measured from boot, not the wall clock: the Pi has no battery-backed clock, so after a
    power cut its wall clock can be wrong until it syncs over Wi-Fi."""
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


def pack_4bpp(codes: np.ndarray) -> bytes:
    """Pack 480x800 ink codes into the panel buffer: two pixels per byte, left pixel high."""
    if codes.shape != (PANEL_ROWS, PANEL_COLS):
        raise ValueError(f"expected {PANEL_ROWS}x{PANEL_COLS} codes, got {codes.shape}")
    if codes.max(initial=0) > 0xF:
        raise ValueError("ink codes must fit in 4 bits")
    codes = codes.astype(np.uint8, copy=False)
    return ((codes[:, 0::2] << 4) | codes[:, 1::2]).tobytes()


@contextmanager
def _timeout(seconds: int):
    def on_alarm(signum, frame):
        raise TimeoutError(f"panel did not finish within {seconds} s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class PanelDisplay:
    """One-shot access to the panel: init, refresh, deep sleep, power off."""

    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir if state_dir is not None else STATE_DIR

    def show(self, codes: np.ndarray) -> None:
        self._refresh(pack_4bpp(codes))

    def clear(self) -> None:
        self._refresh(bytes([WHITE_CODE << 4 | WHITE_CODE]) * (PANEL_ROWS * PANEL_COLS // 2))

    @contextmanager
    def _lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "panel.lock", "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another photopainter process is using the panel") from None
            yield

    def _refresh(self, buffer: bytes) -> None:
        with self._lock():
            from photopainter.hardware import epd7in3e  # claims GPIO pins on import

            epd = epd7in3e.EPD()
            start = time.monotonic()
            with _timeout(REFRESH_TIMEOUT_S):
                if epd.init() != 0:
                    raise RuntimeError("panel init failed")
                try:
                    epd.display(buffer)
                finally:
                    epd.sleep()  # deep sleep + display power off, even after an error
            log.info("panel refreshed in %.1f s", time.monotonic() - start)
