//! User settings: tolerant loading, one-time migration of the old Python
//! `config.json`, atomic saves and language autodetection.

use crate::options::*;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub const LANGUAGES: [&str; 10] = ["en", "tr", "es", "fr", "de", "ru", "ar", "zh", "ja", "ko"];
pub const DEFAULT_SESSION_NAME: &str = "youtube_downloader";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Settings {
    /// "" = autodetect (resolved at start-up, written on the next save).
    pub language: String,
    /// 0 = unlimited.
    pub max_concurrent: u32,
    /// "" = ~/Downloads.
    pub output_dir: String,
    pub mode: Mode,
    pub quality: Quality,
    pub video_codec: VideoCodec,
    pub audio_codec: AudioCodec,
    pub container: Container,
    pub reencode: bool,
    pub preset: Preset,
    pub playlist: bool,
    /// Check for a new app release at start-up.
    pub auto_update_check: bool,
    /// Keep the managed yt-dlp current on a daily schedule.
    pub ytdlp_auto_update: bool,
    pub concurrent_fragments: u32,
    pub cookies_browser: CookieChoice,
    pub cookie_file: String,
    pub anyip_enabled: bool,
    pub anyip_username: String,
    pub anyip_network_type: NetworkType,
    pub anyip_session_name: String,
    // Additions beyond parity; all off by default because they change output
    // or behaviour.
    /// Keep the queue and history across restarts; unfinished tasks resume.
    pub restore_queue: bool,
    pub use_download_archive: bool,
    pub embed_subtitles: bool,
    pub subtitle_langs: String,
    pub embed_chapters: bool,
    pub embed_thumbnail: bool,
    pub embed_metadata: bool,
    pub sponsorblock_remove: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            language: String::new(),
            max_concurrent: 2,
            output_dir: String::new(),
            mode: Mode::Video,
            quality: Quality::Best,
            video_codec: VideoCodec::Auto,
            audio_codec: AudioCodec::M4a,
            container: Container::None,
            reencode: false,
            preset: Preset::Balanced,
            playlist: false,
            auto_update_check: true,
            ytdlp_auto_update: true,
            concurrent_fragments: 4,
            cookies_browser: CookieChoice::Auto,
            cookie_file: String::new(),
            anyip_enabled: false,
            anyip_username: String::new(),
            anyip_network_type: NetworkType::Residential,
            anyip_session_name: DEFAULT_SESSION_NAME.into(),
            restore_queue: false,
            use_download_archive: false,
            embed_subtitles: false,
            subtitle_langs: "en.*".into(),
            embed_chapters: false,
            embed_thumbnail: false,
            embed_metadata: false,
            sponsorblock_remove: false,
        }
    }
}

impl Settings {
    /// Normalise values a strict deserialise can still let through.
    pub fn normalise(mut self) -> Self {
        if !self.language.is_empty() && !LANGUAGES.contains(&self.language.as_str()) {
            self.language.clear();
        }
        if self.concurrent_fragments == 0 {
            self.concurrent_fragments = 1;
        }
        self.anyip_username = self.anyip_username.trim().to_owned();
        if self.anyip_session_name.trim().is_empty() {
            self.anyip_session_name = DEFAULT_SESSION_NAME.into();
        }
        if self.subtitle_langs.trim().is_empty() {
            self.subtitle_langs = "en.*".into();
        }
        self
    }

