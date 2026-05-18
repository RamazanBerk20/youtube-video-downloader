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
    COOKIES_DECRYPT_FAILED_SIGNATURE,
    NO_VIDEO_FORMAT_SIGNATURE,
    DownloadManager,
    DownloadTask,
    audio_codec_options,
    audio_codec_uses_bitrate,
    audio_quality_options,
    cookies_browser_options,
    has_ffmpeg,
    has_js_runtime,
    video_codec_options,
    video_container_options,
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


def _install_text_qol_bindings(widget, *, is_text: bool, get_lang) -> None:
    """Wire keyboard + right-click QoL onto an Entry or Text widget.

    Bindings added:
      Ctrl+A          → select all
      Ctrl+Backspace  → delete previous word
      Ctrl+Delete     → delete next word
      Right-click     → context menu (Cut / Copy / Paste / Select all)

    Tk's default bindings for these are inconsistent across platforms and
    completely missing for Text widgets — without this, the user has to
    use the mouse for everything. `is_text` switches between the index
    conventions for Entry (linear int positions) and Text ("line.col").
    `get_lang` is a zero-arg callable returning the active i18n.I18n so
    the menu labels follow the language picker."""

    def select_all(_evt=None):
        try:
            if is_text:
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "1.0")
            else:
                widget.select_range(0, "end")
                widget.icursor("end")
        except tk.TclError:
            pass
        return "break"

    def _delete_word_back(_evt=None):
        try:
            if is_text:
                # Walk backward from insertion point: first skip whitespace,
                # then skip word characters. Anything we cross gets deleted.
                idx = widget.index("insert")
                start = idx
                # Skip trailing whitespace
                while widget.compare(start, ">", "1.0"):
                    prev = widget.index(f"{start}-1c")
                    ch = widget.get(prev, start)
                    if not ch.strip():
                        start = prev
                    else:
                        break
                # Skip word chars
                while widget.compare(start, ">", "1.0"):
                    prev = widget.index(f"{start}-1c")
                    ch = widget.get(prev, start)
                    if ch.isalnum() or ch == "_":
                        start = prev
                    else:
                        break
                if widget.compare(start, "<", idx):
                    widget.delete(start, idx)
            else:
                pos = widget.index("insert")
                text = widget.get()
                cut = pos
                while cut > 0 and not text[cut - 1].strip():
                    cut -= 1
                while cut > 0 and (text[cut - 1].isalnum() or text[cut - 1] == "_"):
                    cut -= 1
                if cut < pos:
                    widget.delete(cut, pos)
        except tk.TclError:
            pass
        return "break"

    def _delete_word_forward(_evt=None):
        try:
            if is_text:
                idx = widget.index("insert")
                end = idx
                while widget.compare(end, "<", "end-1c"):
                    nxt = widget.index(f"{end}+1c")
                    ch = widget.get(end, nxt)
                    if not ch.strip():
                        end = nxt
                    else:
                        break
                while widget.compare(end, "<", "end-1c"):
                    nxt = widget.index(f"{end}+1c")
                    ch = widget.get(end, nxt)
                    if ch.isalnum() or ch == "_":
                        end = nxt
                    else:
                        break
                if widget.compare(end, ">", idx):
                    widget.delete(idx, end)
            else:
                pos = widget.index("insert")
                text = widget.get()
                cut = pos
                n = len(text)
                while cut < n and not text[cut].strip():
                    cut += 1
                while cut < n and (text[cut].isalnum() or text[cut] == "_"):
                    cut += 1
                if cut > pos:
                    widget.delete(pos, cut)
        except tk.TclError:
            pass
        return "break"

    # Ctrl+A intentionally overrides Tk's default Linux binding (which is
    # "move to start of line"). Most users today expect "select all".
    for seq in ("<Control-a>", "<Control-A>"):
        widget.bind(seq, select_all, add="+")
    for seq in ("<Control-BackSpace>", "<Control-Key-BackSpace>"):
        widget.bind(seq, _delete_word_back, add="+")
    for seq in ("<Control-Delete>", "<Control-Key-Delete>"):
        widget.bind(seq, _delete_word_forward, add="+")

    def _show_context_menu(event):
        lang = get_lang()
        menu = tk.Menu(widget, tearoff=0)
        try:
            menu.add_command(
                label=lang.t("menu.cut"),
                command=lambda: widget.event_generate("<<Cut>>"),
            )
            menu.add_command(
                label=lang.t("menu.copy"),
                command=lambda: widget.event_generate("<<Copy>>"),
            )
            menu.add_command(
                label=lang.t("menu.paste"),
                command=lambda: widget.event_generate("<<Paste>>"),
            )
            menu.add_separator()
            menu.add_command(label=lang.t("menu.select_all"), command=select_all)
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    # macOS surfaces right-click as Button-2 on some hardware; Button-3
    # is the cross-platform standard.
    widget.bind("<Button-3>", _show_context_menu, add="+")
    widget.bind("<Button-2>", _show_context_menu, add="+")


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

        # The × button has two meanings depending on state:
        #   running / queued → cancel the task (manager.cancel)
        #   done / failed / cancelled → remove this row from the queue
        # In both cases it's always enabled, so the user always has a way
        # to make the row disappear.
        if t.status == "running":
            self.detail_lbl.configure(
                text=f"{t.percent:.1f}%   ·   {t.speed}   ·   ETA {t.eta}"
            )
        elif t.status == "queued":
            self.detail_lbl.configure(text="")
        elif t.status == "done":
            self.detail_lbl.configure(text="100%")
        elif t.status == "failed":
            self.detail_lbl.configure(
                text=_ellipsize(t.error.replace("\n", " "), 110)
            )
        elif t.status == "cancelled":
            self.detail_lbl.configure(text="")
        self.cancel_btn.configure(state="normal")


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
        # Same one-shot guard for the "install Deno (JS runtime)" banner.
        self._js_runtime_banner_shown = False
        # Browsers Auto-mode has already tried and exhausted this session
        # (either DPAPI-failed or bot-check still fired with their cookies).
        # Used to step through `browser_detect.candidate_browsers()` without
        # going in circles.
        self._auto_cookies_tried: set[str] = set()

        self.title(self.i18n.t("app.title"))
        # Default size bumped to fit the longest translations of the
        # options row without horizontal overflow — German's "Re-encode"
        # checkbox label is the worst offender at ~50 chars. minsize is
        # below that on purpose so the user can still shrink the window
        # if they accept some text being clipped.
        self.geometry("1100x880")
        self.minsize(820, 700)

        self._build_ui()
        self._apply_language()
        self._update_quality_enabled()
        if self.mode_var.get() == "audio":
            self.container_frame.grid_remove()
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
        _install_text_qol_bindings(
            self.url_box, is_text=True, get_lang=lambda: self.i18n
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
        _install_text_qol_bindings(
            self.out_entry, is_text=False, get_lang=lambda: self.i18n
        )
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

        # Container dropdown — applies only to video downloads. Picking
        # anything other than "None" runs an
        # FFmpegVideoConvertor pass that remuxes (or re-encodes when codecs
        # don't fit the target container) into the chosen format. Replaces
        # the older single-purpose "Force MP4" checkbox so the user can
        # request MKV / MOV / AVI / FLV / MPEG / WebM too.
        container_frame = ctk.CTkFrame(opts, fg_color="transparent")
        container_frame.grid(row=1, column=3, columnspan=2,
                             padx=(20, 12), pady=4, sticky="w")
        self.container_lbl = ctk.CTkLabel(container_frame, text="")
        self.container_lbl.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append(
            (self.container_lbl, "text", "label.container")
        )

        self.container_var = tk.StringVar(
            value=self.i18n.localize_option(
                self.settings.container or "None"
            )
        )
        self.container_menu = ctk.CTkOptionMenu(
            container_frame, variable=self.container_var,
            values=self._current_container_values(), width=170,
        )
        self.container_menu.pack(side="left")

        # Re-encode-to-H.264/MP4 checkbox — guaranteed-compatible output
        # at the cost of a slow CPU pass. Wins over the container choice
        # (output is always .mp4). Lives in the same row as the container
        # dropdown so it's visually tied to the container concept.
        self.reencode_var = tk.BooleanVar(value=bool(self.settings.reencode_h264))
        self.reencode_chk = ctk.CTkCheckBox(
            container_frame, text="", variable=self.reencode_var,
        )
        self.reencode_chk.pack(side="left", padx=(16, 0))
        self._labels_to_translate.append(
            (self.reencode_chk, "text", "check.reencode_h264")
        )

        # Keep the wrapper frame around so we can show/hide the whole thing
        # when the user flips between video and audio modes.
        self.container_frame = container_frame

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

        # Right-aligned cluster: "Check for Updates" button + auto-update
        # toggle. Column 3 absorbs leftover space (weight=1) so this group
        # snaps to the right edge regardless of window width.
        update_group = ctk.CTkFrame(actions, fg_color="transparent")
        update_group.grid(row=0, column=3, sticky="e", padx=(6, 0))

        self.auto_update_var = tk.BooleanVar(
            value=bool(self.settings.auto_update_check)
        )
        self.auto_update_chk = ctk.CTkCheckBox(
            update_group, text="", variable=self.auto_update_var,
            command=self._on_auto_update_toggle,
        )
        self.auto_update_chk.pack(side="left", padx=(0, 8))
        self._labels_to_translate.append(
            (self.auto_update_chk, "text", "check.auto_update")
        )

        self.check_updates_btn = ctk.CTkButton(
            update_group, text="", height=34, width=150,
            fg_color="#3a3a3a", hover_color="#2a2a2a",
            command=self._on_check_updates,
        )
        self.check_updates_btn.pack(side="left")
        self._labels_to_translate.append(
            (self.check_updates_btn, "text", "button.check_updates")
        )

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
        # The log is read-only but we still want copy + select-all via
        # keyboard / right-click. is_text=True for index handling.
        _install_text_qol_bindings(
            self.log, is_text=True, get_lang=lambda: self.i18n
        )

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

    def _internal_container_options(self) -> list[str]:
        return video_container_options()

    def _current_container_values(self) -> list[str]:
        return [self.i18n.localize_option(k) for k in self._internal_container_options()]

    def _container_internal(self) -> str:
        return self.i18n.delocalize_option(
            self.container_var.get(), self._internal_container_options()
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

    def _maybe_show_js_runtime_banner(self) -> None:
        """Surface the "install Deno" banner when a task failed because no
        JS runtime is on PATH. One-shot per session."""
        if self._js_runtime_banner_shown:
            return
        self._js_runtime_banner_shown = True

        plan = installer.plan_install("deno")
        if plan.pm is None or plan.requires_manual:
            hint = plan.manual_hint or self.i18n.t("banner.no_pm")
            self._show_banner(
                "js-runtime",
                message=self.i18n.t("banner.js_runtime_missing") + "  " + hint,
                accent="#7a3030",
            )
            return
        self._show_banner(
            "js-runtime",
            message=self.i18n.t("banner.js_runtime_missing"),
            action=self.i18n.t("button.install"),
            on_action=self._on_install_deno,
            accent="#7a3030",
        )

    def _on_install_deno(self) -> None:
        plan = installer.plan_install("deno")
        if plan.requires_manual:
            self._log(self.i18n.t("banner.manual_install"))
            self._log("  " + (plan.manual_hint or "manual install required"))
            return
        self._log(self.i18n.t("log.installing", package="deno"))
        self._log(self.i18n.t("log.install_running", cmd=" ".join(plan.command)))
        self._dismiss_banner("js-runtime")
        threading.Thread(
            target=self._run_install_bg,
            args=(plan, "deno"),
            daemon=True,
        ).start()

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
        # Per-package post-install probe: even if the PM returns 0, the
        # binary might not be on PATH for this Python process yet (Windows
        # PATH refresh quirk, etc.). Probe explicitly.
        probe_failed = False
        if ok:
            if package == "ffmpeg":
                probe_failed = not has_ffmpeg()
            elif package == "deno":
                probe_failed = not has_js_runtime()
        if ok and not probe_failed:
            self.after(0, lambda: self._log(self.i18n.t("log.install_done",
                                                        package=package)))
        else:
            err = summary if not ok else "binary still not on PATH"
            self.after(0, lambda e=err: self._log(self.i18n.t(
                "log.install_failed", error=e)))
            # Restore the banner so the user can try again.
            if package == "ffmpeg" and not has_ffmpeg():
                self.after(0, self._check_ffmpeg_at_startup)
            elif package == "deno" and not has_js_runtime():
                self.after(0, self._reshow_js_runtime_banner)

    def _reshow_js_runtime_banner(self) -> None:
        """Re-arm and re-display the JS-runtime banner after an install
        attempt failed, so the user can try again."""
        self._js_runtime_banner_shown = False
        self._maybe_show_js_runtime_banner()

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

    def _on_check_updates(self) -> None:
        """Manual "Check for Updates" button. Same logic as the startup
        check but always logs a result (so the user knows the click did
        something even when there's nothing to update)."""
        self._log(self.i18n.t("log.update_checking"))
        threading.Thread(
            target=self._manual_check_updates_bg, daemon=True
        ).start()

    def _manual_check_updates_bg(self) -> None:
        if updater.can_enable_auto_update():
            self.after(0, lambda: self._show_banner(
                "git-init",
                message=self.i18n.t("banner.no_git_checkout"),
                action=self.i18n.t("button.enable_auto_update"),
                on_action=self._on_enable_auto_update,
                accent="#2a4a78",
            ))
            return
        behind, err = updater.commits_behind()
        if err is not None:
            self.after(0, lambda e=err: self._log(
                self.i18n.t("log.update_failed", error=e)))
            return
        if behind <= 0:
            self.after(0, lambda: self._log(self.i18n.t("log.update_uptodate")))
            return
        self.after(0, lambda n=behind: self._show_banner(
            "update",
            message=self.i18n.t("banner.update_available", n=n),
            action=self.i18n.t("button.update_now"),
            on_action=self._on_update_now,
            accent="#2a4a78",
        ))

    def _on_auto_update_toggle(self) -> None:
        """User flipped the "Auto-update at startup" checkbox. Persist and
        log so the change is visible."""
        enabled = bool(self.auto_update_var.get())
        self.settings.auto_update_check = enabled
        self.settings.save()
        self._log(self.i18n.t(
            "log.auto_update_toggled",
            state=self.i18n.t("auto_update.on" if enabled else "auto_update.off"),
        ))

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

    def _handle_cookies_failure(self, task: DownloadTask) -> None:
        """Decide what to do when a download fails for a cookies-related
        reason — either YouTube's bot-check rate-limit or a yt-dlp DPAPI
        decryption failure on a Chromium-based browser.

        In "Auto" mode this walks the candidate-browser list, retrying the
        task with each browser at most once per session. When candidates
        are exhausted (or we're not on Auto) it falls back to the manual-
        pick banner.
        """
        per_task = (task.cookies_browser or "Off")

        if per_task == "Auto":
            # Walk to the next candidate browser we haven't tried yet.
            for candidate in browser_detect.candidate_browsers():
                if candidate in self._auto_cookies_tried:
                    continue
                self._auto_cookies_tried.add(candidate)
                self.manager.set_auto_cookies_browser(candidate)
                self._log(self.i18n.t(
                    "log.auto_cookies_switched",
                    browser=browser_detect.display_name_for(candidate),
                ))
                if self.manager.retry_task(task.id):
                    return  # retried; row will refresh as queued→running
                break  # task wasn't retryable — fall through to banner
            else:
                # No untried candidate has a cookie jar — nothing left to try.
                self._log(self.i18n.t("log.auto_cookies_no_browser"))

        # Either Auto is exhausted, or the user explicitly chose Off/a
        # specific browser — surface the manual-pick banner once.
        if self._bot_detection_banner_shown:
            return
        if self._cookies_internal() not in ("Off", "Auto"):
            return  # user already picked a specific browser; banner moot
        self._bot_detection_banner_shown = True
        self._show_banner(
            "bot-detection",
            message=self.i18n.t("banner.bot_detection"),
            accent="#7a5a30",
        )

    def _prompt_restart(self) -> None:
        if not messagebox.askyesno(
            self.i18n.t("msg.restart.title"),
            self.i18n.t("msg.restart.body"),
        ):
            return
        self._persist_user_prefs()
        # os.execv replaces the current process but on Windows under a GUI
        # parent that races with Tk teardown and the user sees the window
        # vanish without a new one appearing. Spawn a detached child first,
        # then quit the current process. The new instance comes up clean.
        try:
            kwargs: dict = {"close_fds": True}
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — keeps the
                # child alive after this process exits and detaches it
                # from any parent console.
                kwargs["creationflags"] = 0x00000008 | 0x00000200
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(
                [sys.executable, *sys.argv],
                cwd=str(Path(__file__).resolve().parent),
                **kwargs,
            )
        except OSError as e:
            self._log(f"Failed to spawn restart process: {e}")
            return
        # Tear down the current Tk loop after Tk has finished returning
        # from this callback. quit() exits mainloop; destroy() releases
        # widgets. Doing it via after(0, ...) avoids killing the dialog
        # while it's still on the stack.
        self.after(50, self._destroy_for_restart)

    def _destroy_for_restart(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
        # If destroy didn't take the interpreter down (e.g. mainloop
        # already exited for some reason), exit explicitly so the parent
        # batch / shell doesn't hold a zombie.
        sys.exit(0)

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

        if hasattr(self, "container_menu"):
            cn_keys = self._internal_container_options()
            current_internal = self._container_internal()
            if current_internal not in cn_keys:
                current_internal = "None"
            self.container_menu.configure(values=self._current_container_values())
            self.container_var.set(self.i18n.localize_option(current_internal))

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

        # Container conversion only applies to video downloads.
        if self.mode_var.get() == "audio":
            self.container_frame.grid_remove()
        else:
            self.container_frame.grid()

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
        container = (
            self._container_internal() if mode == "video"
            else "None"
        )
        reencode_h264 = bool(self.reencode_var.get()) and mode == "video"
        fragments = _parse_fragments(self.fragments_var.get())
        cookies_browser = self._cookies_internal()
        for url in urls:
            self.manager.add(
                url, out_dir, mode, quality, codec, playlist,
                container=container,
                reencode_h264=reencode_h264,
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
                    err_lower = (task.error or "").lower()
                    if (BOT_DETECTION_SIGNATURE in err_lower
                            or COOKIES_DECRYPT_FAILED_SIGNATURE in err_lower):
                        self._handle_cookies_failure(task)
                    elif (NO_VIDEO_FORMAT_SIGNATURE in err_lower
                          and not has_js_runtime()):
                        self._maybe_show_js_runtime_banner()
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
        """× button handler — meaning depends on current task state.

        For active tasks (queued/running) we cancel and let the manager
        emit the lifecycle event. For terminal tasks (done/failed/
        cancelled) we remove the row from the queue immediately, since
        cancelling them is meaningless and the user clearly wants the
        row gone."""
        task = self.manager.tasks.get(task_id)
        if task is None:
            # Task already cleaned out — still try to drop a stale row.
            row = self.rows.pop(task_id, None)
            if row is not None:
                row.destroy()
                self._update_empty_visibility()
            return
        if task.status in ("done", "failed", "cancelled"):
            # Remove this specific task from the manager + GUI without
            # touching the others (clear_done would nuke them all).
            with self.manager._lock:
                self.manager.tasks.pop(task_id, None)
            row = self.rows.pop(task_id, None)
            if row is not None:
                row.destroy()
            self._update_empty_visibility()
            return
        # queued / running → cancel; manager emits a task_done event that
        # the regular event handler will use to refresh this row.
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
        self.settings.container = self._container_internal()
        self.settings.reencode_h264 = bool(self.reencode_var.get())
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
