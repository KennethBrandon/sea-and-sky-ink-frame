#!/usr/bin/env bash
# Download the JPL DE421 ephemeris (about 17 MB) once per machine and link it into data/.
#
# Usage: scripts/fetch-ephemeris.sh
#
# The file goes to a shared folder outside the repo (default ~/.cache/photopainter-dev/,
# override with PHOTOPAINTER_EPHEMERIS_CACHE), so every checkout and worktree links to
# the same copy: run this once in each new worktree; only the first run downloads.
# data/de421.bsp is gitignored. scripts/deploy.sh copies the linked file to the Pi as a
# real file (rsync --copy-unsafe-links).
set -euo pipefail

FILE=de421.bsp
# Two JPL sources with the same ephemeris data in different file layouts, so each has its
# own checksum (both give identical positions; checked 2026-11-03).
URLS=(
  "https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/$FILE"
  "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/a_old_versions/$FILE"
)
SHA256S=(
  a20a7139da04cbc462454634918e9a9ca69127044e2cc9d4f9c16e238d2deedc
  08b20db2ae22488650641c5a9033e5bfda4b1c4b440cfeaf20f621cfa18ecdb3
)
CACHE=${PHOTOPAINTER_EPHEMERIS_CACHE:-$HOME/.cache/photopainter-dev}
cd "$(dirname "$0")/.."

die() { echo "fetch-ephemeris.sh: $*" >&2; exit 1; }

check() {  # check <file>: matches one of the known checksums
  local sum
  sum=$( (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1") | cut -d' ' -f1)
  for known in "${SHA256S[@]}"; do [[ $sum == "$known" ]] && return 0; done
  return 1
}

mkdir -p "$CACHE"
if [[ -f "$CACHE/$FILE" ]] && check "$CACHE/$FILE"; then
  echo "Already downloaded: $CACHE/$FILE"
else
  tmp="$CACHE/.$FILE.download"
  ok=
  for url in "${URLS[@]}"; do
    echo "Downloading $url"
    if curl -fL --progress-bar --retry 2 --connect-timeout 20 -o "$tmp" "$url" && check "$tmp"; then
      ok=1
      break
    fi
    echo "  failed or checksum mismatch; trying the next source" >&2
  done
  [[ -n $ok ]] || { rm -f "$tmp"; die "couldn't download a valid $FILE"; }
  mv "$tmp" "$CACHE/$FILE"
fi

mkdir -p data
if [[ -e data/$FILE && ! -L data/$FILE ]]; then
  check "data/$FILE" || die "data/$FILE is a real file with the wrong checksum; delete it and rerun"
  echo "data/$FILE is already a real file; left as is"
else
  ln -sfn "$CACHE/$FILE" "data/$FILE"
  echo "Linked data/$FILE -> $CACHE/$FILE"
fi
