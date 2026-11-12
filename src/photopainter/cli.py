"""Command line: photopainter preview | show | next | clear | tick | battery | buttons | press | serve | scene."""

from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from photopainter.render.layout import open_image
from photopainter.render.palette import preview_image
from photopainter.render.pipeline import RenderOptions, render, to_panel_codes

log = logging.getLogger("photopainter")


def _add_render_args(
    p: argparse.ArgumentParser, image_help: str = "image file, or - to read it from stdin", default_image=None
) -> None:
    if default_image is None:
        p.add_argument("image", help=image_help)
    else:
        p.add_argument("image", nargs="?", default=default_image, help=f"{image_help} (default: {default_image})")
    p.add_argument("--fit", choices=["auto", "fill", "fit"], default="auto",
                   help="fill: crop to fill the frame; fit: whole image with white borders; "
                        "auto (default): fill unless that would crop more than about a third")
    p.add_argument("--portrait", action="store_true", help="frame is hung in portrait orientation")
    p.add_argument("--flip", action=argparse.BooleanOptionalAction, default=None,
                   help="rotate 180°, for a panel mounted upside down (show and next default to the "
                        "config's display.flip, which is on)")
    p.add_argument("--palette", choices=["estimated", "nominal"], default="estimated",
                   help="ink colors to dither against (default: estimated real ink colors)")
    p.add_argument("--no-dither", action="store_true", help="map to nearest ink without dithering")
    d = RenderOptions()
    p.add_argument("--saturation", type=float, default=d.saturation,
                   help=f"scale all color before dithering; 1.0 = none (default {d.saturation})")
    p.add_argument("--vibrance", type=float, default=d.vibrance,
                   help=f"boost muted colors more than strong ones; 0 = none (default {d.vibrance})")
    p.add_argument("--contrast", type=float, default=d.contrast,
                   help=f"contrast before dithering; 1.0 = none (default {d.contrast})")
    p.add_argument("--gamma", type=float, default=d.gamma,
                   help=f"brighten midtones before dithering; >1 is brighter (default {d.gamma})")


def _options(args: argparse.Namespace) -> RenderOptions:
    return RenderOptions(
        fit=args.fit,
        portrait=args.portrait,
        flip=bool(args.flip),
        palette=args.palette,
        dither=not args.no_dither,
        saturation=args.saturation,
        contrast=args.contrast,
        gamma=args.gamma,
        vibrance=args.vibrance,
    )


def _panel_options(args: argparse.Namespace) -> RenderOptions:
    """`_options` for sending to the panel: without --flip/--no-flip, the config decides."""
    from dataclasses import replace

    opts = _options(args)
    if args.flip is None:
        from photopainter.config import load_config

        opts = replace(opts, flip=load_config()[0].display.flip)
    return opts


def _load(arg: str):
    if arg == "-":
        return open_image(io.BytesIO(sys.stdin.buffer.read()))
    return open_image(arg)


def _render(args: argparse.Namespace):
    start = time.monotonic()
    indices = render(_load(args.image), _options(args))
    log.info("rendered in %.1f s", time.monotonic() - start)
    return indices


def cmd_preview(args: argparse.Namespace) -> int:
    indices = _render(args)
    if args.output:
        out = Path(args.output)
    elif args.image == "-":
        out = Path("previews/stdin.png")
    else:
        out = Path("previews") / f"{Path(args.image).stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    preview_image(indices).save(out)
    print(out)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from photopainter.hardware.panel import PanelDisplay

    opts = _panel_options(args)
    codes = to_panel_codes(_render(args), opts)
    PanelDisplay().show(codes)
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    from photopainter.hardware.panel import STATE_DIR, PanelDisplay
    from photopainter.slideshow import SlideshowState, list_images, rotation_order

    folder = Path(args.image)
    images = list_images(folder)
    if not images:
        log.error("no images in %s", folder)
        return 1
    state = SlideshowState(STATE_DIR / "slideshow.json")
    opts = _panel_options(args)
    for path in rotation_order(images, state.last_shown(folder)):
        try:
            start = time.monotonic()
            codes = to_panel_codes(render(open_image(path), opts), opts)
            log.info("rendered %s in %.1f s", path.name, time.monotonic() - start)
        except Exception as exc:  # noqa: BLE001 - skip unreadable files, keep the slideshow going
            log.warning("skipping %s: %s", path.name, exc)
            continue
        PanelDisplay().show(codes)
        state.record(folder, path.name)
        log.info("showing %s", path.name)
        return 0
    log.error("none of the %d images in %s could be read", len(images), folder)
    return 1


def cmd_clear(args: argparse.Namespace) -> int:
    from photopainter.hardware.panel import PanelDisplay

    PanelDisplay().clear()
    return 0


