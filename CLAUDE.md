# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project

Software for **Sea & Sky Ink Frame** (its name in the README since 2026-11-08; it was Tide &
Sky), a battery-powered color e-ink photo frame: a **Waveshare RPi Zero
PhotoPainter** (7.3" E Ink Spectra 6, 800×480, 6 colors) driven by a **Raspberry Pi
Zero 2 W**. It will hang, plugged in, in a home near the Wedge in Newport Beach, CA, and
show:

1. **A day screen:** tides for the Wedge (NOAA station 9410580) and how far the water is
   running from them (the Los Angeles gauge), plus weather and surf (Open-Meteo),
   sunrise/sunset and the moon phase.
2. **A photo every hour** for about 10 minutes, then back to tides.
3. **The night sky after sunset:** the view through a south-facing window, redrawn hourly.
4. **A web dashboard on the Pi** to see what's on the wall, switch screens, pick the next
   photo and change settings, reached over Tailscale.

**[PRD.md](PRD.md) is the plan:** goals, behavior, architecture, the contracts between
modules, and the task list that agents work from. Read it before starting new work.

**Status:** the frame runs itself. `photopainter tick`, run every minute by a systemd
timer, shows the day screen by day, a photo from `~/photos` for 10 minutes each hour, and
the night sky after sunset, with the Pi staying on under USB power. Built so far: the shared foundations (PRD.md F1: config, state, sun times, ink
drawing, the message screen, `photopainter scene`), the day screen's data (T1: tide,
weather and surf caches, moon phase and rise/set), the day screen (T2: `photopainter scene
tides`, with the measured water level from `sources/gauge.py`, added 2026-11-04), and the scheduler, tick and photo screen (C1+C2: `schedule.py`, `tick.py`,
`scenes/photo.py`, `photopainter-tick.timer`), and the dashboard (W1+W2: `photopainter
serve`, `web/`, `photopainter-web.service`; running on the Pi), and the sky screen
(S1+S2: star, constellation, Milky Way and skyline data, `astro/sky.py`, `scenes/sky.py`;
on the Pi since 2026-11-04), with the weather in its top-left corner (S3, 2026-11-04), and the sea screen (N2: `scenes/sea.py`, not on a default rotation), and the combo screen (N3: `scenes/combo.py`, four screens at once, not on a default rotation). The old 3-minute slideshow timer is
gone; `photopainter next` stays for manual use. The battery is read every tick; running low on it, every screen carries a small battery
and a nearly flat one settles on a photo (`hardware/battery.py`, 2026-11-04;
`render/battery_badge.py`, 2026-11-06); the frame stays plugged in and the pack is a UPS, since the board
can't wake the Pi (see "Power and battery rules"). Two buttons on the back step through the
schedule's screens, a long press of forward switches to photos only and back, and both held turn
low power on or off (`buttons.py`, wired 2026-11-06). The RTC coin cell is still to do (H2).
Sections marked *(planned)* describe intended design; keep this file true to what
is actually in the repo.

Read [docs/hardware.md](docs/hardware.md) before touching display, GPIO, battery or RTC
code. It has the pin map, palette, driver quirks and sources.

## Hardware at a glance

- Panel: Spectra 6, driver `epd7in3e`, 800×480 landscape, colors black, white, yellow,
  red, blue and green. A full refresh takes **about 30 s** (measured; Waveshare says
  12 s). No partial refresh.
- SPI0: RST = BCM 17, DC = BCM 25, CS = CE0 (BCM 8), BUSY = BCM 24, **PWR = BCM 27**
  (this differs from generic Waveshare demos).
- I2C bus 1: INA219 battery monitor at `0x43`, DS3231 RTC at `0x68`.
- Buttons: to ground from the P1 header's RXD (BCM 15, forward) and TXD (BCM 14, back), with
  the Pi's pull-ups. The serial port has to stay off (`docs/hardware.md` "Buttons").
- Pi Zero 2 W: 4× Cortex-A53, **512 MB RAM**, 2.4 GHz Wi-Fi only.

## Architecture

Each panel update is one short-lived run. Only that run drives the panel:

```
systemd timer (1 min, on the minute) → photopainter tick: config → clock check
  → sources.ensure_all_fresh() → overrides.json → schedule.decide() → scene key changed
  (and not quiet hours)? → render scene → incoming.png (the dashboard flashes the old picture
  for 15 s, then this one, as the panel does) → panel refresh → panel deep sleep
  → current.png + status.json
photopainter serve (photopainter-web.service, always running): the dashboard. An action
  writes overrides.json or config.json → starts `photopainter tick` in the background
  (one at a time, one queued) → the tick does the rest. Previews are drawn by `photopainter
  scene` in a child process. The server never drives the panel itself.
```

