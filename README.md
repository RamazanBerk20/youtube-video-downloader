# YouTube Video Downloader

A small, fast cross-platform desktop app for downloading YouTube videos and
audio, built on [`yt-dlp`](https://github.com/yt-dlp/yt-dlp). Runs on Linux and
Windows. It is written in Rust with [Tauri 2](https://tauri.app/) and a
React/TypeScript UI (it was a Python/CustomTkinter app before version 2).

## Features

- **Concurrent downloads** — paste many URLs (one per line), pick a max-
  simultaneous count (1–5, 10, `∞`, or a custom number), and they run in
  parallel with a per-task progress row
- **Metadata preview** — title, channel, duration and thumbnail are shown
  before you queue a URL
- **Multilingual UI** — English, Türkçe, Español, Français, Deutsch, Русский,
  العربية, 中文, 日本語 and 한국어 (10 languages); the choice is remembered and
  auto-detected from your system locale on first launch
- **System, light or dark theme**
- **Video or audio** — toggle with a single radio button
- **Codec selector** — for video: Auto (mp4 if possible, else webm/mkv) /
  MP4 (H.264) / WebM (VP9) / WebM (AV1). For audio: m4a (AAC, default) /
  mp3 / opus / flac / wav / Original (no transcoding)
- **Quality selector** — Best / 4K / 1440p / 1080p / 720p / 480p / 360p /
  Worst for video; Best / 320 / 256 / 192 / 128 / 96 kbps for lossy audio
  (disabled for flac/wav/Original)
- **Container and compatibility controls** — keep the original container,
  remux to MP4/MKV/WebM/MOV, or transcode to legacy-friendly AVI/FLV/MPEG/WMV;
  the compatibility re-encode picks codecs that fit the container, with
  hardware acceleration where available
- **Browser cookies + JS runtime support** — uses browser cookies when YouTube
  asks for sign-in, falls back to a `cookies.txt` export when Chromium cookie
  decryption is blocked, and checks for Deno/Node/Bun for yt-dlp's YouTube
  anti-bot challenge
- **Automatic rate-limit rescue** — when cookie retries fail, the app can
  activate a configured anyIP proxy for queued/future downloads and retry the
  failed task once
- **Managed, self-updating yt-dlp** — the app downloads the official yt-dlp
  release itself and keeps it current; no Python or pip involved
- **Playlist support** — playlists download into a folder named after the
  playlist, with zero-padded index prefixes
- **Cancel in every phase** (fetching, downloading, merging, encoding) **and
  retry per row**, plus Cancel all and Clear finished
- **Optional extras** (all off by default) — restore the queue on launch, a
  download archive that skips videos you already have, embedding of subtitles /
  chapters / thumbnail / metadata, and SponsorBlock
- **Session log** — timestamped panel with each completed file and any
  warnings/errors
- **Remembers preferences** — language, theme, concurrency, output folder,
  format, quality, codecs, container, playlist toggle, fragments, cookie
  source, cookie file and anyIP rescue settings
- **In-app updates** — checks this repository's releases; AppImage and Windows
  installs can update in place after a SHA-256 check

## Requirements

- [`ffmpeg`](https://ffmpeg.org/) on `PATH` (audio extraction, merging, encoding).
  If it is missing the app shows a banner that guides you to install it.
- A JavaScript runtime on `PATH` — Deno recommended, Node/Bun also work (needed
  by yt-dlp for YouTube's anti-bot challenge solver)

Python is **not** needed any more, and neither is a separate yt-dlp install.

```bash
sudo pacman -S ffmpeg          # Arch / CachyOS
sudo apt install ffmpeg        # Debian / Ubuntu
winget install Gyan.FFmpeg     # Windows
```

## Install

### Arch Linux (AUR)

Three packages, all installing the same files and conflicting with each other:

```bash
yay -S youtube-video-downloader        # stable release, built from source
yay -S youtube-video-downloader-bin    # stable release, prebuilt (repackaged .deb)
yay -S youtube-video-downloader-git    # latest main, built from source
```

`ffmpeg` is a dependency. `deno` (or `nodejs`/`bun`) and a secret service such
as `gnome-keyring` are optional. AUR installs are updated by your package
manager; the in-app update check only links to the release page.

### Release files

Download from the [releases page](https://github.com/RamazanBerk20/youtube-video-downloader/releases/latest)
and, if you like, check the file against `SHA256SUMS`:

- **Linux AppImage** (`YouTube-Downloader_2.0.1_amd64.AppImage`) — `chmod +x`
  it and run it. Updates itself in place.
- **Linux `.deb`** (`YouTube-Downloader_2.0.1_amd64.deb`) —
  `sudo apt install ./<file>.deb`. `ffmpeg` is installed as a recommended
  package. Updates come from new releases (the app only links to them).
- **Windows installer** (`YouTube-Downloader_2.0.1_x64-setup.exe`) — updates
  itself by downloading the newer installer.

Releases are **not code-signed** (there are no signing keys), so Windows
SmartScreen will warn ("Windows protected your PC" → More info → Run anyway).
`SHA256SUMS` is the integrity check.

## Build from source

Needs Node 22+, [pnpm](https://pnpm.io/), a recent stable Rust toolchain, and on
Linux the Tauri 2 system packages (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`,
`librsvg2-dev`, `libglib2.0-dev`, `libdbus-1-dev`, `patchelf`).

```bash
pnpm install
pnpm tauri build          # bundles land in src-tauri/target/release/bundle/
pnpm tauri dev            # run with hot reload
```

## Settings and data

| | Settings | Queue / archive / yt-dlp |
|---|---|---|
| Linux | `~/.config/youtube-downloader/` | `~/.local/share/youtube-downloader/` |
| Windows | `%APPDATA%\youtube-downloader\` | `%LOCALAPPDATA%\youtube-downloader\` |

The settings directory is the one the Python version used. The old
`config.json` is **migrated automatically on first run**; it is only read, and
left in place.

## Usage

1. Paste one or more YouTube URLs — one per line
2. Pick an output folder (defaults to `~/Downloads`)
3. Choose **Video** or **Audio**, then a **Codec** and a **Quality**
4. Tick **Download as playlist** if a URL is a playlist
5. Pick **Max concurrent**
6. Click **Add to queue** — downloads start immediately

Files are named `<title> [<id>].<ext>`; playlist entries land in a subfolder
named after the playlist.

If Chromium-family browser cookies fail on Windows, this is usually Chrome /
Edge / Brave App-Bound Encryption blocking yt-dlp's decrypt path. Use Firefox
cookies, or export YouTube cookies to a Netscape-format `cookies.txt` file and
select it in the app.

## Coming from the Python version

Deliberate behaviour changes (full list in [`docs/PARITY.md`](docs/PARITY.md)):

- The queue is only restored (and auto-started) at launch if you enable
  **restore queue**; it is off by default.
- Container MP4/MOV without re-encode now converts Opus audio to AAC (video is
  still copied) instead of stream-copying Opus into MP4.
- No separate video-convert pass and no ac3 audio extraction (bug fixes); playlists
  keep going past an unavailable entry.
- yt-dlp options were compared mechanically with the old app's across 9,288
  combinations; format selector, output template, retries, cookies and
  JS-runtime settings are identical.
- The Python files (`app.py`, `start.sh`, …) are no longer used.

## Development

```bash
pnpm dev          # UI only, at http://localhost:1420
pnpm test         # vitest
pnpm build        # tsc -b && vite build
cargo test --manifest-path src-tauri/Cargo.toml --all-targets --locked
pnpm check        # all of the above
```

Outside Tauri (plain `pnpm dev` in a browser) the UI runs against an in-browser
mock backend (`src/lib/mockBackend.ts`). Words in a URL script its behaviour:
none = normal download, `list` = 3-item playlist, `fail` = unavailable video,
`bot` = bot check rescued with cookies, `botx` = bot check never rescued,
`noformat` = format not available (JS-runtime banner), `slow` = 10× slower
(for testing cancel). CI (`.github/workflows/ci.yml`) runs type-check, tests,
build, `cargo fmt`, `clippy -D warnings` and `cargo test` on Linux and
Windows. Pushing a `v*` tag runs `release.yml`.

## Support

This project is free. If you want to support its development, you can do so
through [GitHub Sponsors](https://github.com/sponsors/RamazanBerk20).

## License

[MIT](LICENSE) © 2026 ramazanberksirin