    /// Tolerant per-field load: every invalid value falls back to its default
    /// (settings.py:105-153). `legacy` selects the old snake_case keys.
    pub fn from_json(obj: &Map<String, Value>, legacy: bool) -> Settings {
        let d = Settings::default();
        let k = |snake: &'static str, camel: &'static str| if legacy { snake } else { camel };
        let language = match obj.get("language").and_then(Value::as_str) {
            Some(l) if l.is_empty() || LANGUAGES.contains(&l) => l.to_owned(),
            _ => d.language.clone(),
        };
        // Old files use 9999 for ∞; the new file uses 0.
        let max_concurrent = match coerce_int(obj.get(k("max_concurrent", "maxConcurrent"))) {
            Some(n) if n >= 9999 || (!legacy && n == 0) => 0,
            Some(n) => n.max(1) as u32,
            None => d.max_concurrent,
        };
        let container = match obj
            .get("container")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
        {
            Some(c) => c,
            // Legacy boolean from before the container menu existed (downloader.py:1403).
            None if legacy && obj.get("force_mp4").is_some_and(coerce_bool) => Container::Mp4,
            None => d.container,
        };
        let session = string(obj, k("anyip_session_name", "anyipSessionName"));
        Settings {
            language,
            max_concurrent,
            output_dir: string(obj, k("output_dir", "outputDir")),
            mode: choice(obj, "mode", d.mode),
            quality: choice(obj, "quality", d.quality),
            video_codec: choice(obj, k("video_codec", "videoCodec"), d.video_codec),
            audio_codec: choice(obj, k("audio_codec", "audioCodec"), d.audio_codec),
            container,
            reencode: boolean(obj, k("reencode_h264", "reencode"), d.reencode),
            preset: choice(obj, k("reencode_quality", "preset"), d.preset),
            playlist: boolean(obj, "playlist", d.playlist),
            auto_update_check: boolean(
                obj,
                k("auto_update_check", "autoUpdateCheck"),
                d.auto_update_check,
            ),
            ytdlp_auto_update: boolean(
                obj,
                k("ytdlp_auto_update", "ytdlpAutoUpdate"),
                d.ytdlp_auto_update,
            ),
            concurrent_fragments: coerce_int(
                obj.get(k("concurrent_fragments", "concurrentFragments")),
            )
            .map(|n| n.clamp(1, u32::MAX as i64) as u32)
            .unwrap_or(d.concurrent_fragments),
            cookies_browser: choice(
                obj,
                k("cookies_browser", "cookiesBrowser"),
                d.cookies_browser,
            ),
            cookie_file: string(obj, k("cookie_file", "cookieFile")),
            anyip_enabled: boolean(obj, k("anyip_enabled", "anyipEnabled"), d.anyip_enabled),
            anyip_username: string(obj, k("anyip_username", "anyipUsername")),
            anyip_network_type: choice(
                obj,
                k("anyip_network_type", "anyipNetworkType"),
                d.anyip_network_type,
            ),
            anyip_session_name: if session.trim().is_empty() {
                d.anyip_session_name.clone()
            } else {
                session
            },
            restore_queue: boolean(obj, "restoreQueue", false),
            use_download_archive: boolean(obj, "useDownloadArchive", false),
            embed_subtitles: boolean(obj, "embedSubtitles", false),
            subtitle_langs: obj
                .get("subtitleLangs")
                .and_then(Value::as_str)
                .unwrap_or("en.*")
                .to_owned(),
            embed_chapters: boolean(obj, "embedChapters", false),
            embed_thumbnail: boolean(obj, "embedThumbnail", false),
            embed_metadata: boolean(obj, "embedMetadata", false),
            sponsorblock_remove: boolean(obj, "sponsorblockRemove", false),
        }
        .normalise()
    }

    /// Apply a partial update from the UI. Unknown keys or invalid values are
    /// rejected (unlike file loading, the UI must send valid data).
    pub fn patched(&self, patch: &Map<String, Value>) -> Result<Settings, String> {
        let mut v = serde_json::to_value(self).map_err(|e| e.to_string())?;
        let obj = v.as_object_mut().expect("settings serialise to an object");
        for (key, value) in patch {
            if !obj.contains_key(key) {
                return Err(format!("unknown setting: {key}"));
            }
            obj.insert(key.clone(), value.clone());
        }
        let s: Settings = serde_json::from_value(v).map_err(|e| format!("invalid setting: {e}"))?;
        Ok(s.normalise())
    }
}

