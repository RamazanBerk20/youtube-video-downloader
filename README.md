# YouTube Video Downloader

A cross-platform desktop GUI for downloading YouTube videos and audio, built on
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and
[`customtkinter`](https://github.com/TomSchimansky/CustomTkinter). Runs on
Linux and Windows.

## Features

- **Video or audio (mp3)** — toggle with a single radio button
- **Quality selector** — Best / 4K / 1440p / 1080p / 720p / 480p / 360p / Worst
  for video; 320 / 256 / 192 / 128 / 96 kbps for audio
- **Playlist support** — entire playlists download into a folder named after
  the playlist, with zero-padded index prefixes
- **Live progress** — progress bar, current filename, speed, ETA, and
  playlist position `(3 / 24)`
- **Cancellable** — cooperative cancel via the Cancel button
- **Session log** — timestamped panel showing each completed file and any
  warnings/errors
- **One-click open** — opens the output folder when the download finishes
- **Dark theme** — CustomTkinter, looks the same on both OSes

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) on `PATH` (required for audio extraction and
  for merging high-quality video+audio streams)

## Install & Run

### Linux / macOS

```bash
git clone https://github.com/<your-user>/youtube-video-downloader.git
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

```bat
git clone https://github.com/<your-user>/youtube-video-downloader.git
cd youtube-video-downloader
start.bat
```

Or just double-click `start.bat`. Install `ffmpeg` first with
`winget install Gyan.FFmpeg` or grab a build from
<https://www.gyan.dev/ffmpeg/builds/>.

## Usage

1. Paste a YouTube video or playlist URL into the **URL** field
2. Pick an output folder (defaults to `~/Downloads`)
3. Choose **Video** or **Audio (mp3)** and a quality
4. Check **Download as playlist** if the URL is a playlist
5. Click **Download**

Files are named `<title> [<id>].<ext>`; playlist entries land in a subfolder
named after the playlist.

## Project layout

```
app.py             # CustomTkinter GUI
downloader.py      # Background yt-dlp worker (thread + event queue)
requirements.txt   # yt-dlp, customtkinter
start.sh           # Linux/macOS launcher
start.bat          # Windows launcher
LICENSE            # MIT
```

## License

[MIT](LICENSE) © 2026 ramazanberksirin
