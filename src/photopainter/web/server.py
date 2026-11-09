"""The dashboard's web server: `photopainter serve` (PRD.md §5 "HTTP API", tasks W1/W2).

A Flask app with a JSON API and a static single page (`web/static/`). It never drives the
panel (PRD.md §4): an action writes `overrides.json` or `config.json`, then starts
`photopainter tick` in the background (`jobs.TickRunner`), which decides, draws and
refreshes exactly as the timer's ticks do. Previews are drawn by `photopainter scene` in a
child process, one at a time.

| Method | Path | Does |
|---|---|---|
| GET | `/` | The dashboard page |
| GET | `/api/status` | `status.json` as the tick wrote it (its `battery` is the tick's last reading), plus: `server_time`, `timezone`, `location`, `mode` (the pin in effect, or Auto), `overrides`, `day_night` (`is_day`, `next_at`, `next`: "sunset"/"sunrise"), `ticks` (`running`, `queued`), `image` (`version`, `width`, `height` of current.png), `config_error`, `screens` (which kinds can be drawn here), `screens_why` (why one can't: {kind: text}, e.g. no harbor chart for this place), `fake_panel`, `health` (`health.system`; among it `network`: [{`interface`, `ipv4`, `ipv6`}] for each interface with an address, and `tailscale`: {`state`: connected, stopped, logged_out or not_installed, `ips`, `dns_name`, `online`}, each null on the Mac), `sources` (`health.sources`), `problems` ([{`level`, `text`}]), `upcoming` ([{`at`, `kind`, `photo`, `minutes`, `why`}]: the next few changes the schedule makes on its own; `minutes` is how long until the change after it (null if none); `why` is sunrise, sunset, pin_ends, photo, photo_ends, redraw (the same screen drawn afresh) or schedule) |
| GET | `/api/battery` | The last run on the battery from `battery.csv` (`cycle`: `started_at`, `ended_at`, `ongoing`, `used_mah`, `readings` [{`t`, `volts`, `milliamps`, `state`}], thinned to about 400), the time-left guess for an ongoing one (`estimate`: `avg_ma`, `percent`, `left_mah`, `hours_left`, `runs_out_at`, `hours_to_low`, `low_at`), the last five days for the chart (`history`: `from`, `to`, `step_s`, `readings` [{`t`, `volts`, `milliamps`, `state`}] averaged into 15-minute buckets, `spans` [{`state`, `start`, `end`}] of each state), `capacity_mah`, `indicator_volts`, `low_volts` and the tick's `power` state (`power.py`) |
| GET | `/api/current.png` | The image on the wall (404 before the first refresh) |
| GET | `/api/incoming.png` | The picture a refresh under way is bringing in (404 when none is; `refreshing.panel_at` in the status says one is) |
| GET | `/api/config` | The config in effect, as JSON (the defaults if the file is bad: see `config_error`) |
| PUT | `/api/config` | Replace it: a whole config, checked by `config.validate`; 400 `{error, errors}` if bad |
| POST | `/api/mode` | `{"kind": "auto"/"tides"/"sky"/"sea"/"photo"/"card"/"harbor"/"combo", "until": null/"next_change"/"1h"/ISO}` (until defaults to "next_change"; "card" is the color test card) |
| GET | `/api/photos` | `[{name, size, hidden, up_next_position, showing, pinned, next, modified, preview_url}]` in the rotation's order; `up_next_position` is 1 for the first in the queue, null if not queued; `next` marks the photo the next photo step will show |
| GET | `/api/photos/<name>/preview.png` | The photo dithered as the panel will show it (cached in `photo-previews/`) |
| POST | `/api/photos` | Upload: multipart field `photo` (one or more files), each going at the end of the order. 201 `{saved, errors}` |
| PUT | `/api/photos/order` | Set the rotation's order: `{"order": [names]}` gives `{"order": [every photo's name, in the order now]}`; 400 if it isn't a list of photo names |
| POST | `/api/photos/<name>/show-now` | Pin this photo for `photos.show_now_minutes` |
| POST / DELETE | `/api/photos/<name>/up-next` | Add to the end of the Up next queue / take it off |
| POST | `/api/photos/<name>/hide` | Hide or unhide: body `{"hidden": bool}`, or no body to toggle. Hiding also takes it off Up next |
| DELETE | `/api/photos/<name>` | Move it to `<photo folder>/.trash/` (and off Up next, the hidden list and a Show now pin) |
| GET | `/api/preview.png?kind=tides&at=ISO` | Draw a screen without touching the panel (`photo=NAME`, `message=TEXT` for those kinds). `at` is a time or a named moment of a day (`2026-11-20@sunset`; `astro/moments.py`), and uses the forecast for that time however old the fetch. For `kind=sea`, `waves` and `clouds` draw a look instead of the forecast's (`scenes/base.py`) |
| POST | `/api/preview.png` | The same from JSON `{kind, at, photo, message, waves, clouds, config}`: `config` is a whole config, checked like PUT `/api/config` but never saved, so a settings change can be seen before saving |
| POST | `/api/name` | Rename the frame on the network, at once (`frame_name.py`): `{"name": "InkFrame"}` gives `{name, previous, warnings, message}`; 400 if the name can't be used. `health.hostname` in the status is the name now |
| GET | `/api/wifi` | The saved Wi-Fi networks (`wifi.py`): `{networks: [{name, ssid, connected, hidden, uuid}], connected}` (`connected` is the name of the one the frame is on, or null). Never a secret |
| POST | `/api/wifi` | Save a network for the frame to join when it's in range (it never switches now): `{"ssid", "password", "hidden"}`, `password` empty or missing for an open network. 201 `{network, networks, connected, message}`; 400 if the name or password can't be used or the network is already saved |
| DELETE | `/api/wifi/<name>` | Forget a saved network: `{name, networks, connected, message}`; 404 if none is called that, 409 for the one the frame is on |
| GET | `/setup` | The page a phone gets on the setup hotspot (`hotspot.py`, `static/setup.html`). While the hotspot is up, anything else asked for from its network redirects here, which is what makes a phone open it by itself |
| GET | `/api/setup` | What that page needs: `{on, ssid, networks: [{ssid, signal, secure}], result, joining}`. Asking keeps the hotspot up for `hotspot.IDLE_MINUTES` more. Never the hotspot's password |
| POST | `/api/setup/join` | Try a network: `{"ssid", "password", "hidden"}`. 202 and a tick takes the hotspot down to try it; 400 if the name or password can't be used; 409 if the hotspot isn't up or a try is under way |
| POST | `/api/redraw` | Draw what's on the wall again (after a settings change): the next tick refreshes although the screen hasn't changed |

Errors are JSON: `{"error": "<readable message>"}` with a 4xx/5xx status. Times are ISO
8601 with a UTC offset. A tick starts after: mode, Show now, settings saved, redraw, upload
and delete (Up next and Hide don't change what's on the wall now).

No authentication (v1): the server is for the Viewer's LAN and the Owner's tailnet only.
Never port-forward it or expose it with Tailscale Funnel without adding a login first.
"""