fn choice<T: DeserializeOwned>(obj: &Map<String, Value>, key: &str, default: T) -> T {
    obj.get(key)
        .and_then(|v| serde_json::from_value(v.clone()).ok())
        .unwrap_or(default)
}

fn string(obj: &Map<String, Value>, key: &str) -> String {
    obj.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn boolean(obj: &Map<String, Value>, key: &str, default: bool) -> bool {
    obj.get(key).map(coerce_bool).unwrap_or(default)
}

/// Python `_coerce_bool` (settings.py:50).
fn coerce_bool(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::String(s) => matches!(
            s.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::Null => false,
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `int(value)`: ints, floats (truncated), bools and integer strings.
fn coerce_int(v: Option<&Value>) -> Option<i64> {
    match v? {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f as i64)),
        Value::Bool(b) => Some(*b as i64),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

/// Where everything lives. The old config directory is kept so users find
/// their settings in the same place.
#[derive(Clone, Debug)]
pub struct Paths {
    /// `<config>/youtube-downloader/`
    pub config_dir: PathBuf,
    /// `<local data>/youtube-downloader/`
    pub data_dir: PathBuf,
}

impl Paths {
    pub fn detect() -> Paths {
        let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
        let config_base = if cfg!(windows) {
            std::env::var_os("APPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join("AppData").join("Roaming"))
        } else if cfg!(target_os = "macos") {
            home.join("Library").join("Application Support")
        } else {
            std::env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .unwrap_or_else(|| home.join(".config"))
        };
        let data_base = dirs::data_local_dir().unwrap_or_else(|| config_base.clone());
        Paths {
            config_dir: config_base.join("youtube-downloader"),
            data_dir: data_base.join("youtube-downloader"),
        }
    }
    /// The Python app's file; read once for migration, kept for the old app.
    pub fn legacy_config(&self) -> PathBuf {
        self.config_dir.join("config.json")
    }
    pub fn settings_file(&self) -> PathBuf {
        self.config_dir.join("settings.json")
    }
    pub fn secret_file(&self) -> PathBuf {
        self.config_dir.join("anyip-password")
    }
    pub fn queue_file(&self) -> PathBuf {
        self.data_dir.join("queue.json")
    }
    pub fn archive_file(&self) -> PathBuf {
        self.data_dir.join("download-archive.txt")
    }
    pub fn bin_dir(&self) -> PathBuf {
        self.data_dir.join("bin")
    }
    pub fn tmp_dir(&self) -> PathBuf {
        self.data_dir.join("tmp")
    }
}

/// Result of loading: the settings plus a password found in the old file
/// that still has to be moved to the keyring.
#[derive(Debug, Default)]
pub struct Loaded {
    pub settings: Settings,
    pub legacy_password: Option<String>,
    pub migrated: bool,
}

pub fn load(paths: &Paths) -> Loaded {
    if let Some(obj) = read_object(&paths.settings_file()) {
        // A password still sitting in the old file (e.g. the keyring move
        // failed last time) is picked up again.
        let legacy_password = read_object(&paths.legacy_config()).and_then(|o| legacy_password(&o));
        return Loaded {
            settings: Settings::from_json(&obj, false),
            legacy_password,
            migrated: false,
        };
    }
    match read_object(&paths.legacy_config()) {
        Some(obj) => Loaded {
            settings: Settings::from_json(&obj, true),
            legacy_password: legacy_password(&obj),
            migrated: true,
        },
        None => Loaded::default(),
    }
}

fn legacy_password(obj: &Map<String, Value>) -> Option<String> {
    obj.get("anyip_password")
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty())
        .map(str::to_owned)
}

fn read_object(path: &Path) -> Option<Map<String, Value>> {
    let text = fs::read_to_string(path).ok()?;
    match serde_json::from_str::<Value>(&text).ok()? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

pub fn save(paths: &Paths, settings: &Settings) -> std::io::Result<()> {
    let mut json = serde_json::to_string_pretty(settings)?;
    json.push('\n');
    write_atomic(&paths.settings_file(), json.as_bytes(), false)
}

/// Remove the plaintext password from the old config.json, keeping every
/// other key so the old app can still read it.
pub fn scrub_legacy_password(paths: &Paths) -> std::io::Result<()> {
    let path = paths.legacy_config();
    let Some(mut obj) = read_object(&path) else {
        return Ok(());
    };
    if obj.remove("anyip_password").is_none() {
        return Ok(());
    }
    obj.insert("anyip_password".into(), Value::String(String::new()));
    let json = serde_json::to_string_pretty(&Value::Object(obj))?;
    write_atomic(&path, json.as_bytes(), false)
}

/// Write via a temp file + rename so a crash never leaves a truncated file
/// (fixes BUG #11). `private` = 0600 on Unix.
pub fn write_atomic(path: &Path, bytes: &[u8], private: bool) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let tmp = dir.join(format!(
        ".{}.tmp{}",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("file"),
        std::process::id()
    ));
    {
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        if private {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        #[cfg(not(unix))]
        let _ = private;
        let mut f = opts.open(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

/// Language autodetect (i18n.py:1143). `candidates` are locale strings in
/// priority order.
pub fn detect_language_from(candidates: &[String]) -> &'static str {
    const PREFIXES: [(&str, &[&str]); 9] = [
        ("tr", &["tr_", "tr.", "tur", "turkish"]),
        ("es", &["es_", "es.", "spa", "spanish"]),
        ("fr", &["fr_", "fr.", "fre", "fra", "french"]),
        ("de", &["de_", "de.", "ger", "deu", "german"]),
        ("ru", &["ru_", "ru.", "rus", "russian"]),
        ("ar", &["ar_", "ar.", "ara", "arabic"]),
        ("zh", &["zh_", "zh.", "chi", "zho", "chinese"]),
        ("ja", &["ja_", "ja.", "jpn", "japanese"]),
        ("ko", &["ko_", "ko.", "kor", "korean"]),
    ];
    for c in candidates {
        let head = c.to_lowercase();
        for (code, prefixes) in PREFIXES {
            if head == code || prefixes.iter().any(|p| head.starts_with(p)) {
                return code;
            }
        }
    }
    "en"
}

pub fn detect_language() -> &'static str {
    let mut candidates = Vec::new();
    #[cfg(windows)]
    if let Some(name) = windows_locale_name() {
        // "tr-TR" → "tr_TR" so the prefix table applies.
        candidates.push(name.replace('-', "_"));
    }
    candidates.extend(posix_locale_candidates(&|k| std::env::var(k).ok()));
    detect_language_from(&candidates)
}

/// i18n.py:1143-1165: `locale.getlocale()[0]` (the LC_CTYPE category: first
/// non-empty of LC_ALL, LC_CTYPE, LANG), then the first non-empty of
/// LC_ALL, LC_MESSAGES, LANG.
pub fn posix_locale_candidates(get: &dyn Fn(&str) -> Option<String>) -> Vec<String> {
    let first = |keys: &[&str]| keys.iter().filter_map(|k| get(k)).find(|v| !v.is_empty());
    [
        first(&["LC_ALL", "LC_CTYPE", "LANG"]),
        first(&["LC_ALL", "LC_MESSAGES", "LANG"]),
    ]
    .into_iter()
    .flatten()
    .collect()
}

#[cfg(windows)]
fn windows_locale_name() -> Option<String> {
    use windows_sys::Win32::Globalization::GetUserDefaultLocaleName;
    let mut buf = [0u16; 85];
    // SAFETY: buffer length is passed; the API writes at most that many u16s.
    let n = unsafe { GetUserDefaultLocaleName(buf.as_mut_ptr(), buf.len() as i32) };
    (n > 1).then(|| String::from_utf16_lossy(&buf[..n as usize - 1]))
}

pub fn home_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("."))
}

pub fn default_output_dir() -> PathBuf {
    home_dir().join("Downloads")
}

/// `os.path.expanduser` for the leading `~`.
pub fn expand_user(raw: &str) -> PathBuf {
    let raw = raw.trim();
    if raw == "~" {
        return home_dir();
    }
    if let Some(rest) = raw.strip_prefix("~/").or_else(|| raw.strip_prefix("~\\")) {
        return home_dir().join(rest);
    }
    PathBuf::from(raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn old_fixture() -> Map<String, Value> {
        // Shape written by the Python app's Settings.save().
        serde_json::from_str(
            r#"{
  "language": "tr",
  "max_concurrent": 9999,
  "output_dir": "~/Videos/yt",
  "mode": "audio",
  "quality": "192 kbps",
  "video_codec": "Auto (best H.264 / VP9)",
  "audio_codec": "ac3",
  "container": "MKV",
  "reencode_h264": "yes",
  "reencode_quality": "Quality",
  "playlist": 1,
  "auto_update_check": false,
  "concurrent_fragments": "16",
  "cookies_browser": "Firefox",
  "cookie_file": "/home/u/cookies.txt",
  "anyip_enabled": true,
  "anyip_username": "  user1  ",
  "anyip_password": "s3cret,pw",
  "anyip_network_type": "Mobile",
  "anyip_session_name": "   ",
  "some_future_key": 42
}"#,
        )
        .unwrap()
    }

    #[test]
    fn migrates_old_config_shape() {
        let s = Settings::from_json(&old_fixture(), true);
        assert_eq!(s.language, "tr");
        assert_eq!(s.max_concurrent, 0, "9999 means unlimited");
        assert_eq!(s.output_dir, "~/Videos/yt");
        assert_eq!(s.mode, Mode::Audio);
        assert_eq!(s.quality, Quality::K192);
        assert_eq!(
            s.video_codec,
            VideoCodec::Auto,
            "legacy label resets to default"
        );
        assert_eq!(s.audio_codec, AudioCodec::Ac3);
        assert_eq!(s.container, Container::Mkv);
        assert!(s.reencode);
        assert_eq!(s.preset, Preset::Quality);
        assert!(s.playlist);
        assert!(!s.auto_update_check);
        assert_eq!(s.concurrent_fragments, 16);
        assert_eq!(s.cookies_browser, CookieChoice::Firefox);
        assert_eq!(s.cookie_file, "/home/u/cookies.txt");
        assert!(s.anyip_enabled);
        assert_eq!(s.anyip_username, "user1");
        assert_eq!(s.anyip_network_type, NetworkType::Mobile);
        assert_eq!(s.anyip_session_name, DEFAULT_SESSION_NAME);
        assert!(!s.use_download_archive && !s.embed_thumbnail && !s.sponsorblock_remove);
    }

    #[test]
    fn force_mp4_maps_to_container_mp4() {
        let obj: Map<String, Value> = serde_json::from_str(r#"{"force_mp4": true}"#).unwrap();
        assert_eq!(Settings::from_json(&obj, true).container, Container::Mp4);
        let obj: Map<String, Value> =
            serde_json::from_str(r#"{"force_mp4": true, "container": "None"}"#).unwrap();
        assert_eq!(
            Settings::from_json(&obj, true).container,
            Container::None,
            "explicit container wins"
        );
        let obj: Map<String, Value> =
            serde_json::from_str(r#"{"force_mp4": true, "container": "AVI"}"#).unwrap();
        assert_eq!(Settings::from_json(&obj, true).container, Container::Avi);
    }

    #[test]
    fn garbage_values_fall_back_to_defaults() {
        let obj: Map<String, Value> = serde_json::from_str(
            r#"{"language":"xx","max_concurrent":"lots","mode":3,"concurrent_fragments":-5,"cookies_browser":"Netscape"}"#,
        )
        .unwrap();
        let s = Settings::from_json(&obj, true);
        assert_eq!(s.language, "");
        assert_eq!(s.max_concurrent, 2);
        assert_eq!(s.mode, Mode::Video);
        assert_eq!(s.concurrent_fragments, 1, "max(1, int)");
        assert_eq!(s.cookies_browser, CookieChoice::Auto);
        assert_eq!(Settings::from_json(&Map::new(), true), Settings::default());
    }

    #[test]
    fn load_prefers_new_file_and_reads_legacy_once() {
        let dir = std::env::temp_dir().join(format!("ytdl-settings-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let paths = Paths {
            config_dir: dir.clone(),
            data_dir: dir.join("data"),
        };
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            paths.legacy_config(),
            serde_json::to_string(&old_fixture()).unwrap(),
        )
        .unwrap();

        let loaded = load(&paths);
        assert!(loaded.migrated);
        assert_eq!(loaded.legacy_password.as_deref(), Some("s3cret,pw"));
        save(&paths, &loaded.settings).unwrap();
        let saved = fs::read_to_string(paths.settings_file()).unwrap();
        assert!(
            !saved.contains("s3cret"),
            "password never reaches settings.json"
        );

        scrub_legacy_password(&paths).unwrap();
        let legacy = fs::read_to_string(paths.legacy_config()).unwrap();
        assert!(!legacy.contains("s3cret"));
        assert!(
            legacy.contains("\"language\": \"tr\""),
            "other keys kept for the old app"
        );

        let again = load(&paths);
        assert!(!again.migrated);
        assert_eq!(again.legacy_password, None);
        assert_eq!(
            again.settings, loaded.settings,
            "round-trips through the new file"
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn patch_rejects_unknown_and_invalid() {
        let s = Settings::default();
        let ok: Map<String, Value> =
            serde_json::from_str(r#"{"maxConcurrent":0,"container":"WMV"}"#).unwrap();
        let p = s.patched(&ok).unwrap();
        assert_eq!((p.max_concurrent, p.container), (0, Container::Wmv));
        let bad: Map<String, Value> = serde_json::from_str(r#"{"anyipPassword":"x"}"#).unwrap();
        assert!(s.patched(&bad).is_err());
        let bad: Map<String, Value> = serde_json::from_str(r#"{"quality":"8K"}"#).unwrap();
        assert!(s.patched(&bad).is_err());
    }

    #[test]
    fn language_autodetect_table() {
        let d =
            |v: &[&str]| detect_language_from(&v.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert_eq!(d(&["tr_TR.UTF-8"]), "tr");
        assert_eq!(d(&["French_France.1252"]), "fr");
        assert_eq!(d(&["de"]), "de");
        assert_eq!(d(&["C.UTF-8", "ja_JP"]), "ja");
        assert_eq!(d(&["en_US.UTF-8"]), "en");
        assert_eq!(d(&[]), "en");
        assert_eq!(d(&["zho"]), "zh");
    }

    #[test]
    fn lc_ctype_counts_like_python_getlocale() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |k: &str| {
                pairs
                    .iter()
                    .find(|(n, _)| *n == k)
                    .map(|(_, v)| v.to_string())
            }
        };
        let c = posix_locale_candidates(&env(&[
            ("LC_CTYPE", "tr_TR.UTF-8"),
            ("LANG", "en_US.UTF-8"),
        ]));
        assert_eq!(detect_language_from(&c), "tr");
        let c = posix_locale_candidates(&env(&[("LC_MESSAGES", "de_DE"), ("LANG", "C")]));
        assert_eq!(detect_language_from(&c), "de");
        assert!(posix_locale_candidates(&env(&[])).is_empty());
    }

    #[test]
    fn expand_user_handles_tilde() {
        assert_eq!(expand_user("~/x"), home_dir().join("x"));
        assert_eq!(expand_user("/abs"), PathBuf::from("/abs"));
    }
}
