"""The frame's name on the network: what makes the dashboard http://<name>.local.

The name is the Pi's hostname and nothing else, so there's no setting that could disagree
with it. `set_name` hands the work to `scripts/pi/set-frame-name`, run under sudo, which
applies it at once without a reboot: the hostname, the .local name, the Tailscale name.

The server runs this only on the Pi. With `--fake-panel` it keeps the name in memory, so a
dashboard on the Mac can never rename the Mac.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi" / "set-frame-name"
MAX_LENGTH = 32

_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


class BadName(ValueError):
    """A name that can't be used; the message is for the person typing it."""


class RenameFailed(RuntimeError):
    """The Pi didn't take the name."""


def check(name: object) -> str:
    """The name to use, or BadName. Capitals are kept: addresses ignore them."""
    if not isinstance(name, str) or not name.strip():
        raise BadName("Type a name for the frame")
    name = name.strip()
    if len(name) > MAX_LENGTH:
        raise BadName(f"Keep the name to {MAX_LENGTH} characters")
    if not _NAME.fullmatch(name):
        raise BadName("Use only letters, numbers and dashes, with no dash at the start or end")
    if name.isdigit():
        raise BadName("Use at least one letter")
    if name.lower() == "localhost":
        raise BadName("That name means every computer itself; pick another")
    return name


def set_name(name: str, *, run=subprocess.run) -> list[str]:
    """Rename the Pi. Returns warnings for the parts that didn't take (Tailscale can fail
    while the .local name works); raises RenameFailed if the hostname itself didn't change."""
    try:
        done = run(["sudo", "-n", str(SCRIPT), name], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenameFailed(f"Couldn't rename the frame: {exc}") from exc
    if done.returncode != 0:
        why = done.stderr.strip() or "the rename script failed"
        raise RenameFailed(f"Couldn't rename the frame: {why}")
    return [line.strip().removeprefix("warning: ") for line in done.stdout.splitlines()
            if line.startswith("warning: ")]