from __future__ import annotations

import io
import logging
import signal
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from flask import Flask, Response, redirect, request, send_file
from werkzeug.exceptions import HTTPException

from photopainter import hotspot, power, screens, state
from photopainter.astro.moments import MOMENTS
from photopainter.config import Config, ConfigError, edit_config, load_config, save_config, validate
from photopainter.scenes.base import CLOUD_LOOKS, SCENE_KINDS, WAVE_LOOKS
from photopainter.schedule import (
    PIN_KINDS, Overrides, Pin, SunTimes, decide, next_day_night_change, resolve_until,
    show_now_until, upcoming_change,
)
from photopainter.slideshow import SlideshowState, rotation_order
from photopainter.web import frame_name, health, photos, wifi
from photopainter.web.jobs import Renderer, RenderError, TickRunner, photopainter_command

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
EMPTY_OVERRIDES: dict[str, Any] = {"pin": None, "up_next": [], "redraw_at": None}
STATUS_KEYS = ("updated_at", "showing", "refreshing", "next_change", "last_error", "battery", "power",
               "data")
MODES = ("auto", *PIN_KINDS)
RECENT_ERROR = timedelta(minutes=30)  # status.json last_error counts as a problem this long
TICKS_LATE = timedelta(minutes=5)  # no tick for this long: the timer isn't running
UPCOMING = 4  # how many coming changes /api/status lists
STOP_WAIT_S = 240  # on SIGTERM, how long a running tick gets to finish its panel refresh


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


