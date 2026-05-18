"""YouTube Downloader — CustomTkinter GUI with concurrent downloads + i18n."""
from __future__ import annotations

import datetime
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import browser_detect
import installer
import updater
from downloader import (
    BOT_DETECTION_SIGNATURE,
    DownloadManager,
    DownloadTask,
    audio_codec_options,
    audio_codec_uses_bitrate,
    audio_quality_options,
    cookies_browser_options,
    has_ffmpeg,
    video_codec_options,
    video_quality_options,
)
from i18n import (
    I18n, LANGUAGES, LANGUAGE_DISPLAY_NAMES,
    autodetect_language, code_for_display, display_name,
)
from settings import Settings


POLL_INTERVAL_MS = 120
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
PLACEHOLDER_COLOR = "#6e6e6e"
INFINITY_LABEL = "∞"
INFINITY_VALUE = 9999  # effectively unlimited; manager just starts everything queued
MAX_CONCURRENT_PRESETS = ["1", "2", "3", "4", "5", "10", INFINITY_LABEL]
# Per-video parallel HTTP fragments. Higher = more wall-clock speed on fast
# connections (YouTube throttles each TCP stream individually). 4 is yt-dlp-
# friendly; 16–32 saturates a gigabit link; users can type any positive int.
FRAGMENT_PRESETS = ["1", "2", "4", "8", "16", "32", "64"]


def _parse_concurrent(raw: str) -> int:
    """Translate the combobox text to an int the scheduler can use.
    Empty/garbage falls back to 2; '∞' becomes a very large number."""
    s = (raw or "").strip()
    if s == INFINITY_LABEL or s.lower() in ("inf", "infinity", "unlimited"):
        return INFINITY_VALUE
    try:
        return max(1, int(s))
    except (TypeError, ValueError):
        return 2


def _format_concurrent(value: int) -> str:
    """Inverse of _parse_concurrent for displaying a saved setting."""
    if value >= INFINITY_VALUE or value <= 0:
        return INFINITY_LABEL
    return str(value)


def _parse_fragments(raw: str) -> int:
    """Coerce the fragment combobox text to a positive int. Empty/garbage
    falls back to 4 (the same default we ship). No upper cap — a user with
    a fat pipe might want 64 or more, and yt-dlp handles it fine."""
    try:
        return max(1, int((raw or "").strip()))
    except (TypeError, ValueError):
        return 4


