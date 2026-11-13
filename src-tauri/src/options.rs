//! Option vocabularies. The serialised value of every variant is the exact
//! English key the old app stored in config.json, so old files load as-is and
//! the UI localises only the keys listed in spec 1.5 item 56.

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Video,
    Audio,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Quality {
    Best,
    #[serde(rename = "4K (2160p)")]
    P2160,
    #[serde(rename = "1440p")]
    P1440,
    #[serde(rename = "1080p")]
    P1080,
    #[serde(rename = "720p")]
    P720,
    #[serde(rename = "480p")]
    P480,
    #[serde(rename = "360p")]
    P360,
    Worst,
    #[serde(rename = "320 kbps")]
    K320,
    #[serde(rename = "256 kbps")]
    K256,
    #[serde(rename = "192 kbps")]
    K192,
    #[serde(rename = "128 kbps")]
    K128,
    #[serde(rename = "96 kbps")]
    K96,
}

impl Quality {
    pub const VIDEO: [Quality; 8] = [
        Quality::Best,
        Quality::P2160,
        Quality::P1440,
        Quality::P1080,
        Quality::P720,
        Quality::P480,
        Quality::P360,
        Quality::Worst,
    ];
    pub const AUDIO: [Quality; 6] = [
        Quality::Best,
        Quality::K320,
        Quality::K256,
        Quality::K192,
        Quality::K128,
        Quality::K96,
    ];

    /// Height ceiling for video selectors; `None` for Best/Worst/audio values.
    pub fn height(self) -> Option<u32> {
        match self {
            Quality::P2160 => Some(2160),
            Quality::P1440 => Some(1440),
            Quality::P1080 => Some(1080),
            Quality::P720 => Some(720),
            Quality::P480 => Some(480),
            Quality::P360 => Some(360),
            _ => None,
        }
    }

