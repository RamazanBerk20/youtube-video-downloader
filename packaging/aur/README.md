# YouTube Video Downloader AUR packages

Each directory mirrors one AUR package base:

- `youtube-video-downloader`: stable release built from the tag tarball.
- `youtube-video-downloader-bin`: the prebuilt `.deb` from the GitHub release,
  repackaged.
- `youtube-video-downloader-git`: latest `main` branch built from source.

The stable and `-bin` packages carry `TODO_AFTER_RELEASE` in `sha256sums` until
the release exists. After tagging and publishing `v<pkgver>`, run
`scripts/update-aur-checksums.sh`: it fills the checksums with `updpkgsums` and
regenerates every `.SRCINFO` with `makepkg --printsrcinfo`.

Then validate the package in a clean environment and push only `PKGBUILD` and
`.SRCINFO` to the matching AUR Git repository. For a new release, bump `pkgver`
(and reset `pkgrel=1`) in the stable and `-bin` packages first.
