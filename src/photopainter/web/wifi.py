"""The frame's saved Wi-Fi networks: see them, add the next house's before the frame goes
there, forget one.

NetworkManager runs the Pi's networking (Raspberry Pi OS Trixie, with netplan behind it:
the network from `scripts/flash-sd.sh` is a netplan profile under /run). Listing is plain
`nmcli` as `pi`, which never prints secrets. Adding and removing need root and go through
`scripts/pi/wifi-connection` under `sudo -n`: `add` reads a NetworkManager keyfile from its
stdin (an argument would show in `ps`), writes it under
/etc/NetworkManager/system-connections/ with mode 0600 and loads it; `remove` deletes a
profile by uuid, never the one the frame is on. A password passes through once, from the
request to the keyfile; nothing here keeps, logs or returns it. Tried on the Pi on
2026-11-05 with a throwaway name.

Adding never switches networks: NetworkManager keeps the connection it has and joins a
saved one when it finds it with nothing connected, as after the move or a power cut at the
other house; Tailscale follows. The frame has 2.4 GHz Wi-Fi only.

With `--fake-panel` the server uses `FakeNetworks`, a list in memory, so a dashboard on the
Mac never touches the Mac's networks.
"""

from __future__ import annotations

import logging
import re
import subprocess
import uuid as uuidlib
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi" / "wifi-connection"
MAX_SSID_BYTES = 32  # IEEE 802.11
PASSWORD_LENGTH = (8, 63)  # a WPA passphrase; the 64-hex-digit raw key isn't offered
WIFI_TYPE = "802-11-wireless"
HOTSPOT_PROFILE = "photopainter-setup"  # the setup hotspot (`hotspot.py`), not a network to list
_NOT_FOR_A_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class BadNetwork(ValueError):
    """Can't be added or removed as asked; the message is for the person typing."""

    status = 400


class NotSaved(BadNetwork):
    status = 404


class InUse(BadNetwork):
    status = 409


class WifiFailed(RuntimeError):
    """nmcli or the helper failed, or isn't here."""


@dataclass(frozen=True)
class Network:
    name: str  # the profile's name in NetworkManager (the SSID, for ones added here)
    ssid: str
    connected: bool
    hidden: bool
    uuid: str

    def to_dict(self) -> dict:
        return asdict(self)


# --- checks --------------------------------------------------------------------------------


def check_ssid(ssid: object) -> str:
    if not isinstance(ssid, str) or not ssid.strip():
        raise BadNetwork("Type the network's name")
    ssid = ssid.strip()
    if any(ch < " " or ch == "\x7f" for ch in ssid):
        raise BadNetwork("The network name has characters that can't be typed")
    if len(ssid.encode("utf-8")) > MAX_SSID_BYTES:
        raise BadNetwork(f"A network name is at most {MAX_SSID_BYTES} characters (fewer with accents or emoji)")
    return ssid


def check_password(password: object) -> str | None:
    """The WPA passphrase to use, or None for an open network (empty or missing)."""
    if password is None or password == "":
        return None
    if not isinstance(password, str):
        raise BadNetwork("The password must be text")
    low, high = PASSWORD_LENGTH
    if not low <= len(password) <= high:
        raise BadNetwork(f"A Wi-Fi password is {low} to {high} characters; leave it empty for an open network")
    if any(not " " <= ch <= "~" for ch in password):
        raise BadNetwork("Use only letters, numbers, spaces and punctuation in the password")
    return password


def check_hidden(hidden: object) -> bool:
    if not isinstance(hidden, bool):
        raise BadNetwork("hidden must be true or false")
    return hidden


# --- the keyfile ---------------------------------------------------------------------------