class Dashboard:
    """What the routes share: the clock, ticks, the renderer and helpers."""

    def __init__(self, *, fake_panel: bool, clock: Callable[[], datetime] | None,
                 ticks: TickRunner | None, render: Callable[[list[str], Path], None] | None,
                 sun: SunTimes | None):
        self.fake_panel = fake_panel
        self.fake_hostname: str | None = None  # --fake-panel renames this, never the Mac
        self.wifi = wifi.FakeNetworks() if fake_panel else wifi  # a list in memory on the Mac; nmcli on the Pi
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ticks = ticks or TickRunner(photopainter_command("tick", *(["--fake-panel"] if fake_panel else [])))
        self.render = render or Renderer()
        self._sun = sun
        self._sun_lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._image_info: tuple[tuple[int, int], dict] | None = None

    # --- basics ------------------------------------------------------------------------

    @property
    def dir(self) -> Path:
        return state.state_dir()

    def config(self) -> tuple[Config, str | None]:
        return load_config()

    def now(self, config: Config) -> datetime:
        return self.clock().astimezone(config.location.tz).replace(microsecond=0)

    def sun(self, config: Config) -> SunTimes:
        """Sun times by local date: the tick's `sun.json` cache, Skyfield on a miss."""
        if self._sun is not None:
            return self._sun
        from photopainter.tick import _sun_times

        inner = _sun_times(config, self.dir)

        def sun(day):
            with self._sun_lock:  # one Skyfield load at a time
                return inner(day)

        return sun

    def overrides(self, now: datetime) -> Overrides:
        return Overrides.from_dict(state.read_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES)) \
            .without_expired(now)

    def edit_overrides(self, now: datetime, change: Callable[[Overrides], Overrides]) -> Overrides:
        with state.update_json(self.dir / state.OVERRIDES, EMPTY_OVERRIDES) as data:
            new = change(Overrides.from_dict(data).without_expired(now))
            data.clear()
            data.update(new.to_dict())
        return new

    def edit_config(self, change: Callable[[Config], Config]) -> Config:
        """Read-modify-write config.json under its lock (Hide, Delete)."""
        try:
            return edit_config(change)
        except ConfigError as exc:
            raise ApiError(f"The settings file has a problem, so it wasn't changed. Fix it under "
                           f"Settings first. ({exc})", 409) from exc

    def status(self) -> dict:
        data = state.read_json(self.dir / state.STATUS, {})
        data = data if isinstance(data, dict) else {}
        return {**{k: None for k in STATUS_KEYS}, **data}

    # --- pieces of /api/status ---------------------------------------------------------

    def image(self) -> dict | None:
        path = self.dir / state.CURRENT_PNG
        try:
            st = path.stat()
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        if self._image_info is None or self._image_info[0] != sig:
            from PIL import Image

            try:
                with Image.open(path) as img:
                    width, height = img.size
            except Exception:  # noqa: BLE001 - half-written or broken: report no size
                width = height = None
            self._image_info = (sig, {"version": f"{st.st_mtime_ns:x}", "width": width, "height": height})
        return self._image_info[1]

    def day_night(self, now: datetime, config: Config) -> dict | None:
        try:
            sun = self.sun(config)
            events = sun(now.date())
            off = timedelta(minutes=config.schedule.night_offset_minutes)
            is_day = events.sunrise - off <= now < events.sunset + off
            return {"is_day": is_day, "next_at": next_day_night_change(now, config, sun),
                    "next": "sunset" if is_day else "sunrise",
                    "sunrise": events.sunrise.replace(microsecond=0),
                    "sunset": events.sunset.replace(microsecond=0)}
        except Exception:  # noqa: BLE001 - e.g. no ephemeris: the page manages without
            log.warning("couldn't work out sunrise and sunset", exc_info=True)
            return None


def mode_of(overrides: Overrides, now: datetime) -> dict:
    pin = overrides.active_pin(now)
    if pin is None:
        return {"kind": "auto", "photo": None, "until": None, "set_at": None}
    return {"kind": pin.kind, "photo": pin.photo, "until": pin.until, "set_at": pin.set_at}


def problems(status: dict, config_error: str | None, sys_health: dict, sources: dict,
             now: datetime, ticks: TickRunner, fake_panel: bool) -> list[dict]:
    """What the Owner should know about, worst first: [{"level", "text"}]."""
    out: list[dict] = []
    if config_error:
        out.append({"level": "error", "text": f"The settings file has a problem, so the frame uses the "
                                              f"defaults: {config_error}"})
    if ticks.last_error:
        out.append({"level": "error", "text": ticks.last_error})
    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    if data.get("clock_ok") is False or (sys_health.get("clock") or {}).get("synced") is False:
        out.append({"level": "error", "text": "The clock isn't set yet (it needs internet time)"})
    power = sys_health.get("power") or {}
    if power.get("undervoltage_now"):
        out.append({"level": "error", "text": "Under-voltage now: the USB power supply is too weak"})
    elif power.get("undervoltage_since_boot"):
        out.append({"level": "warn", "text": "Under-voltage since boot: the USB power supply may be too weak"})
    if (sys_health.get("tailscale") or {}).get("state") in ("stopped", "logged_out"):
        out.append({"level": "warn", "text": "Tailscale is off: the dashboard is only reachable on the home Wi-Fi"})
    battery = status.get("battery") if isinstance(status.get("battery"), dict) else {}
    if battery.get("state") == "discharging":
        since = _when(battery.get("on_battery_since"))
        for_ = "" if since is None else f" for {int((now - since).total_seconds() // 60)} minutes"
        if battery.get("low"):
            out.append({"level": "error", "text": f"Battery low: the frame has run on its battery{for_}. "
                                                  "It has settled on a photo and stays there until the power is back; "
                                                  "that photo stays on the wall when the battery runs out"})
        else:
            out.append({"level": "warn", "text": f"Running on the battery{for_}: is the power out?"})
    updated = _when(status.get("updated_at"))
    if not fake_panel:
        if updated is None:
            out.append({"level": "warn", "text": "No tick has run yet"})
        elif now - updated > TICKS_LATE:
            out.append({"level": "error", "text": "The schedule hasn't run for "
                                                  f"{int((now - updated).total_seconds() // 60)} minutes "
                                                  "(is photopainter-tick.timer on?)"})
    for name, src in sources.items():
        if src["level"] in ("error", "warn"):
            out.append({"level": src["level"], "text": f"{name.capitalize()}: {src['note']}"})
    err = status.get("last_error") if isinstance(status.get("last_error"), dict) else None
    at = _when(err.get("at")) if err else None
    if err and at and now - at < RECENT_ERROR and not str(err.get("where", "")).endswith("-fetch"):
        out.append({"level": "warn", "text": f"{err.get('where')}: {err.get('message')}"})
    out.sort(key=lambda p: p["level"] != "error")
    return out


