"""Tiny dict-based i18n for English + Turkish.

A single `I18n` instance is created at startup. Widgets register themselves
via `on_change()` callbacks so they re-render their labels when the user
flips the language toggle at runtime.
"""
from __future__ import annotations

import locale
import os
from typing import Callable

LANGUAGES: tuple[str, ...] = ("en", "tr")
DEFAULT_LANG = "en"

_T: dict[str, dict[str, str]] = {
    "en": {
        "app.title": "YouTube Downloader",
        "header.title": "YouTube Downloader",

        "field.url": "URL(s)",
        "field.url.placeholder": "Paste one YouTube URL per line, then click Add…",
        "field.save_to": "Save to",

        "button.add": "Add to queue",
        "button.browse": "Browse",
        "button.paste": "Paste",
        "button.cancel_all": "Cancel all",
        "button.clear_done": "Clear finished",
        "button.open_folder": "Open folder",
        "button.cancel": "Cancel",

        "label.format": "Format",
        "radio.video": "Video",
        "radio.audio": "Audio",
        "label.codec": "Codec",
        "label.quality": "Quality",
        "check.force_mp4": "Force MP4 (slow)",
        "check.playlist": "Download as playlist",
        "label.max_concurrent": "Max concurrent",

        "label.queue": "Queue",
        "label.log": "Log",

        "queue.empty": "No downloads queued. Paste URLs above and click Add.",

        "status.queued": "queued",
        "status.running": "downloading",
        "status.done": "done",
        "status.failed": "failed",
        "status.cancelled": "cancelled",

        "log.ready": "Ready. Paste URLs and click Add.",
        "log.no_ffmpeg": "Warning: ffmpeg not found on PATH. Audio (mp3) extraction and high-quality video merging will fail.",
        "log.no_ffmpeg.install": "         Install: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Added: {url}",
        "log.task_started": "Starting: {title}",
        "log.task_done": "Finished: {title}",
        "log.task_failed": "Failed: {title}  —  {error}",
        "log.task_cancelled": "Cancelled: {title}",

        "msg.no_url.title": "No URLs",
        "msg.no_url.body": "Paste at least one YouTube URL first.",
        "msg.bad_folder.title": "Output folder",
        "msg.bad_folder.body": "Cannot create folder:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg is not installed. Audio extraction and high-quality video merging will fail.",
        "banner.update_available": "An update is available ({n} new commit(s) on origin/main).",
        "banner.no_pm": "ffmpeg is not installed and no supported package manager was found on this system. Install it manually and restart the app.",
        "banner.manual_install": "Run this command yourself to install:",
        "button.install": "Install",
        "button.update_now": "Update now",
        "button.dismiss": "Dismiss",
        "log.installing": "Installing {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "Installed {package}.",
        "log.install_failed": "Install failed: {error}",
        "log.update_checking": "Checking for updates…",
        "log.update_uptodate": "Up to date.",
        "log.update_running": "Pulling latest from origin/main…",
        "log.update_done": "Updated. Restart the app to use the new version.",
        "log.update_failed": "Update failed: {error}",
        "msg.restart.title": "Restart required",
        "msg.restart.body": "The app has been updated. Restart now to use the new version?",
    },
    "tr": {
        "app.title": "YouTube İndirici",
        "header.title": "YouTube İndirici",

        "field.url": "Bağlantı(lar)",
        "field.url.placeholder": "Her satıra bir YouTube bağlantısı yapıştırıp Ekle'ye basın…",
        "field.save_to": "Kaydet",

        "button.add": "Kuyruğa ekle",
        "button.browse": "Gözat",
        "button.paste": "Yapıştır",
        "button.cancel_all": "Tümünü iptal et",
        "button.clear_done": "Tamamlananları temizle",
        "button.open_folder": "Klasörü aç",
        "button.cancel": "İptal",

        "label.format": "Biçim",
        "radio.video": "Video",
        "radio.audio": "Ses",
        "label.codec": "Kodek",
        "label.quality": "Kalite",
        "check.force_mp4": "MP4'e zorla (yavaş)",
        "check.playlist": "Oynatma listesi olarak indir",
        "label.max_concurrent": "Eş zamanlı sayısı",

        "label.queue": "Kuyruk",
        "label.log": "Günlük",

        "queue.empty": "Kuyrukta indirme yok. Yukarıya bağlantı yapıştırıp Ekle'ye basın.",

        "status.queued": "kuyrukta",
        "status.running": "indiriliyor",
        "status.done": "tamamlandı",
        "status.failed": "başarısız",
        "status.cancelled": "iptal edildi",

        "log.ready": "Hazır. Bağlantıları yapıştırıp Ekle'ye basın.",
        "log.no_ffmpeg": "Uyarı: ffmpeg PATH'te bulunamadı. Ses (mp3) çıkarma ve yüksek kaliteli video birleştirme başarısız olur.",
        "log.no_ffmpeg.install": "         Kurulum: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Eklendi: {url}",
        "log.task_started": "Başlıyor: {title}",
        "log.task_done": "Tamamlandı: {title}",
        "log.task_failed": "Başarısız: {title}  —  {error}",
        "log.task_cancelled": "İptal edildi: {title}",

        "msg.no_url.title": "Bağlantı yok",
        "msg.no_url.body": "Lütfen önce en az bir YouTube bağlantısı yapıştırın.",
        "msg.bad_folder.title": "Çıkış klasörü",
        "msg.bad_folder.body": "Klasör oluşturulamadı:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg kurulu değil. Ses çıkarma ve yüksek kaliteli video birleştirme başarısız olur.",
        "banner.update_available": "Bir güncelleme mevcut (origin/main'de {n} yeni commit).",
        "banner.no_pm": "ffmpeg kurulu değil ve sisteminizde desteklenen bir paket yöneticisi bulunamadı. Lütfen elle kurun ve uygulamayı yeniden başlatın.",
        "banner.manual_install": "Kurmak için bu komutu kendiniz çalıştırın:",
        "button.install": "Kur",
        "button.update_now": "Şimdi güncelle",
        "button.dismiss": "Kapat",
        "log.installing": "{package} kuruluyor…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} kuruldu.",
        "log.install_failed": "Kurulum başarısız: {error}",
        "log.update_checking": "Güncellemeler kontrol ediliyor…",
        "log.update_uptodate": "Güncel.",
        "log.update_running": "origin/main'den en son sürüm alınıyor…",
        "log.update_done": "Güncellendi. Yeni sürümü kullanmak için uygulamayı yeniden başlatın.",
        "log.update_failed": "Güncelleme başarısız: {error}",
        "msg.restart.title": "Yeniden başlatma gerekli",
        "msg.restart.body": "Uygulama güncellendi. Yeni sürümü kullanmak için şimdi yeniden başlatılsın mı?",
    },
}


def autodetect_language() -> str:
    """Return 'tr' if the user's system locale looks Turkish, else 'en'."""
    candidates: list[str] = []
    try:
        loc = locale.getlocale()[0]
        if loc:
            candidates.append(loc)
    except (ValueError, AttributeError):
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v:
            candidates.append(v)
            break
    for c in candidates:
        head = c.lower()
        # matches "tr", "tr_TR", "tr_TR.UTF-8", "turkish_türkiye"
        if head.startswith(("tr_", "tr.", "tur")):
            return "tr"
        if head == "tr":
            return "tr"
    return "en"


class I18n:
    def __init__(self, lang: str = DEFAULT_LANG) -> None:
        self.lang = lang if lang in LANGUAGES else DEFAULT_LANG
        self._listeners: list[Callable[[], None]] = []

    def t(self, key: str, **kwargs: object) -> str:
        table = _T.get(self.lang) or _T[DEFAULT_LANG]
        s = table.get(key) or _T[DEFAULT_LANG].get(key, key)
        if kwargs:
            try:
                s = s.format(**kwargs)
            except (KeyError, IndexError):
                pass
        return s

    def set_language(self, lang: str) -> None:
        if lang not in LANGUAGES or lang == self.lang:
            return
        self.lang = lang
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)
