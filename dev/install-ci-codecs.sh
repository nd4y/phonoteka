#!/usr/bin/env bash
set -euo pipefail

codecs_ready() {
  ffmpeg -version >/dev/null 2>&1 &&
    ffprobe -version >/dev/null 2>&1 &&
    fpcalc -version >/dev/null 2>&1 &&
    python3 -c 'import ctypes; ctypes.CDLL("libebur128.so.1")' >/dev/null 2>&1
}

if codecs_ready; then
  echo 'Media codecs are already available; package installation is unnecessary.'
  exit 0
fi

options=(-o Acquire::Retries=1 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
if [[ -n "${CODEC_APT_SOURCES:-}" ]]; then
  if [[ ! -r "$CODEC_APT_SOURCES" ]]; then
    echo 'Configured CI package source list is unavailable.' >&2
    exit 1
  fi
  options+=(-o "Dir::Etc::sourcelist=$CODEC_APT_SOURCES" -o Dir::Etc::sourceparts=-)
fi
packages=(ffmpeg libebur128-1 libchromaprint-tools)
echo 'Installing media codecs using the runner package indexes.'
if ! sudo timeout --kill-after=10s 180s apt-get "${options[@]}" install -y --no-install-recommends "${packages[@]}"; then
  echo 'Initial installation failed; refreshing package indexes once.'
  sudo timeout --kill-after=10s 180s apt-get "${options[@]}" update
  sudo timeout --kill-after=10s 180s apt-get "${options[@]}" install -y --no-install-recommends "${packages[@]}"
fi

if ! codecs_ready; then
  echo 'Required media tools or libebur128 are still unavailable.' >&2
  exit 1
fi
echo 'Media tools and libebur128 are available; all media tests remain required.'
