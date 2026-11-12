#!/usr/bin/env bash
# Copy this repo to the Pi, install the `photopainter` command there, and restart the
# dashboard if it's running so that it picks up the new code.
#
# Usage: scripts/deploy.sh
#   The host defaults to "photopainter" (from ~/.ssh/config); override with
#   PHOTOPAINTER_HOST=otherhost scripts/deploy.sh
#
# Deploys only from the `main` branch with everything committed, because rsync copies
# whatever folder it runs in (a task worktree's branch, uncommitted edits and all).
# Override for a deliberate one-off: PHOTOPAINTER_DEPLOY_ANY=1 scripts/deploy.sh
#
# On the Pi the code runs straight from ~/eink-photopainter/src using apt's Python
# packages (python3-pil, python3-numpy, python3-spidev, python3-gpiozero, python3-lgpio,
# python3-skyfield, python3-flask).
set -euo pipefail

HOST=${PHOTOPAINTER_HOST:-photopainter}
DEST=eink-photopainter
cd "$(dirname "$0")/.."

if [[ "${PHOTOPAINTER_DEPLOY_ANY:-}" != 1 ]]; then
  branch=$(git symbolic-ref --quiet --short HEAD || echo "(detached HEAD)")
  if [[ "$branch" != main ]]; then
    echo "deploy.sh: refusing to deploy from '$branch'; deploy from main (see PRD.md §7)." >&2
    echo "           Override: PHOTOPAINTER_DEPLOY_ANY=1 scripts/deploy.sh" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "deploy.sh: refusing to deploy uncommitted changes; commit or stash them first:" >&2
    git status --short >&2
    echo "           Override: PHOTOPAINTER_DEPLOY_ANY=1 scripts/deploy.sh" >&2
    exit 1
  fi
fi

# The Pi needs the JPL ephemeris for sunrise/sunset and the sky. It's gitignored, and
# rsync --delete would remove the Pi's copy if this checkout didn't have it.
if [[ ! -f data/de421.bsp ]]; then
  echo "deploy.sh: data/de421.bsp is missing; run scripts/fetch-ephemeris.sh first." >&2
  exit 1
fi

# --copy-unsafe-links: files linked in from outside the repo (e.g. a shared ephemeris
# download in data/) arrive on the Pi as real files, not dangling links.
# .claude/ holds the worktrees, inside the main checkout this script deploys from.
rsync -az --delete --copy-unsafe-links \
  --exclude .claude/ --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude .pytest_cache/ \
  --exclude '*.egg-info/' --exclude photos/ --exclude previews/ --exclude docs/images/ \
  --exclude .env --exclude config.toml --exclude .DS_Store \
  ./ "$HOST:$DEST/"

ssh "$HOST" "set -e
  chmod 755 $DEST/scripts/pi/photopainter
  sudo ln -sfn \"\$HOME/$DEST/scripts/pi/photopainter\" /usr/local/bin/photopainter
  photopainter --help >/dev/null
  sudo install -m 644 $DEST/deploy/systemd/*.service $DEST/deploy/systemd/*.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo install -D -m 644 $DEST/deploy/network/photopainter-captive.conf /etc/NetworkManager/dnsmasq-shared.d/photopainter-captive.conf
  python3 -c 'import segno' 2>/dev/null || echo 'No QR code on the Wi-Fi setup screen until: sudo apt install -y --no-install-recommends python3-segno'"

# Each tick starts fresh, but the dashboard and the buttons run the old code until they're
# restarted. Restarting one also kills any tick it started, which could be mid-refresh, so wait
# for those to finish.
ssh "$HOST" 'bash -s' <<'REMOTE'
set -e
restart_when_idle() {
  unit=$1 name=$2
  if ! systemctl is-active --quiet "$unit"; then
    echo "$name isn't running, so there's nothing to restart."
    return 0
  fi
  main=$(systemctl show -p MainPID --value "$unit")
  for _ in $(seq 240); do  # longer than the 180 s a refresh may take
    pgrep -P "$main" >/dev/null || break
    sleep 1
  done
  if pgrep -P "$main" >/dev/null; then
    echo "deploy.sh: the code is on the Pi, but $name is busy and still running the old code." >&2
    echo "           When it's idle: sudo systemctl restart $unit" >&2
    return 1
  fi
  sudo systemctl restart "$unit"
  echo "$name restarted."
}
restart_when_idle photopainter-web.service "The dashboard"
restart_when_idle photopainter-buttons.service "The buttons service"
REMOTE

echo "Deployed to $HOST:~/$DEST. Run it there as: photopainter"
echo "Units installed (not enabled). Run the frame's schedule: sudo systemctl enable --now photopainter-tick.timer"
echo "Once the buttons are wired: sudo systemctl enable --now photopainter-buttons.service"
echo "The 3-minute slideshow (photopainter-next) is retired. If the Pi still has it:"
echo "  sudo systemctl disable --now photopainter-next.timer && sudo rm /etc/systemd/system/photopainter-next.*"
