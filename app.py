"""YouTube Downloader — CustomTkinter GUI with concurrent downloads + i18n."""
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
    DownloadManager,
    DownloadTask,
    audio_quality_options,
    has_ffmpeg,
    video_quality_options,
)
from i18n import I18n, LANGUAGES, autodetect_language
from settings import Settings


POLL_INTERVAL_MS = 120
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"


# ---- Per-task row -----------------------------------------------------------


class TaskRow(ctk.CTkFrame):
    """One row in the queue list: status icon, title, progress, speed/ETA, cancel."""

    STATUS_ICON = {
        "queued":    "⏸",
        "running":   "▶",
        "done":      "✓",
        "failed":    "✗",
        "cancelled": "⊘",
    }
    STATUS_COLOR = {
        "queued":    "#888a8e",
        "running":   "#4aa3df",
        "done":      "#54b25f",
        "failed":    "#d04848",
        "cancelled": "#888a8e",
    }

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        task: DownloadTask,
        i18n: I18n,
        on_cancel,
    ) -> None:
        super().__init__(master, corner_radius=6)
        self.task = task
        self.i18n = i18n
        self.on_cancel = on_cancel

        self.grid_columnconfigure(2, weight=1)

        self.icon = ctk.CTkLabel(self, text="⏸", width=22, font=ctk.CTkFont(size=14))
        self.icon.grid(row=0, column=0, padx=(10, 2), pady=(8, 0), sticky="w")

        self.num = ctk.CTkLabel(self, text=f"#{task.id}", width=34, anchor="w",
                                text_color="#888a8e")
        self.num.grid(row=0, column=1, padx=(0, 6), pady=(8, 0), sticky="w")

        self.title_lbl = ctk.CTkLabel(self, text=task.title or task.url,
                                      anchor="w", justify="left")
        self.title_lbl.grid(row=0, column=2, padx=4, pady=(8, 0), sticky="ew")

        self.status_lbl = ctk.CTkLabel(self, text=i18n.t("status.queued"),
                                       text_color=self.STATUS_COLOR["queued"],
                                       width=110, anchor="e")
        self.status_lbl.grid(row=0, column=3, padx=(6, 6), pady=(8, 0), sticky="e")

        self.cancel_btn = ctk.CTkButton(
            self, text="×", width=28, height=24,
            fg_color="transparent", hover_color="#3a3a3a",
            text_color="#aaaaaa", font=ctk.CTkFont(size=16),
            command=self._on_cancel_clicked,
        )
        self.cancel_btn.grid(row=0, column=4, padx=(0, 8), pady=(6, 0), sticky="e")

        self.progress = ctk.CTkProgressBar(self, height=8)
        self.progress.grid(row=1, column=0, columnspan=5,
                           padx=10, pady=(6, 4), sticky="ew")
        self.progress.set(0)

        self.detail_lbl = ctk.CTkLabel(self, text="", anchor="w",
                                       text_color="#9aa0a6",
                                       font=ctk.CTkFont(size=11))
        self.detail_lbl.grid(row=2, column=0, columnspan=5,
                             padx=10, pady=(0, 8), sticky="ew")

        self.refresh()

    def _on_cancel_clicked(self) -> None:
        self.on_cancel(self.task.id)

    def refresh(self) -> None:
        t = self.task
        self.icon.configure(text=self.STATUS_ICON.get(t.status, "?"),
                            text_color=self.STATUS_COLOR.get(t.status, "#888a8e"))
        self.status_lbl.configure(
            text=self.i18n.t(f"status.{t.status}"),
            text_color=self.STATUS_COLOR.get(t.status, "#888a8e"),
        )
        self.title_lbl.configure(text=_ellipsize(t.title or t.url, 80))

        self.progress.set(max(0.0, min(1.0, t.percent / 100.0)))

        if t.status == "running":
            self.detail_lbl.configure(
                text=f"{t.percent:.1f}%   ·   {t.speed}   ·   ETA {t.eta}"
            )
            self.cancel_btn.configure(state="normal")
        elif t.status == "queued":
            self.detail_lbl.configure(text="")
            self.cancel_btn.configure(state="normal")
        elif t.status == "done":
            self.detail_lbl.configure(text="100%")
            self.cancel_btn.configure(state="disabled")
        elif t.status == "failed":
            self.detail_lbl.configure(
                text=_ellipsize(t.error.replace("\n", " "), 110)
            )
            self.cancel_btn.configure(state="disabled")
        elif t.status == "cancelled":
            self.detail_lbl.configure(text="")
            self.cancel_btn.configure(state="disabled")


