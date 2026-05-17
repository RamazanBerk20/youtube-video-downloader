"""YouTube Downloader — CustomTkinter GUI (Windows + Linux)."""
from __future__ import annotations

import datetime
import os
import queue
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from downloader import (
    DownloadJob,
    DownloadWorker,
    audio_quality_options,
    video_quality_options,
)


DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
POLL_INTERVAL_MS = 120


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("YouTube Downloader")
        self.geometry("760x660")
        self.minsize(640, 560)

        self.worker = DownloadWorker()
        self._last_output_dir: Path | None = None

        self._build_ui()
        self._poll_events()

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        header = ctk.CTkLabel(
            self,
            text="YouTube Downloader",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        header.grid(row=0, column=0, padx=20, pady=(18, 8), sticky="w")

        # URL row
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        url_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(url_frame, text="URL", width=70, anchor="w").grid(row=0, column=0, padx=(0, 8))
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(
            url_frame,
            textvariable=self.url_var,
            placeholder_text="Paste a YouTube video or playlist URL…",
        )
        self.url_entry.grid(row=0, column=1, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._start_download())
        self.paste_btn = ctk.CTkButton(url_frame, text="Paste", width=80, command=self._paste)
        self.paste_btn.grid(row=0, column=2, padx=(8, 0))

        # Output folder row
        out_frame = ctk.CTkFrame(self, fg_color="transparent")
        out_frame.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
        out_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(out_frame, text="Save to", width=70, anchor="w").grid(row=0, column=0, padx=(0, 8))
        self.out_var = tk.StringVar(value=str(DEFAULT_DOWNLOAD_DIR))
        self.out_entry = ctk.CTkEntry(out_frame, textvariable=self.out_var)
        self.out_entry.grid(row=0, column=1, sticky="ew")
        self.browse_btn = ctk.CTkButton(out_frame, text="Browse", width=80, command=self._browse)
        self.browse_btn.grid(row=0, column=2, padx=(8, 0))

        # Options row
        opts = ctk.CTkFrame(self)
        opts.grid(row=3, column=0, padx=20, pady=10, sticky="ew")
        opts.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(opts, text="Format").grid(row=0, column=0, padx=(14, 8), pady=12)
        self.mode_var = tk.StringVar(value="video")
        self.video_rb = ctk.CTkRadioButton(
            opts, text="Video", variable=self.mode_var, value="video", command=self._on_mode_change
        )
        self.audio_rb = ctk.CTkRadioButton(
            opts, text="Audio (mp3)", variable=self.mode_var, value="audio", command=self._on_mode_change
        )
        self.video_rb.grid(row=0, column=1, padx=6, pady=12)
        self.audio_rb.grid(row=0, column=2, padx=6, pady=12)

        ctk.CTkLabel(opts, text="Quality").grid(row=0, column=3, padx=(22, 8), pady=12)
        self.quality_var = tk.StringVar(value=video_quality_options()[0])
        self.quality_menu = ctk.CTkOptionMenu(
            opts, variable=self.quality_var, values=video_quality_options(), width=150
        )
        self.quality_menu.grid(row=0, column=4, padx=6, pady=12)

        self.playlist_var = tk.BooleanVar(value=False)
        self.playlist_chk = ctk.CTkCheckBox(opts, text="Download as playlist", variable=self.playlist_var)
        self.playlist_chk.grid(row=0, column=5, padx=(20, 14), pady=12, sticky="e")

        # Action row
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, padx=20, pady=(4, 8), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)

        self.download_btn = ctk.CTkButton(
            actions, text="Download", height=40, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_download,
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.cancel_btn = ctk.CTkButton(
            actions, text="Cancel", height=40, width=130,
            fg_color="#a02929", hover_color="#7a1d1d",
            command=self._cancel_download, state="disabled",
        )
        self.cancel_btn.grid(row=0, column=1, padx=6)

        self.open_btn = ctk.CTkButton(
            actions, text="Open folder", height=40, width=130,
            command=self._open_folder, state="disabled",
        )
        self.open_btn.grid(row=0, column=2, padx=(6, 0))

        # Progress + log panel
        panel = ctk.CTkFrame(self)
        panel.grid(row=5, column=0, padx=20, pady=(8, 16), sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(3, weight=1)

        self.progress = ctk.CTkProgressBar(panel, height=16)
        self.progress.grid(row=0, column=0, padx=14, pady=(14, 6), sticky="ew")
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(panel, text="Idle.", anchor="w", justify="left")
        self.status_label.grid(row=1, column=0, padx=14, pady=(0, 2), sticky="ew")

        self.detail_label = ctk.CTkLabel(
            panel, text="", anchor="w", justify="left", text_color="#9aa0a6"
        )
        self.detail_label.grid(row=2, column=0, padx=14, pady=(0, 6), sticky="ew")

        mono = tkfont.nametofont("TkFixedFont").actual()["family"]
        self.log = ctk.CTkTextbox(panel, font=ctk.CTkFont(family=mono, size=11), wrap="word")
        self.log.grid(row=3, column=0, padx=14, pady=(6, 14), sticky="nsew")
        self.log.configure(state="disabled")

        self._log("Ready. Paste a URL and press Download.")

    # ---- Event handlers ---------------------------------------------------

    def _on_mode_change(self) -> None:
        opts = audio_quality_options() if self.mode_var.get() == "audio" else video_quality_options()
        self.quality_menu.configure(values=opts)
        self.quality_var.set(opts[0])

    def _paste(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text:
            self.url_var.set(text)

    def _browse(self) -> None:
        initial = self.out_var.get() or str(DEFAULT_DOWNLOAD_DIR)
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.out_var.set(path)

    def _start_download(self) -> None:
        if self.worker.is_running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please paste a YouTube URL first.")
            return

        out_dir = Path(self.out_var.get().strip() or DEFAULT_DOWNLOAD_DIR).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", f"Cannot create folder:\n{e}")
            return

        job = DownloadJob(
            url=url,
            output_dir=out_dir,
            mode=self.mode_var.get(),
            quality=self.quality_var.get(),
            playlist=self.playlist_var.get(),
        )
        self._last_output_dir = out_dir
        self._set_busy(True)
        self.progress.set(0)
        self.status_label.configure(text="Starting…")
        self.detail_label.configure(text="")
        self._log(f"▶  {url}")
        try:
            self.worker.start(job)
        except Exception as e:  # noqa: BLE001
            self._set_busy(False)
            messagebox.showerror("Download error", str(e))

    def _cancel_download(self) -> None:
        if self.worker.is_running:
            self.worker.cancel()
            self.status_label.configure(text="Cancelling…")

    def _open_folder(self) -> None:
        path = self._last_output_dir
        if not path or not path.exists():
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as e:
            messagebox.showerror("Open folder", str(e))

    # ---- Worker → GUI event pump -----------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.worker.events.get_nowait())
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._poll_events)

    def _handle_event(self, evt: dict) -> None:
        kind = evt.get("type")
        if kind == "progress":
            self.progress.set(max(0.0, min(1.0, evt["percent"] / 100.0)))
            idx, total = evt.get("index"), evt.get("total")
            counter = f"  ({idx}/{total})" if idx and total else ""
            self.status_label.configure(
                text=f"{evt['percent']:.1f}%  ·  {evt['filename']}{counter}"
            )
            self.detail_label.configure(text=f"Speed: {evt['speed']}    ETA: {evt['eta']}")
        elif kind == "finished_file":
            self._log(f"✓  {evt['filename']}")
        elif kind == "log":
            self._log(evt["message"])
        elif kind == "done":
            self._set_busy(False)
            if evt["ok"]:
                self.progress.set(1.0)
                self.status_label.configure(text="Done.")
                self.detail_label.configure(text="")
                self._log("Finished.")
                self.open_btn.configure(state="normal")
            else:
                err = evt.get("error") or "unknown error"
                if err == "cancelled":
                    self.status_label.configure(text="Cancelled.")
                    self._log("Cancelled.")
                else:
                    self.status_label.configure(text="Failed — see log below.")
                    self._log(f"✗  {err}")

    # ---- Helpers ----------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        idle = "disabled" if busy else "normal"
        running = "normal" if busy else "disabled"
        for w in (
            self.download_btn, self.url_entry, self.out_entry, self.browse_btn,
            self.paste_btn, self.playlist_chk, self.quality_menu,
            self.video_rb, self.audio_rb,
        ):
            w.configure(state=idle)
        self.cancel_btn.configure(state=running)

    def _log(self, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    try:
        app = App()
    except Exception as e:  # noqa: BLE001
        # Surface startup errors (e.g. no display) clearly instead of a stack trace.
        print(f"Failed to start GUI: {e}", file=sys.stderr)
        sys.exit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