`decide()` is pure (time, config, overrides and sun times in; a `Decision` out), and its
rules are in the `schedule.py` docstring. By day and by night the frame runs a rotation of
screens (`schedule.day`, `schedule.night`: steps of `{screen, minutes}` cycling from local
midnight; `screen` is tides, sky, sea, photo, harbor or combo; by day the default gives every
screen 10 minutes an hour, `[photo, tides, harbor, sky, sea, combo]` (the Owner's own rotation,
the default since 2026-11-08, PRD D15), and `[sky 60]` by night; `[photo 60]` alone is a
slideshow). Most schedule, tick and dashboard tests use `[photo 10, tides 50]`, the old default,
as their fixture schedule (`photo_then_tides()`), and test the default itself separately. A photo step shows the next photo:
the tick picks it when it draws (Up next first, then the rotation, in the order of the
dashboard's photo grid: drag to move, new uploads last) and moves the rotation on,
and with no photos to show it decides with `Schedule.without_photos()`. Old files still load:
`tides_refresh_minutes`/`sky_refresh_minutes` as one-step rotations, and the old `schedule.photo`
rule folded into the rotations (`config._fold_old_photo`; its `show_minutes` becomes
`photos.show_now_minutes`, how long Show now lasts). A sky step (and a
Sky pin, day or night) is drawn for the middle of its bucket (`schedule._night_scene`), and so
is a sea step or pin (`schedule._sea_scene`). The
dashboard's Redraw writes `redraw_at` to `overrides.json` (`/api/redraw`) and the next tick
redraws the same screen. `tick --at ISO --fake-panel` simulates any moment on the Mac
(see Commands).

See PRD.md §4 for the full design.

```
src/photopainter/
  cli.py            # photopainter preview | show | next | clear | tick | serve | scene
  config.py         # Config dataclasses (mirror config.json), validate(), load_config()
  state.py          # paths (+ PHOTOPAINTER_* env overrides), atomic JSON under fcntl lock
  slideshow.py      # folder listing in the folder's saved order (.order.json), rotation
                    #   order, remembers the last photo shown
  schedule.py       # decide() -> Decision (pure), Overrides/Pin, upcoming_change(),
                    #   helpers for the dashboard: resolve_until(), show_now_until()
  tick.py           # one tick: config → clock → data → decide → render → panel → status
  buttons.py        # `photopainter buttons`: presses into actions (Decoder); forward and back
                    #   through the schedule or, in photos only, the photos; its own service,
                    #   since low power stops the dashboard
  hotspot.py        # no Wi-Fi: whether the frame is on it (link_up), when the mark is due, the
                    #   setup hotspot's steps (advance -> Outcome), Nmcli on the Pi, FakeNetwork
  astro/            # Skyfield, loaded lazily; ephemeris = data/de421.bsp, no downloads
    sun.py          # sun_events(): sunrise, sunset, civil dawn/dusk for a day and place
    moments.py      # moment_at(): named moments of a day for previews (sunset, dusk, moon…)
    moon.py         # moon_phase(at) (angle, % lit, name), moon_events(): moonrise/moonset
    eclipse.py      # find_eclipse(): the eclipse on anywhere on Earth, or due within 12 h;
                    #   its kind anywhere, whether it's seen here, times, how it looks now
    sky.py          # star/constellation/Milky Way loaders; sky_view(): alt/az of every
                    #   star (one vectorized call), the Moon (+ bright-limb angle), planets,
                    #   the Sun; sun_altaz(): the Sun at many times; moon_view(): the Moon
                    #   alone; galactic(): sky
                    #   direction → galactic l, b
  sources/          # web data → JSON caches in the state dir (formats: PRD.md §5)
    common.py       # get_json (urllib, 10 s timeout), refresh(): staleness, retry backoff
    tides.py        # NOAA CO-OPS predictions → tides.json (refetch: 24 h, < 14 days ahead,
                    #   or not reaching back to where the chart starts)
    weather.py      # Open-Meteo forecast → weather.json (refetch: 1 h)
    surf.py         # Open-Meteo marine (offshore swell, sea temp °C→°F) → surf.json (1 h)
    gauge.py        # NOAA water levels: measured minus predicted at the Los Angeles gauge,
                    #   since Newport's closed in 1994 → gauge.json (30 min)
    __main__.py     # python -m photopainter.sources [--force]: refresh them all now
  scenes/           # screens: render(scene, ctx) -> ink indices; render_scene() dispatch
    base.py         # Scene, SceneContext
    message.py      # plain text screen (first line = heading)
    setup.py        # Wi-Fi setup: a QR code that joins the hotspot, and what to do next
    photo.py        # a photo: named, or next in the slideshow rotation (hidden skipped)
    tides.py        # the day screen: tide + swell, weather, tide chart (layout in its
                    #   docstring); also finds new/full moons and eclipses for the chart
    sky.py          # the sky screen: stereographic view through the window (projection
                    #   and choices in its docstring), black at night, colored by the Sun's
                    #   height otherwise; SkyStyle holds the drawing choices; the weather
                    #   top left, drawn with tides.py's helpers
    sea.py          # the sea screen: today's ocean from the window, woodblock-style, from the
                    #   swell, wind, sky and tide; SeaStyle holds the drawing's numbers
    card.py         # the color test card (a dashboard pin; the schedule never picks it)
    harbor.py       # the harbor chart: Newport Bay as a nautical chart with today's tide wave
                    #   (predicted and measured), wind, swell, sun and moon; design in its docstring
    combo.py        # the combo screen: tides, sky, harbor and sea in a quarter of the panel each,
                    #   each panel its screen's own drawing at a smaller size
  render/           # pure Pillow/NumPy; runs on the Mac
    draw.py         # InkCanvas ("P" image of ink indices, no anti-aliasing), INK, font()
    battery_badge.py # the battery indicator: draw() puts it on a finished screen, in a corner;
                    #   draw_low_power() the crescent, draw_no_wifi() the no-Wi-Fi mark
    layout.py       # open (EXIF rotate, JPEG draft decode), fit: auto | fill | fit
    palette.py      # the 6 inks: panel codes, nominal + estimated colors, dithering, preview
    pipeline.py     # RenderOptions, render() -> ink indices, to_panel_codes() -> 480x800
    card.py         # the color test card's layout and patches (solids, dot mixes, targets)
    card_analysis.py # reads a photo of the card: inks, dot mixing, ΔE of dithered targets
  hardware/         # the ONLY package that imports spidev / gpiozero / smbus (lazily)
    epd7in3e.py     # Waveshare driver, vendored unmodified (commit d47d28d)
    epdconfig.py    # Waveshare pin/SPI config, vendored unmodified (claims GPIO at import)
    panel.py        # PanelDisplay: lock, min interval, timeout, init/display/sleep; pack_4bpp
    fake.py         # FakePanel: the panel's lock, keeps what it was sent
    buttons.py      # the two buttons' pins, via lgpio: GpioButtons, FakeButtons
    battery.py      # the INA219 on the battery line, via /dev/i2c-1: Reading (volts, mA,
                    #   state, percent), Battery.read(), FakeBattery (fake_battery.json)
    rtc.py          # (deferred, no file yet) DS3231 time; it can't wake the Pi (hardware.md)
  web/              # the dashboard (Flask); API table in server.py's docstring
    server.py       # create_app(), the JSON API (status with `upcoming`, /api/redraw, POST
                    #   /api/preview.png with unsaved settings), main() for `serve`
    jobs.py         # TickRunner (ticks in the background), Renderer (`scene` in a child)
    photos.py       # photo names, uploads (JPEG/PNG/WebP, ≤ 25 MB, shrunk to 1600 px),
                    #   their order, the .trash folder, preview cache file names
    frame_name.py   # the frame's name = the Pi's hostname (no setting to disagree with it);
                    #   checks a new one, runs scripts/pi/set-frame-name
    wifi.py         # saved Wi-Fi networks: list (nmcli), add/remove and a trial for the setup
                    #   hotspot through scripts/pi/wifi-connection as root; FakeNetworks under
                    #   --fake-panel
    health.py       # Wi-Fi, addresses (ip -j addr), Tailscale (status --json), clock, CPU
                    #   temp, power (vcgencmd), memory; data sources
    static/         # index.html, style.css, app.js: one phone-first page (two columns from
                    #   900 px, where previews draw on the frame itself), no build, no CDN.
                    #   Schedule and a box per screen (Tides, Sky, Sea, Harbor, Photos) close to
                    #   one line, each with its own Save and, for the screens, a preview;
                    #   then the same kind of box for Battery (reading, low power, chart, settings),
                    #   Frame (what several screens share, name, Wi-Fi, color card), Health
                    #   (also the dot up top), Help (what the two buttons do, words and a
                    #   drawing; also the ? up top) and last About (© Kenneth Brandon, every
                    #   data credit and software license; data/README.md)
data/               # fonts/ (DejaVu Sans + license), README.md (sources and licenses),
                    #   de421.bsp (gitignored link, from scripts/fetch-ephemeris.sh),
                    #   stars.json, constellations.json, milkyway.png, skyline.json (sky),
                    #   harbor.json (the harbor chart's map and soundings)
config.example.json # the default config (a test keeps it equal to default_config())
tests/              # pytest, runs on the Mac with no hardware and no network
  fixtures/         #   recorded NOAA / Open-Meteo responses (noaa_*, openmeteo_*), and
                    #   tides/weather/surf/gauge.json: caches in PRD §5 format (real, 2026-11-03);
                    #   horizons_altaz.json (JPL reference positions), stars_small.json,
                    #   golden_wedge.json (hashes of the Wedge's screens, test_golden.py)
scripts/
  flash-sd.sh       # Mac: flash + preconfigure an SD card (cloud-init, apt packages)
  fetch-ephemeris.sh # Mac: download de421.bsp once to ~/.cache/photopainter-dev/, link
                    #   it into data/ (run once per checkout or worktree)
  build-star-data.py # Mac: rebuild stars.json, constellations.json, milkyway.png from
                    #   pinned HYG / Stellarium / ESO files (only to change sources)
  build-skyline.py  # Mac: rebuild skyline.json (Catalina from the Wedge) from DEM data
  sea-playground.py # Mac: try the sea screen's look live, a slider for every SeaStyle number
  build-harbor.py   # Mac: rebuild harbor.json from an Overpass response and NOAA ENC soundings
                    #   (saved files, or --fetch)
  analyze-card.py   # Mac: measure the inks from photos of the color test card
  build-readme-images.py # Mac: redraw README.md's pictures (docs/images/) with the frame's own code,
                    #   from the fixtures; run it after a screen changes
  readme-dashboard-shots.mjs # Mac: the README's dashboard pictures, from a `serve --fake-panel`,
                    #   with headless Chrome (Node, no install); then `build-readme-images.py dashboard`
  deploy.sh         # Mac: rsync repo to the Pi, install command + systemd units, restart the
                    #   dashboard once any tick it started has finished
  sync-photos.sh    # Mac: convert (HEIC ok, max 1600 px) and add photos to Pi ~/photos
                    #   (never deletes unless --mirror; never touches dot-files like .trash/)
  show.sh           # Mac: show one photo on the frame now (streams it over SSH)
  next.sh           # Mac: skip to the next slideshow photo now
  pi/photopainter   # Pi launcher, symlinked to /usr/local/bin/photopainter
  pi/set-frame-name # Pi, as root: applies a new name at once (hostname, .local, Tailscale)
  pi/wifi-connection # Pi, as root: saves a Wi-Fi network from a keyfile on stdin (mode 0600
                    #   in /etc/NetworkManager/system-connections/), or forgets one by uuid
deploy/systemd/     # photopainter-tick.service + .timer (every minute, on the minute),
                    #   photopainter-web.service (the dashboard on port 80, as pi)
                    #   photopainter-buttons.service (the buttons, as pi)
deploy/network/     # photopainter-captive.conf: the hotspot's DNS answers every name with the frame
docs/               # hardware.md, pi-setup.md; images/ (README.md's pictures, not deployed; samples/
                    #   are public-domain photos, credited in README.md: personal photos stay out of git)
```

Rendering path: `open_image` → `fit_to_canvas` (canvas is 800×480, or 480×800 with
`--portrait`) → `map_to_inks` (Pillow Floyd–Steinberg against a 6-color palette) gives
ink indices 0–5 → `to_panel_codes` rotates to the panel's 480 rows × 800 columns and maps
to panel codes (0,1,2,3,5,6) → `pack_4bpp` → `epd.display(buffer)`. We never call the
driver's `getbuffer()`.

Charts and text skip the dithering: scenes draw on an `InkCanvas`, whose pixels are ink
indices (black 0, white 1, yellow 2, red 3, blue 4, green 5, the `palette.INKS` order).
Always pass `fill=INK["red"]`, never an RGB tuple or color name; `indices()` raises if
anything off-palette was drawn. `render_scene` checks every screen's size and inks.

## Development workflow

- **Parallel work uses git worktrees** (rules in PRD.md §7). Every session that writes
  code gets its own worktree, `task/…` branch and `.venv`; the main checkout is for the
  coordinator and hardware tasks. Only `main` is deployed: `scripts/deploy.sh` refuses
  other branches and uncommitted changes (`PHOTOPAINTER_DEPLOY_ANY=1` overrides).
- Develop on the Mac and deploy to the Pi over SSH. **Everything outside
  `hardware/` must import and run on macOS** so it can be tested without the frame (a
  test enforces that `cli` and `hardware.panel` import without hardware libraries).
- Pi access: `ssh photopainter` (an alias in `~/.ssh/config` for `pi@<frame name>.local`), with key
  `~/.ssh/photopainter_ed25519` (set up in `~/.ssh/config`). Login is by key only, and
  sudo needs no password. Run one-off commands with `ssh photopainter '<cmd>'`.
- The Pi runs Raspberry Pi OS Lite 64-bit (Trixie, image 2026-10-06), flashed with
  `scripts/flash-sd.sh` (see [docs/pi-setup.md](docs/pi-setup.md)). SPI and I2C are
  enabled. Installed from apt: `i2c-tools`, `python3-pil`, `python3-numpy`,
  `python3-spidev`, `python3-gpiozero` and `python3-lgpio`. The screens and the
  dashboard also need `python3-skyfield` and `python3-flask`: `flash-sd.sh` now installs them on first
  boot, but the current Pi doesn't have them yet (task P1 installs them; command in
  [docs/pi-setup.md](docs/pi-setup.md#packages)).
- On the Pi the repo lives at `~/eink-photopainter` (deployed copy, not a git checkout),
  photos at `~/photos` (with `.order.json`, the order dragged on the dashboard), state (lock, slideshow position, lgpio files,
  the `tides`/`weather`/`surf`/`gauge.json` caches and `sources.json`, the last fetch attempt of
  each) in `~/.cache/photopainter/`, plus the tick's `status.json`, `current.png` (the
  image on the wall), `incoming.png` (the one a refresh is bringing in, only during it),
  `overrides.json` (pins and Up next, for the dashboard), `sun.json`
  (sun times by day, so a tick that changes nothing doesn't load Skyfield),
  `photo-previews/` (the dashboard's dithered photo tiles, drawn once each),
  `battery.csv` (a reading a minute, about the last five days), `hotspot.json` (the setup
  hotspot's state and, while it's up, its password) and `fake_battery.json` and `fake_wifi.json`
  (only `--fake-panel` runs, written by hand to simulate a battery or no Wi-Fi). Deleted photos go to
  `~/photos/.trash/`. Logs: `journalctl -u photopainter-tick` (a tick that changes nothing
  logs nothing) and `journalctl -u photopainter-web` (the dashboard and the ticks it starts).
- The Pi's networking is NetworkManager with netplan behind it: the network from
  `flash-sd.sh` is a netplan profile (`/etc/netplan/90-NM-*.yaml`, generated under `/run`),
  and the ones the dashboard saves (Frame > Wi-Fi networks, `web/wifi.py`) are keyfiles
  in `/etc/NetworkManager/system-connections/`, written as root by
  `scripts/pi/wifi-connection` with the keyfile on its stdin. The dashboard never switches
  networks and never removes the one the frame is on; a saved network is joined when the
  frame finds it with nothing else connected, as at the Viewer's house. The setup hotspot
  (`hotspot.py`) is one more profile, `photopainter-setup`, that never connects by itself and
  isn't listed as a saved network. `deploy.sh` installs its DNS file in
  `/etc/NetworkManager/dnsmasq-shared.d/`, which only the hotspot's dnsmasq reads.
- Waveshare's demo repo, pinned to commit `d47d28d`, is on the Pi at `~/waveshare/demo`.
  It's for reference and hardware tests only, not part of this repo.
- Commands:

  ```bash
  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # once per checkout or worktree
  scripts/fetch-ephemeris.sh                       # once per checkout or worktree (sun tests need it)
  .venv/bin/pytest
  .venv/bin/photopainter preview photos/x.jpg      # writes previews/x.png (how the panel will look)
  .venv/bin/photopainter scene message --message "hello" -o previews/hello.png   # a screen, no panel
  PHOTOPAINTER_STATE_DIR=tests/fixtures \
    .venv/bin/photopainter scene tides --at 2026-11-03T14:10 -o previews/day.png  # day screen, fixture caches
  .venv/bin/photopainter scene sky --at 2027-07-03T22:30 -o previews/sky.png    # the night sky (no caches needed)
  PHOTOPAINTER_STATE_DIR=tests/fixtures \
    .venv/bin/photopainter scene sea --at 2026-11-03T14:10 -o previews/sea.png  # the sea, fixture caches
  PHOTOPAINTER_STATE_DIR=tests/fixtures .venv/bin/photopainter scene sea --at 2026-11-20@sunset \
    --forecast --waves big --clouds clear -o previews/sea.png  # as the dashboard previews: a named moment, a look
  .venv/bin/python scripts/sea-playground.py       # the sea's look on sliders: http://127.0.0.1:8743
  PHOTOPAINTER_STATE_DIR=tests/fixtures \
    .venv/bin/photopainter scene harbor --at 2026-11-03T14:10 -o previews/harbor.png  # the harbor chart, fixture caches
  PHOTOPAINTER_STATE_DIR=tests/fixtures \
    .venv/bin/photopainter scene combo --at 2026-11-03T14:10 -o previews/combo.png    # all four at once, fixture caches
  PHOTOPAINTER_STATE_DIR=/tmp/pp SSL_CERT_FILE=/etc/ssl/cert.pem \
    .venv/bin/python -m photopainter.sources       # fetch tides/weather/surf/gauge on the Mac
  scripts/deploy.sh                                # after committing on main (needs data/de421.bsp)
  scripts/sync-photos.sh ~/Downloads/a.jpg photos/ # add these to the Pi's ~/photos (keeps the rest)
  scripts/sync-photos.sh --mirror --dry-run photos/   # what making ~/photos exactly these would
                                                   #   delete (dashboard uploads too); drop --dry-run to do it
  S=/tmp/pp-sim; mkdir -p $S && cp tests/fixtures/{tides,weather,surf,gauge}.json $S/
  PHOTOPAINTER_STATE_DIR=$S .venv/bin/photopainter tick --fake-panel --at 2026-11-04T09:20
                                                   # simulate a tick: $S/status.json, $S/current.png
  scripts/show.sh photos/x.jpg                     # show one photo now
  scripts/next.sh                                  # skip to the next photo now
  .venv/bin/python scripts/analyze-card.py IMG_1.HEIC IMG_2.HEIC   # inks from photos of the
                                                   #   color test card (docs/hardware.md)
  ssh photopainter sudo systemctl enable --now photopainter-tick.timer    # run the frame
  ssh photopainter sudo systemctl disable --now photopainter-tick.timer   # stop it (keeps the image)
  ssh photopainter cat .cache/photopainter/status.json                    # what's showing, what's next
  ssh photopainter photopainter battery --watch 5                         # battery volts, mA, state
  ssh photopainter sudo systemctl enable --now photopainter-buttons.service  # the buttons (on since 2026-11-06)
  ssh photopainter photopainter press next                                # what a button does: next, back, photos, low-power
  PHOTOPAINTER_STATE_DIR=$S .venv/bin/photopainter press next --fake-panel --at 2026-11-04T09:20  # on the Mac
  PHOTOPAINTER_STATE_DIR=tests/fixtures .venv/bin/photopainter scene tides --at 2026-11-03T14:10 \
    --battery-volts 3.5 --battery-corner top-left -o previews/badge.png  # a screen with the battery indicator
  echo '{"volts": 3.15, "milliamps": -180}' > $S/fake_battery.json         # simulate a low battery for
                                                   #   `tick --fake-panel` / `serve --fake-panel`
  ssh photopainter sudo systemctl enable --now photopainter-web.service   # dashboard: http://<frame name>.local
  # (the unit listens on `::`, IPv4 and IPv6: Chrome resolves the .local name to IPv6 first)
  # the dashboard on the Mac: scratch state, a config whose photos.folder is a scratch folder
  S=/tmp/pp-web; mkdir -p $S/photos && cp tests/fixtures/{tides,weather,surf,gauge}.json $S/
  echo '{"photos": {"folder": "'$S/photos'"}}' > $S/config.json
  PHOTOPAINTER_STATE_DIR=$S PHOTOPAINTER_CONFIG=$S/config.json SSL_CERT_FILE=/etc/ssl/cert.pem \
    .venv/bin/photopainter serve --fake-panel --host 127.0.0.1 --port 8742   # http://127.0.0.1:8742
  ```

  `tick --at` uses the caches as they are (no fetching) and counts the clock as set. A manual `show`/`next`
  isn't known to the tick: it stays up until the schedule's next change.

  A dashboard preview with a time (`at`, or a named moment like `2026-11-20@sunset`) passes
  `--forecast`, so the weather and swell come from the forecast for that moment however long
  after the fetch it falls; past the cache's end they're left out. A preview of now goes by
  the wall's rules, so it shows what the wall would.

  The dashboard has **no login** (v1): it's for the Viewer's LAN and the Owner's tailnet.
  Never port-forward it or expose it with Tailscale Funnel without adding one first. On
  the Mac there's no timer, so only dashboard actions start ticks; a scheduled change waits
  until the next action.

## Conventions

- Python, targeting the version shipped with Raspberry Pi OS on the Pi (3.13.5 today).
  Don't use syntax newer than the Pi's interpreter.
- Pillow for imaging; NumPy is fine. Avoid heavy dependencies such as OpenCV or pandas:
  the Pi has 512 MB of RAM and installs on it are slow.
- **No pip or venv on the Pi.** The launcher runs the code from `~/eink-photopainter/src`
  with apt's Python packages. A new runtime dependency must exist as an apt `python3-*`
  package; if it doesn't, raise it with the user before adding a venv. (The Mac uses a
  normal `.venv`; `pillow-heif` and the optional `rawpy` there are Mac-only.)
- Settings are JSON at `~/.config/photopainter/config.json` on the Pi (`config.py`; the
  dashboard edits it), with `config.example.json` committed. A missing or bad file
  falls back to the defaults (`load_config` never raises). Saving the dashboard's
  settings writes every value, so a changed default in `config.py` doesn't reach a Pi
  whose config was saved that way: set it there too (as the sky's 190/170/90 were on
  2026-11-04). Paths honor
  `PHOTOPAINTER_CONFIG`, `PHOTOPAINTER_STATE_DIR` and `PHOTOPAINTER_DATA_DIR` (tests set
  the first two to a temp folder). Write state JSON only through `state.write_json` /
  `state.update_json`. All `datetime`s are timezone-aware. Secrets (Wi-Fi passwords,
  Tailscale keys, API tokens) go in `.env` or on the Pi only. Never commit them. Personal photos
  stay out of git (`photos/` and `previews/` are ignored), and so do personal details: no
  real Wi-Fi names, tailnet names or the frame's own hostname (examples use `InkFrame`).
- License: CC BY-NC-SA 4.0 (`LICENSE`). `data/`, the vendored Waveshare driver and the
  sample photos keep their own; a new third-party file goes in `data/README.md` with its
  license and credit, and in the list at the top of `LICENSE`.
- Web data: standard-library `urllib` through `sources/common.get_json` (no `requests`),
  10 s timeout, a User-Agent with no personal details (no email). A source's
  `ensure_fresh()` never raises: on failure it keeps the old cache, returns the error and
  waits 5 minutes before trying again. Tests use recorded responses only. Open-Meteo data
  is CC BY 4.0, non-commercial: credit it wherever it's shown (`data/README.md`). On the
  Mac, the python.org Python has no CA certificates for HTTPS; set
  `SSL_CERT_FILE=/etc/ssl/cert.pem` (the Pi's Debian Python uses the system store).
- Use type hints and small modules. Write pytest tests for everything that doesn't need
  hardware, and fake the `hardware/` interfaces in tests.
- `tests/test_golden.py` holds every approved screen to its pixels with the default config (the
  Wedge): fifteen screens and moments, drawn from the fixture caches, each a hash of its ink
  indices. A failure saves the new picture and names it. Re-record (`.venv/bin/python
  tests/test_golden.py`) only for a change the Owner approved over previews, never in the same
  commit as a refactor. The hashes depend on the Pillow and NumPy versions the file notes.
- Copy upstream Waveshare code into `hardware/` with the upstream commit noted in the file
  header. Don't use a git submodule, and don't modify copied files beyond what's needed.

## Display rules

These are enforced in `hardware/panel.py`; keep them that way.

- Do palette mapping and 4-bit packing ourselves (`render/`, `pack_4bpp`) so the preview
  PNG and the panel match. Buffers are exactly 480 rows × 800 columns.
- After every refresh, `epd.sleep()` puts the panel in deep sleep and cuts display power,
  even after an error. `init()` runs again before the next refresh.
- Only one process may drive the panel at a time (`~/.cache/photopainter/panel.lock`).
- No minimum time between refreshes: the Owner removed the 1-minute guard on 2026-11-06, because a
  change that didn't go up felt broken. One refresh runs at a time (the lock above, and ticks take
  turns), so the fastest it goes is one about every 30 s, and only when someone asks. The schedule
  still never plans screens less than 3 minutes apart (`config.MIN_REFRESH_MINUTES`). Waveshare's
  general e-paper guidance is 3 minutes, not confirmed for this panel: if ghosting or fading shows up
  after a lot of quick changes, look here first.
- After power-up the tick keeps the old image until NTP sets the clock. If that takes over 10 minutes
  it shows photos, a new one an hour, since they're the one screen that doesn't need the time, or a
  "Clock not set" screen with no photos to show. An outage with no reboot doesn't lose the clock: the
  tick trusts `/run/systemd/timesync/synchronized`, which stays until the next boot, not the kernel's
  own flag, which goes false after about 9 hours offline.
- A refresh that hasn't finished in 180 s raises a timeout instead of hanging on BUSY.
- The panel draws upside down as the frame hangs (found by the Owner on 2026-11-04), so
  `display.flip` defaults to true. The tick, `show` and `next` follow the config
  (`--flip`/`--no-flip` override it for one run). Previews and `current.png` show the
  picture the way the viewer sees it, never turned around.
- The `estimated` ink colors in `render/palette.py` are guesses, white-balanced so a
  photo's white maps to the white ink. To measure them: the dashboard's Frame > Color
  test card, then `scripts/analyze-card.py` (steps and first results in
  [docs/hardware.md](docs/hardware.md#measuring-the-ink-colors)). That card also compares
  today's dither, which works in sRGB, with `map_to_inks(linear=True)`. Measured inks with a
  dither closer to how dots mix scored closer to the targets but lost on the panel
  (2026-11-04), so the guesses and today's dither stay until a comparison on the panel beats
  them.
- Color defaults (`RenderOptions`): **saturation 1.3, vibrance 0.5**, gamma 1.0, contrast
  1.0. The user chose them on the panel on 2026-11-04 as the most consistent, with two photos
  side by side under each setting (S1.6 V1.0, chosen on 2026-11-03 one photo at a time, gave
  way to them). A gamma lift looked washed out. Don't change the defaults without another
  on-panel comparison. The method that works: one refresh with a grid of whole photos at
  different settings, each box labeled, and every setting on more than one photo (see
  `docs/hardware.md`). The CLI flags default to `RenderOptions`, and a test keeps them in
  sync.
- The day screen (`scenes/tides.py`) was designed with the user over labeled previews and
  approved on 2026-11-03; its layout is drawn in the module docstring. Change it only
  with the user, the same way. Choices they made: a tide chart from 24 h back to 2 days
  ahead at full width; night as 50% ordered dots that fade in from 6° before sunset to
  astronomical dusk; every high and low labeled on a white box, with heights on the
  highest and lowest in view; icons for new and full moons and eclipses (all of them,
  not just those visible locally), but none for sunrise and sunset; swell always labeled
  "offshore", never as surf height at the Wedge (PRD D9). It hasn't been checked on the
  panel yet (P1). The chart's span and labels are settings (`tides.chart_days_behind`,
  `chart_days_ahead`, `label_tides`, `label_times`, `label_heights`), with the choices above as their
  defaults; on a crowded chart, labels that would overlap each other or hide the curve are
  left out, and past about 13 days the day names shorten to letters. Swell and wind have an arrow pointing the way they're moving, as seen facing
  south like the Viewer (from the west points left, from the south down); `tides.flip_arrows`
  off makes north up instead. The text beside them still says where they come from
  (`flow_arrow`). A saved setting reaches the panel at the screen's next redraw.
- The day screen's measured water level was chosen by the Owner on 2026-11-04 over labeled
  previews (PRD D11). Blue stays the tide table; a red line shows the water over the last
  day and red dots where it's likely to go, with no legend (Owner, 2026-11-06: it's obvious, and
  it was in the way; the harbor and four-up charts lost theirs too). The big number, the
  next high and low and the chart's labels include the gap; "+1.3 over table" shows only
  from 0.5 ft. It's an estimate for the Wedge from the Los Angeles gauge, not a reading
  there: the dashboard says so, and the dots only hold the last 25 h's gap steady, so they
  can't see a Kelvin wave or storm coming. Readings over 2 h old say "as of"; over 12 h, the
  screen is the tide table alone. It hasn't been checked on the panel yet.
- The sky screen (`scenes/sky.py`) was designed with the user the same way and approved
  on 2026-11-04; its layout, projection and choices are in the module docstring, and
  `SkyStyle` holds them (defaults = the approved design). The window faces 190° (about 10°
  west of south, measured from a satellite view; config `sky.center_azimuth_deg`), and
  the view is 170° wide up to the zenith. Choices they made: black sky, green 1-px
  constellation lines and names; the brightest stars in their real colors by B−V (orange
  dithered from red and yellow; hot stars white in a blue ring); the Milky Way as ordered
  white dots; below the horizon the sea (with the Moon's path when it's up), surf and
  dark brown stippled sand with the compass points; Catalina from its real skyline, 4×
  taller, grey (50% white dots) with a white ridge line (chosen on the panel). Credits (ESO for the Milky Way) go on the
  dashboard, not the panel. The weather sits top left in place of the time and date (Owner,
  2026-11-04, chosen over labeled previews of four places): the condition with an icon, the
  temperature, today's high and low, and the wind with an arrow pointing the way it blows
  as seen from the window; it uses the day screen's cache, rules and helpers from
  `scenes/tides.py`, with stars instead of a crescent for a clear night. Top right: the
  next sunrise or sunset (through the night too), the moonrise while the Moon is down,
  soonest first, and "Sky at 9:30 pm" in small letters (the time the sky is drawn for, not
  the refresh). Both corners sit on solid blue boxes, the same day and night, in white bold
  letters: flush with the screen's edges, the inner corner rounded, no line around them (the
  Owner chose that on 2026-11-06 over labeled previews of five ways, over white boxes ruled in
  black by day, which looked like construction paper, and thin letters on black at night,
  which were hard to read; `corner_panel`, on the combo screen too). On the Pi (2026-11-04): `photopainter scene sky` takes
  4.1-4.7 s from a cold start (the render itself 2.5-3.1 s; budget 15 s), 79 MB peak;
  first shown on the panel that night; Catalina's fill was chosen there, the rest is
  still to be judged on the panel (P1). With the Sun less than 15° below the horizon the
  same view changes with its height (added 2026-11-04, previews only so far): a dithered
  blue and white sky with a sunset along the horizon when the Sun is low, the Sun with its
  path on the water and today's arc with the hours marked, stars of magnitude 2 and
  brighter, bright sand. While the sky is light (the Sun above -2°) names are black on small
  white boxes: the Owner chose that on 2026-11-05 over labeled previews, since white letters
  with a thin outline got lost in the pale dots. The user chose the lighter sky and
  to keep the white constellation lines and the hour arc by day; those and the day's star
  limit are settings on the dashboard (`sky.day_magnitude_limit`, `day_constellation_lines`,
  `sun_arc`). `lit_style`, `sky_shares` and
  `star_limit` hold it; deeper than 15° (`NIGHT_SUN_ALT`) the approved night look is
  untouched, pixel for pixel. Blue and yellow dots are kept apart in the sunset: mixed,
  they read as grey-green mud. During an eclipse seen from here (`astro/eclipse.py`; the look
  is in the module docstring) the Moon, its usual size, goes red in Earth's real shadow, or the
  Moon bites the Sun, to scale, in the direction it really does. The Owner chose that look on
  2026-11-07 over labeled previews (no bigger Moon, no dashed shadow edge). Every eclipse on
  Earth, seen here or not, leads the top right from 12 h before it begins, in two rows: its
  name by how deep it gets anywhere ("Total solar eclipse" though it's only partial here, as
  the Owner asked on 2026-11-07), then what's seen here ("Partial here until 12:22 pm",
  "11:11 am, not seen here"). Only moments near a full or new moon search for one (40 ms on the
  Mac, 4.2 s for the whole sky screen on the Pi), and any other render is pixel for pixel as it
  was. The Owner judged the total lunar eclipse on the panel on 2026-11-07 (March 3, 2026 at
  3:30 am): it looks good. An eclipse doesn't take over the schedule (Owner, 2026-11-07), so a solar one
  needs a Sky pin or a day rotation with the sky in it: the default day rotation never shows
  it, and a lunar one redraws with the night's rotation (hourly by default). An ordinary new moon
  that would touch the Sun's disc isn't drawn, so it can't pass for an eclipse. The sea screen's
  Moon doesn't show eclipses.