def keyfile(name: str, ssid: str, password: str | None, hidden: bool, uuid: str) -> str:
    """A NetworkManager keyfile for the network: the text `wifi-connection add` reads.

    The SSID goes as a list of bytes, the one form NetworkManager never misreads (a name of
    digits alone would be taken for a list). Strings are escaped as GKeyFile writes them.
    An open network has no [wifi-security] section."""
    lines = ["[connection]", f"id={_escape(name)}", f"uuid={uuid}", "type=wifi", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode("utf-8"))]
    if hidden:
        lines.append("hidden=true")
    lines.append("")
    if password is not None:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={_escape(password)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def _escape(value: str) -> str:
    """As g_key_file_set_string writes a value: backslashes doubled, leading spaces `\\s`."""
    out = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    stripped = out.lstrip(" ")
    return "\\s" * (len(out) - len(stripped)) + stripped


# --- on the Pi -----------------------------------------------------------------------------


def _run(argv: list[str], *, stdin: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)


def _nmcli(args: list[str], run) -> str:
    try:
        done = run(["nmcli", *args])
    except FileNotFoundError:
        raise WifiFailed("nmcli isn't here: the frame's networks can only be managed on the Pi") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise WifiFailed(f"nmcli failed: {exc}") from exc
    if done.returncode != 0:
        raise WifiFailed(f"nmcli failed: {done.stderr.strip() or f'exit {done.returncode}'}")
    return done.stdout


def _helper(args: list[str], stdin: str | None, run) -> str:
    """Run scripts/pi/wifi-connection as root; its stdout (stripped), or WifiFailed."""
    try:
        done = run(["sudo", "-n", str(SCRIPT), *args], stdin=stdin, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WifiFailed(f"Couldn't change the frame's networks: {exc}") from exc
    if done.returncode != 0:
        raise WifiFailed(f"Couldn't change the frame's networks: {done.stderr.strip() or 'the helper failed'}")
    return done.stdout.strip()


def _split_terse(line: str) -> list[str]:
    """The fields of a `nmcli -t` tabular line: `:` separates them, `\\:` and `\\\\` are escapes."""
    parts: list[str] = []
    field: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            field.append(line[i + 1])
            i += 2
        elif ch == ":":
            parts.append("".join(field))
            field = []
            i += 1
        else:
            field.append(ch)
            i += 1
    parts.append("".join(field))
    return parts


def saved(*, run=None) -> list[Network]:
    """The saved Wi-Fi networks, the connected one first, then by name. Never a secret:
    nmcli hides them unless asked, and nothing here asks."""
    run = run or _run
    out: list[Network] = []
    for line in _nmcli(["-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show"], run).splitlines():
        parts = _split_terse(line)
        if len(parts) != 4 or parts[2] != WIFI_TYPE or parts[0] == HOTSPOT_PROFILE:
            continue
        name, uid, _, active = parts
        detail = _nmcli(["-t", "-m", "multiline", "-f", "802-11-wireless.ssid,802-11-wireless.hidden",
                         "connection", "show", "uuid", uid], run)
        fields = dict(entry.split(":", 1) for entry in detail.splitlines() if ":" in entry)
        out.append(Network(name, fields.get("802-11-wireless.ssid") or name, active == "yes",
                           fields.get("802-11-wireless.hidden") == "yes", uid))
    out.sort(key=lambda n: (not n.connected, n.ssid.lower()))
    return out


def add(ssid: object, password: object, hidden: object = False, *, run=None) -> Network:
    """Save a network for the frame to join when it's in range. It never switches now."""
    run = run or _run
    ssid, password, hidden = check_ssid(ssid), check_password(password), check_hidden(hidden)
    _refuse_known(saved(run=run), ssid)
    uid = str(uuidlib.uuid4())
    stem = f"{(_NOT_FOR_A_FILE_NAME.sub('_', ssid).strip('._-') or 'network')[:40]}-{uid[:8]}"
    _helper(["add", stem], keyfile(ssid, ssid, password, hidden, uid), run)
    log.info("saved Wi-Fi network %s%s", ssid, " (hidden)" if hidden else "")
    return Network(ssid, ssid, False, hidden, uid)


def add_trial(ssid: object, password: object, hidden: object = False, *, run=None) -> str:
    """Save a network for the setup hotspot to try (`hotspot.py`): its uuid. Unlike `add`, a name
    the frame knows is fine, since a new password for the same network is the usual reason to be
    here. The old profile stays until this one has worked."""
    run = run or _run
    ssid, password, hidden = check_ssid(ssid), check_password(password), check_hidden(hidden)
    uid = str(uuidlib.uuid4())
    stem = f"{(_NOT_FOR_A_FILE_NAME.sub('_', ssid).strip('._-') or 'network')[:40]}-{uid[:8]}"
    _helper(["add", stem], keyfile(ssid, ssid, password, hidden, uid), run)
    return uid


def remove(name: object, *, run=None) -> Network:
    """Forget a saved network, by its name. Never the one the frame is on."""
    run = run or _run
    net = _find(saved(run=run), name)
    _helper(["remove", net.uuid], None, run)
    log.info("forgot Wi-Fi network %s", net.ssid)
    return net


def _refuse_known(nets: list[Network], ssid: str) -> None:
    for n in nets:
        if n.ssid == ssid or n.name == ssid:
            raise BadNetwork(f"The frame already knows {ssid}" + (": it's on it now" if n.connected else ""))


def _find(nets: list[Network], name: object) -> Network:
    for n in nets:
        if n.name == name:
            if n.connected:
                raise InUse(f"The frame is connected to {n.ssid} now, so it keeps that one")
            return n
    raise NotSaved(f"No saved network is called {name}")


# --- on the Mac ----------------------------------------------------------------------------


class FakeNetworks:
    """`--fake-panel`: the same three calls on a list in memory, seeded with a made-up home
    network the frame is "on", so the page has something to show on the Mac."""

    def __init__(self):
        self.networks = [Network("Home", "Home", True, False, "00000000-0000-4000-8000-000000000001")]

    def saved(self) -> list[Network]:
        return sorted(self.networks, key=lambda n: (not n.connected, n.ssid.lower()))

    def add(self, ssid: object, password: object, hidden: object = False) -> Network:
        ssid, password, hidden = check_ssid(ssid), check_password(password), check_hidden(hidden)
        _refuse_known(self.networks, ssid)
        net = Network(ssid, ssid, False, hidden, str(uuidlib.uuid4()))
        self.networks.append(net)
        return net

    def add_trial(self, ssid: object, password: object, hidden: object = False) -> str:
        check_ssid(ssid), check_password(password), check_hidden(hidden)
        return str(uuidlib.uuid4())

    def remove(self, name: object) -> Network:
        net = _find(self.networks, name)
        self.networks.remove(net)
        return net