    /// yt-dlp `--audio-quality` value: "0" (VBR best) or a kbps number.
    pub fn audio_quality(self) -> &'static str {
        match self {
            Quality::K320 => "320",
            Quality::K256 => "256",
            Quality::K192 => "192",
            Quality::K128 => "128",
            Quality::K96 => "96",
            _ => "0",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum VideoCodec {
    Auto,
    #[serde(rename = "MP4 (H.264)")]
    H264,
    #[serde(rename = "WebM (VP9)")]
    Vp9,
    #[serde(rename = "WebM (AV1)")]
    Av1,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum AudioCodec {
    #[serde(rename = "m4a (AAC)")]
    M4a,
    #[serde(rename = "mp3")]
    Mp3,
    #[serde(rename = "opus")]
    Opus,
    #[serde(rename = "vorbis (ogg)")]
    Vorbis,
    #[serde(rename = "flac")]
    Flac,
    #[serde(rename = "wav")]
    Wav,
    #[serde(rename = "alac")]
    Alac,
    #[serde(rename = "ac3")]
    Ac3,
    Original,
}

impl AudioCodec {
    /// Whether the Quality menu applies (spec 1.5 item 50).
    pub fn has_bitrate(self) -> bool {
        matches!(
            self,
            AudioCodec::M4a
                | AudioCodec::Mp3
                | AudioCodec::Opus
                | AudioCodec::Vorbis
                | AudioCodec::Ac3
        )
    }

    /// yt-dlp `--audio-format`, or None when yt-dlp must not extract
    /// (Original keeps the source; ac3 is converted by our own ffmpeg pass).
    pub fn ytdlp_format(self) -> Option<&'static str> {
        match self {
            AudioCodec::M4a => Some("m4a"),
            AudioCodec::Mp3 => Some("mp3"),
            AudioCodec::Opus => Some("opus"),
            AudioCodec::Vorbis => Some("vorbis"),
            AudioCodec::Flac => Some("flac"),
            AudioCodec::Wav => Some("wav"),
            AudioCodec::Alac => Some("alac"),
            AudioCodec::Ac3 | AudioCodec::Original => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Container {
    None,
    #[serde(rename = "MP4")]
    Mp4,
    #[serde(rename = "MKV")]
    Mkv,
    #[serde(rename = "WebM")]
    Webm,
    #[serde(rename = "MOV")]
    Mov,
    #[serde(rename = "AVI")]
    Avi,
    #[serde(rename = "FLV")]
    Flv,
    #[serde(rename = "MPEG")]
    Mpeg,
    #[serde(rename = "WMV")]
    Wmv,
}

impl Container {
    pub fn ext(self) -> Option<&'static str> {
        match self {
            Container::None => None,
            Container::Mp4 => Some("mp4"),
            Container::Mkv => Some("mkv"),
            Container::Webm => Some("webm"),
            Container::Mov => Some("mov"),
            Container::Avi => Some("avi"),
            Container::Flv => Some("flv"),
            Container::Mpeg => Some("mpeg"),
            Container::Wmv => Some("wmv"),
        }
    }

    pub fn is_legacy(self) -> bool {
        matches!(
            self,
            Container::Avi | Container::Flv | Container::Mpeg | Container::Wmv
        )
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Preset {
    Fast,
    Balanced,
    Quality,
}

impl Preset {
    /// Index into the fast/balanced/quality tuples of the encoder tables.
    pub fn idx(self) -> usize {
        self as usize
    }
    pub fn lower(self) -> &'static str {
        ["fast", "balanced", "quality"][self.idx()]
    }
}

/// A browser yt-dlp can read cookies from.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Browser {
    Firefox,
    Chrome,
    Brave,
    Chromium,
    Edge,
    Opera,
    Safari,
    Vivaldi,
}

impl Browser {
    pub const ALL: [Browser; 8] = [
        Browser::Firefox,
        Browser::Chrome,
        Browser::Brave,
        Browser::Chromium,
        Browser::Edge,
        Browser::Opera,
        Browser::Safari,
        Browser::Vivaldi,
    ];
    /// yt-dlp `--cookies-from-browser` name.
    pub fn id(self) -> &'static str {
        match self {
            Browser::Firefox => "firefox",
            Browser::Chrome => "chrome",
            Browser::Brave => "brave",
            Browser::Chromium => "chromium",
            Browser::Edge => "edge",
            Browser::Opera => "opera",
            Browser::Safari => "safari",
            Browser::Vivaldi => "vivaldi",
        }
    }
    /// First letter capitalised, rest lower-case (browser_detect.py:229).
    pub fn display(self) -> String {
        let id = self.id();
        id[..1].to_uppercase() + &id[1..]
    }
    pub fn is_chromium_family(self) -> bool {
        matches!(
            self,
            Browser::Chrome
                | Browser::Edge
                | Browser::Brave
                | Browser::Chromium
                | Browser::Vivaldi
                | Browser::Opera
        )
    }
}

/// The "Browser cookies" setting.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum CookieChoice {
    Auto,
    Off,
    Firefox,
    Chrome,
    Brave,
    Chromium,
    Edge,
    Opera,
    Safari,
    Vivaldi,
}

impl CookieChoice {
    pub fn browser(self) -> Option<Browser> {
        Some(match self {
            CookieChoice::Auto | CookieChoice::Off => return None,
            CookieChoice::Firefox => Browser::Firefox,
            CookieChoice::Chrome => Browser::Chrome,
            CookieChoice::Brave => Browser::Brave,
            CookieChoice::Chromium => Browser::Chromium,
            CookieChoice::Edge => Browser::Edge,
            CookieChoice::Opera => Browser::Opera,
            CookieChoice::Safari => Browser::Safari,
            CookieChoice::Vivaldi => Browser::Vivaldi,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum NetworkType {
    Residential,
    Mixed,
    Mobile,
}

/// Parse the editable Max-concurrent box (spec 1.6 item 59). 0 = unlimited.
pub fn parse_concurrency(text: &str) -> u32 {
    let t = text.trim();
    if ["∞", "inf", "infinity", "unlimited"]
        .iter()
        .any(|w| t.eq_ignore_ascii_case(w))
    {
        return 0;
    }
    match t.parse::<i64>() {
        Ok(n) if n >= 9999 => 0,
        Ok(n) => n.max(1) as u32,
        Err(_) => 2,
    }
}

/// Parse the editable Parallel-parts box (spec 1.6 item 62); no upper cap.
pub fn parse_fragments(text: &str) -> u32 {
    match text.trim().parse::<i64>() {
        Ok(n) => n.clamp(1, u32::MAX as i64) as u32,
        Err(_) => 4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concurrency_parsing_matches_old_rules() {
        assert_eq!(parse_concurrency("∞"), 0);
        assert_eq!(parse_concurrency(" Unlimited "), 0);
        assert_eq!(parse_concurrency("INF"), 0);
        assert_eq!(parse_concurrency("3"), 3);
        assert_eq!(parse_concurrency("0"), 1);
        assert_eq!(parse_concurrency("-4"), 1);
        assert_eq!(parse_concurrency("9999"), 0);
        assert_eq!(parse_concurrency("abc"), 2);
        assert_eq!(parse_concurrency("2.5"), 2);
    }

    #[test]
    fn fragments_parsing_matches_old_rules() {
        assert_eq!(parse_fragments("16"), 16);
        assert_eq!(parse_fragments("0"), 1);
        assert_eq!(parse_fragments("x"), 4);
        assert_eq!(parse_fragments("1000"), 1000);
    }

    #[test]
    fn serialised_keys_are_the_old_english_keys() {
        let s = |v: serde_json::Value| v.as_str().unwrap().to_owned();
        assert_eq!(
            s(serde_json::to_value(Quality::P2160).unwrap()),
            "4K (2160p)"
        );
        assert_eq!(
            s(serde_json::to_value(AudioCodec::Vorbis).unwrap()),
            "vorbis (ogg)"
        );
        assert_eq!(
            s(serde_json::to_value(VideoCodec::H264).unwrap()),
            "MP4 (H.264)"
        );
        assert_eq!(s(serde_json::to_value(Container::Webm).unwrap()), "WebM");
        assert_eq!(s(serde_json::to_value(CookieChoice::Auto).unwrap()), "Auto");
        assert_eq!(s(serde_json::to_value(Mode::Audio).unwrap()), "audio");
        assert_eq!(Browser::Firefox.display(), "Firefox");
    }
}
