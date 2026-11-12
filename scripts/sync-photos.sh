#!/usr/bin/env bash
# Copy photos from the Mac to the Pi's photo folder (~/photos).
#
# Usage: scripts/sync-photos.sh [--mirror] [--dry-run] <photo or folder>...
#   scripts/sync-photos.sh photos/
#   scripts/sync-photos.sh ~/Downloads/IMG_1234.HEIC ~/Downloads/beach.jpg
#   scripts/sync-photos.sh --mirror --dry-run photos/     # what a mirror would delete
#
# By default it only adds and updates: photos already on the Pi stay, including ones
# uploaded from the dashboard. A photo with the same name as one given here is replaced.
#
#   --mirror    make ~/photos exactly the photos given: delete the Pi's other photos,
#               dashboard uploads included (check with --dry-run first)
#   --dry-run   show what would be copied or deleted, change nothing
#
# Dot-files are never copied or deleted, even with --mirror: the dashboard's trash
# (~/photos/.trash/), its photo order (.order.json) and half-written uploads stay put.
# Photos new to the Pi go after the ones the order names, by name.
#
# Each photo is converted to a JPEG of at most 1600 px on its long side with macOS's
# built-in `sips` (this also handles iPhone HEIC). The originals are not changed.
#
# PHOTOPAINTER_HOST picks the Pi (default: photopainter). PHOTOPAINTER_SYNC_DEST syncs
# into that local folder instead of the Pi (tests; no SSH).
set -euo pipefail

HOST=${PHOTOPAINTER_HOST:-photopainter}
REMOTE_DIR=photos
MAX_EDGE=1600
LOCAL_DEST=${PHOTOPAINTER_SYNC_DEST:-}

usage() { echo "usage: $0 [--mirror] [--dry-run] <photo or folder>..." >&2; exit 1; }

mirror=0
dry_run=0
sources=()
for arg in "$@"; do
  case $arg in
    --mirror) mirror=1 ;;
    --dry-run|-n) dry_run=1 ;;
    -h|--help) usage ;;
    -*) echo "error: unknown option $arg" >&2; usage ;;
    *) sources+=("$arg") ;;
  esac
done
[[ ${#sources[@]} -ge 1 ]] || usage

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

count=0
add() {
  local src=$1 name
  name=$(basename "${src%.*}").jpg
  if [[ -e $staging/$name ]]; then
    echo "error: two photos would both be named $name" >&2
    exit 1
  fi
  sips -s format jpeg -s formatOptions 90 -Z "$MAX_EDGE" "$src" --out "$staging/$name" >/dev/null
  count=$((count + 1))
  echo "  $(basename "$src") -> $name"
}

for arg in "${sources[@]}"; do
  if [[ -d $arg ]]; then
    while IFS= read -r -d '' f; do add "$f"; done < <(
      find "$arg" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
        -o -iname '*.heic' -o -iname '*.heif' -o -iname '*.tif' -o -iname '*.tiff' \) -print0)
  elif [[ -f $arg ]]; then
    add "$arg"
  else
    echo "error: not found: $arg" >&2
    exit 1
  fi
done
[[ $count -gt 0 ]] || { echo "error: no photos found" >&2; exit 1; }

# '.*' keeps dot-files out of the copy and, with --delete, safe from it (excluded files
# are never deleted): the dashboard's .trash/, .order.json and temp files.
opts=(-rt --exclude '.*')
[[ $mirror -eq 1 ]] && opts+=(--delete)
[[ $dry_run -eq 1 ]] && opts+=(--dry-run --itemize-changes)

if [[ -n $LOCAL_DEST ]]; then
  dest=$LOCAL_DEST
  where=$LOCAL_DEST
  [[ $dry_run -eq 1 ]] || mkdir -p "$dest"
else
  dest="$HOST:$REMOTE_DIR"
  where="$HOST:~/$REMOTE_DIR"
  [[ $dry_run -eq 1 ]] || ssh "$HOST" "mkdir -p $REMOTE_DIR"
fi
rsync "${opts[@]}" "$staging/" "$dest/"

mode=$([[ $mirror -eq 1 ]] && echo "mirrored" || echo "added or updated")
if [[ $dry_run -eq 1 ]]; then
  echo "Dry run: nothing changed on $where ($count photo(s) would be $mode)"
else
  echo "Synced $count photo(s) to $where ($mode)"
fi