- The sea screen (`scenes/sea.py`) is a picture of today's ocean, not a chart. The Owner
  approved the look over mockups on 2026-11-04, then asked for the waves further apart and
  for whitecaps that show in strong wind; `SeaStyle` holds the numbers and
  `scripts/sea-playground.py` puts them on sliders, drawn by the real code. The module
  docstring says what each number drives: the swell's height sets the crests and foam, its
  period how far apart the rows are, its direction their tilt against the window's bearing;
  wind sets the chop and, from 10 mph, whitecaps as dashes on the water; the tide with the
  measured gap sets the beach's width. The weather cache has no cloud cover, so the WMO code
  picks the sky (0-1 clear, 2 clear with two clouds, the rest overcast, rain and fog
  included). The Sun is a disc at its real bearing and height, inside
  a span of 55° either side of the window. The jetty runs out from the bottom left where the Owner drew it, with flat water and no
  beach to its left. The words are in a plain box top left, with arrows beside
  the swell's and wind's directions as seen from the window (from the west points left) and a
  small "Updated" line for when the screen came up; nothing
  else goes on the picture; what's missing is left out, and no swell data draws a nearly
  flat sea. The words don't say "offshore" as the approved mockup didn't (PRD D9 asks for
  it on the day screen). It's drawn for the middle of its bucket, and seeded from that
  moment. With the Sun down it goes dark, with the Moon: a variant, not yet
  chosen by the Owner. Not on any default rotation; add it on the dashboard. It hasn't been
  checked on the panel yet. The dashboard's Sea box previews any time of any day (so the Sun,
  the Moon and the light or dark look) and, in place of the forecast, a wave look (`WAVES`:
  calm, big swell, windy, gale) and a cloud look (the Owner, 2026-11-05). The looks are for
  previews only: the wall always draws the real sea. The words box covers the Sun through the
  morning and to about 1 pm (seen on 2026-11-05, not yet raised with a change).

