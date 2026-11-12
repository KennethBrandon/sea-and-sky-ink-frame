#!/usr/bin/env bash
# Skip to the next photo in the frame's slideshow right now (from the Mac).
#
# Usage: scripts/next.sh [photopainter next options]
# The photo takes about 33 s to appear; the slideshow then continues in order from it.
set -euo pipefail

HOST=${PHOTOPAINTER_HOST:-photopainter}
extra=""
[[ $# -gt 0 ]] && extra=$(printf '%q ' "$@")
echo "Showing the next photo on $HOST; it takes about 33 s to appear..."
ssh "$HOST" "photopainter next $extra"
