"""Tiny dict-based i18n for English + Turkish.

A single `I18n` instance is created at startup. Widgets register themselves
via `on_change()` callbacks so they re-render their labels when the user
flips the language toggle at runtime.
"""
from __future__ import annotations

import locale
import os
from typing import Callable

# Arabic shaping: Tk 8.6 has no built-in HarfBuzz integration, so Arabic
# letters render in their isolated forms (disconnected). `arabic-reshaper`
# substitutes the contextual presentation-form glyphs and `python-bidi`
# rewrites the string to visual order so the LTR Tk renderer places them
# correctly. Both libs are pure-Python pip deps. If they're missing the
# helper degrades to identity — Arabic still displays, just not pretty.
try:
    import arabic_reshaper as _arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
    _HAS_RTL_SHAPING = True
except ImportError:
    _HAS_RTL_SHAPING = False


def _shape_rtl(text: str) -> str:
    """Pre-shape an RTL string (presentation forms + bidi) for Tk."""
    if not _HAS_RTL_SHAPING or not text:
        return text
    try:
        return _bidi_get_display(_arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001 — never crash translations
        return text

LANGUAGES: tuple[str, ...] = (
    "en", "tr", "es", "fr", "de", "ru", "ar", "zh", "ja",
)
DEFAULT_LANG = "en"

# Native names shown in the language picker. The dropdown keeps the user-
# facing label in their own script so they can find their language without
# knowing English. `code_for_display()` reverses this for the on_change hook.
LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "tr": "Türkçe",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "ru": "Русский",
    "ar": "العربية",
    "zh": "中文",
    "ja": "日本語",
}

# Internal option keys that should be translated for display. Everything not
# in this set is technical (codec names, resolutions, bitrates) and stays
# English in every language.
LOCALIZED_OPTIONS: frozenset[str] = frozenset({"Best", "Worst", "Auto", "Original"})

