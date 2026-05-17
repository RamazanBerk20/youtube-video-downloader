# YouTube Video Downloader

A cross-platform desktop GUI for downloading YouTube videos and audio, built on
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and
[`customtkinter`](https://github.com/TomSchimansky/CustomTkinter). Runs on
Linux and Windows.

## Features

- **Concurrent downloads** — paste many URLs (one per line), pick a max-
  simultaneous count (1–5), and they run in parallel with a per-task
  progress row
- **English + Türkçe** — toggle EN / TR in the top-right; the choice is
  remembered. Auto-detects from your system locale on first launch.
- **Video or audio (mp3)** — toggle with a single radio button
- **Quality selector** — Best / 4K / 1440p / 1080p / 720p / 480p / 360p /
  Worst for video; 320 / 256 / 192 / 128 / 96 kbps for audio
- **Playlist support** — entire playlists download into a folder named after
  the playlist, with zero-padded index prefixes
- **Per-task cancel + Cancel all + Clear finished** — fine-grained control
  over the queue
- **Session log** — timestamped panel showing each completed file and any
  warnings/errors
- **Remembers preferences** — language, max-concurrent, output folder,
  format, quality, playlist toggle persist in a config file:
  `%APPDATA%\youtube-downloader\config.json` (Windows) /
  `~/.config/youtube-downloader/config.json` (Linux) /
  `~/Library/Application Support/youtube-downloader/config.json` (macOS)
- **Dark theme** — CustomTkinter, identical on both OSes

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) on `PATH` (required for audio extraction and
  for merging high-quality video+audio streams)

## Install & Run

### Linux / macOS

```bash
git clone https://github.com/RamazanBerk20/youtube-video-downloader.git
cd youtube-video-downloader
./start.sh
```

The launcher creates a `.venv/`, installs `yt-dlp` and `customtkinter` on
first run, then starts the GUI. Subsequent launches skip the setup.

Make sure `ffmpeg` is installed first:

```bash
sudo pacman -S ffmpeg          # Arch / CachyOS
sudo apt install ffmpeg        # Debian / Ubuntu
brew install ffmpeg            # macOS
```

### Windows

1. Install **Python 3.10+** from <https://www.python.org/downloads/> — make
   sure to tick **"Add python.exe to PATH"** in the installer. (If you skip
   this step, double-clicking `start.bat` will open the Microsoft Store
   instead of running.)
2. Install **ffmpeg**:
   ```bat
   winget install Gyan.FFmpeg
   ```
   or grab a build from <https://www.gyan.dev/ffmpeg/builds/> and add its
   `bin\` folder to `PATH`.
3. Clone and launch:
   ```bat
   git clone https://github.com/RamazanBerk20/youtube-video-downloader.git
   cd youtube-video-downloader
   start.bat
   ```
   Or just double-click `start.bat` after cloning.

If you previously got a *"Windows doesn't support this version"* dialog, it
means the bundled Microsoft Store stub ran instead of a real Python.
Install Python from the link above and try again, or disable the alias at
**Settings → Apps → Advanced app settings → App execution aliases**.

## Usage

1. Paste one or more YouTube URLs into the **URL(s)** textbox — one per line
2. Pick an output folder (defaults to `~/Downloads`)
3. Choose **Video** or **Audio (mp3)** and a quality
4. Tick **Download as playlist** if a URL is a playlist
5. Pick **Max concurrent** (1–5)
6. Click **Add to queue** — downloads start immediately

Each download gets its own row showing status, progress, speed and ETA, plus
a per-task cancel button. Use **Cancel all** to stop everything in flight,
or **Clear finished** to remove completed rows.

Files are named `<title> [<id>].<ext>`; playlist entries land in a subfolder
named after the playlist.

## Project layout

```
app.py             # CustomTkinter GUI: queue list, language toggle, polling loop
downloader.py      # DownloadManager + DownloadTask (concurrent yt-dlp threads)
i18n.py            # English + Turkish translation tables
settings.py        # JSON-backed user preferences
requirements.txt   # yt-dlp, customtkinter
start.sh           # Linux/macOS launcher
start.bat          # Windows launcher
LICENSE            # MIT
```

## License

[MIT](LICENSE) © 2026 ramazanberksirin
