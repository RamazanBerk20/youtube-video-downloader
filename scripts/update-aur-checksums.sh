#!/usr/bin/env bash
# After the release for the pkgver in packaging/aur is published: download the
# real sources, fill in their sha256sums (replacing TODO_AFTER_RELEASE) and
# regenerate .SRCINFO in every AUR package directory.
# Needs updpkgsums (pacman-contrib) and makepkg. Writes only inside packaging/aur.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/packaging/aur"

for pkg in youtube-video-downloader youtube-video-downloader-bin; do
  (cd "$pkg" && updpkgsums)
done
for pkg in youtube-video-downloader youtube-video-downloader-bin youtube-video-downloader-git; do
  (cd "$pkg" && makepkg --printsrcinfo > .SRCINFO)
  if grep -q TODO_AFTER_RELEASE "$pkg/PKGBUILD" "$pkg/.SRCINFO"; then
    printf 'placeholder checksum left in %s\n' "$pkg" >&2
    exit 1
  fi
done
printf 'AUR checksums and .SRCINFO updated for v%s\n' \
  "$(sed -n 's/^pkgver=//p' youtube-video-downloader/PKGBUILD)"