- The harbor chart (`scenes/harbor.py`, scene kind `harbor`) was approved by the Owner as a
  mockup on 2026-11-04: lower Newport Bay north up, land buff dots, harbor and ocean blue dots,
  green marsh, sand, black shoreline, piers and jetties, red and green beacons with numbers,
  place names, a title block top right, a compass rose bottom left with sunrise and sunset on
  its rim and the Sun and Moon at their azimuths now, a scale bar and lat/lon ticks. It isn't in
  the default rotation: add it on the dashboard, or pin it. Choices since the mockup, all the
  Owner's (2026-11-04): the map's bounds are settings (`harbor.south`, `north`, `center_lon`,
  for the map above the band; the width follows; `data/harbor.json` covers 33.54-33.70 N, 118.02-117.78 W and a view past
  that is fitted back inside), with the default south edge lowered so the NWP safe-water buoy
  shows bottom right; the tide in a band along the bottom (chosen over a box in the title block and one over the
  ocean, from previews), by default 24 h back to 4 days ahead: the day screen's `tide_chart` at a
  smaller size, under its measured-water rules (PRD D11), with its span and labels settings of
  its own (`harbor.chart_days_behind`, `chart_days_ahead`, `label_tides`, `label_times`,
  `label_heights`; the tide and gauge caches reach back as far as either chart, `Config.chart_days_behind`);
  and, because the mockup's text
  was hard to read over the ocean, a much paler ocean (12.5 % dots against 22 %), white halos
  on words over water and fewer, larger depths. The swell and wind words moved into the title
  block, each line keyed with a small copy of its arrow; on the map, an even field of thin
  wind arrows with tail feathers (one per 10 mph) and always two wavy swell arrows. Since
  2026-11-05 (Owner) the swell is blue and the wind another ink, with a wind arrow's length
  following the wind: 19 px at 5 mph, 30 at 10, 52 from 20 mph up, and 0.7 of that in the combo,
  whose harbor panel draws them the same way (`draw_on_water`). Green is from labeled previews,
  since red already means the measured water and the port-hand beacons; not judged on the panel
  yet. The field runs in lines along the wind: about 40 arrows on a light day, 25 at 12 mph, 12-22
  from 20 mph, when they start to join up into streaks. Depths fill in around the arrows and never
  touch each other. Both come up to the compass rose: only its disc and what's on its rim today are
  kept clear (`rose_rects`), not the square around it. Soundings are real NOAA ENC depths in whole feet below MLLW, never typed by
  hand; the fringe of green mud that widens as the tide drops is a picture, not survey data. The
  credits ("© OpenStreetMap contributors", ODbL; NOAA) and what the depths mean are on the dashboard,
  not the panel, where they were too small to read (Owner, 2026-11-06). The title block's swell and
  wind words are 13 px, in two lines each; it may grow taller for them but not wider, so it keeps off
  the bay (Owner, 2026-11-06). Marks, names and
  depths follow the view; a name that would land on another is left out, so a map zoomed far
  out is sparse. Not checked on the panel yet. 0.6 s to render on the Mac, 1 s from a cold start,
  85 MB peak; not timed on the Pi.