class _TextboxPlaceholder:
    """Manually emulate placeholder_text on a CTkTextbox (which doesn't support it).

    Inserts greyed-out hint text when the textbox is empty + unfocused; clears
    on focus-in. `get_user_text()` returns the real content (empty string when
    the placeholder is showing).
    """

    def __init__(self, textbox: ctk.CTkTextbox, text: str) -> None:
        self.textbox = textbox
        self.text = text
        self._showing = False
        self._normal_color = textbox.cget("text_color")
        textbox.bind("<FocusIn>", self._on_focus_in, add="+")
        textbox.bind("<FocusOut>", self._on_focus_out, add="+")
        self._show()

    def _show(self) -> None:
        if self._showing or self.textbox.get("1.0", "end").strip():
            return
        self._showing = True
        self.textbox.configure(text_color=PLACEHOLDER_COLOR)
        self.textbox.insert("1.0", self.text)

    def _hide(self) -> None:
        if not self._showing:
            return
        self._showing = False
        self.textbox.delete("1.0", "end")
        self.textbox.configure(text_color=self._normal_color)

    def _on_focus_in(self, _evt) -> None:
        if self._showing:
            self._hide()

    def _on_focus_out(self, _evt) -> None:
        self._show()

    def get_user_text(self) -> str:
        return "" if self._showing else self.textbox.get("1.0", "end")

    def before_insert(self) -> None:
        """Call before programmatically inserting text into the textbox."""
        if self._showing:
            self._hide()

    def clear(self) -> None:
        """Clear user text and re-display the placeholder."""
        self._showing = False
        self.textbox.delete("1.0", "end")
        self._show()

    def set_text(self, text: str) -> None:
        was_showing = self._showing
        if was_showing:
            self._hide()
        self.text = text
        if was_showing:
            self._show()


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
        # Show the "enable browser cookies" banner at most once per session
        # so repeated failed tasks don't pile up duplicate banners.
        self._bot_detection_banner_shown = False

        self.title(self.i18n.t("app.title"))
        self.geometry("900x800")
        self.minsize(720, 640)

        self._build_ui()
        self._apply_language()
        self._update_quality_enabled()
        if self.mode_var.get() == "audio":
            self.force_mp4_chk.grid_remove()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Startup checks: show banners for missing ffmpeg and pending updates.
        self._check_ffmpeg_at_startup()
        if self.settings.auto_update_check:
            threading.Thread(
                target=self._check_updates_bg, daemon=True
            ).start()

        self.after(POLL_INTERVAL_MS, self._tick)

    # ---- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(7, weight=3)  # queue (was 6)
        self.grid_rowconfigure(9, weight=1)  # log   (was 8)

        # Row 0: banner stack (ffmpeg-missing, update-available, ...).
        # Created up front but only gridded when at least one banner is
        # shown — a CTkFrame defaults to 200×200 px, so leaving an empty
        # one in the grid pushes every other row down by ~200 px.
        self.banners = ctk.CTkFrame(self, fg_color="transparent")
        self.banners.grid_columnconfigure(0, weight=1)
        self._banner_widgets: dict[str, ctk.CTkFrame] = {}

        # Header row: title + language toggle
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=1, column=0, padx=20, pady=(12, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        self.header_lbl = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=22, weight="bold")
        )
        self.header_lbl.grid(row=0, column=0, sticky="w")
        self._labels_to_translate.append((self.header_lbl, "text", "header.title"))

        # Language picker — uses native names ("Türkçe", "Русский", "中文"…)
        # so users can find their language without knowing English. Arabic
        # is pre-shaped via display_name() so it appears in connected form.
        self.lang_toggle = ctk.CTkOptionMenu(
            header,
            values=[display_name(code) for code in LANGUAGES],
            command=self._on_language_change, width=140,
        )
        self.lang_toggle.set(display_name(self.i18n.lang))
        self.lang_toggle.grid(row=0, column=1, sticky="e")

        # URL multi-line input
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.grid(row=2, column=0, padx=20, pady=(8, 4), sticky="ew")
        url_frame.grid_columnconfigure(1, weight=1)

        self.url_lbl = ctk.CTkLabel(url_frame, text="", width=80, anchor="nw")
        self.url_lbl.grid(row=0, column=0, padx=(0, 8), pady=(4, 0), sticky="nw")
        self._labels_to_translate.append((self.url_lbl, "text", "field.url"))

        self.url_box = ctk.CTkTextbox(url_frame, height=78, wrap="none",
                                      font=ctk.CTkFont(size=12))
        self.url_box.grid(row=0, column=1, sticky="ew")
        self.url_box.bind("<Control-Return>", lambda _e: self._on_add())
        self.url_placeholder = _TextboxPlaceholder(
            self.url_box, self.i18n.t("field.url.placeholder")
        )

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
        out_frame.grid(row=3, column=0, padx=20, pady=4, sticky="ew")
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

        # Options: three rows so widgets keep their natural width when narrow.
        # Row 0: Format radios + Quality.
        # Row 1: Codec (label + dropdown).
        # Row 2: Playlist toggle + Max concurrent.
        # Column 5 absorbs leftover horizontal space.
        opts = ctk.CTkFrame(self)
        opts.grid(row=4, column=0, padx=20, pady=10, sticky="ew")
        opts.grid_columnconfigure(5, weight=1)

        self.fmt_lbl = ctk.CTkLabel(opts, text="")
        self.fmt_lbl.grid(row=0, column=0, padx=(14, 8), pady=(12, 4), sticky="w")
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
        self.video_rb.grid(row=0, column=1, padx=6, pady=(12, 4), sticky="w")
        self.audio_rb.grid(row=0, column=2, padx=6, pady=(12, 4), sticky="w")
        self._labels_to_translate.append((self.video_rb, "text", "radio.video"))
        self._labels_to_translate.append((self.audio_rb, "text", "radio.audio"))

        # Quality label + dropdown live in a sub-frame so they sit flush
        # against each other and the whole pair aligns with the Force-MP4
        # checkbox (row 1) and the Max-concurrent group (row 2). All three
        # use column 3 with padx=(20, 12) sticky="w".
        quality_frame = ctk.CTkFrame(opts, fg_color="transparent")
        quality_frame.grid(row=0, column=3, columnspan=2,
                           padx=(20, 12), pady=(12, 4), sticky="w")
        self.q_lbl = ctk.CTkLabel(quality_frame, text="")
        self.q_lbl.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append((self.q_lbl, "text", "label.quality"))

        self.quality_var = tk.StringVar(
            value=self.i18n.localize_option(self.settings.quality)
        )
        self.quality_menu = ctk.CTkOptionMenu(
            quality_frame, variable=self.quality_var,
            values=self._current_quality_values(), width=140,
        )
        self.quality_menu.pack(side="left")

        # Row 1 — codec selector. The dropdown values swap with the mode.
        self.codec_lbl = ctk.CTkLabel(opts, text="")
        self.codec_lbl.grid(row=1, column=0, padx=(14, 8), pady=4, sticky="w")
        self._labels_to_translate.append((self.codec_lbl, "text", "label.codec"))

        initial_codec = (
            self.settings.audio_codec if self.mode_var.get() == "audio"
            else self.settings.video_codec
        )
        self.codec_var = tk.StringVar(value=self.i18n.localize_option(initial_codec))
        self.codec_menu = ctk.CTkOptionMenu(
            opts, variable=self.codec_var,
            values=self._current_codec_values(),
            width=180,
            command=self._on_codec_change,
        )
        self.codec_menu.grid(row=1, column=1, columnspan=2, padx=6, pady=4,
                             sticky="w")

        # "Force MP4" applies only to video downloads. It runs an
        # FFmpegVideoConvertor pass after merging that re-encodes to a
        # guaranteed-mp4 file (H.264 + AAC). Slow when the source isn't
        # already mp4-compatible, but always produces a playable .mp4.
        self.force_mp4_var = tk.BooleanVar(value=self.settings.force_mp4)
        self.force_mp4_chk = ctk.CTkCheckBox(
            opts, text="", variable=self.force_mp4_var,
        )
        self.force_mp4_chk.grid(row=1, column=3, columnspan=2,
                                padx=(20, 12), pady=4, sticky="w")
        self._labels_to_translate.append(
            (self.force_mp4_chk, "text", "check.force_mp4")
        )

        self.playlist_var = tk.BooleanVar(value=self.settings.playlist)
        self.playlist_chk = ctk.CTkCheckBox(opts, text="", variable=self.playlist_var)
        self.playlist_chk.grid(row=2, column=0, columnspan=3, padx=(14, 12),
                               pady=(4, 12), sticky="w")
        self._labels_to_translate.append((self.playlist_chk, "text", "check.playlist"))

        # Max-concurrent label + combobox in a sub-frame, aligned with the
        # Quality and Force-MP4 groups above (same column 3 left edge).
        mc_frame = ctk.CTkFrame(opts, fg_color="transparent")
        mc_frame.grid(row=2, column=3, padx=(20, 12), pady=(4, 12), sticky="w")
        self.mc_lbl = ctk.CTkLabel(mc_frame, text="")
        self.mc_lbl.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append((self.mc_lbl, "text", "label.max_concurrent"))

        self.max_concurrent_var = tk.StringVar(
            value=_format_concurrent(self.settings.max_concurrent)
        )
        self.max_concurrent_menu = ctk.CTkComboBox(
            mc_frame, variable=self.max_concurrent_var,
            values=MAX_CONCURRENT_PRESETS, width=110,
        )
        self.max_concurrent_menu.pack(side="left")

        # Parallel HTTP fragments per video — sits in a NEW column right
        # next to Max-concurrent on the same row. This is the single biggest
        # knob for raw download speed: YouTube throttles each TCP stream,
        # so splitting across N connections is what unlocks fast pipes.
        # Combobox accepts any positive int the user types in.
        frag_frame = ctk.CTkFrame(opts, fg_color="transparent")
        frag_frame.grid(row=2, column=4, padx=(12, 12), pady=(4, 4), sticky="w")
        self.frag_lbl = ctk.CTkLabel(frag_frame, text="")
        self.frag_lbl.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append((self.frag_lbl, "text", "label.fragments"))

        self.fragments_var = tk.StringVar(
            value=str(max(1, int(self.settings.concurrent_fragments)))
        )
        self.fragments_menu = ctk.CTkComboBox(
            frag_frame, variable=self.fragments_var,
            values=FRAGMENT_PRESETS, width=110,
        )
        self.fragments_menu.pack(side="left")

        # Row 3 — browser cookie source. yt-dlp will read the chosen
        # browser's cookie jar off disk and send the user's YouTube session
        # with each request, bypassing "Sign in to confirm you're not a
        # bot" rate-limiting. "Off" sends no cookies (default).
        cookies_frame = ctk.CTkFrame(opts, fg_color="transparent")
        cookies_frame.grid(row=3, column=3, columnspan=2,
                           padx=(20, 12), pady=(4, 12), sticky="w")
        self.cookies_lbl = ctk.CTkLabel(cookies_frame, text="")
        self.cookies_lbl.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append((self.cookies_lbl, "text", "label.cookies"))

        # Initial display string is the localised form of the saved internal
        # key (e.g. "Off" → "Kapalı" in Turkish, "Firefox" stays "Firefox").
        self.cookies_var = tk.StringVar(
            value=self.i18n.localize_option(self.settings.cookies_browser or "Off")
        )
        self.cookies_menu = ctk.CTkOptionMenu(
            cookies_frame, variable=self.cookies_var,
            values=self._current_cookies_values(),
            width=160,
        )
        self.cookies_menu.pack(side="left")

        # Action row
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=5, column=0, padx=20, pady=(0, 6), sticky="ew")
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
        self.queue_lbl.grid(row=6, column=0, padx=22, pady=(10, 2), sticky="w")
        self._labels_to_translate.append((self.queue_lbl, "text", "label.queue"))

        # Queue scrollable frame
        self.queue_scroll = ctk.CTkScrollableFrame(self, fg_color=("#dddddd", "#1e1e1e"))
        self.queue_scroll.grid(row=7, column=0, padx=20, pady=(0, 8), sticky="nsew")
        self.queue_scroll.grid_columnconfigure(0, weight=1)

        self.empty_lbl = ctk.CTkLabel(self.queue_scroll, text="",
                                      text_color="#888a8e",
                                      font=ctk.CTkFont(size=12))
        self.empty_lbl.grid(row=0, column=0, padx=20, pady=20)
        self._labels_to_translate.append((self.empty_lbl, "text", "queue.empty"))
        # The scrollable frame only handles wheel events on its empty canvas
        # area, so any child widget that intercepts the wheel will stop
        # scrolling. Forward wheel events from children back to the canvas.
        self._bind_wheel_to_queue(self.empty_lbl)

        # Log label
        self.log_lbl = ctk.CTkLabel(self, text="",
                                    font=ctk.CTkFont(size=13, weight="bold"),
                                    anchor="w")
        self.log_lbl.grid(row=8, column=0, padx=22, pady=(4, 2), sticky="w")
        self._labels_to_translate.append((self.log_lbl, "text", "label.log"))

        mono = tkfont.nametofont("TkFixedFont").actual()["family"]
        self.log = ctk.CTkTextbox(self, height=140, wrap="word",
                                  font=ctk.CTkFont(family=mono, size=11))
        self.log.grid(row=9, column=0, padx=20, pady=(0, 16), sticky="nsew")
        self.log.configure(state="disabled")

        self._log(self.i18n.t("log.ready"))

    # Quality / codec dropdowns: the *values* shown to the user are localised
    # ("En iyi" instead of "Best" in Turkish), but the rest of the code
    # works with internal English keys. The _internal_*_options helpers
    # return the canonical lists; _current_*_values are what gets pumped
    # into the dropdown widgets.

    def _internal_quality_options(self) -> list[str]:
        if self.mode_var.get() == "audio":
            return audio_quality_options()
        return video_quality_options()

    def _internal_codec_options(self) -> list[str]:
        if self.mode_var.get() == "audio":
            return audio_codec_options()
        return video_codec_options()

    def _current_quality_values(self) -> list[str]:
        return [self.i18n.localize_option(k) for k in self._internal_quality_options()]

    def _current_codec_values(self) -> list[str]:
        return [self.i18n.localize_option(k) for k in self._internal_codec_options()]

    def _internal_cookies_options(self) -> list[str]:
        return cookies_browser_options()

    def _current_cookies_values(self) -> list[str]:
        return [self.i18n.localize_option(k) for k in self._internal_cookies_options()]

    def _cookies_internal(self) -> str:
        return self.i18n.delocalize_option(
            self.cookies_var.get(), self._internal_cookies_options()
        )

    def _quality_internal(self) -> str:
        return self.i18n.delocalize_option(
            self.quality_var.get(), self._internal_quality_options()
        )

    def _codec_internal(self) -> str:
        return self.i18n.delocalize_option(
            self.codec_var.get(), self._internal_codec_options()
        )

    def _update_quality_enabled(self) -> None:
        """Grey-out the quality dropdown when audio codec ignores bitrate."""
        if self.mode_var.get() == "audio":
            uses_br = audio_codec_uses_bitrate(self._codec_internal())
            self.quality_menu.configure(state="normal" if uses_br else "disabled")
        else:
            self.quality_menu.configure(state="normal")

    # ---- Banners (ffmpeg-missing, update-available) ----------------------

    def _show_banner(
        self,
        key: str,
        *,
        message: str,
        action: str | None = None,
        on_action=None,
        accent: str = "#2a4a78",
    ) -> None:
        """Show or replace a banner. Banners stack vertically inside self.banners."""
        was_empty = not self._banner_widgets
        existing = self._banner_widgets.pop(key, None)
        if existing is not None:
            existing.destroy()

        banner = ctk.CTkFrame(self.banners, fg_color=accent, corner_radius=6)
        banner.grid_columnconfigure(0, weight=1)

        lbl = ctk.CTkLabel(banner, text=message, anchor="w", justify="left",
                           wraplength=600)
        lbl.grid(row=0, column=0, padx=12, pady=8, sticky="ew")

        col = 1
        if action and on_action is not None:
            btn = ctk.CTkButton(
                banner, text=action, width=130,
                fg_color="#3a6dd0", hover_color="#2a55a8",
                command=on_action,
            )
            btn.grid(row=0, column=col, padx=4, pady=6)
            col += 1

        dismiss = ctk.CTkButton(
            banner, text="×", width=28, height=24,
            fg_color="transparent", hover_color="#1a2a4a",
            text_color="#cccccc", font=ctk.CTkFont(size=16),
            command=lambda k=key: self._dismiss_banner(k),
        )
        dismiss.grid(row=0, column=col, padx=(2, 8), pady=6)

        # Stack banners top-to-bottom in arrival order
        banner.grid(row=len(self._banner_widgets), column=0, sticky="ew",
                    pady=(0, 6))
        self._banner_widgets[key] = banner
        if was_empty:
            self.banners.grid(row=0, column=0, padx=20, pady=(8, 0), sticky="ew")

    def _dismiss_banner(self, key: str) -> None:
        widget = self._banner_widgets.pop(key, None)
        if widget is not None:
            widget.destroy()
        # Re-grid remaining banners to close any gap left behind.
        for i, w in enumerate(self._banner_widgets.values()):
            w.grid_configure(row=i)
        # Hide the (now-empty) container so it doesn't reserve a 200-px slot.
        if not self._banner_widgets:
            self.banners.grid_forget()

    # ---- Startup checks ---------------------------------------------------

    def _check_ffmpeg_at_startup(self) -> None:
        if has_ffmpeg():
            return
        plan = installer.plan_install("ffmpeg")
        if plan.pm is None or plan.requires_manual:
            # No way to auto-install. Show a non-actionable banner.
            hint = plan.manual_hint or self.i18n.t("banner.no_pm")
            self._show_banner(
                "ffmpeg",
                message=self.i18n.t("banner.ffmpeg_missing") + "  " + hint,
                accent="#7a3030",
            )
            return
        self._show_banner(
            "ffmpeg",
            message=self.i18n.t("banner.ffmpeg_missing"),
            action=self.i18n.t("button.install"),
            on_action=self._on_install_ffmpeg,
            accent="#7a3030",
        )

    def _check_updates_bg(self) -> None:
        # Case 1: git is installed but this folder isn't a checkout (ZIP
        # install). Offer to convert it so future updates can be pulled.
        # This check is local-only, so do it before the network fetch.
        if updater.can_enable_auto_update():
            self.after(0, lambda: self._show_banner(
                "git-init",
                message=self.i18n.t("banner.no_git_checkout"),
                action=self.i18n.t("button.enable_auto_update"),
                on_action=self._on_enable_auto_update,
                accent="#2a4a78",
            ))
            return

        # Case 2: real git checkout — check upstream commit count.
        behind, err = updater.commits_behind()
        if err is not None:
            return  # silent: no git installed / no network / etc.
        if behind <= 0:
            return  # up to date
        # Hop back to the main thread to mutate widgets
        self.after(0, lambda n=behind: self._show_banner(
            "update",
            message=self.i18n.t("banner.update_available", n=n),
            action=self.i18n.t("button.update_now"),
            on_action=self._on_update_now,
            accent="#2a4a78",
        ))

    # ---- Install + update actions ----------------------------------------

    def _on_install_ffmpeg(self) -> None:
        plan = installer.plan_install("ffmpeg")
        if plan.requires_manual:
            self._log(self.i18n.t("banner.manual_install"))
            self._log("  " + " ".join(plan.command or [plan.manual_hint]))
            return
        self._log(self.i18n.t("log.installing", package="ffmpeg"))
        self._log(self.i18n.t("log.install_running",
                              cmd=" ".join(plan.command)))
        # Disable the install button while it runs by hiding the banner
        # (we'll re-show on success or failure).
        self._dismiss_banner("ffmpeg")
        threading.Thread(
            target=self._run_install_bg,
            args=(plan, "ffmpeg"),
            daemon=True,
        ).start()

    def _run_install_bg(self, plan: "installer.InstallPlan", package: str) -> None:
        def log_line(line: str) -> None:
            self.after(0, lambda l=line: self._log(l))
        ok, summary = installer.run_install(plan, log_line)
        if ok and (package != "ffmpeg" or has_ffmpeg()):
            self.after(0, lambda: self._log(self.i18n.t("log.install_done",
                                                        package=package)))
        else:
            err = summary if not ok else "binary still not on PATH"
            self.after(0, lambda e=err: self._log(self.i18n.t(
                "log.install_failed", error=e)))
            # Restore the banner so the user can try again
            if package == "ffmpeg" and not has_ffmpeg():
                self.after(0, self._check_ffmpeg_at_startup)

    def _on_enable_auto_update(self) -> None:
        self._dismiss_banner("git-init")
        self._log(self.i18n.t("log.auto_update_enabling"))
        threading.Thread(target=self._run_enable_auto_update_bg, daemon=True).start()

    def _run_enable_auto_update_bg(self) -> None:
        ok, output = updater.enable_auto_update()
        for line in (output or "").splitlines():
            if line.strip():
                self.after(0, lambda l=line: self._log(l))
        if ok:
            self.after(0, lambda: self._log(self.i18n.t("log.auto_update_enabled")))
            # Files may have changed (reset --hard pulled origin/main).
            # Same restart prompt as a regular update.
            self.after(0, self._prompt_restart)
        else:
            self.after(0, lambda e=output or "unknown": self._log(self.i18n.t(
                "log.auto_update_enable_failed", error=e)))

    def _on_update_now(self) -> None:
        self._dismiss_banner("update")
        self._log(self.i18n.t("log.update_running"))
        threading.Thread(target=self._run_update_bg, daemon=True).start()

    def _run_update_bg(self) -> None:
        ok, output = updater.pull()
        for line in (output or "").splitlines():
            if line.strip():
                self.after(0, lambda l=line: self._log(l))
        if ok:
            self.after(0, lambda: self._log(self.i18n.t("log.update_done")))
            self.after(0, self._prompt_restart)
        else:
            self.after(0, lambda: self._log(self.i18n.t(
                "log.update_failed", error=output or "unknown")))

    def _handle_bot_detection(self, task: DownloadTask) -> None:
        """Decide what to do when YouTube's bot-check rate-limit fires.

        Three branches:
          - Task was on "Auto" and we've never retried it: auto-detect the
            user's browser, switch the manager to use it, retry the task
            silently. If detection finds nothing, fall through to banner.
          - Task was on "Auto" but retry already happened (browser cookies
            didn't help): show banner so the user can pick a different
            browser manually.
          - Task had explicit cookies setting (Off or a specific browser):
            show the existing banner.
        """
        per_task = (task.cookies_browser or "Off")

        if per_task == "Auto" and task.retry_attempts == 0:
            detected = browser_detect.detect_default_browser()
            if detected:
                self.manager.set_auto_cookies_browser(detected)
                self._log(self.i18n.t(
                    "log.auto_cookies_switched",
                    browser=browser_detect.display_name_for(detected),
                ))
                if self.manager.retry_task(task.id):
                    return  # task is back in the queue with cookies wired up
            else:
                self._log(self.i18n.t("log.auto_cookies_no_browser"))

        # Either we exhausted Auto, or cookies are Off, or no browser
        # detected — surface the manual-pick banner (once per session).
        if self._bot_detection_banner_shown:
            return
        if self._cookies_internal() not in ("Off", "Auto"):
            return  # user already enabled cookies explicitly — banner moot
        self._bot_detection_banner_shown = True
        self._show_banner(
            "bot-detection",
            message=self.i18n.t("banner.bot_detection"),
            accent="#7a5a30",
        )

    def _prompt_restart(self) -> None:
        if messagebox.askyesno(
            self.i18n.t("msg.restart.title"),
            self.i18n.t("msg.restart.body"),
        ):
            self._persist_user_prefs()
            try:
                os.execv(sys.executable, [sys.executable, *sys.argv])
            except OSError as e:
                self._log(f"Failed to restart: {e}")

    # ---- Language ---------------------------------------------------------

    def _apply_language(self) -> None:
        self.title(self.i18n.t("app.title"))
        for widget, attr, key in self._labels_to_translate:
            try:
                widget.configure(**{attr: self.i18n.t(key)})
            except tk.TclError:
                pass
        if hasattr(self, "url_placeholder"):
            self.url_placeholder.set_text(self.i18n.t("field.url.placeholder"))

        # Re-localise the dropdown values, preserving the user's selection
        # by mapping through the internal key. delocalize_option uses the
        # *old* language's table because the var still holds the old display
        # string, then we look the internal key back up via the *new*
        # language's table.
        if hasattr(self, "quality_menu"):
            q_keys = self._internal_quality_options()
            # Resolve current internal key by trying both languages so a stale
            # display from before the switch still maps cleanly.
            current_internal = self._quality_internal()
            if current_internal not in q_keys:
                current_internal = q_keys[0]
            self.quality_menu.configure(values=self._current_quality_values())
            self.quality_var.set(self.i18n.localize_option(current_internal))

        if hasattr(self, "codec_menu"):
            c_keys = self._internal_codec_options()
            current_internal = self._codec_internal()
            if current_internal not in c_keys:
                current_internal = c_keys[0]
            self.codec_menu.configure(values=self._current_codec_values())
            self.codec_var.set(self.i18n.localize_option(current_internal))

        if hasattr(self, "cookies_menu"):
            ck_keys = self._internal_cookies_options()
            current_internal = self._cookies_internal()
            if current_internal not in ck_keys:
                current_internal = "Off"
            self.cookies_menu.configure(values=self._current_cookies_values())
            self.cookies_var.set(self.i18n.localize_option(current_internal))

        for row in self.rows.values():
            row.refresh()

    def _on_language_change(self, value: str) -> None:
        # `value` is the native-name display from the dropdown (e.g. "Türkçe",
        # "中文"). Map it back to its ISO code.
        self.i18n.set_language(code_for_display(value))
        self.settings.language = self.i18n.lang
        self.settings.save()

    # ---- User actions -----------------------------------------------------

    def _on_mode_change(self) -> None:
        q_values = self._current_quality_values()
        self.quality_menu.configure(values=q_values)
        if self.quality_var.get() not in q_values:
            self.quality_var.set(q_values[0])

        c_values = self._current_codec_values()
        self.codec_menu.configure(values=c_values)
        # Remember each mode's last-used codec so flipping back restores it.
        if self.mode_var.get() == "audio":
            preferred_internal = self.settings.audio_codec
        else:
            preferred_internal = self.settings.video_codec
        preferred = self.i18n.localize_option(preferred_internal)
        self.codec_var.set(preferred if preferred in c_values else c_values[0])

        # Force MP4 only makes sense in video mode.
        if self.mode_var.get() == "audio":
            self.force_mp4_chk.grid_remove()
        else:
            self.force_mp4_chk.grid()

        self._update_quality_enabled()

    def _on_codec_change(self, _value: str | None = None) -> None:
        # Persist per-mode (always as the internal English key, even though
        # the user may see 'Otomatik' in the dropdown).
        internal = self._codec_internal()
        if self.mode_var.get() == "audio":
            self.settings.audio_codec = internal
        else:
            self.settings.video_codec = internal
        self._update_quality_enabled()

    def _on_paste(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not text:
            return
        self.url_placeholder.before_insert()
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
        raw = self.url_placeholder.get_user_text()
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
        quality = self._quality_internal()
        codec = self._codec_internal()
        playlist = self.playlist_var.get()
        force_mp4 = bool(self.force_mp4_var.get()) and mode == "video"
        fragments = _parse_fragments(self.fragments_var.get())
        cookies_browser = self._cookies_internal()
        for url in urls:
            self.manager.add(
                url, out_dir, mode, quality, codec, playlist,
                force_mp4=force_mp4,
                concurrent_fragments=fragments,
                cookies_browser=cookies_browser,
            )
            self._log(self.i18n.t("log.task_added", url=url))

        self.url_placeholder.clear()
        self._persist_user_prefs()

    def _on_cancel_all(self) -> None:
        self.manager.cancel_all()

    def _on_clear_done(self) -> None:
        # The manager returns exactly which task ids it removed under its
        # lock, so the GUI destroys precisely those rows — no inference,
        # no race with download threads transitioning state mid-click.
        for tid in self.manager.clear_done():
            row = self.rows.pop(tid, None)
            if row is not None:
                row.destroy()
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

        self.manager.schedule(
            max_concurrent=_parse_concurrent(self.max_concurrent_var.get())
        )

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
                    if BOT_DETECTION_SIGNATURE in (task.error or "").lower():
                        self._handle_bot_detection(task)
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
        self._bind_wheel_to_queue(row)

    def _bind_wheel_to_queue(self, widget) -> None:
        """Forward mouse-wheel events on `widget` (and all its descendants)
        to the queue's scrollable canvas."""
        canvas = self.queue_scroll._parent_canvas

        def on_wheel_delta(event):
            step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            canvas.yview_scroll(step, "units")
            return "break"

        def on_wheel_up(_event):
            canvas.yview_scroll(-3, "units")
            return "break"

        def on_wheel_down(_event):
            canvas.yview_scroll(3, "units")
            return "break"

        def bind_recursive(w):
            w.bind("<MouseWheel>", on_wheel_delta, add="+")  # Windows / macOS
            w.bind("<Button-4>", on_wheel_up, add="+")        # Linux scroll-up
            w.bind("<Button-5>", on_wheel_down, add="+")      # Linux scroll-down
            for child in w.winfo_children():
                bind_recursive(child)

        bind_recursive(widget)

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
        mc = _parse_concurrent(self.max_concurrent_var.get())
        self.settings.language = self.i18n.lang
        self.settings.max_concurrent = mc
        self.settings.output_dir = self.out_var.get().strip()
        self.settings.mode = self.mode_var.get()
        # Always persist the internal English key, not the localised display.
        self.settings.quality = self._quality_internal()
        codec_internal = self._codec_internal()
        if self.mode_var.get() == "audio":
            self.settings.audio_codec = codec_internal
        else:
            self.settings.video_codec = codec_internal
        self.settings.force_mp4 = bool(self.force_mp4_var.get())
        self.settings.playlist = bool(self.playlist_var.get())
        self.settings.concurrent_fragments = _parse_fragments(self.fragments_var.get())
        self.settings.cookies_browser = self._cookies_internal()
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