# locale.getlocale() / $LANG values that should map to each language. Order
# matters — the first matching prefix wins. Lowercase comparison.
_LOCALE_PREFIXES: dict[str, tuple[str, ...]] = {
    "tr": ("tr_", "tr.", "tur", "turkish"),
    "es": ("es_", "es.", "spa", "spanish"),
    "fr": ("fr_", "fr.", "fre", "fra", "french"),
    "de": ("de_", "de.", "ger", "deu", "german"),
    "ru": ("ru_", "ru.", "rus", "russian"),
    "ar": ("ar_", "ar.", "ara", "arabic"),
    "zh": ("zh_", "zh.", "chi", "zho", "chinese"),
    "ja": ("ja_", "ja.", "jpn", "japanese"),
}

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
        # Dropdown values that should be localized (rest are technical):
        "option.best": "Best",
        "option.worst": "Worst",
        "option.auto": "Auto",
        "option.original": "Original",
        "check.force_mp4": "Force MP4 (re-encode if needed — slow)",
        "check.playlist": "Download as playlist",
        "label.max_concurrent": "Max concurrent",
        "label.fragments": "Parallel parts",

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
        "banner.no_git_checkout": "Auto-update is disabled — this folder isn't a git checkout. Connect to GitHub to enable updates? (Local file edits will be overwritten.)",
        "banner.no_pm": "ffmpeg is not installed and no supported package manager was found on this system. Install it manually and restart the app.",
        "banner.manual_install": "Run this command yourself to install:",
        "button.install": "Install",
        "button.update_now": "Update now",
        "button.enable_auto_update": "Enable",
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
        "log.auto_update_enabling": "Connecting this folder to GitHub origin/main…",
        "log.auto_update_enabled": "Auto-update enabled. Files synced to latest origin/main.",
        "log.auto_update_enable_failed": "Failed to enable auto-update: {error}",
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
        # Dropdown values that should be localized (rest are technical):
        "option.best": "En iyi",
        "option.worst": "En kötü",
        "option.auto": "Otomatik",
        "option.original": "Orijinal",
        "check.force_mp4": "MP4'e zorla (gerekirse yeniden kodla — yavaş)",
        "check.playlist": "Oynatma listesi olarak indir",
        "label.max_concurrent": "Eş zamanlı sayısı",
        "label.fragments": "Paralel parça",

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
        "banner.no_git_checkout": "Otomatik güncelleme devre dışı — bu klasör bir git deposu değil. Güncellemeleri etkinleştirmek için GitHub'a bağlanılsın mı? (Yerel dosya düzenlemeleri üzerine yazılır.)",
        "banner.no_pm": "ffmpeg kurulu değil ve sisteminizde desteklenen bir paket yöneticisi bulunamadı. Lütfen elle kurun ve uygulamayı yeniden başlatın.",
        "banner.manual_install": "Kurmak için bu komutu kendiniz çalıştırın:",
        "button.install": "Kur",
        "button.update_now": "Şimdi güncelle",
        "button.enable_auto_update": "Etkinleştir",
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
        "log.auto_update_enabling": "Bu klasör GitHub origin/main'e bağlanıyor…",
        "log.auto_update_enabled": "Otomatik güncelleme etkin. Dosyalar en son origin/main ile eşitlendi.",
        "log.auto_update_enable_failed": "Otomatik güncelleme etkinleştirilemedi: {error}",
        "msg.restart.title": "Yeniden başlatma gerekli",
        "msg.restart.body": "Uygulama güncellendi. Yeni sürümü kullanmak için şimdi yeniden başlatılsın mı?",
    },
    "es": {
        "app.title": "Descargador de YouTube",
        "header.title": "Descargador de YouTube",

        "field.url": "URL",
        "field.url.placeholder": "Pegue un enlace de YouTube por línea, luego pulse Añadir…",
        "field.save_to": "Guardar en",

        "button.add": "Añadir a la cola",
        "button.browse": "Examinar",
        "button.paste": "Pegar",
        "button.cancel_all": "Cancelar todo",
        "button.clear_done": "Limpiar terminados",
        "button.open_folder": "Abrir carpeta",
        "button.cancel": "Cancelar",

        "label.format": "Formato",
        "radio.video": "Vídeo",
        "radio.audio": "Audio",
        "label.codec": "Códec",
        "label.quality": "Calidad",
        "check.force_mp4": "Forzar MP4 (recodificar si es necesario — lento)",
        "check.playlist": "Descargar como lista de reproducción",
        "label.max_concurrent": "Máx. simultáneas",
        "label.fragments": "Partes paralelas",

        "label.queue": "Cola",
        "label.log": "Registro",

        "option.best": "Mejor",
        "option.worst": "Peor",
        "option.auto": "Automático",
        "option.original": "Original",

        "queue.empty": "No hay descargas en cola. Pegue URLs arriba y pulse Añadir.",

        "status.queued": "en cola",
        "status.running": "descargando",
        "status.done": "completado",
        "status.failed": "fallido",
        "status.cancelled": "cancelado",

        "log.ready": "Listo. Pegue URLs y pulse Añadir.",
        "log.no_ffmpeg": "Aviso: ffmpeg no encontrado en PATH. La extracción de audio y la combinación de vídeo de alta calidad fallarán.",
        "log.no_ffmpeg.install": "         Instalar: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Añadido: {url}",
        "log.task_started": "Iniciando: {title}",
        "log.task_done": "Terminado: {title}",
        "log.task_failed": "Fallido: {title}  —  {error}",
        "log.task_cancelled": "Cancelado: {title}",

        "msg.no_url.title": "Sin URLs",
        "msg.no_url.body": "Pegue al menos un enlace de YouTube primero.",
        "msg.bad_folder.title": "Carpeta de salida",
        "msg.bad_folder.body": "No se puede crear la carpeta:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg no está instalado. La extracción de audio y la combinación de vídeo de alta calidad fallarán.",
        "banner.update_available": "Hay una actualización disponible ({n} commit(s) nuevos en origin/main).",
        "banner.no_git_checkout": "Auto-actualización deshabilitada — esta carpeta no es un repositorio git. ¿Conectar a GitHub para habilitar actualizaciones? (Las ediciones locales serán sobrescritas.)",
        "banner.no_pm": "ffmpeg no está instalado y no se encontró un gestor de paquetes compatible. Instálelo manualmente y reinicie la app.",
        "banner.manual_install": "Ejecute este comando para instalar:",
        "button.install": "Instalar",
        "button.update_now": "Actualizar ahora",
        "button.enable_auto_update": "Habilitar",
        "button.dismiss": "Descartar",
        "log.installing": "Instalando {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} instalado.",
        "log.install_failed": "Instalación fallida: {error}",
        "log.update_checking": "Comprobando actualizaciones…",
        "log.update_uptodate": "Actualizado.",
        "log.update_running": "Descargando lo último desde origin/main…",
        "log.update_done": "Actualizado. Reinicie la app para usar la nueva versión.",
        "log.update_failed": "Actualización fallida: {error}",
        "log.auto_update_enabling": "Conectando esta carpeta a GitHub origin/main…",
        "log.auto_update_enabled": "Auto-actualización habilitada. Archivos sincronizados con origin/main.",
        "log.auto_update_enable_failed": "No se pudo habilitar la auto-actualización: {error}",
        "msg.restart.title": "Reinicio requerido",
        "msg.restart.body": "La app se ha actualizado. ¿Reiniciar ahora para usar la nueva versión?",
    },
    "fr": {
        "app.title": "Téléchargeur YouTube",
        "header.title": "Téléchargeur YouTube",

        "field.url": "URL",
        "field.url.placeholder": "Collez une URL YouTube par ligne, puis cliquez sur Ajouter…",
        "field.save_to": "Enregistrer dans",

        "button.add": "Ajouter à la file",
        "button.browse": "Parcourir",
        "button.paste": "Coller",
        "button.cancel_all": "Tout annuler",
        "button.clear_done": "Effacer terminés",
        "button.open_folder": "Ouvrir le dossier",
        "button.cancel": "Annuler",

        "label.format": "Format",
        "radio.video": "Vidéo",
        "radio.audio": "Audio",
        "label.codec": "Codec",
        "label.quality": "Qualité",
        "check.force_mp4": "Forcer MP4 (réencoder si nécessaire — lent)",
        "check.playlist": "Télécharger comme playlist",
        "label.max_concurrent": "Téléch. simultanés max",
        "label.fragments": "Parties parallèles",

        "label.queue": "File d'attente",
        "label.log": "Journal",

        "option.best": "Meilleur",
        "option.worst": "Pire",
        "option.auto": "Auto",
        "option.original": "Original",

        "queue.empty": "Aucun téléchargement en file. Collez des URL ci-dessus et cliquez sur Ajouter.",

        "status.queued": "en file",
        "status.running": "téléchargement",
        "status.done": "terminé",
        "status.failed": "échoué",
        "status.cancelled": "annulé",

        "log.ready": "Prêt. Collez des URL et cliquez sur Ajouter.",
        "log.no_ffmpeg": "Avertissement : ffmpeg introuvable dans PATH. L'extraction audio et la fusion vidéo haute qualité échoueront.",
        "log.no_ffmpeg.install": "         Installer : winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Ajouté : {url}",
        "log.task_started": "Démarrage : {title}",
        "log.task_done": "Terminé : {title}",
        "log.task_failed": "Échec : {title}  —  {error}",
        "log.task_cancelled": "Annulé : {title}",

        "msg.no_url.title": "Aucune URL",
        "msg.no_url.body": "Collez d'abord au moins une URL YouTube.",
        "msg.bad_folder.title": "Dossier de sortie",
        "msg.bad_folder.body": "Impossible de créer le dossier :\n{error}",

        "banner.ffmpeg_missing": "ffmpeg n'est pas installé. L'extraction audio et la fusion vidéo haute qualité échoueront.",
        "banner.update_available": "Une mise à jour est disponible ({n} nouveau(x) commit(s) sur origin/main).",
        "banner.no_git_checkout": "Auto-mise à jour désactivée — ce dossier n'est pas un dépôt git. Se connecter à GitHub pour activer les mises à jour ? (Les modifications locales seront écrasées.)",
        "banner.no_pm": "ffmpeg n'est pas installé et aucun gestionnaire de paquets compatible n'a été trouvé. Installez-le manuellement et relancez l'app.",
        "banner.manual_install": "Exécutez cette commande pour installer :",
        "button.install": "Installer",
        "button.update_now": "Mettre à jour",
        "button.enable_auto_update": "Activer",
        "button.dismiss": "Ignorer",
        "log.installing": "Installation de {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} installé.",
        "log.install_failed": "Échec de l'installation : {error}",
        "log.update_checking": "Recherche de mises à jour…",
        "log.update_uptodate": "À jour.",
        "log.update_running": "Récupération depuis origin/main…",
        "log.update_done": "Mise à jour effectuée. Redémarrez l'app pour utiliser la nouvelle version.",
        "log.update_failed": "Échec de la mise à jour : {error}",
        "log.auto_update_enabling": "Connexion de ce dossier à GitHub origin/main…",
        "log.auto_update_enabled": "Auto-mise à jour activée. Fichiers synchronisés avec origin/main.",
        "log.auto_update_enable_failed": "Échec de l'activation de l'auto-mise à jour : {error}",
        "msg.restart.title": "Redémarrage requis",
        "msg.restart.body": "L'app a été mise à jour. Redémarrer maintenant pour utiliser la nouvelle version ?",
    },
    "de": {
        "app.title": "YouTube-Downloader",
        "header.title": "YouTube-Downloader",

        "field.url": "URL(s)",
        "field.url.placeholder": "Fügen Sie eine YouTube-URL pro Zeile ein und klicken Sie auf Hinzufügen…",
        "field.save_to": "Speichern unter",

        "button.add": "Zur Warteschlange",
        "button.browse": "Durchsuchen",
        "button.paste": "Einfügen",
        "button.cancel_all": "Alle abbrechen",
        "button.clear_done": "Fertige löschen",
        "button.open_folder": "Ordner öffnen",
        "button.cancel": "Abbrechen",

        "label.format": "Format",
        "radio.video": "Video",
        "radio.audio": "Audio",
        "label.codec": "Codec",
        "label.quality": "Qualität",
        "check.force_mp4": "MP4 erzwingen (ggf. neu kodieren — langsam)",
        "check.playlist": "Als Playlist herunterladen",
        "label.max_concurrent": "Max. gleichzeitig",
        "label.fragments": "Parallele Teile",

        "label.queue": "Warteschlange",
        "label.log": "Protokoll",

        "option.best": "Beste",
        "option.worst": "Schlechteste",
        "option.auto": "Automatisch",
        "option.original": "Original",

        "queue.empty": "Keine Downloads in der Warteschlange. URLs oben einfügen und Hinzufügen klicken.",

        "status.queued": "in Warteschlange",
        "status.running": "wird heruntergeladen",
        "status.done": "fertig",
        "status.failed": "fehlgeschlagen",
        "status.cancelled": "abgebrochen",

        "log.ready": "Bereit. URLs einfügen und Hinzufügen klicken.",
        "log.no_ffmpeg": "Warnung: ffmpeg nicht im PATH gefunden. Audio-Extraktion und hochqualitative Video-Zusammenführung schlagen fehl.",
        "log.no_ffmpeg.install": "         Installieren: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Hinzugefügt: {url}",
        "log.task_started": "Starte: {title}",
        "log.task_done": "Fertig: {title}",
        "log.task_failed": "Fehlgeschlagen: {title}  —  {error}",
        "log.task_cancelled": "Abgebrochen: {title}",

        "msg.no_url.title": "Keine URLs",
        "msg.no_url.body": "Bitte zuerst mindestens eine YouTube-URL einfügen.",
        "msg.bad_folder.title": "Ausgabeordner",
        "msg.bad_folder.body": "Ordner kann nicht erstellt werden:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg ist nicht installiert. Audio-Extraktion und hochqualitative Video-Zusammenführung schlagen fehl.",
        "banner.update_available": "Ein Update ist verfügbar ({n} neue(r) Commit(s) auf origin/main).",
        "banner.no_git_checkout": "Auto-Update deaktiviert — dieser Ordner ist kein git-Checkout. Mit GitHub verbinden, um Updates zu aktivieren? (Lokale Dateiänderungen werden überschrieben.)",
        "banner.no_pm": "ffmpeg ist nicht installiert und kein unterstützter Paketmanager wurde gefunden. Bitte manuell installieren und die App neu starten.",
        "banner.manual_install": "Führen Sie diesen Befehl selbst aus, um zu installieren:",
        "button.install": "Installieren",
        "button.update_now": "Jetzt aktualisieren",
        "button.enable_auto_update": "Aktivieren",
        "button.dismiss": "Ablehnen",
        "log.installing": "Installiere {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} installiert.",
        "log.install_failed": "Installation fehlgeschlagen: {error}",
        "log.update_checking": "Suche nach Updates…",
        "log.update_uptodate": "Aktuell.",
        "log.update_running": "Hole neueste Version von origin/main…",
        "log.update_done": "Aktualisiert. Bitte App neu starten, um die neue Version zu verwenden.",
        "log.update_failed": "Update fehlgeschlagen: {error}",
        "log.auto_update_enabling": "Verbinde diesen Ordner mit GitHub origin/main…",
        "log.auto_update_enabled": "Auto-Update aktiviert. Dateien mit origin/main synchronisiert.",
        "log.auto_update_enable_failed": "Auto-Update konnte nicht aktiviert werden: {error}",
        "msg.restart.title": "Neustart erforderlich",
        "msg.restart.body": "Die App wurde aktualisiert. Jetzt neu starten, um die neue Version zu verwenden?",
    },
    "ru": {
        "app.title": "Загрузчик YouTube",
        "header.title": "Загрузчик YouTube",

        "field.url": "Ссылки",
        "field.url.placeholder": "Вставьте по одной ссылке YouTube на строку и нажмите Добавить…",
        "field.save_to": "Сохранить в",

        "button.add": "В очередь",
        "button.browse": "Обзор",
        "button.paste": "Вставить",
        "button.cancel_all": "Отменить все",
        "button.clear_done": "Очистить завершённые",
        "button.open_folder": "Открыть папку",
        "button.cancel": "Отмена",

        "label.format": "Формат",
        "radio.video": "Видео",
        "radio.audio": "Аудио",
        "label.codec": "Кодек",
        "label.quality": "Качество",
        "check.force_mp4": "Принудительно MP4 (перекодировать при необходимости — медленно)",
        "check.playlist": "Скачать как плейлист",
        "label.max_concurrent": "Макс. одновременно",
        "label.fragments": "Парал. частей",

        "label.queue": "Очередь",
        "label.log": "Журнал",

        "option.best": "Лучшее",
        "option.worst": "Худшее",
        "option.auto": "Авто",
        "option.original": "Оригинал",

        "queue.empty": "Очередь пуста. Вставьте ссылки выше и нажмите Добавить.",

        "status.queued": "в очереди",
        "status.running": "загружается",
        "status.done": "готово",
        "status.failed": "ошибка",
        "status.cancelled": "отменено",

        "log.ready": "Готово. Вставьте ссылки и нажмите Добавить.",
        "log.no_ffmpeg": "Предупреждение: ffmpeg не найден в PATH. Извлечение аудио и слияние видео высокого качества не сработают.",
        "log.no_ffmpeg.install": "         Установить: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "Добавлено: {url}",
        "log.task_started": "Начинаем: {title}",
        "log.task_done": "Готово: {title}",
        "log.task_failed": "Ошибка: {title}  —  {error}",
        "log.task_cancelled": "Отменено: {title}",

        "msg.no_url.title": "Нет ссылок",
        "msg.no_url.body": "Сначала вставьте хотя бы одну ссылку YouTube.",
        "msg.bad_folder.title": "Папка вывода",
        "msg.bad_folder.body": "Не удалось создать папку:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg не установлен. Извлечение аудио и слияние видео высокого качества не сработают.",
        "banner.update_available": "Доступно обновление ({n} новых коммитов в origin/main).",
        "banner.no_git_checkout": "Автообновление отключено — эта папка не является git-репозиторием. Подключить к GitHub для включения обновлений? (Локальные правки файлов будут перезаписаны.)",
        "banner.no_pm": "ffmpeg не установлен, и поддерживаемый пакетный менеджер не найден. Установите вручную и перезапустите приложение.",
        "banner.manual_install": "Выполните эту команду для установки:",
        "button.install": "Установить",
        "button.update_now": "Обновить",
        "button.enable_auto_update": "Включить",
        "button.dismiss": "Закрыть",
        "log.installing": "Установка {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} установлен.",
        "log.install_failed": "Ошибка установки: {error}",
        "log.update_checking": "Проверка обновлений…",
        "log.update_uptodate": "Актуальная версия.",
        "log.update_running": "Загрузка последней версии из origin/main…",
        "log.update_done": "Обновлено. Перезапустите приложение, чтобы использовать новую версию.",
        "log.update_failed": "Ошибка обновления: {error}",
        "log.auto_update_enabling": "Подключение этой папки к GitHub origin/main…",
        "log.auto_update_enabled": "Автообновление включено. Файлы синхронизированы с origin/main.",
        "log.auto_update_enable_failed": "Не удалось включить автообновление: {error}",
        "msg.restart.title": "Требуется перезапуск",
        "msg.restart.body": "Приложение обновлено. Перезапустить сейчас, чтобы использовать новую версию?",
    },
    "ar": {
        "app.title": "محمّل يوتيوب",
        "header.title": "محمّل يوتيوب",

        "field.url": "الرابط (الروابط)",
        "field.url.placeholder": "ألصق رابط يوتيوب واحد في كل سطر، ثم انقر إضافة…",
        "field.save_to": "حفظ في",

        "button.add": "إضافة إلى القائمة",
        "button.browse": "تصفّح",
        "button.paste": "لصق",
        "button.cancel_all": "إلغاء الكل",
        "button.clear_done": "مسح المكتملة",
        "button.open_folder": "فتح المجلد",
        "button.cancel": "إلغاء",

        "label.format": "الصيغة",
        "radio.video": "فيديو",
        "radio.audio": "صوت",
        "label.codec": "الترميز",
        "label.quality": "الجودة",
        "check.force_mp4": "إجبار MP4 (إعادة الترميز عند الحاجة — بطيء)",
        "check.playlist": "تحميل كقائمة تشغيل",
        "label.max_concurrent": "الحد الأقصى المتزامن",
        "label.fragments": "أجزاء متوازية",

        "label.queue": "القائمة",
        "label.log": "السجل",

        "option.best": "الأفضل",
        "option.worst": "الأسوأ",
        "option.auto": "تلقائي",
        "option.original": "الأصلي",

        "queue.empty": "لا توجد تحميلات في القائمة. ألصق الروابط في الأعلى وانقر إضافة.",

        "status.queued": "في الانتظار",
        "status.running": "جارٍ التحميل",
        "status.done": "اكتمل",
        "status.failed": "فشل",
        "status.cancelled": "أُلغي",

        "log.ready": "جاهز. ألصق الروابط وانقر إضافة.",
        "log.no_ffmpeg": "تحذير: ffmpeg غير موجود في PATH. سيفشل استخراج الصوت ودمج الفيديو عالي الجودة.",
        "log.no_ffmpeg.install": "         التثبيت: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "أُضيف: {url}",
        "log.task_started": "البدء: {title}",
        "log.task_done": "اكتمل: {title}",
        "log.task_failed": "فشل: {title}  —  {error}",
        "log.task_cancelled": "أُلغي: {title}",

        "msg.no_url.title": "لا توجد روابط",
        "msg.no_url.body": "ألصق رابط يوتيوب واحدًا على الأقل أولًا.",
        "msg.bad_folder.title": "مجلد الإخراج",
        "msg.bad_folder.body": "لا يمكن إنشاء المجلد:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg غير مثبَّت. سيفشل استخراج الصوت ودمج الفيديو عالي الجودة.",
        "banner.update_available": "يتوفر تحديث ({n} commit جديد على origin/main).",
        "banner.no_git_checkout": "التحديث التلقائي معطّل — هذا المجلد ليس مستودع git. هل تريد الاتصال بـ GitHub لتمكين التحديثات؟ (سيتم استبدال أي تعديلات محلية على الملفات.)",
        "banner.no_pm": "ffmpeg غير مثبَّت ولم يُعثر على مدير حزم مدعوم. ثبّته يدويًا وأعد تشغيل التطبيق.",
        "banner.manual_install": "نفّذ هذا الأمر للتثبيت بنفسك:",
        "button.install": "تثبيت",
        "button.update_now": "تحديث الآن",
        "button.enable_auto_update": "تمكين",
        "button.dismiss": "إغلاق",
        "log.installing": "تثبيت {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "تم تثبيت {package}.",
        "log.install_failed": "فشل التثبيت: {error}",
        "log.update_checking": "التحقق من التحديثات…",
        "log.update_uptodate": "محدّث.",
        "log.update_running": "سحب آخر تحديث من origin/main…",
        "log.update_done": "تم التحديث. أعد تشغيل التطبيق لاستخدام الإصدار الجديد.",
        "log.update_failed": "فشل التحديث: {error}",
        "log.auto_update_enabling": "ربط هذا المجلد بـ GitHub origin/main…",
        "log.auto_update_enabled": "تم تمكين التحديث التلقائي. تمت مزامنة الملفات مع origin/main.",
        "log.auto_update_enable_failed": "فشل تمكين التحديث التلقائي: {error}",
        "msg.restart.title": "يلزم إعادة التشغيل",
        "msg.restart.body": "تم تحديث التطبيق. هل تريد إعادة التشغيل الآن لاستخدام الإصدار الجديد؟",
    },
    "zh": {
        "app.title": "YouTube 下载器",
        "header.title": "YouTube 下载器",

        "field.url": "网址",
        "field.url.placeholder": "每行粘贴一个 YouTube 网址,然后点击添加…",
        "field.save_to": "保存到",

        "button.add": "添加到队列",
        "button.browse": "浏览",
        "button.paste": "粘贴",
        "button.cancel_all": "全部取消",
        "button.clear_done": "清除已完成",
        "button.open_folder": "打开文件夹",
        "button.cancel": "取消",

        "label.format": "格式",
        "radio.video": "视频",
        "radio.audio": "音频",
        "label.codec": "编码",
        "label.quality": "质量",
        "check.force_mp4": "强制 MP4(必要时重新编码 — 较慢)",
        "check.playlist": "作为播放列表下载",
        "label.max_concurrent": "最大并发",
        "label.fragments": "并行分段数",

        "label.queue": "队列",
        "label.log": "日志",

        "option.best": "最佳",
        "option.worst": "最差",
        "option.auto": "自动",
        "option.original": "原始",

        "queue.empty": "队列为空。请在上方粘贴网址并点击添加。",

        "status.queued": "等待中",
        "status.running": "下载中",
        "status.done": "已完成",
        "status.failed": "失败",
        "status.cancelled": "已取消",

        "log.ready": "就绪。粘贴网址并点击添加。",
        "log.no_ffmpeg": "警告:PATH 中找不到 ffmpeg。音频提取和高质量视频合并将失败。",
        "log.no_ffmpeg.install": "         安装: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "已添加:{url}",
        "log.task_started": "开始:{title}",
        "log.task_done": "完成:{title}",
        "log.task_failed": "失败:{title}  —  {error}",
        "log.task_cancelled": "已取消:{title}",

        "msg.no_url.title": "没有网址",
        "msg.no_url.body": "请先粘贴至少一个 YouTube 网址。",
        "msg.bad_folder.title": "输出文件夹",
        "msg.bad_folder.body": "无法创建文件夹:\n{error}",

        "banner.ffmpeg_missing": "未安装 ffmpeg。音频提取和高质量视频合并将失败。",
        "banner.update_available": "有可用更新(origin/main 上有 {n} 个新提交)。",
        "banner.no_git_checkout": "自动更新已禁用 — 此文件夹不是 git 仓库。连接到 GitHub 以启用更新?(本地文件修改将被覆盖。)",
        "banner.no_pm": "未安装 ffmpeg,且未找到支持的包管理器。请手动安装并重新启动应用。",
        "banner.manual_install": "请自行运行此命令以安装:",
        "button.install": "安装",
        "button.update_now": "立即更新",
        "button.enable_auto_update": "启用",
        "button.dismiss": "关闭",
        "log.installing": "正在安装 {package}…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "已安装 {package}。",
        "log.install_failed": "安装失败:{error}",
        "log.update_checking": "正在检查更新…",
        "log.update_uptodate": "已是最新版本。",
        "log.update_running": "从 origin/main 拉取最新版本…",
        "log.update_done": "已更新。请重启应用以使用新版本。",
        "log.update_failed": "更新失败:{error}",
        "log.auto_update_enabling": "正在将此文件夹连接到 GitHub origin/main…",
        "log.auto_update_enabled": "自动更新已启用。文件已与 origin/main 同步。",
        "log.auto_update_enable_failed": "启用自动更新失败:{error}",
        "msg.restart.title": "需要重启",
        "msg.restart.body": "应用已更新。是否立即重启以使用新版本?",
    },
    "ja": {
        "app.title": "YouTube ダウンローダー",
        "header.title": "YouTube ダウンローダー",

        "field.url": "URL",
        "field.url.placeholder": "1行に1つの YouTube URL を貼り付けて、追加をクリックしてください…",
        "field.save_to": "保存先",

        "button.add": "キューに追加",
        "button.browse": "参照",
        "button.paste": "貼り付け",
        "button.cancel_all": "すべてキャンセル",
        "button.clear_done": "完了をクリア",
        "button.open_folder": "フォルダを開く",
        "button.cancel": "キャンセル",

        "label.format": "形式",
        "radio.video": "動画",
        "radio.audio": "音声",
        "label.codec": "コーデック",
        "label.quality": "品質",
        "check.force_mp4": "MP4 を強制(必要なら再エンコード — 遅い)",
        "check.playlist": "プレイリストとしてダウンロード",
        "label.max_concurrent": "最大同時数",
        "label.fragments": "並列パート数",

        "label.queue": "キュー",
        "label.log": "ログ",

        "option.best": "最高",
        "option.worst": "最低",
        "option.auto": "自動",
        "option.original": "オリジナル",

        "queue.empty": "キューは空です。上に URL を貼り付けて追加をクリックしてください。",

        "status.queued": "待機中",
        "status.running": "ダウンロード中",
        "status.done": "完了",
        "status.failed": "失敗",
        "status.cancelled": "キャンセル済み",

        "log.ready": "準備完了。URL を貼り付けて追加をクリックしてください。",
        "log.no_ffmpeg": "警告: ffmpeg が PATH に見つかりません。音声抽出と高品質動画のマージは失敗します。",
        "log.no_ffmpeg.install": "         インストール: winget install Gyan.FFmpeg (Windows) / sudo pacman -S ffmpeg (Arch) / brew install ffmpeg (macOS)",
        "log.task_added": "追加されました: {url}",
        "log.task_started": "開始: {title}",
        "log.task_done": "完了: {title}",
        "log.task_failed": "失敗: {title}  —  {error}",
        "log.task_cancelled": "キャンセル: {title}",

        "msg.no_url.title": "URL がありません",
        "msg.no_url.body": "まず YouTube URL を1つ以上貼り付けてください。",
        "msg.bad_folder.title": "出力フォルダ",
        "msg.bad_folder.body": "フォルダを作成できません:\n{error}",

        "banner.ffmpeg_missing": "ffmpeg がインストールされていません。音声抽出と高品質動画のマージは失敗します。",
        "banner.update_available": "アップデートがあります(origin/main に新しいコミットが {n} 件)。",
        "banner.no_git_checkout": "自動更新は無効です — このフォルダは git チェックアウトではありません。更新を有効にするには GitHub に接続しますか?(ローカルのファイル編集は上書きされます。)",
        "banner.no_pm": "ffmpeg がインストールされておらず、対応するパッケージマネージャも見つかりません。手動でインストールしてアプリを再起動してください。",
        "banner.manual_install": "次のコマンドを自分で実行してインストールしてください:",
        "button.install": "インストール",
        "button.update_now": "今すぐ更新",
        "button.enable_auto_update": "有効化",
        "button.dismiss": "閉じる",
        "log.installing": "{package} をインストールしています…",
        "log.install_running": "$ {cmd}",
        "log.install_done": "{package} をインストールしました。",
        "log.install_failed": "インストールに失敗しました: {error}",
        "log.update_checking": "更新を確認しています…",
        "log.update_uptodate": "最新版です。",
        "log.update_running": "origin/main から最新版を取得しています…",
        "log.update_done": "更新しました。新しいバージョンを使うにはアプリを再起動してください。",
        "log.update_failed": "更新に失敗しました: {error}",
        "log.auto_update_enabling": "このフォルダを GitHub origin/main に接続しています…",
        "log.auto_update_enabled": "自動更新が有効になりました。ファイルは origin/main と同期されました。",
        "log.auto_update_enable_failed": "自動更新の有効化に失敗しました: {error}",
        "msg.restart.title": "再起動が必要",
        "msg.restart.body": "アプリが更新されました。新しいバージョンを使うために今すぐ再起動しますか?",
    },
}


