#!/usr/bin/env bash
# Flash Raspberry Pi OS Lite to an SD card (macOS) and preconfigure it for the PhotoPainter.
#
# Usage:
#   scripts/flash-sd.sh <disk> <image.img.xz>   erase the card, write the image, configure it
#   scripts/flash-sd.sh --config-only           only configure an already-flashed card
#                                               (mounted at /Volumes/bootfs)
# Example:
#   scripts/flash-sd.sh disk3 ~/Downloads/2026-10-06-raspios-trixie-arm64-lite.img.xz
#
# Get the image from https://downloads.raspberrypi.com/raspios_lite_arm64_latest
# and find the disk with `diskutil list external physical`.
#
# The card is set up with: hostname, user (SSH-key login only, passwordless sudo),
# Wi-Fi, time zone, keyboard, SSH, SPI + I2C, and the apt packages the frame needs
# (installed on first boot, so the Pi must reach the internet then). The script asks for:
#   - the disk name again, to confirm the erase
#   - the Wi-Fi name and password, which go only into the card's network-config file
#   - your Mac password (sudo), for the raw disk write
# Works with macOS's built-in bash 3.2.
set -euo pipefail

HOSTNAME_PI=InkFrame  # the dashboard can change it later: Settings > Frame name
USER_PI=pi
PUBKEY_FILE="$HOME/.ssh/photopainter_ed25519.pub"
WIFI_COUNTRY=US
TIMEZONE=America/Los_Angeles
KEYMAP=us
BOOT=/Volumes/bootfs

die() { echo "error: $*" >&2; exit 1; }
yaml_quote() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

ask_wifi() {
  read -rp "Wi-Fi network name (must be 2.4 GHz capable): " WIFI_SSID
  read -rsp "Wi-Fi password (not shown): " WIFI_PSK
  echo
  [[ -n $WIFI_SSID && -n $WIFI_PSK ]] || die "Wi-Fi name and password are required"
}

# Write cloud-init first-boot config (Raspberry Pi OS Trixie and later).
# Format per https://www.raspberrypi.com/news/cloud-init-on-raspberry-pi-os/
# The password is locked: login is by SSH key only. Run `sudo passwd pi` on the Pi
# if you want a console password.
write_config() {
  [[ -f $BOOT/cmdline.txt && -f $BOOT/meta-data ]] || die "$BOOT doesn't look like a Raspberry Pi OS (Trixie+) boot partition"
  local pubkey
  pubkey=$(<"$PUBKEY_FILE")

  cat >"$BOOT/user-data" <<EOF
#cloud-config
# PhotoPainter first-boot configuration, written by scripts/flash-sd.sh
hostname: $HOSTNAME_PI
manage_etc_hosts: true
timezone: $TIMEZONE
keyboard:
  layout: $KEYMAP
users:
  - name: $USER_PI
    groups: users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $pubkey
    sudo: ALL=(ALL) NOPASSWD:ALL
enable_ssh: true
ssh_pwauth: false
# The code runs on apt's Python packages (no pip on the Pi); see docs/pi-setup.md.
package_update: true
packages:
  - i2c-tools
  - python3-pil
  - python3-numpy
  - python3-spidev
  - python3-gpiozero
  - python3-lgpio
  - python3-skyfield
  - python3-flask
  - python3-segno
runcmd:
  - [raspi-config, nonint, do_spi, "0"]
  - [raspi-config, nonint, do_i2c, "0"]
EOF

  cat >"$BOOT/network-config" <<EOF
network:
  version: 2
  wifis:
    renderer: NetworkManager
    wlan0:
      dhcp4: true
      regulatory-domain: "$WIFI_COUNTRY"
      access-points:
        $(yaml_quote "$WIFI_SSID"):
          password: $(yaml_quote "$WIFI_PSK")
      optional: true
EOF
  WIFI_PSK=
  sync
  echo "Wrote user-data and network-config to $BOOT."
}

finish() {
  local disk=$1
  diskutil eject "/dev/$disk"
  echo
  echo "Done. Put the card in the Pi, power the Waveshare board over USB-C, wait 3-5 minutes, then:"
  echo "  ssh photopainter"
}

[[ -f $PUBKEY_FILE ]] || die "missing $PUBKEY_FILE (create it: ssh-keygen -t ed25519 -f ${PUBKEY_FILE%.pub})"

if [[ ${1:-} == --config-only ]]; then
  [[ -d $BOOT ]] || die "$BOOT is not mounted; insert the flashed card first"
  disk=$(diskutil info "$BOOT" | sed -n 's/.*Part of Whole: *//p')
  [[ $disk =~ ^disk[0-9]+$ ]] || die "couldn't find the disk behind $BOOT"
  ask_wifi
  write_config
  finish "$disk"
  exit 0
fi

[[ $# -eq 2 ]] || die "usage: $0 <disk, e.g. disk3> <image.img.xz>   or   $0 --config-only"
DISK=${1#/dev/}
IMAGE=$2
[[ $DISK =~ ^disk[0-9]+$ ]] || die "disk must look like disk3"
[[ -f $IMAGE ]] || die "image not found: $IMAGE"
command -v xz >/dev/null || die "xz not found (brew install xz)"

# Safety: only an external, removable, physical disk of a plausible SD card size.
info=$(diskutil info "$DISK") || die "no such disk: $DISK"
grep -q "Device Location: *External" <<<"$info" || die "$DISK is not an external disk"
grep -q "Removable Media: *Removable" <<<"$info" || die "$DISK is not removable media"
grep -q "Virtual: *No" <<<"$info" || die "$DISK is a virtual disk"
bytes=$(sed -n 's/.*Disk Size:.*(\([0-9]*\) Bytes).*/\1/p' <<<"$info")
(( ${bytes:-0} > 4000000000 && ${bytes:-0} < 260000000000 )) || die "$DISK size looks wrong for an SD card (${bytes:-?} bytes)"
[[ -e $BOOT ]] && die "$BOOT is already mounted; eject other Raspberry Pi cards first"

echo
diskutil list "$DISK"
echo
read -rp "This ERASES everything on /dev/$DISK. Type $DISK to continue: " ok
[[ $ok == "$DISK" ]] || die "aborted, nothing was written"

ask_wifi

echo "Your Mac password is needed for the raw disk write."
sudo -v

diskutil unmountDisk "/dev/$DISK"
echo "Writing $(basename "$IMAGE") to /dev/$DISK. This takes several minutes; press Ctrl-T for progress."
xz -dc "$IMAGE" | sudo dd of="/dev/r$DISK" bs=4m
sync
echo "Write finished. If macOS says a disk is not readable, click Ignore (that's the Linux partition)."

diskutil mountDisk "/dev/$DISK" >/dev/null 2>&1 || true
for _ in $(seq 1 30); do [[ -d $BOOT ]] && break; sleep 1; done
if [[ ! -d $BOOT ]]; then
  WIFI_PSK=
  die "the card was written, but $BOOT didn't mount. Unplug and replug the card reader, then run: $0 --config-only"
fi

write_config
finish "$DISK"