def _when(value: Any) -> datetime | None:
    try:
        return state.from_iso(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _json(data: Any, status: int = 200) -> Response:
    return Response(state.dumps(data), status=status, mimetype="application/json")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("expected a JSON object in the request body")
    return data


def _hhmm(dt: datetime) -> str:
    return f"{dt:%I:%M %p}".lstrip("0").lower()


def create_app(*, fake_panel: bool = False, clock: Callable[[], datetime] | None = None,
               ticks: TickRunner | None = None, render: Callable[[list[str], Path], None] | None = None,
               sun: SunTimes | None = None) -> Flask:
    """The dashboard app. Tests pass a fixed `clock`, fake `ticks`, `render` and `sun`."""
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = photos.MAX_UPLOAD_BYTES + 1024 * 1024  # + form overhead
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # revalidate (ETag) so a deploy shows at once
    dash = Dashboard(fake_panel=fake_panel, clock=clock, ticks=ticks, render=render, sun=sun)
    app.extensions["photopainter"] = dash

    # --- errors --------------------------------------------------------------------------

    @app.errorhandler(ApiError)
    def api_error(exc: ApiError):
        return _json({"error": exc.message, **exc.extra}, exc.status)

    @app.errorhandler(photos.PhotoError)
    def photo_error(exc: photos.PhotoError):
        return _json({"error": exc.message}, exc.status)

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if not request.path.startswith("/api/"):
            return exc
        message = exc.description or exc.name
        if exc.code == 413:
            message = f"That's over {photos.MAX_UPLOAD_BYTES // 2**20} MB"
        return _json({"error": message}, exc.code or 500)

    @app.errorhandler(Exception)
    def unexpected(exc: Exception):
        log.exception("%s %s failed", request.method, request.path)
        return _json({"error": f"Something went wrong on the frame: {type(exc).__name__}: {exc}"}, 500)

    # --- page ----------------------------------------------------------------------------

    @app.before_request
    def captive_portal():
        """A phone on the setup hotspot gets the setup page, whatever it asked for and of whatever
        name (`deploy/network/photopainter-captive.conf` sends every name here). Its "am I online?"
        check gets the redirect too, so it opens the page by itself. Nobody else is affected."""
        address = (request.remote_addr or "").removeprefix("::ffff:")
        if not address.startswith(hotspot.SUBNET_PREFIX) or request.path == "/setup" \
                or request.path.startswith("/api/setup"):
            return None
        if not hotspot.is_on(dash.dir):
            return None
        log.info("hotspot: %s asked for %s%s, sent to the setup page", address, request.host, request.path)
        response = redirect(hotspot.SETUP_URL, 302)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/setup")
    def setup_page():
        log.info("hotspot: setup page opened by %s", request.remote_addr)
        return app.send_static_file("setup.html")

    @app.get("/api/setup")
    def api_setup():
        hotspot.touch(dash.dir, dash.clock())
        return _json(hotspot.for_page(hotspot.load(dash.dir)))

    @app.post("/api/setup/join")
    def api_setup_join():
        """Save the network as a trial and leave the trying to a tick, since it takes the hotspot
        down. The password goes from the request straight to the keyfile (`wifi.py`)."""
        data = hotspot.load(dash.dir)
        if data["state"] != "on":
            raise ApiError("The frame's setup network has ended. Hold its back button to start it again", 409)
        if data["join"] is not None:
            raise ApiError("The frame is already trying a network", 409)
        body = _body()
        try:
            uuid = dash.wifi.add_trial(body.get("ssid"), body.get("password"), body.get("hidden", False))
        except wifi.BadNetwork as exc:
            raise ApiError(str(exc), exc.status) from None
        except wifi.WifiFailed as exc:
            raise ApiError(str(exc), 500) from None
        ssid = wifi.check_ssid(body.get("ssid"))
        hotspot.ask_join(dash.dir, ssid, uuid, dash.clock())
        log.info("hotspot: %s asked to try %s", request.remote_addr, ssid)
        dash.ticks.request()
        return _json({"ssid": ssid, "message": "Now look at the frame"}, 202)

    # --- status --------------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        config, config_error = dash.config()
        now = dash.now(config)
        status = dash.status()
        overrides = dash.overrides(now)
        sys_health = health.system(config.photos.path)
        if dash.fake_hostname:
            sys_health["hostname"] = dash.fake_hostname
        srcs = health.sources(dash.dir, config, now)
        gone = screens.missing(config)
        body = {
            **status,
            "server_time": now,
            "timezone": config.location.timezone,
            "location": config.location.name,
            "mode": mode_of(overrides, now),
            "overrides": overrides.to_dict(),
            "day_night": dash.day_night(now, config),
            "upcoming": upcoming_changes(dash, config, now, overrides),
            "ticks": dash.ticks.snapshot(),
            "image": dash.image(),
            "config_error": config_error,
            "screens": {k: find_spec(f"photopainter.scenes.{k}") is not None and k not in gone for k in SCENE_KINDS},
            "screens_why": gone,
            "fake_panel": dash.fake_panel,
            "health": sys_health,
            "sources": srcs,
            "problems": problems(status, config_error, sys_health, srcs, now, dash.ticks, dash.fake_panel),
        }
        return _json(body)

    @app.get("/api/battery")
    def api_battery():
        config, _ = dash.config()
        return _json(power.summary(dash.dir, config, dash.now(config), dash.status().get("power")))

    @app.get("/api/current.png")
    def api_current():
        path = dash.dir / state.CURRENT_PNG
        if not path.is_file():
            raise ApiError("Nothing has been drawn yet", 404)
        return send_file(io.BytesIO(path.read_bytes()), mimetype="image/png", max_age=0,
                         etag=dash.image()["version"] if dash.image() else True,
                         last_modified=path.stat().st_mtime, conditional=True)

    @app.get("/api/incoming.png")
    def api_incoming():
        try:  # the tick takes it away when the refresh ends
            data = (dash.dir / state.INCOMING_PNG).read_bytes()
        except OSError:
            raise ApiError("No refresh is under way", 404) from None
        return send_file(io.BytesIO(data), mimetype="image/png", max_age=0)

    # --- config --------------------------------------------------------------------------

    @app.get("/api/config")
    def api_config():
        return _json(dash.config()[0].to_dict())

    @app.put("/api/config")
    def api_put_config():
        try:
            config = validate(_body())
        except ConfigError as exc:
            raise ApiError(str(exc), 400, errors=exc.errors) from None
        save_config(config)
        dash.ticks.request()
        return _json({"config": config.to_dict(), "message": "Settings saved"})

    @app.post("/api/redraw")
    def api_redraw():
        """Draw the wall again with today's settings: the tick redraws the screen it's showing
        once `redraw_at` is newer than that screen (schedule.Overrides, tick.py)."""
        config = dash.config()[0]
        now = dash.now(config)
        dash.edit_overrides(now, lambda o: replace(o, redraw_at=now))
        dash.ticks.request()
        return _json({"message": "Drawing the wall again"})

    @app.post("/api/name")
    def api_name():
        """Rename the frame. The page moves itself to the new address afterwards."""
        try:
            name = frame_name.check(_body().get("name"))
        except frame_name.BadName as exc:
            raise ApiError(str(exc)) from exc
        previous = dash.fake_hostname or socket.gethostname()
        warnings: list[str] = []
        if dash.fake_panel:
            dash.fake_hostname = name
        else:
            try:
                warnings = frame_name.set_name(name)
            except frame_name.RenameFailed as exc:
                raise ApiError(str(exc), 500) from exc
        return _json({"name": name, "previous": previous, "warnings": warnings,
                      "message": f"The frame is now {name}"})

    # --- Wi-Fi networks ------------------------------------------------------------------

    def wifi_list() -> dict:
        try:
            nets = dash.wifi.saved()
        except wifi.WifiFailed as exc:
            raise ApiError(str(exc), 500) from None
        return {"networks": [n.to_dict() for n in nets],
                "connected": next((n.name for n in nets if n.connected), None)}

    @app.get("/api/wifi")
    def api_wifi():
        return _json(wifi_list())

    @app.post("/api/wifi")
    def api_wifi_add():
        """Save a network for the frame to join when it's in range. The password goes from
        the request straight to the keyfile (`wifi.py`): never kept, logged or echoed."""
        body = _body()
        try:
            net = dash.wifi.add(body.get("ssid"), body.get("password"), body.get("hidden", False))
        except wifi.BadNetwork as exc:
            raise ApiError(str(exc), exc.status) from None
        except wifi.WifiFailed as exc:
            raise ApiError(str(exc), 500) from None
        return _json({"network": net.to_dict(), **wifi_list(),
                      "message": f"Saved. The frame will join {net.ssid} when it's in range"}, 201)

    @app.delete("/api/wifi/<path:name>")
    def api_wifi_remove(name: str):
        try:
            net = dash.wifi.remove(name)
        except wifi.BadNetwork as exc:
            raise ApiError(str(exc), exc.status) from None
        except wifi.WifiFailed as exc:
            raise ApiError(str(exc), 500) from None
        return _json({"name": name, **wifi_list(), "message": f"The frame forgot {net.ssid}"})

    # --- mode ----------------------------------------------------------------------------

    @app.post("/api/mode")
    def api_mode():
        body = _body()
        kind = body.get("kind")
        if kind not in MODES:
            raise ApiError(f"kind must be one of {', '.join(MODES)} (got {kind!r})")
        config = dash.config()[0]
        now = dash.now(config)
        pin = None
        gone = screens.missing(config)
        if kind in gone:
            raise ApiError(f"{gone[kind]}.")
        if kind != "auto":
            try:
                until = resolve_until(body.get("until", "next_change"), now, config, dash.sun(config))
            except ValueError as exc:
                raise ApiError(str(exc)) from None
            pin = Pin(kind, until=until, photo=None, set_at=now)
        overrides = dash.edit_overrides(now, lambda o: replace(o, pin=pin, cycle=None if pin is None else o.cycle))
        dash.ticks.request()
        if pin is None:
            message = "Back to the schedule"
        else:
            label = {"tides": "Day screen", "sky": "Sky", "sea": "Sea", "photo": "Photos",
                     "card": "Color test card", "harbor": "Harbor chart", "combo": "Combo"}[kind]
            message = f"{label} until {_hhmm(pin.until)}" if pin.until else f"{label} until you change it"
        return _json({"mode": mode_of(overrides, now), "message": message})

    # --- photos --------------------------------------------------------------------------

    @app.get("/api/photos")
    def api_photos():
        config = dash.config()[0]
        now = dash.now(config)
        folder = config.photos.path
        files = photos.listing(folder)
        hidden = set(config.photos.hidden)
        overrides = dash.overrides(now)
        pin = overrides.active_pin(now)
        showing = dash.status().get("showing") or {}
        queue = list(overrides.up_next)
        coming = sum(photo_queue(dash, config, overrides, files), [])
        upcoming = coming[0] if coming else None
        out = []
        for path in files:
            st = path.stat()
            preview = photos.preview_file(dash.dir, path, config)
            out.append({
                "name": path.name,
                "size": st.st_size,
                "hidden": path.name in hidden,
                "up_next_position": queue.index(path.name) + 1 if path.name in queue else None,
                "showing": showing.get("kind") == "photo" and showing.get("photo") == path.name,
                "pinned": pin is not None and pin.photo == path.name,
                "next": path.name == upcoming,
                "modified": datetime.fromtimestamp(st.st_mtime, config.location.tz).replace(microsecond=0),
                "preview_url": f"/api/photos/{_quote(path.name)}/preview.png?v={preview.stem.split('-')[1]}",
            })
        return _json(out)

    @app.get("/api/photos/<name>/preview.png")
    def api_photo_preview(name: str):
        config = dash.config()[0]
        path = photos.find(config.photos.path, name)
        out = photos.preview_file(dash.dir, path, config)
        if not out.is_file():
            with dash._preview_lock:
                if not out.is_file():
                    try:
                        dash.render(["photo", "--photo", name], out)
                    except RenderError as exc:
                        raise ApiError(f"{name} couldn't be drawn: {exc}", 422) from None
                    photos.drop_previews(dash.dir, name, keep=out)
        current = request.args.get("v") == out.stem.split("-")[1]
        response = send_file(out, mimetype="image/png", conditional=True, etag=out.stem,
                             max_age=31536000 if current else 0)
        if current:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.post("/api/photos")
    def api_upload():
        files = request.files.getlist("photo") + request.files.getlist("photos")
        if not files:
            raise ApiError("No photo in the upload (send it as the form field \"photo\")")
        config = dash.config()[0]
        saved, errors = [], []
        for f in files:
            data = f.read(photos.MAX_UPLOAD_BYTES + 1)
            try:
                saved.append(photos.save_upload(config.photos.path, f.filename, data))
            except photos.PhotoError as exc:
                errors.append({"file": f.filename, "error": exc.message, "status": exc.status})
        new = [s for s in saved if not s["duplicate"]]
        if new:
            dash.ticks.request()  # e.g. the first photo in an empty folder makes photo steps count
            threading.Thread(target=_warm_previews, args=(dash, [s["name"] for s in new]),
                             name="preview-warm", daemon=True).start()
        body: dict[str, Any] = {"saved": saved, "errors": errors}
        if errors:
            body["error"] = "; ".join(f"{e['file']}: {e['error']}" for e in errors)
            return _json(body, errors[0]["status"] if not saved else 207)
        return _json(body, 201 if new else 200)

    @app.put("/api/photos/order")
    def api_photo_order():
        order = _body().get("order")
        if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
            raise ApiError("order must be a list of photo names")
        return _json({"order": photos.set_order(dash.config()[0].photos.path, order)})

    @app.post("/api/photos/<name>/show-now")
    def api_show_now(name: str):
        config = dash.config()[0]
        photos.find(config.photos.path, name)
        now = dash.now(config)
        pin = Pin("photo", until=show_now_until(now, config), photo=name, set_at=now)
        overrides = dash.edit_overrides(now, lambda o: replace(o, pin=pin))
        dash.ticks.request()
        return _json({"mode": mode_of(overrides, now), "message": f"Showing {name} until {_hhmm(pin.until)}"})

    @app.route("/api/photos/<name>/up-next", methods=["POST", "DELETE"])
    def api_up_next(name: str):
        config = dash.config()[0]
        now = dash.now(config)
        if request.method == "POST":
            photos.find(config.photos.path, name)
            if name in config.photos.hidden:
                raise ApiError(f"{name} is hidden; unhide it first", 409)
            overrides = dash.edit_overrides(
                now, lambda o: o if name in o.up_next else replace(o, up_next=(*o.up_next, name)))
            message = f"{name} is up next" if overrides.up_next[0] == name else \
                f"{name} is number {overrides.up_next.index(name) + 1} in Up next"
        else:
            photos.check_name(name)
            overrides = dash.edit_overrides(
                now, lambda o: replace(o, up_next=tuple(n for n in o.up_next if n != name)))
            message = f"{name} is off Up next"
        return _json({"up_next": list(overrides.up_next), "message": message})

    @app.post("/api/photos/<name>/hide")
    def api_hide(name: str):
        config = dash.config()[0]
        photos.find(config.photos.path, name)
        body = request.get_json(silent=True)
        want = body.get("hidden") if isinstance(body, dict) else None
        if want is not None and not isinstance(want, bool):
            raise ApiError("hidden must be true or false")
        hide = (name not in config.photos.hidden) if want is None else want

        def change(c: Config) -> Config:
            rest = tuple(n for n in c.photos.hidden if n != name)
            return replace(c, photos=replace(c.photos, hidden=(*rest, name) if hide else rest))

        dash.edit_config(change)
        if hide:
            now = dash.now(config)
            dash.edit_overrides(now, lambda o: replace(o, up_next=tuple(n for n in o.up_next if n != name)))
        return _json({"name": name, "hidden": hide,
                      "message": f"{name} is hidden" if hide else f"{name} is back in the rotation"})

    @app.delete("/api/photos/<name>")
    def api_delete(name: str):
        config, config_error = dash.config()
        photos.move_to_trash(config.photos.path, name)
        if name in config.photos.hidden and not config_error:
            dash.edit_config(lambda c: replace(c, photos=replace(
                c.photos, hidden=tuple(n for n in c.photos.hidden if n != name))))
        now = dash.now(config)

        def change(o: Overrides) -> Overrides:
            o = replace(o, up_next=tuple(n for n in o.up_next if n != name))
            return replace(o, pin=None) if o.pin is not None and o.pin.photo == name else o

        dash.edit_overrides(now, change)
        photos.drop_previews(dash.dir, name)
        dash.ticks.request()
        return _json({"name": name, "message": f"{name} moved to the trash"})

    # --- screen previews -----------------------------------------------------------------

    @app.get("/api/preview.png")
    def api_preview():
        return _preview(request.args.to_dict(), None)

    @app.post("/api/preview.png")
    def api_preview_with_settings():
        """The same from JSON, plus `config`: draw with these settings (checked, not saved)."""
        body = _body()
        config = None
        if body.get("config") is not None:
            try:
                config = validate(body["config"])
            except ConfigError as exc:
                raise ApiError(str(exc), 400, errors=exc.errors) from None
        params = {k: str(v) for k, v in body.items() if k != "config" and v is not None and v != ""}
        return _preview(params, config)

    def _preview(params: dict[str, str], config: Config | None) -> Response:
        kind = params.get("kind", "tides")
        if kind not in SCENE_KINDS:
            raise ApiError(f"kind must be one of {', '.join(SCENE_KINDS)} (got {kind!r})")
        args = [kind]
        at = params.get("at")
        if at:
            _check_at(at)
            args += ["--at", at, "--forecast"]
        for name, looks in (("waves", WAVE_LOOKS), ("clouds", CLOUD_LOOKS)):
            look = params.get(name)
            if not look:
                continue
            if kind != "sea":
                raise ApiError(f"{name} is for the sea screen")
            if look not in looks:
                raise ApiError(f"{name} must be one of {', '.join(looks)} (got {look!r})")
            args += [f"--{name}", look]
        if params.get("photo"):
            args += ["--photo", photos.check_name(params["photo"])]
        if kind == "message":
            args += ["--message", params.get("message") or "Hello"]
        tmp = dash.dir / "previews-tmp" / f"{threading.get_ident()}.png"
        settings = tmp.with_suffix(".json") if config is not None else None
        try:
            if settings is not None:  # the renderer is a child process: hand it the settings as a file
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text(state.dumps(config.to_dict()), encoding="utf-8")
                args += ["--config", str(settings)]
            dash.render(args, tmp)
            data = tmp.read_bytes()
        except RenderError as exc:
            raise ApiError(f"The {kind} screen couldn't be drawn: {exc}", 500) from None
        finally:
            tmp.unlink(missing_ok=True)
            if settings is not None:
                settings.unlink(missing_ok=True)
        response = Response(data, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _check_at(at: str) -> None:
    """A preview's `at`: an ISO 8601 time, or a named moment of a day (2026-11-20@sunset)."""
    day, mark, moment = at.partition("@")
    try:
        if mark:
            date.fromisoformat(day)
        else:
            datetime.fromisoformat(at)
    except ValueError:
        raise ApiError(f"at {at!r}: expected an ISO 8601 time such as 2026-11-04T21:00") from None
    if mark and moment not in MOMENTS:
        raise ApiError(f"at {at!r}: the moment must be one of {', '.join(MOMENTS)}")


def _warm_previews(dash: Dashboard, names: list[str]) -> None:
    """Draw new photos' previews in the background, so the grid has them when it asks."""
    config = dash.config()[0]
    for name in names:
        try:
            path = photos.find(config.photos.path, name)
            out = photos.preview_file(dash.dir, path, config)
            with dash._preview_lock:
                if not out.is_file():
                    dash.render(["photo", "--photo", name], out)
        except Exception as exc:  # noqa: BLE001 - the grid asks again and reports it
            log.info("no preview for %s yet: %s", name, exc)


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


def photo_queue(dash: Dashboard, config: Config, overrides: Overrides,
                files: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """Photo names in the order the photo steps will show them: the Up next queue (hidden
    or missing ones skipped), and the rotation from the last photo shown, which repeats."""
    folder = config.photos.path
    if files is None:
        files = photos.listing(folder)
    hidden = set(config.photos.hidden)
    names = {p.name for p in files}
    queue = [n for n in overrides.up_next if n not in hidden and n in names]
    rotation = rotation_order([p for p in files if p.name not in hidden],
                              SlideshowState(dash.dir / state.SLIDESHOW).last_shown(folder))
    return queue, [p.name for p in rotation if p.name not in queue]


def upcoming_changes(dash: Dashboard, config: Config, now: datetime, overrides: Overrides) -> list[dict]:
    """The next few changes the frame makes on its own (`/api/status` `upcoming`): `at`,
    `kind`, `photo` (the photo a step will show, when it can be known), `minutes` (until the
    change after it; None if there's none) and `why`: "sunrise", "sunset", "pin_ends", "photo",
    "photo_ends", "redraw" (the same screen drawn afresh) or "schedule". A run of redraws of
    the same screen is one row (the first), so a night of hourly sky redraws doesn't fill the
    list. Empty when the sun times can't be worked out."""
    tz = config.location.tz
    queue, rotation = photo_queue(dash, config, overrides)
    # As the tick decides: no photo steps with nothing to show, none for screens with no data here.
    config = screens.schedule_here(config, photos=bool(queue or rotation))
    try:
        sun = dash.sun(config)
        here = decide(now, config, overrides, sun)
        is_day = _is_day(now, config, sun)
    except Exception:  # noqa: BLE001 - e.g. no ephemeris: the page manages without
        log.warning("couldn't work out what comes next", exc_info=True)
        return []
    photos_ahead = 0
    out: list[dict] = []
    scene, pin = here.scene, overrides.active_pin(now)
    last_why = None
    change = upcoming_change(now, config, overrides, sun)
    for _ in range(UPCOMING * 12):  # a night of hourly redraws shows as one row, then sunrise
        if len(out) >= UPCOMING or change is None:
            break
        following = upcoming_change(change[0], config, overrides, sun)
        when, kind = change[0].astimezone(tz), change[1]
        then = decide(when, config, overrides, sun)
        day_then, pin_then = _is_day(when, config, sun), overrides.active_pin(when)
        photo = None
        if kind == "photo":
            if pin_then is not None and pin_then.photo:  # Show now: that photo
                photo = pin_then.photo
            else:  # a step: the schedule names Up next's first every time; project the order
                i = photos_ahead
                photos_ahead += 1
                if i < len(queue):
                    photo = queue[i]
                elif rotation:
                    photo = rotation[(i - len(queue)) % len(rotation)]
        if pin is not None and pin_then is None:
            why = "pin_ends"
        elif day_then != is_day:
            why = "sunrise" if day_then else "sunset"
        elif kind == "photo":
            why = "photo"
        elif scene.kind == "photo":
            why = "photo_ends"
        elif kind == scene.kind:
            why = "redraw"
        else:
            why = "schedule"
        if not (why == "redraw" and last_why == "redraw"):
            out.append({"at": when, "kind": kind, "photo": photo, "why": why,
                        "minutes": None if following is None else _minutes_between(when, following[0])})
        scene, pin, is_day, last_why, change = then.scene, pin_then, day_then, why, following
    return out


def _minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes in real time (two local times subtract by wall clock)."""
    return round((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 60)


def _is_day(at: datetime, config: Config, sun: SunTimes) -> bool:
    local = at.astimezone(config.location.tz)
    events = sun(local.date())
    off = timedelta(minutes=config.schedule.night_offset_minutes)
    return events.sunrise - off <= local < events.sunset + off


# --- running it ----------------------------------------------------------------------------


def main(*, host: str, port: int, fake_panel: bool) -> int:
    """`photopainter serve`: run the dashboard until SIGTERM or Ctrl-C."""
    from werkzeug.serving import make_server

    app = create_app(fake_panel=fake_panel)
    dash: Dashboard = app.extensions["photopainter"]
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no line per request (polls every 10 s)
    try:
        server = make_server(host, port, app, threaded=True)
    except (OSError, SystemExit) as exc:  # werkzeug prints why and exits on a bind error
        hint = " (port 80 needs CAP_NET_BIND_SERVICE: see photopainter-web.service)" if port < 1024 else ""
        detail = f": {exc}" if isinstance(exc, OSError) else ""
        log.error("can't listen on %s:%d%s%s", host, port, detail, hint)
        return 1

    def stop(signum, _frame):
        log.info("stopping (signal %d)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    log.info("dashboard at http://%s:%d/%s", shown, port, " (fake panel)" if fake_panel else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if dash.ticks.running:
        log.info("waiting for the running tick to finish")
    if not dash.ticks.close(wait_s=STOP_WAIT_S):
        log.warning("a tick was still running after %d s", STOP_WAIT_S)
    return 0