def _ellipsize(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- Main app ---------------------------------------------------------------


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.settings = Settings.load()
        lang = self.settings.language or autodetect_language()
        self.i18n = I18n(lang)
        self.i18n.on_change(self._apply_language)

        self.manager = DownloadManager()
        self.rows: dict[int, TaskRow] = {}
        self._last_output_dir: Path | None = None
        self._labels_to_translate: list[tuple[ctk.CTkBaseClass, str, str]] = []
        # entries: (widget, attr, key) — attr is "text" or "placeholder_text"

        self.title(self.i18n.t("app.title"))
        self.geometry("900x800")
        self.minsize(720, 640)

        self._build_ui()
        self._apply_language()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(POLL_INTERVAL_MS, self._tick)

    # ---- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=3)  # queue
        self.grid_rowconfigure(8, weight=1)  # log

        # Header row: title + language toggle
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(18, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        self.header_lbl = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=22, weight="bold")
        )
        self.header_lbl.grid(row=0, column=0, sticky="w")
        self._labels_to_translate.append((self.header_lbl, "text", "header.title"))

        self.lang_toggle = ctk.CTkSegmentedButton(
            header, values=[code.upper() for code in LANGUAGES],
            command=self._on_language_change, width=110,
        )
        self.lang_toggle.set(self.i18n.lang.upper())
        self.lang_toggle.grid(row=0, column=1, sticky="e")

        # URL multi-line input
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.grid(row=1, column=0, padx=20, pady=(8, 4), sticky="ew")
        url_frame.grid_columnconfigure(1, weight=1)

        self.url_lbl = ctk.CTkLabel(url_frame, text="", width=80, anchor="nw")
        self.url_lbl.grid(row=0, column=0, padx=(0, 8), pady=(4, 0), sticky="nw")
        self._labels_to_translate.append((self.url_lbl, "text", "field.url"))

        self.url_box = ctk.CTkTextbox(url_frame, height=78, wrap="none",
                                      font=ctk.CTkFont(size=12))
        self.url_box.grid(row=0, column=1, sticky="ew")
        self.url_box.bind("<Control-Return>", lambda _e: self._on_add())
        self._refresh_url_placeholder()

        side = ctk.CTkFrame(url_frame, fg_color="transparent")
        side.grid(row=0, column=2, padx=(8, 0), sticky="ns")
        self.paste_btn = ctk.CTkButton(side, text="", width=90, command=self._on_paste)
        self.paste_btn.grid(row=0, column=0, pady=(0, 4))
        self.add_btn = ctk.CTkButton(
            side, text="", width=90, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_add,
        )
        self.add_btn.grid(row=1, column=0)
        self._labels_to_translate.append((self.paste_btn, "text", "button.paste"))
        self._labels_to_translate.append((self.add_btn, "text", "button.add"))

        # Output folder
        out_frame = ctk.CTkFrame(self, fg_color="transparent")
        out_frame.grid(row=2, column=0, padx=20, pady=4, sticky="ew")
        out_frame.grid_columnconfigure(1, weight=1)
        self.save_lbl = ctk.CTkLabel(out_frame, text="", width=80, anchor="w")
        self.save_lbl.grid(row=0, column=0, padx=(0, 8))
        self._labels_to_translate.append((self.save_lbl, "text", "field.save_to"))

        self.out_var = tk.StringVar(
            value=self.settings.output_dir or str(DEFAULT_DOWNLOAD_DIR)
        )
        self.out_entry = ctk.CTkEntry(out_frame, textvariable=self.out_var)
        self.out_entry.grid(row=0, column=1, sticky="ew")
        self.browse_btn = ctk.CTkButton(out_frame, text="", width=80,
                                        command=self._on_browse)
        self.browse_btn.grid(row=0, column=2, padx=(8, 0))
        self._labels_to_translate.append((self.browse_btn, "text", "button.browse"))

        # Options frame
        opts = ctk.CTkFrame(self)
        opts.grid(row=3, column=0, padx=20, pady=10, sticky="ew")
        for c in (1, 3, 5):
            opts.grid_columnconfigure(c, weight=0)
        opts.grid_columnconfigure(7, weight=1)

        self.fmt_lbl = ctk.CTkLabel(opts, text="")
        self.fmt_lbl.grid(row=0, column=0, padx=(14, 8), pady=12)
        self._labels_to_translate.append((self.fmt_lbl, "text", "label.format"))

        self.mode_var = tk.StringVar(value=self.settings.mode)
        self.video_rb = ctk.CTkRadioButton(
            opts, text="", variable=self.mode_var, value="video",
            command=self._on_mode_change,
        )
        self.audio_rb = ctk.CTkRadioButton(
            opts, text="", variable=self.mode_var, value="audio",
            command=self._on_mode_change,
        )
        self.video_rb.grid(row=0, column=1, padx=6, pady=12)
        self.audio_rb.grid(row=0, column=2, padx=6, pady=12)
        self._labels_to_translate.append((self.video_rb, "text", "radio.video"))
        self._labels_to_translate.append((self.audio_rb, "text", "radio.audio"))

        self.q_lbl = ctk.CTkLabel(opts, text="")
        self.q_lbl.grid(row=0, column=3, padx=(20, 8), pady=12)
        self._labels_to_translate.append((self.q_lbl, "text", "label.quality"))

        self.quality_var = tk.StringVar(value=self.settings.quality)
        self.quality_menu = ctk.CTkOptionMenu(
            opts, variable=self.quality_var,
            values=self._current_quality_values(), width=140,
        )
        self.quality_menu.grid(row=0, column=4, padx=6, pady=12)

        self.playlist_var = tk.BooleanVar(value=self.settings.playlist)
        self.playlist_chk = ctk.CTkCheckBox(opts, text="", variable=self.playlist_var)
        self.playlist_chk.grid(row=0, column=5, padx=(20, 12), pady=12)
        self._labels_to_translate.append((self.playlist_chk, "text", "check.playlist"))

        self.mc_lbl = ctk.CTkLabel(opts, text="")
        self.mc_lbl.grid(row=0, column=6, padx=(20, 8), pady=12, sticky="e")
        self._labels_to_translate.append((self.mc_lbl, "text", "label.max_concurrent"))

        self.max_concurrent_var = tk.StringVar(value=str(self.settings.max_concurrent))
        self.max_concurrent_menu = ctk.CTkOptionMenu(
            opts, variable=self.max_concurrent_var,
            values=["1", "2", "3", "4", "5"], width=70,
            command=lambda _v: None,
        )
        self.max_concurrent_menu.grid(row=0, column=7, padx=(0, 14), pady=12, sticky="w")

        # Action row
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, padx=20, pady=(0, 6), sticky="ew")
        actions.grid_columnconfigure(3, weight=1)

        self.cancel_all_btn = ctk.CTkButton(
            actions, text="", height=34, width=140,
            fg_color="#a02929", hover_color="#7a1d1d",
            command=self._on_cancel_all,
        )
        self.cancel_all_btn.grid(row=0, column=0, padx=(0, 6))

        self.clear_done_btn = ctk.CTkButton(
            actions, text="", height=34, width=170,
            fg_color="#3a3a3a", hover_color="#2a2a2a",
            command=self._on_clear_done,
        )
        self.clear_done_btn.grid(row=0, column=1, padx=6)

        self.open_btn = ctk.CTkButton(
            actions, text="", height=34, width=140, command=self._on_open_folder,
        )
        self.open_btn.grid(row=0, column=2, padx=6)
        self._labels_to_translate.append((self.cancel_all_btn, "text", "button.cancel_all"))
        self._labels_to_translate.append((self.clear_done_btn, "text", "button.clear_done"))
        self._labels_to_translate.append((self.open_btn, "text", "button.open_folder"))

        # Queue label
        self.queue_lbl = ctk.CTkLabel(self, text="",
                                      font=ctk.CTkFont(size=13, weight="bold"),
                                      anchor="w")
        self.queue_lbl.grid(row=5, column=0, padx=22, pady=(10, 2), sticky="w")
        self._labels_to_translate.append((self.queue_lbl, "text", "label.queue"))

        # Queue scrollable frame
        self.queue_scroll = ctk.CTkScrollableFrame(self, fg_color=("#dddddd", "#1e1e1e"))
        self.queue_scroll.grid(row=6, column=0, padx=20, pady=(0, 8), sticky="nsew")
        self.queue_scroll.grid_columnconfigure(0, weight=1)

        self.empty_lbl = ctk.CTkLabel(self.queue_scroll, text="",
                                      text_color="#888a8e",
                                      font=ctk.CTkFont(size=12))
        self.empty_lbl.grid(row=0, column=0, padx=20, pady=20)
        self._labels_to_translate.append((self.empty_lbl, "text", "queue.empty"))

        # Log label
        self.log_lbl = ctk.CTkLabel(self, text="",
                                    font=ctk.CTkFont(size=13, weight="bold"),
                                    anchor="w")
        self.log_lbl.grid(row=7, column=0, padx=22, pady=(4, 2), sticky="w")
        self._labels_to_translate.append((self.log_lbl, "text", "label.log"))

        mono = tkfont.nametofont("TkFixedFont").actual()["family"]
        self.log = ctk.CTkTextbox(self, height=140, wrap="word",
                                  font=ctk.CTkFont(family=mono, size=11))
        self.log.grid(row=8, column=0, padx=20, pady=(0, 16), sticky="nsew")
        self.log.configure(state="disabled")

        self._log(self.i18n.t("log.ready"))
        if not has_ffmpeg():
            self._log(self.i18n.t("log.no_ffmpeg"))
            self._log(self.i18n.t("log.no_ffmpeg.install"))

    def _current_quality_values(self) -> list[str]:
        if self.mode_var.get() == "audio":
            return audio_quality_options()
        return video_quality_options()

    # ---- Language ---------------------------------------------------------

    def _apply_language(self) -> None:
        self.title(self.i18n.t("app.title"))
        for widget, attr, key in self._labels_to_translate:
            try:
                widget.configure(**{attr: self.i18n.t(key)})
            except tk.TclError:
                pass
        self._refresh_url_placeholder()
        for row in self.rows.values():
            row.refresh()

    def _refresh_url_placeholder(self) -> None:
        try:
            self.url_box.configure(placeholder_text=self.i18n.t("field.url.placeholder"))
        except tk.TclError:
            pass

    def _on_language_change(self, value: str) -> None:
        self.i18n.set_language(value.lower())
        self.settings.language = self.i18n.lang
        self.settings.save()

    # ---- User actions -----------------------------------------------------

    def _on_mode_change(self) -> None:
        values = self._current_quality_values()
        self.quality_menu.configure(values=values)
        if self.quality_var.get() not in values:
            self.quality_var.set(values[0])

    def _on_paste(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not text:
            return
        existing = self.url_box.get("1.0", "end").strip()
        new = (existing + "\n" + text).strip() if existing else text
        self.url_box.delete("1.0", "end")
        self.url_box.insert("1.0", new + "\n")

    def _on_browse(self) -> None:
        initial = self.out_var.get() or str(DEFAULT_DOWNLOAD_DIR)
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.out_var.set(path)

    def _on_add(self) -> None:
        raw = self.url_box.get("1.0", "end")
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        urls = [u for u in urls if u.lower().startswith(("http://", "https://"))]
        if not urls:
            messagebox.showwarning(
                self.i18n.t("msg.no_url.title"),
                self.i18n.t("msg.no_url.body"),
            )
            return

        out_dir = Path(self.out_var.get().strip() or DEFAULT_DOWNLOAD_DIR).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(
                self.i18n.t("msg.bad_folder.title"),
                self.i18n.t("msg.bad_folder.body", error=str(e)),
            )
            return

        self._last_output_dir = out_dir
        mode = self.mode_var.get()
        quality = self.quality_var.get()
        playlist = self.playlist_var.get()
        for url in urls:
            self.manager.add(url, out_dir, mode, quality, playlist)
            self._log(self.i18n.t("log.task_added", url=url))

        self.url_box.delete("1.0", "end")
        self._persist_user_prefs()

    def _on_cancel_all(self) -> None:
        self.manager.cancel_all()

    def _on_clear_done(self) -> None:
        removed = self.manager.clear_done()
        if not removed:
            return
        for tid in [tid for tid, row in self.rows.items()
                    if tid not in self.manager.tasks]:
            self.rows[tid].destroy()
            del self.rows[tid]
        self._update_empty_visibility()

    def _on_open_folder(self) -> None:
        path = self._last_output_dir or Path(self.out_var.get()).expanduser()
        if not path.exists():
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

    def _on_close(self) -> None:
        self._persist_user_prefs()
        self.destroy()

    # ---- Event pump -------------------------------------------------------

    def _tick(self) -> None:
        try:
            while True:
                self._handle_event(self.manager.events.get_nowait())
        except queue.Empty:
            pass

        try:
            mc = int(self.max_concurrent_var.get())
        except (TypeError, ValueError):
            mc = 2
        self.manager.schedule(max_concurrent=mc)

        self.after(POLL_INTERVAL_MS, self._tick)

    def _handle_event(self, evt: dict) -> None:
        kind = evt.get("type")
        tid = evt.get("task_id")

        if kind == "task_added":
            task = self.manager.tasks.get(tid)
            if task is not None:
                self._add_row(task)
            self._update_empty_visibility()

        elif kind == "task_started":
            row = self.rows.get(tid)
            task = self.manager.tasks.get(tid)
            if row and task:
                row.refresh()
                self._log(self.i18n.t("log.task_started",
                                      title=task.title or task.url))

        elif kind == "task_progress":
            row = self.rows.get(tid)
            if row:
                row.refresh()

        elif kind == "task_log":
            msg = evt.get("message", "")
            if msg:
                self._log(f"#{tid}  {msg}")

        elif kind == "task_done":
            row = self.rows.get(tid)
            task = self.manager.tasks.get(tid)
            if row:
                row.refresh()
            if task:
                if task.status == "done":
                    self._log(self.i18n.t("log.task_done",
                                          title=task.title or task.url))
                elif task.status == "failed":
                    self._log(self.i18n.t("log.task_failed",
                                          title=task.title or task.url,
                                          error=_first_line(task.error)))
                    # Surface the full error too (multi-line for ffmpeg case)
                    if "\n" in task.error:
                        for line in task.error.splitlines()[1:]:
                            if line.strip():
                                self._log(f"   {line}")
                elif task.status == "cancelled":
                    self._log(self.i18n.t("log.task_cancelled",
                                          title=task.title or task.url))

        elif kind == "log":
            msg = evt.get("message", "")
            if msg:
                self._log(msg)

    # ---- Queue list management -------------------------------------------

    def _add_row(self, task: DownloadTask) -> None:
        row = TaskRow(self.queue_scroll, task, self.i18n, on_cancel=self._cancel_task)
        row.grid(row=task.id, column=0, padx=4, pady=4, sticky="ew")
        self.rows[task.id] = row

    def _cancel_task(self, task_id: int) -> None:
        self.manager.cancel(task_id)
        row = self.rows.get(task_id)
        if row:
            row.refresh()

    def _update_empty_visibility(self) -> None:
        if self.rows:
            try:
                self.empty_lbl.grid_remove()
            except tk.TclError:
                pass
        else:
            self.empty_lbl.grid()

    # ---- Misc -------------------------------------------------------------

    def _log(self, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _persist_user_prefs(self) -> None:
        try:
            mc = int(self.max_concurrent_var.get())
        except (TypeError, ValueError):
            mc = 2
        self.settings.language = self.i18n.lang
        self.settings.max_concurrent = mc
        self.settings.output_dir = self.out_var.get().strip()
        self.settings.mode = self.mode_var.get()
        self.settings.quality = self.quality_var.get()
        self.settings.playlist = bool(self.playlist_var.get())
        self.settings.save()


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def main() -> None:
    try:
        app = App()
    except Exception as e:  # noqa: BLE001
        print(f"Failed to start GUI: {e}", file=sys.stderr)
        sys.exit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
