#!/usr/bin/env bash
# Show a photo from the Mac on the frame.
#
# Usage: scripts/show.sh <photo> [photopainter show options]
#   scripts/show.sh photos/beach.jpg
#   scripts/show.sh photos/IMG_1234.HEIC --fit fit --saturation 1.2
#
# The photo is streamed over SSH (nothing is stored on the Pi). iPhone HEIC/HEIF photos
# are converted to JPEG with macOS's built-in `sips` first.
set -euo pipefail

HOST=${PHOTOPAINTER_HOST:-photopainter}
[[ $# -ge 1 ]] || { echo "usage: $0 <photo> [show options, e.g. --fit fit --portrait]" >&2; exit 1; }
photo=$1
shift
[[ -f $photo ]] || { echo "error: not found: $photo" >&2; exit 1; }
name=$(basename "$photo")

case "$photo" in
  *.[Hh][Ee][Ii][CcFf])
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT
    sips -s format jpeg "$photo" --out "$tmpdir/photo.jpg" >/dev/null
    photo=$tmpdir/photo.jpg
    ;;
esac

extra=""
[[ $# -gt 0 ]] && extra=$(printf '%q ' "$@")
echo "Sending $name to $HOST; the refresh takes about 30 s..."
ssh "$HOST" "photopainter show - $extra" <"$photo"
