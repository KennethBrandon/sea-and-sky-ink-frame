# Raspberry Pi setup

How to get the Pi Zero 2 W in the PhotoPainter from blank SD card to reachable over SSH.

## How everything connects

- **No cable to the Mac is needed.** The Pi joins your Wi-Fi, and all programming happens
  over SSH. Claude Code can run commands on the Pi directly with `ssh`.
- **Power goes into the USB-C port on the Waveshare board.** That port powers the Pi
  through the 40-pin header and charges the battery. Don't power the Pi's own micro-USB
  port at the same time.
- The Pi's micro-USB port isn't used in normal operation. It's only a fallback (see
  Troubleshooting).

## What you need

- A microSD card, 16 GB or larger.
- A USB SD card reader. This MacBook Pro (16-inch, 2019) has no SD slot.
- A **2.4 GHz** Wi-Fi network. The Pi Zero 2 W has no 5 GHz radio. If your router
  combines both bands under one network name, that's usually fine.

## 1. Flash the SD card (on the Mac)

### Option A: `scripts/flash-sd.sh` (what we use)

1. Create the Pi's SSH key once:
   `ssh-keygen -t ed25519 -f ~/.ssh/photopainter_ed25519`. Then add a `photopainter`
   host entry to `~/.ssh/config` with `HostName InkFrame.local`, `User pi` and that
   `IdentityFile`.
2. Download the latest image from
   <https://downloads.raspberrypi.com/raspios_lite_arm64_latest> and check it against the
   `.sha256` file published next to it.
3. Find the card with `diskutil list external physical`, then run:

   ```bash
   scripts/flash-sd.sh disk3 path/to/raspios-trixie-arm64-lite.img.xz
   ```

   The script refuses any disk that isn't external, removable and SD-card sized. It asks
   you to retype the disk name, then asks for the Wi-Fi name and password and your Mac
   password. It writes the image and then the cloud-init first-boot files (`user-data`,
   `network-config`), which set up the hostname, the `pi` user with key-only SSH, Wi-Fi,
   the time zone, SPI and I2C, and install the apt packages listed under
   [Packages](#packages). The write takes about 3 minutes.
4. If the card doesn't remount after the write, replug the reader and run
   `scripts/flash-sd.sh --config-only`.

The `pi` user has no password, so login is by SSH key only and sudo needs no password. To
use a console, set one later with `sudo passwd pi`.

### Option B: Raspberry Pi Imager

1. Install Raspberry Pi Imager, either with `brew install --cask raspberry-pi-imager` or
   from <https://www.raspberrypi.com/software/>.
2. In Imager, choose:
   - **Device:** Raspberry Pi Zero 2 W
   - **OS:** Raspberry Pi OS Lite (64-bit), under "Raspberry Pi OS (other)". Lite has no
     desktop, which saves RAM. Waveshare's prebuilt image isn't needed: it's built around
     their AI-art demo.
   - **Storage:** the SD card. Double-check that it's the card, not another drive.
3. Fill in the customisation settings before writing:

   | Setting | Value |
   |---|---|
   | Hostname | `InkFrame` (the Pi will be at `InkFrame.local`) |
   | Username / password | `pi` plus a password you choose |
   | Wi-Fi | Your 2.4 GHz network name and password, country `US` |
   | Locale | Your time zone and keyboard layout |
   | SSH | Enabled, **public-key authentication**, using the contents of `~/.ssh/photopainter_ed25519.pub` |
   | Raspberry Pi Connect | Off |

4. Write the card, eject it, and put it in the Pi's microSD slot.

## 2. First boot

1. Plug USB-C into the Waveshare board. If nothing happens, press the board's power button.
2. Wait 3–5 minutes. On first boot the Pi expands the filesystem and may reboot.
3. The e-ink screen won't change, because nothing drives it yet. That's expected.

## 3. Connect from the Mac

```bash
ping -c 3 InkFrame.local
```

```bash
ssh pi@InkFrame.local
```

Once this works, record the hostname, user and key in the "Pi access" line of `CLAUDE.md`.

## Troubleshooting

- **`InkFrame.local` not found:** look for `InkFrame` in your router's list of
  connected devices and try its IP address. Check that the Wi-Fi is 2.4 GHz and that the
  password and country were entered correctly. If any of those were wrong, re-flash the card.
  A frame that already runs somewhere else doesn't need re-flashing: add the new network
  from the dashboard before it moves (see "The next house's Wi-Fi").
- **Still not on the network:** two fallbacks get a console without Wi-Fi:
  - Serial console through the board's 3-pin debug header. Waveshare's listing says a
    CH343 USB-to-serial adapter comes in the box. This needs UART enabled on the SD card
    and possibly a driver on the Mac.
  - USB networking through the Pi's micro-USB **data** port (labelled USB, not PWR). This
    takes some extra setup on the card.

## The next house's Wi-Fi

The frame only knows the network it was flashed with. Before it travels, add the network
at the house it's going to from the dashboard: **Frame > Wi-Fi networks**, then the
network's name, its password (empty for an open network) and whether the name is hidden.
The frame keeps the connection it has and joins the new one when it starts up there;
Tailscale follows, so the dashboard is reachable over the tailnet again. Check the band
first: the Pi Zero 2 W has 2.4 GHz Wi-Fi only, and on a 5 GHz-only network it never
connects, with no message anywhere.

What it writes: a NetworkManager keyfile in `/etc/NetworkManager/system-connections/`
(mode 0600, root only), through `scripts/pi/wifi-connection` under sudo, with the password
on the helper's stdin so it never shows in a process list. The network from `flash-sd.sh`
is netplan's (`/etc/netplan/90-NM-*.yaml`), and the dashboard refuses to remove the
network the frame is on. Over SSH, `nmcli connection show` lists them all and
`sudo nmcli connection delete id NAME` removes one.

## When the frame has no Wi-Fi

An outage needs nothing from anyone: the frame carries on and reconnects by itself, with a small
Wi-Fi mark on the screen while it's down. For a new router, a new password or a new house, hold the
**Back** button for a second and let go. The frame puts up a hotspot, `<name>-Setup`, and shows a
QR code that joins it. A phone gets a page with the networks the frame can hear, and the frame
says on its screen whether the one you pick worked. A network on 5 GHz alone isn't on the list,
since the Pi Zero 2 W can't hear it.

With the frame offline Tailscale is gone too, so this is the one way back without visiting. Over the
phone it comes down to "hold the back button and do what the screen says". The dashboard's Frame > Wi-Fi
networks sets a line for that screen, such as who to call.

## Packages

The code runs on apt's Python packages; there is no pip or venv on the Pi (see
`CLAUDE.md`). `scripts/flash-sd.sh` installs these on first boot. On a Pi set up another
way, or if first boot had no internet, install them by hand:

```bash
sudo apt update && sudo apt install -y --no-install-recommends i2c-tools python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio python3-skyfield python3-flask python3-segno
```

`python3-skyfield` (sunrise/sunset and the sky chart) and `python3-flask` (the dashboard)
are new for the frame's screens and dashboard, and `python3-segno` draws the Wi-Fi setup screen's QR code
(without it that screen still gives the name and password in words). Check with
`python3 -c "import skyfield, flask; print(skyfield.__version__)"`.

`--no-install-recommends` matters: `python3-skyfield` *recommends* matplotlib, astropy,
pandas and IPython, which the frame never imports. Without the flag apt installs about
140 packages (roughly 95 MB) for nothing. `scripts/flash-sd.sh` (cloud-init) still
installs recommends; that only costs disk space and first-boot time.

The JPL ephemeris (`data/de421.bsp`) isn't installed with apt: run
`scripts/fetch-ephemeris.sh` on the Mac, and `scripts/deploy.sh` copies it to the Pi.

## Remote access (Tailscale)

Tailscale puts the Pi on the Owner's private network ("tailnet"), so the dashboard and
SSH work from anywhere without opening ports on the Viewer's router. The dashboard stays
hosted on the Pi; Tailscale only carries the traffic, encrypted end to end. Set up on
2026-11-04 with Tailscale 1.104.5 from Tailscale's own apt repo (not in Debian):

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install -y --no-install-recommends tailscale
sudo tailscale up --hostname=InkFrame   # prints a sign-in link for the Owner to open
```

- Name on the tailnet: `inkframe` (MagicDNS), full name `inkframe.<tailnet>.ts.net`.
  `tailscaled` uses about 19 MB of RAM.
- **Key expiry must be off** for this device (admin console → Machines → photopainter →
  ⋯ → Disable key expiry). Otherwise it drops off the tailnet 180 days after sign-in, while
  it's at the Viewer's house.
- Other devices (the Owner's phone and Mac) need the Tailscale app, signed in to the same
  account. Then the dashboard is `http://inkframe` and SSH is
  `ssh pi@inkframe.<tailnet>.ts.net` with the usual key.
- No keys or auth tokens are stored in this repo. Re-joining after a re-flash means running
  `tailscale up` again and approving the new device.
- Never use Tailscale Funnel or port forwarding for the dashboard: it has no login in v1.

## After SSH works (next steps)

1. Update packages (see [Packages](#packages)), then enable SPI and I2C.
2. Run `i2cdetect -y 1` and confirm the INA219 at `0x43` and the DS3231 at `0x68`.
3. Run Waveshare's `epd_7in3e_test.py` to confirm the panel works.
4. Measure power draw while running, halted and off. This answers the wake-up question in
   `docs/hardware.md`.


## Renaming the frame

The dashboard's Frame > Frame name changes the address at once, with no reboot: the Pi's
hostname, its `.local` name and its Tailscale name (`scripts/pi/set-frame-name`). The page
moves itself to the new address. Capitals are kept for show; addresses ignore them.

- The old address stops working, so update any bookmarks.
- `~/.ssh/config` on the Mac names the Pi too: change its `HostName` to the new name. The
  `photopainter` alias, and the `HostKeyAlias` that keeps the Pi's key recognized, stay.
- A name set by hand in the Tailscale admin console wins over this one there.
