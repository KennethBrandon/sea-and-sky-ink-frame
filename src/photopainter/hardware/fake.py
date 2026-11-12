"""A stand-in for the panel: `PanelDisplay`'s `show`/`clear`, no GPIO.

Used by tests and by `--fake-panel` (the tick and the dashboard on the Mac). It takes the
same panel lock and keeps what it was sent in `buffers` (packed, as the real panel gets them).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from photopainter.hardware.panel import PANEL_COLS, PANEL_ROWS, PanelDisplay

log = logging.getLogger(__name__)


class FakePanel(PanelDisplay):
    """Records refreshes instead of driving the panel."""

    def __init__(self, state_dir: Path | None = None):
        super().__init__(state_dir)
        self.buffers: list[bytes] = []

    @property
    def refreshes(self) -> int:
        return len(self.buffers)

    def last_codes(self) -> np.ndarray | None:
        """The last image sent, unpacked to 480x800 panel codes."""
        if not self.buffers:
            return None
        packed = np.frombuffer(self.buffers[-1], dtype=np.uint8).reshape(PANEL_ROWS, PANEL_COLS // 2)
        codes = np.empty((PANEL_ROWS, PANEL_COLS), dtype=np.uint8)
        codes[:, 0::2], codes[:, 1::2] = packed >> 4, packed & 0xF
        return codes

    def _refresh(self, buffer: bytes) -> None:
        with self._lock():
            self.buffers.append(buffer)
            log.info("fake panel: refreshed (no hardware)")