- The combo screen (`scenes/combo.py`, scene kind `combo`, named by the Owner on 2026-11-05)
  shows the tides, the sky, the harbor and the sea at once, in a 2×2 grid split by a black
  cross: the tide over the chart on the left, the window's sky over its sea on the right. Each
  panel is its screen's own drawing at a smaller size, never a shrunken image: halving a
  dithered picture turns 50% dots into solid ink. Each thing is said once: the date, moon and
  tide on the tide panel, the weather and the Sun and Moon's times on the sky, swell, wind and
  water on the sea. By day the small sky leaves out constellation names. The harbor panel
  shows the full chart's north and south and the middle of its width, without the tide band,
  rose and title block. It has no settings of its own and isn't
  in the default rotation. Like the sea, its sky and sea are drawn for the middle of the
  bucket and its tide and chart for when it came up (`Scene.updated`). `SkyStyle`'s `notes`,
  `con_names`, `reserved`, `compass_size`, `hour_size` and `sea.draw`'s `scale`, `words` exist
  for it, and at their defaults the full screens hashed the same before and after. The panels'
  layout is a first design, picked from previews on the Mac and not judged on the panel yet. 0.8 s to
  render on the Mac against 0.6 s for the sky, 67 MB peak against 76 MB; not timed on the Pi.
  Side by side, the sky keeps its twilight after sunset while the sea goes dark as soon as the
  Sun is down, which is how each full screen already behaves.