def autodetect_language() -> str:
    """Pick a supported language code from the system locale. Falls back to
    English. Checks `locale.getlocale()` first, then $LC_ALL / $LC_MESSAGES
    / $LANG. Matches patterns like 'tr_TR.UTF-8' or 'french_france' against
    `_LOCALE_PREFIXES`."""
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
        for code, prefixes in _LOCALE_PREFIXES.items():
            if head == code or head.startswith(prefixes):
                return code
    return "en"


def display_name(code: str) -> str:
    """Native name of `code`, shaped for display (Arabic gets presentation
    forms + bidi so it renders cursively in Tk; other scripts pass through)."""
    raw = LANGUAGE_DISPLAY_NAMES.get(code, code)
    if code == "ar":
        return _shape_rtl(raw)
    return raw


def code_for_display(display: str) -> str:
    """Return the ISO code for a native-name string from the language
    picker. Compares against the *shaped* display so it works even when
    the user just picked Arabic from the dropdown."""
    for code in LANGUAGES:
        if display_name(code) == display:
            return code
    return DEFAULT_LANG


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
        # Arabic must be shaped AFTER formatting so the bidi algorithm sees
        # any interpolated values (titles, URLs, error strings) in context.
        if self.lang == "ar":
            s = _shape_rtl(s)
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

    # ---- Dropdown option helpers -----------------------------------------

    def localize_option(self, key: str) -> str:
        """Return the display string for a dropdown value. Internal keys
        like 'Best', 'Auto', 'Worst', 'Original' get translated;
        technical keys ('1080p', 'mp3', 'MP4 (H.264)', etc.) pass through
        unchanged."""
        if key in LOCALIZED_OPTIONS:
            return self.t(f"option.{key.lower()}")
        return key

    def delocalize_option(self, display: str, candidates) -> str:
        """Reverse of localize_option: map a displayed dropdown value back
        to its internal English key. Tries every supported language, so a
        stale display string from before a language switch (when the var
        still holds the previous language's text) still resolves cleanly.
        Arabic candidates are compared in their shaped form because that's
        what we put on screen."""
        for k in candidates:
            if k not in LOCALIZED_OPTIONS:
                if k == display:
                    return k
                continue
            for lang in LANGUAGES:
                raw = _T[lang].get(f"option.{k.lower()}")
                if raw is None:
                    continue
                shown = _shape_rtl(raw) if lang == "ar" else raw
                if shown == display:
                    return k
        return display