def _not_built(module: str, task: str, exc: ModuleNotFoundError) -> bool:
    """True if `exc` is `module` itself missing (a planned module not written yet)."""
    if exc.name in (module, module.rsplit(".", 1)[0]) and exc.name != "photopainter":
        log.error("%s isn't built yet (PRD.md task %s)", module, task)
        return True
    return False


def cmd_tick(args: argparse.Namespace) -> int:
    # photopainter.tick.main(*, fake_panel: bool, at: datetime | None = None) -> int
    try:
        from photopainter.tick import main as tick_main
    except ModuleNotFoundError as exc:
        if _not_built("photopainter.tick", "C2", exc):
            return 2
        raise
    kwargs: dict = {"fake_panel": args.fake_panel}
    if args.at is not None:  # simulate a moment (only passed when asked for)
        from photopainter.config import load_config

        kwargs["at"] = _parse_at(args.at, load_config()[0].location.tz)
    return tick_main(**kwargs)


def cmd_battery(args: argparse.Namespace) -> int:
    from photopainter import state
    from photopainter.hardware.battery import Battery, FakeBattery

    battery = FakeBattery(state.state_dir()) if args.fake_panel else Battery()
    while True:
        print(f"{time.strftime('%H:%M:%S')}  {battery.read()}", flush=True)
        if args.watch is None:
            return 0
        time.sleep(args.watch)


def cmd_buttons(args: argparse.Namespace) -> int:
    from photopainter.buttons import main as buttons_main

    return buttons_main()


def cmd_press(args: argparse.Namespace) -> int:
    from photopainter.buttons import press
    from photopainter.config import load_config

    at = None if args.at is None else _parse_at(args.at, load_config()[0].location.tz)
    return press(args.action.replace("-", "_"), fake_panel=args.fake_panel, at=at)


def cmd_serve(args: argparse.Namespace) -> int:
    # Contract for task W1: photopainter.web.server.main(*, host: str, port: int, fake_panel: bool) -> int
    try:
        from photopainter.web.server import main as serve_main
    except ModuleNotFoundError as exc:
        if _not_built("photopainter.web.server", "W1", exc):
            return 2
        raise
    return serve_main(host=args.host, port=args.port, fake_panel=args.fake_panel)


def _parse_at(text: str | None, tz) -> datetime:
    """--at: ISO 8601; without a UTC offset it's local time at the configured location."""
    if text is None:
        return datetime.now(tz).replace(microsecond=0)
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"--at {text!r}: expected ISO 8601, e.g. 2026-11-03T21:00 or "
                         f"2026-11-03T21:00:00-08:00") from None
    return at.replace(tzinfo=tz) if at.tzinfo is None else at.astimezone(tz)


def _scene_at(text: str | None, config) -> datetime:
    """scene --at: what `_parse_at` takes, or a named moment of a day, e.g. 2026-11-20@sunset."""
    loc = config.location
    if text is None or "@" not in text:
        return _parse_at(text, loc.tz)
    from photopainter.astro.moments import moment_at

    day, _, name = text.partition("@")
    try:
        on = date.fromisoformat(day)
    except ValueError:
        raise ValueError(f"--at {text!r}: expected a date before the @, e.g. 2026-11-20@sunset") from None
    return moment_at(on, name, loc.lat, loc.lon, loc.tz, config.sky.center_azimuth_deg)