## Power and battery rules

The board can't power the Pi off and on by schedule: the RTC's interrupt doesn't reach a
wake pin, no GPIO controls the power switch or the boost, and a halted Pi stays halted
when USB power returns (`docs/hardware.md` "Can the Pi wake itself?", answered from the
schematic on 2026-11-04). So a battery makes the frame a UPS, not a battery-powered frame,
and the frame stays plugged in (PRD.md D1). What's built:

- `hardware/battery.py` reads the INA219 on the battery line through `/dev/i2c-1` (no
  smbus package on the Pi): volts, current (positive = charging, negative = the frame
  runs on the cell), a state (`none`, `charging`, `discharging`, `idle`) and a percent
  estimated from the voltage. The tick reads it every minute into `status.json`
  (`battery`, null without a reading) and `battery.csv` (about five days of readings). The
  dashboard shows it in the Battery box, and Health warns while the frame runs on the battery.
  `photopainter battery [--watch SECONDS]` prints readings on the Pi.
- Everything about the battery on the dashboard is in one Battery box (the Owner, 2026-11-06;
  it was in three places), always there, even with no reading. Closed, its line says the state
  ("Charging, 4.14 V", "On battery, 62 %, ~20 h left", "Low power: Wi-Fi off", "No battery
  reading"). Open: the reading, the low-power state, the chart and the settings with their own
  Save. Health keeps only the warnings; its Power row is the USB supply's under-voltage.
- The Battery box (`/api/battery`, `power.py`) charts the last five days of `battery.csv`
  (the Owner, 2026-11-08, so charging shows too; it was the last run on the battery): the
  voltage in 15-minute points, split where the state changes, tinted green where it charged
  and amber where it ran on the cell, a line at each midnight. Under it, a strip of the current
  on the same days, filled green above zero while charging and amber below on the cell (the
  Owner, 2026-11-09, chosen over a mock-up with the current as a second line on the voltage's
  plot, which crossed the voltage and its labels). Dragging a finger along it (or hovering with
  a mouse) reads the nearest point into the line above the chart: time, volts, current and
  state, with a dot on each. Below it, the last run on the battery. While one is going, it guesses
  the time left (a dashed line, given at most a quarter of the chart): the pack's
  rating (`battery.capacity_mah`, a setting) times the percent the voltage curve gives,
  divided by the last half hour's draw. It reads a voltage sagging under load, so it runs
  short rather than long. Added 2026-11-06 for a 10,000 mAh pack (about two days at 0.2 A).
- **Low power** (`battery.low_power`, off by default; `power.py`): after
  `low_power_after_minutes` (1) on the battery the tick turns Wi-Fi off and stops the
  dashboard, with Wi-Fi back for about four minutes every `data_every_hours` (2) so the
  caches can refresh (no fetch on the tick that turns it on: the connection takes a few
  seconds and a failed fetch backs off five minutes). USB power back brings both back
  within a minute; so does turning the setting off, once the frame is plugged in, since
  the dashboard isn't reachable on battery in low power. A
  `/boot/firmware/photopainter-stay-awake` file keeps the frame out of it. The state is
  `status.json` `power`; the actions run `sudo -n nmcli` and `sudo -n systemctl` (no
  password for `pi`). On the Mac and with `--fake-panel` they're only logged. Wi-Fi is
  about a quarter of the frame's draw, so this buys about a third more time, not weeks.
- **The buttons** (Owner, 2026-11-06; `buttons.py`, `photopainter-buttons.service`, wired to the P1
  header so nothing is soldered to the Pi). A press of forward or back moves to the next or previous
  step of the rotation running now, and the frame carries on through its cycle from there
  (`overrides.json` `cycle`, `schedule.move_cycle`) until the period or the day ends; a photo step shows
  the next photo. Forward held for a second, then let go, goes to photos only: the dashboard's Photos
  mode until you change it, a slideshow at a photo step's pace, day and night, where forward and back
  go through the photos. The same long press, or the dashboard's Auto, goes back to the schedule as set.
  Both held for a second turn `battery.low_power` over: plugged in that only sets it for the next run on
  the battery, and on the battery it's the one way back to Wi-Fi and the dashboard without plugging in. A
  crescent beside the battery's place shows the setting is on (`battery_badge.draw_low_power`), since
  nothing else you can see changes; turning it over redraws the screen. A press while the last one is
  still on its way up (a tick running or queued) is ignored; once it's up the next one goes at once. They need their own
  service because low power stops the dashboard: it holds the pins and watches them (about 32 MB and
  0.3 % of a core, measured on the Pi), and ticks are its children. Photos only has no end of its own, an
  exception to pins expiring, because the Owner asked for it to stay until the long press. The mark's
  look and place are the defaults, until the Owner picks from what's on the panel.
- **No Wi-Fi** (Owner, 2026-11-07, over labelled previews; `hotspot.py` has the rules). An outage
  needs nobody: the frame carries on and reconnects by itself, so nothing turns the hotspot on by
  itself, and the Owner was firm that a QR code must never be needed to get through one. After 10
  minutes down, screens drawn from then on carry a mark, a Wi-Fi fan with a red X, with no refresh of
  its own; it goes at the next redraw once Wi-Fi is back. Alone it sits in the corner, in the
  battery's place, because one place further in it covered the day screen's last day name; with the
  battery up it moves left of the crescent's place. No mark for a router with its internet out
  (a hotspot wouldn't help) or in low power (Wi-Fi is off on purpose). **Back held for a second** starts
  a hotspot, `<name>-Setup`, and shows `scenes/setup.py`; with Wi-Fi working it only redraws to clear a
  mark left up, so it can't knock the frame offline. It's WPA2 with a new password each time, written on
  the panel and in the QR code, because the dashboard has no login. A phone that joins gets the
  setup page by itself: every name resolves to the frame, and `web/server.py` redirects that network to
  `/setup`. There's one radio, so trying a network drops the phone, and the result goes on the panel.
  A network is kept only once it has worked, replacing older profiles of that name. Any press calls it
  off, and it ends 15 minutes after the page was last open. Low power and a nearly flat battery end it
  too. `wifi.help` (Frame > Wi-Fi networks) is a line for that screen, such as who to call. Tried on
  the Pi on 2026-11-07 with home Wi-Fi switched off: the hotspot came up on this radio 5 s after the
  hold (scan included) and the QR screen 39 s after it; an Android phone (Pixel) joined from the QR
  code; a press called it off, and the frame was back on its network 2 s later (Tailscale at once).
  The Mac couldn't reach it on the LAN for about 3 more minutes, while the router came back, not the
  frame. Not tried yet: the page opening by itself, joining a network from it, an iPhone. The
  dashboard's journal says what phones on the hotspot asked for (`hotspot: ...`). To simulate it: `echo '{"connected": false}' > $S/fake_wifi.json`, then
  `photopainter press wifi-setup --fake-panel` and open `/setup` on `serve --fake-panel`
  (`"join_error": "wrong_password"` makes a try fail).
- **Help on the dashboard** (Owner, 2026-11-07): a box saying in plain words what the buttons do, for
  the Viewer, with a drawing of the frame's back, measured from the Owner's photo: the black plate, the
  Pi, the stand and the buttons to scale (`docs/hardware.md` "Buttons"). Their labels sit above the frame
  with lines down to them, chosen over tags on it: white words on the plate looked printed there. The ? at the top of the page opens it. It's words and a drawing
  only. On-screen buttons that really pressed were built and taken out before merging, because the
  Owner found it confusing that they worked. When what the buttons do changes, change its words too.
- **The indicator** (Owner, 2026-11-06): running on the battery below `battery.indicator_volts`
  (3.6 V, a setting; 4.2 V means the whole time it's on the battery), every screen carries a small
  battery, a black outline with a fill for the percent on a white box ruled in black
  (`render/battery_badge.py`, drawn by the tick after `render_scene`, so it's on `current.png` and
  the dashboard too). It appears at once: the screen that's up gets one redraw, under the refresh
  guard and quiet hours (a rotation photo's redraw is the same photo). After that the fill changes
  only when the screen redraws anyway, with no refresh for a level change. Back on USB power it goes
  at the next redraw, with no refresh of its own. Once on, it stays on for that run on the battery,
  so a voltage bouncing around the setting can't cost a refresh each time. Its corner is
  `battery_badge.DEFAULT_CORNER`, bottom right until the Owner picks from previews.
- **The last photo** (Owner, 2026-11-06): running on the battery below `battery.low_volts` (3.3 V, a
  setting), the tick keeps a photo that's up, with no refresh, and otherwise moves to the next one
  the way a photo step does (Up next first, then the rotation), so what's frozen on the wall when
  the cell dies is a photo and not a stale tide chart. Then it only reads the battery and flushes the SD
  card each minute until a tick sees the frame off the battery, even if the voltage climbs back above
  the setting meanwhile (`status.json` `battery.low` stays true for that long). There is no notice screen
  (Owner, 2026-11-06), and with no photos to show it's a blank page. The first full run-down
  (2026-11-07, `docs/hardware.md`) cut out at 3.00 V. At 3.2 V the photo went up only 9 minutes before
  that, so the Owner raised the setting to 3.3 V (about 17 minutes). **It never shuts down:** the board keeps 5 V on the Pi while
  the cell has charge, so a halted Pi would sit dark until someone found the switch, and nobody at the
  Viewer's house should have to. A flat cell ends in a power cut like any other, and the Pi boots when
  the power is back. The low reading is checked only while discharging, so a flat cell on USB power
  never trips it. The two settings aren't checked against each other: an indicator set below the last
  photo's voltage is used as given, so that photo goes up without one.
- Before plugging a pack in: single-cell 3.7 V Li-ion only, and check the connector's
  polarity against the board (pin 1 is +). Nothing on the board survives a reversed cell.
- The percent is a guess from the voltage. The current and the state are what to trust.
- If sleeping between updates ever comes back (a hardware mod), the old rules still apply:
  **never let shutdown logic lock us out.** Skip it on USB power, when a
  `/boot/firmware/photopainter-stay-awake` file exists, and in the first minutes after boot.

## Open questions

Product questions (window bearing, photos at night, quiet hours, orientation, the
Viewer's Wi-Fi) are tracked in PRD.md §8. Hardware and technical ones:

- How long a pack lasts: measured 2026-11-07 (`docs/hardware.md`), 10 h 42 min from a full
  2000 mAh charge at about 0.2 A, cutting out at 3.00 V. What cuts it out at 3.0 V (the boost,
  the charger, or a protection threshold) isn't known, nor whether a flat cell keeps restarting the
  Pi (the journal isn't persistent). The charger gives about 2.07 A from flat, about 1C for this pack,
  with no temperature check. Bluetooth and the activity LED are still on; turning them
  off (`dtoverlay=disable-bt`, `dtparam=act_led_trigger=none`) might buy 5–10 %.
- Is an RTC coin cell installed? On 2026-11-03 the INA219 read 16 mV (no battery), and
  the DS3231 had never been set (oscillator-stopped flag on). A coin cell is planned so the
  clock survives power cuts without Wi-Fi (PRD.md H2). The Li-ion pack is in since
  2026-11-05 (JLJLUP 2000 mAh, re-pinned: it came reversed).
- Dithering quality: Pillow's Floyd–Steinberg uses RGB distance and puts blue/red
  speckle in skin tones. A perceptual (Lab) error-diffusion ditherer may look better but
  must stay fast enough on the Pi.
