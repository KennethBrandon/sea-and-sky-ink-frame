"""The Wi-Fi setup screen: a QR code that joins the frame's hotspot, and what to do next.

Up only after someone held the back button with no Wi-Fi (`hotspot.py`). The QR code holds the
hotspot's name and password, so a phone's camera joins it with no typing; the name and password
are written out as well, for a phone without a camera that reads them. `scene setup` draws made-up
ones when no hotspot is up, for previews.

The code is black on white with no dithering, about 2.6 inches across at 10 pixels a module,
which is what a phone needs to read it from arm's length. It comes from `segno` (apt:
python3-segno). Should that be missing, the words are still there and work without it.
"""

from __future__ import annotations

import numpy as np

from photopainter import hotspot
from photopainter.render.draw import INK, InkCanvas, font, line_height, wrap_text
from photopainter.scenes.base import Scene, SceneContext

ACCENT_HEIGHT = 10  # the blue band of a frame notice (scenes/message.py)
MARGIN = 28
QR_GAP = 36
QR_MAX = {False: 340, True: 300}  # by portrait: the side of the code at most
SAMPLE_PASSWORD = "tide-kelp-0000"  # previews only
SIZES = ((36, 21, 15), (32, 19, 14), (28, 17, 13), (26, 16, 12))  # heading, steps, small print: largest first
BOTTOM = 10  # white left under the words


def wifi_qr_text(ssid: str, password: str) -> str:
    """The string phones read as "join this network" (the de facto `WIFI:` form)."""
    return f"WIFI:T:WPA;S:{_qr_escape(ssid)};P:{_qr_escape(password)};;"


def _qr_escape(value: str) -> str:
    return "".join("\\" + ch if ch in '\\;,:"' else ch for ch in value)


def _matrix(text: str) -> list[bytes] | None:
    try:
        import segno
    except ImportError:
        return None
    return [bytes(row) for row in segno.make(text, error="m", boost_error=False).matrix]


def _draw_qr(canvas: InkCanvas, rows: list[bytes], x: int, y: int, scale: int) -> None:
    for j, row in enumerate(rows):
        for i, on in enumerate(row):
            if on:
                canvas.draw.rectangle([x + i * scale, y + j * scale,
                                       x + (i + 1) * scale - 1, y + (j + 1) * scale - 1], fill=INK["black"])


def render(scene: Scene, ctx: SceneContext) -> np.ndarray:
    data = hotspot.load(ctx.state_dir)
    name = hotspot.frame_name()
    ssid = data["ssid"] or hotspot.setup_ssid(name)
    password = data["password"] or SAMPLE_PASSWORD
    result = data["result"] if isinstance(data["result"], dict) and not data["result"].get("ok") else None

    width, height = ctx.canvas
    portrait = height > width
    canvas = InkCanvas(ctx.canvas, "white")
    canvas.draw.rectangle([0, 0, width - 1, ACCENT_HEIGHT - 1], fill=INK["blue"])

    rows = _matrix(wifi_qr_text(ssid, password))
    side = 0
    if rows is not None:
        scale = QR_MAX[portrait] // len(rows)
        side = len(rows) * scale
        if portrait:
            qx, qy = (width - side) // 2, ACCENT_HEIGHT + MARGIN
        else:
            qx, qy = MARGIN + 8, ACCENT_HEIGHT + (height - ACCENT_HEIGHT - side) // 2
        _draw_qr(canvas, rows, qx, qy, scale)

    if portrait:
        x, y = MARGIN, ACCENT_HEIGHT + MARGIN + side + (QR_GAP if side else 0)
    else:
        x, y = (MARGIN + 8 + side + QR_GAP) if side else MARGIN + 8, ACCENT_HEIGHT + 22
    _words(canvas, x, y, width - x - MARGIN, height - BOTTOM, name, ssid, password, result, ctx.config.wifi.help,
           side > 0)
    return canvas.indices()


def _lay_out(x: int, y: int, w: int, sizes: tuple[int, int, int], name: str, ssid: str, password: str,
             result: dict | None, help_line: str, has_qr: bool) -> tuple[list[tuple], int, int]:
    """([(x, y, text, font, ink)], the rule's y, where the words end) at these sizes."""
    head, body, small = font(sizes[0], bold=True), font(sizes[1]), font(sizes[2])
    number = font(sizes[1], bold=True)
    texts: list[tuple] = []

    def block(text: str, fnt, spacing: float = 1.2, gap: int = 0, ink: int = INK["black"], indent: int = 0) -> None:
        nonlocal y
        for line in wrap_text(text, fnt, w - indent):
            texts.append((x + indent, y, line, fnt, ink))
            y += line_height(fnt, spacing)
        y += gap

    if result is not None:
        block("Try again", head, 1.1, 6)
        block(f"Couldn't join {result.get('ssid')}. {result.get('text', '')}", font(sizes[2] + 4, bold=True), gap=10,
              ink=INK["red"])
    else:
        block("Connect to Wi-Fi", head, 1.1, 12)
    first = "Point your phone's camera at this code. Tap Join." if has_qr else f"Join {ssid} on your phone."
    for i, step in enumerate((first, "Pick your home Wi-Fi and type its password.",
                              "Watch this screen. It takes about a minute."), 1):
        texts.append((x, y, str(i), number, INK["blue"]))
        block(step, body, gap=9, indent=30)
    y += 6
    rule = y
    y += 11
    block(f"No camera? Join {ssid}, password {password}, then open {name}.local.", small, gap=5)
    if help_line:
        block(help_line, small, gap=5)
    block("Press a button to cancel. This turns off by itself in 15 minutes.", small)
    return texts, rule, y


def _words(canvas: InkCanvas, x: int, y: int, w: int, bottom: int, *args) -> None:
    """The largest sizes that fit: "Try again" and a long name need more room."""
    for sizes in SIZES:
        texts, rule, end = _lay_out(x, y, w, sizes, *args)
        if end <= bottom:
            break
    canvas.draw.line([x, rule, x + w, rule], fill=INK["black"])
    for tx, ty, text, fnt, ink in texts:
        canvas.draw.text((tx, ty), text, font=fnt, fill=ink)