def cmd_scene(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from photopainter.config import load_config
    from photopainter.hardware.battery import Reading
    from photopainter.render import battery_badge
    from photopainter.scenes import Scene, SceneContext, render_scene

    config, error = load_config(Path(args.config) if args.config else None)
    if error:
        log.warning("using default settings: %s", error)
    if args.portrait:
        config = replace(config, display=replace(config.display, portrait=True))
    if args.kind == "message" and args.message is None:
        raise ValueError("scene message needs --message TEXT")
    if args.kind != "sea" and (args.waves or args.clouds):
        raise ValueError("--waves and --clouds are for the sea screen")
    at = _scene_at(args.at, config)
    scene = Scene(kind=args.kind, key=f"{args.kind}/{at:%Y-%m-%dT%H:%M}", draw_for=at,
                  photo=args.photo, message=args.message, waves=args.waves, clouds=args.clouds)
    start = time.monotonic()
    indices = render_scene(scene, replace(SceneContext.from_config(config), any_age=args.forecast))
    log.info("rendered %s for %s in %.1f s", args.kind, at.isoformat(timespec="minutes"),
             time.monotonic() - start)
    if args.battery_volts is not None:
        indices = battery_badge.draw(indices, Reading(args.battery_volts, -200.0).percent, args.battery_corner)
    if args.low_power_mark:
        indices = battery_badge.draw_low_power(indices, args.battery_corner)
    if args.no_wifi_mark:
        indices = battery_badge.draw_no_wifi(indices, args.battery_corner, battery_up=args.battery_volts is not None)
    out = Path(args.output) if args.output else Path("previews") / f"{args.kind}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    preview_image(indices).save(out)
    print(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photopainter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preview", help="save a PNG of how an image will look on the panel")
    _add_render_args(p)
    p.add_argument("-o", "--output", help="output PNG (default: previews/<name>.png)")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("show", help="show an image on the panel (run on the Pi)")
    _add_render_args(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("next", help="show the next photo from a folder, in name order (run on the Pi)")
    _add_render_args(p, image_help="folder of photos", default_image=str(Path.home() / "photos"))
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("clear", help="clear the panel to white (run on the Pi)")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("tick", help="decide what to show now and refresh the panel if it changed "
                                    "(run by the timer and the dashboard)")
    p.add_argument("--fake-panel", action="store_true", help="don't touch the panel (Mac, tests)")
    p.add_argument("--at", metavar="ISO",
                   help="simulate this moment, e.g. 2026-11-04T16:57 (local time if no offset): "
                        "uses the caches as they are (no fetching)")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("battery", help="read the battery: volts, current and state (run on the Pi)")
    p.add_argument("--watch", type=float, metavar="SECONDS", help="keep reading, this often")
    p.add_argument("--fake-panel", action="store_true", help="read fake_battery.json in the state dir instead")
    p.set_defaults(func=cmd_battery)

    p = sub.add_parser("buttons", help="watch the buttons on the back of the frame (run on the Pi, "
                                       "by photopainter-buttons.service)")
    p.set_defaults(func=cmd_buttons)

    p = sub.add_parser("press", help="do what a button does: forward, back, photos only on or off, low power on or off, Wi-Fi setup")
    p.add_argument("action", choices=["next", "back", "photos", "low-power", "wifi-setup"])
    p.add_argument("--fake-panel", action="store_true", help="don't touch the panel (Mac, tests)")
    p.add_argument("--at", metavar="ISO", help="simulate this moment, like tick --at")
    p.set_defaults(func=cmd_press)

    p = sub.add_parser("serve", help="run the web dashboard")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=80, help="port (default 80)")
    p.add_argument("--fake-panel", action="store_true", help="don't touch the panel (Mac, tests)")
    p.set_defaults(func=cmd_serve)

    from photopainter.render.battery_badge import CORNERS, DEFAULT_CORNER
    from photopainter.scenes.base import CLOUD_LOOKS, SCENE_KINDS, WAVE_LOOKS

    p = sub.add_parser("scene", help="render a screen to a PNG without the panel (Mac or Pi)")
    p.add_argument("kind", choices=SCENE_KINDS)
    p.add_argument("--at", metavar="ISO",
                   help="moment to draw, e.g. 2026-11-03T21:00 (local time if no offset; default now), "
                        "or a named moment of a day, e.g. 2026-11-20@sunset (dawn, sunrise, sunset, dusk, moon)")
    p.add_argument("--forecast", action="store_true",
                   help="use the caches' forecast for --at however long after the fetch it is, "
                        "as the dashboard's previews do (without this, over 12 h leaves it out)")
    p.add_argument("--waves", choices=WAVE_LOOKS, help="sea scene: this look instead of the forecast's swell and wind")
    p.add_argument("--clouds", choices=CLOUD_LOOKS, help="sea scene: this look instead of the forecast's sky")
    p.add_argument("--photo", metavar="NAME", help="photo scene: file name in the photo folder")
    p.add_argument("--message", metavar="TEXT",
                   help="message scene: first line is the heading, further lines the body")
    p.add_argument("--portrait", action="store_true", help="draw for a portrait frame (480x800)")
    p.add_argument("--battery-volts", type=float, metavar="V",
                   help="draw the battery indicator as it would look on the battery at this voltage")
    p.add_argument("--battery-corner", choices=CORNERS, default=DEFAULT_CORNER,
                   help="where --battery-volts puts it (default: %(default)s)")
    p.add_argument("--low-power-mark", action="store_true", help="draw the mark that low power is on")
    p.add_argument("--no-wifi-mark", action="store_true", help="draw the mark that the frame has no Wi-Fi")
    p.add_argument("--config", metavar="FILE",
                   help="draw with these settings instead of the config file (the dashboard's "
                        "previews of unsaved settings)")
    p.add_argument("-o", "--output", help="output PNG (default: previews/<kind>.png)")
    p.set_defaults(func=cmd_scene)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - one clear line for the user; -v for the traceback
        if args.verbose:
            raise
        log.error("%s", exc)
        return 1
