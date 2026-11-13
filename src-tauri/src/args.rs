//! yt-dlp argument builder (spec section 2). Pure: the same inputs always give
//! the same argv, which the golden tests pin down.

use crate::options::*;
use crate::settings::Settings;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Immutable snapshot of the options at Add time (spec 1.4 item 40).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskSpec {
    pub url: String,
    pub out_dir: String,
    pub mode: Mode,
    pub quality: Quality,
    pub video_codec: VideoCodec,
    pub audio_codec: AudioCodec,
    pub container: Container,
    pub reencode: bool,
    pub preset: Preset,
    pub playlist: bool,
    pub fragments: u32,
    pub cookie_choice: CookieChoice,
    pub cookie_file: String,
    pub extras: Extras,
}

/// Optional additions; each off by default (they change output files).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Extras {
    pub download_archive: bool,
    pub embed_subtitles: bool,
    pub subtitle_langs: String,
    pub embed_chapters: bool,
    pub embed_thumbnail: bool,
    pub embed_metadata: bool,
    pub sponsorblock_remove: bool,
}

impl TaskSpec {
    pub fn snapshot(url: &str, out_dir: &Path, s: &Settings) -> TaskSpec {
        let audio = s.mode == Mode::Audio;
        TaskSpec {
            url: url.to_owned(),
            out_dir: out_dir.to_string_lossy().into_owned(),
            mode: s.mode,
            quality: s.quality,
            video_codec: s.video_codec,
            audio_codec: s.audio_codec,
            // Forced off in audio mode (app.py:1789-1793).
            container: if audio { Container::None } else { s.container },
            reencode: !audio && s.reencode,
            preset: s.preset,
            playlist: s.playlist,
            fragments: s.concurrent_fragments.max(1),
            cookie_choice: s.cookies_browser,
            cookie_file: s.cookie_file.trim().to_owned(),
            extras: Extras {
                download_archive: s.use_download_archive,
                embed_subtitles: s.embed_subtitles,
                subtitle_langs: s.subtitle_langs.clone(),
                embed_chapters: s.embed_chapters,
                embed_thumbnail: s.embed_thumbnail,
                embed_metadata: s.embed_metadata,
                sponsorblock_remove: s.sponsorblock_remove,
            },
        }
    }
}

/// Cookie source actually used by one attempt.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum CookieSource {
    None,
    Browser {
        browser: Browser,
    },
    /// The user's cookies.txt. yt-dlp is given a private copy because it
    /// writes the jar back on exit.
    File {
        path: String,
    },
}

/// Per-attempt inputs that are not part of the snapshot.
#[derive(Clone, Debug, Default)]
pub struct AttemptArgs {
    /// `--cookies-from-browser` value.
    pub cookie_browser: Option<Browser>,
    /// Path of the private cookies.txt copy for `--cookies`.
    pub cookie_copy: Option<PathBuf>,
    /// (name, absolute path) for deno/node/bun found on PATH.
    pub js_runtimes: Vec<(String, PathBuf)>,
    pub ffmpeg_dir: Option<PathBuf>,
    pub archive_file: Option<PathBuf>,
}

/// What runs after yt-dlp exits, per final file.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "type",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum PostPlan {
    None,
    /// Our compatibility transcode (spec 2.6). `remux_first`: the user only
    /// asked for a container (re-encode off), so a stream copy is tried
    /// before any re-encode (fixes BUG #3).
    Transcode {
        target: String,
        preset: Preset,
        remux_first: bool,
    },
    /// Audio to AC-3 with ffmpeg (yt-dlp cannot; fixes BUG #2).
    Ac3 {
        bitrate_k: u32,
    },
}

/// Prefixes of the machine-readable lines we ask yt-dlp to print.
pub const P_PROGRESS: &str = "YTDL-P ";
pub const P_POST: &str = "YTDL-PP ";
pub const P_ITEM: &str = "YTDL-ITEM\t";
pub const P_FILE: &str = "YTDL-FILE ";

/// spec 2.3: the extension our transcode targets, if any.
pub fn transcode_target(spec: &TaskSpec) -> Option<&'static str> {
    if spec.mode == Mode::Audio {
        return None;
    }
    if spec.reencode {
        Some(spec.container.ext().unwrap_or("mp4"))
    } else if spec.container.is_legacy() {
        spec.container.ext()
    } else {
        None
    }
}

pub fn post_plan(spec: &TaskSpec) -> PostPlan {
    match spec.mode {
        Mode::Audio if spec.audio_codec == AudioCodec::Ac3 => PostPlan::Ac3 {
            bitrate_k: match spec.quality.audio_quality() {
                "0" => 640, // AC-3's maximum; "Best"
                q => q.parse().unwrap_or(640),
            },
        },
        Mode::Audio => PostPlan::None,
        Mode::Video => {
            if let Some(t) = transcode_target(spec) {
                PostPlan::Transcode {
                    target: t.into(),
                    preset: spec.preset,
                    remux_first: false,
                }
            } else if let Some(ext) = spec.container.ext() {
                // MP4/MKV/WebM/MOV without re-encode: container change only.
                PostPlan::Transcode {
                    target: ext.into(),
                    preset: spec.preset,
                    remux_first: true,
                }
            } else {
                PostPlan::None
            }
        }
    }
}

fn chain(alts: &[String]) -> String {
    // De-duplicate repeated alternatives ("best/best" for Best).
    let mut seen: Vec<&str> = Vec::new();
    for a in alts {
        if !seen.contains(&a.as_str()) {
            seen.push(a);
        }
    }
    seen.join("/")
}

/// spec 2.2 selectors, including the source-preference overrides.
pub fn video_format(spec: &TaskSpec) -> String {
    if spec.quality == Quality::Worst {
        return "worstvideo+worstaudio/worst".into();
    }
    let h = spec
        .quality
        .height()
        .map(|n| format!("[height<={n}]"))
        .unwrap_or_default();
    let mut codec = spec.video_codec;
    if codec == VideoCodec::Auto && matches!(transcode_target(spec), Some("mp4" | "mov" | "mkv")) {
        match spec.quality {
            Quality::P1080 | Quality::P720 | Quality::P480 | Quality::P360 => {
                codec = VideoCodec::H264
            }
            _ => {
                let hi = format!("[height>1080]{h}");
                return chain(&[
                    format!(
                        "bestvideo{hi}[vcodec^=vp09][dynamic_range=SDR]+bestaudio[acodec=opus]"
                    ),
                    format!("bestvideo{hi}[vcodec^=vp09]+bestaudio[acodec=opus]"),
                    format!("bestvideo{hi}[ext=webm]+bestaudio[ext=webm]"),
                    format!("bestvideo{hi}+bestaudio"),
                    format!("best{hi}"),
                    "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]".into(),
                    "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio".into(),
                    "best[height<=1080][vcodec^=avc1][ext=mp4]".into(),
                    "bestvideo+bestaudio".into(),
                    "best".into(),
                ]);
            }
        }
    }
    let alts: Vec<String> = match codec {
        VideoCodec::Auto => vec![
            format!("bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]"),
            format!("bestvideo{h}+bestaudio"),
            format!("best{h}"),
            "best".into(),
        ],
        VideoCodec::H264 => vec![
            format!("bestvideo{h}[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]"),
            format!("bestvideo{h}[vcodec^=avc1][ext=mp4]+bestaudio"),
            format!("best{h}[vcodec^=avc1][ext=mp4]"),
            format!("bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]"),
            format!("best{h}[ext=mp4]"),
            format!("best{h}"),
            "best".into(),
        ],
        VideoCodec::Vp9 => vec![
            format!("bestvideo{h}[vcodec^=vp09]+bestaudio[acodec=opus]"),
            format!("bestvideo{h}[ext=webm]+bestaudio[ext=webm]"),
            format!("bestvideo{h}+bestaudio"),
            format!("best{h}"),
            "best".into(),
        ],
        VideoCodec::Av1 => vec![
            format!("bestvideo{h}[vcodec^=av01]+bestaudio[acodec=opus]"),
            format!("bestvideo{h}+bestaudio"),
            format!("best{h}"),
            "best".into(),
        ],
    };
    chain(&alts)
}

/// spec 1.9: relative template; yt-dlp runs with the output dir as its cwd,
/// so no user path is ever interpreted as a template (fixes BUG #19).
pub fn output_template(playlist: bool) -> &'static str {
    if playlist {
        "%(playlist_title|playlist)s/%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
    } else {
        "%(title).200B [%(id)s].%(ext)s"
    }
}

/// Full argv (without the program). The proxy is *not* here: it goes into
/// the child environment so credentials never appear in argv (BUG #4).
pub fn build(spec: &TaskSpec, a: &AttemptArgs) -> Vec<String> {
    let mut v: Vec<String> = Vec::with_capacity(64);
    let mut push = |items: &[&str]| v.extend(items.iter().map(|s| s.to_string()));
    // Python-API defaults the CLI does not share (spec 2.1).
    push(&[
        "--ignore-config",
        "--color",
        "never",
        "--no-mtime",
        "--quiet",
        "--no-simulate",
    ]);
    push(&["--newline", "--progress"]);
    push(&[
        "--progress-template",
        &format!("download:{P_PROGRESS}%(progress)j"),
    ]);
    push(&[
        "--progress-template",
        &format!("postprocess:{P_POST}%(progress.status)s %(progress.postprocessor)s"),
    ]);
    push(&[
        "--print",
        &format!(
            "before_dl:{P_ITEM}%(playlist_index)s\t%(n_entries)s\t%(playlist_count)s\t%(id)j\t%(title)j"
        ),
    ]);
    push(&["--print", &format!("after_move:{P_FILE}%(filepath)j")]);
    push(&["--windows-filenames", "--no-restrict-filenames"]);
    push(&[
        "-N",
        &spec.fragments.max(1).to_string(),
        "-R",
        "5",
        "--fragment-retries",
        "5",
    ]);
    push(&["--remote-components", "ejs:github"]);
    for (name, path) in &a.js_runtimes {
        push(&["--js-runtimes", &format!("{name}:{}", path.display())]);
    }
    if let Some(dir) = &a.ffmpeg_dir {
        push(&["--ffmpeg-location", &dir.to_string_lossy()]);
    }
    if spec.playlist {
        // Continue past unavailable entries and report them (fixes BUG #12).
        push(&["--yes-playlist", "--no-abort-on-error"]);
    } else {
        push(&["--no-playlist", "--abort-on-error"]);
    }
    push(&["-o", output_template(spec.playlist)]);
    if let Some(b) = a.cookie_browser {
        push(&["--cookies-from-browser", b.id()]);
    } else if let Some(p) = &a.cookie_copy {
        push(&["--cookies", &p.to_string_lossy()]);
    }
    match spec.mode {
        Mode::Audio => {
            push(&["-f", "bestaudio/best"]);
            if let Some(fmt) = spec.audio_codec.ytdlp_format() {
                let q = if spec.audio_codec.has_bitrate() {
                    spec.quality.audio_quality()
                } else {
                    "0"
                };
                push(&["-x", "--audio-format", fmt, "--audio-quality", q]);
            }
        }
        Mode::Video => push(&["-f", &video_format(spec)]),
    }
    if let Some(p) = &a.archive_file {
        push(&["--download-archive", &p.to_string_lossy()]);
    }
    let x = &spec.extras;
    if x.embed_subtitles && spec.mode == Mode::Video {
        let langs = if x.subtitle_langs.trim().is_empty() {
            "en.*"
        } else {
            x.subtitle_langs.trim()
        };
        push(&["--embed-subs", "--sub-langs", langs]);
    }
    if x.embed_chapters {
        push(&["--embed-chapters"]);
    }
    if x.embed_thumbnail {
        push(&["--embed-thumbnail"]);
    }
    if x.embed_metadata {
        push(&["--embed-metadata"]);
    }
    if x.sponsorblock_remove {
        push(&["--sponsorblock-remove", "sponsor"]);
    }
    // `--` stops option parsing: a URL can never become a flag (BUG #24).
    push(&["--", &spec.url]);
    v
}

/// Validate pasted text (spec 1.4 item 37): trimmed, http(s) only, control
/// characters removed, no de-duplication.
pub fn extract_urls(text: &str) -> Vec<String> {
    text.lines()
        .map(|l| {
            l.chars()
                .filter(|c| !c.is_control())
                .collect::<String>()
                .trim()
                .to_owned()
        })
        .filter(|l| {
            let lower = l.to_lowercase();
            lower.starts_with("http://") || lower.starts_with("https://")
        })
        .collect()
}

/// anyIP proxy URL (downloader.py:170-203).
pub fn anyip_proxy_url(
    username: &str,
    password: &str,
    network: NetworkType,
    session: &str,
) -> Option<String> {
    use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
    let user = username.trim();
    if user.is_empty() || password.is_empty() {
        return None;
    }
    let mut parts: Vec<String> = user
        .split(',')
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(String::from)
        .collect();
    if !parts.iter().any(|p| p.starts_with("type_")) {
        match network {
            NetworkType::Residential => parts.push("type_residential".into()),
            NetworkType::Mobile => parts.push("type_mobile".into()),
            NetworkType::Mixed => {}
        }
    }
    if !parts.iter().any(|p| p.starts_with("session_")) {
        let mut cleaned = String::new();
        let mut in_bad = false;
        for c in session.chars() {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                cleaned.push(c);
                in_bad = false;
            } else if !in_bad {
                cleaned.push('_');
                in_bad = true;
            }
        }
        let cleaned = cleaned.trim_matches('_');
        if !cleaned.is_empty() {
            parts.push(format!("session_{cleaned}"));
        }
    }
    // Python quote(safe='') leaves only A-Za-z0-9 and "_.-~" unescaped.
    const SET: &percent_encoding::AsciiSet = &NON_ALPHANUMERIC
        .remove(b'_')
        .remove(b'.')
        .remove(b'-')
        .remove(b'~');
    let joined = parts.join(",");
    let u = utf8_percent_encode(&joined, SET);
    let p = utf8_percent_encode(password, SET);
    Some(format!("http://{u}:{p}@portal.anyip.io:1080"))
}

/// Environment entries that route a child through the proxy.
pub fn proxy_env(url: &str) -> Vec<(&'static str, String)> {
    ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
        .into_iter()
        .map(|k| (k, url.to_owned()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_extraction() {
        let got = extract_urls(
            " https://a/1 \n\nftp://x\n-o evil\nHTTP://B/2\r\nfile:///etc/passwd\nhttps://a/1\nhttps://x/\u{7}y",
        );
        assert_eq!(
            got,
            ["https://a/1", "HTTP://B/2", "https://a/1", "https://x/y"]
        );
    }

    #[test]
    fn anyip_url_format() {
        assert_eq!(
            anyip_proxy_url(
                " user1 ",
                "p@ss,w/rd",
                NetworkType::Residential,
                "youtube_downloader"
            )
            .unwrap(),
            "http://user1%2Ctype_residential%2Csession_youtube_downloader:p%40ss%2Cw%2Frd@portal.anyip.io:1080"
        );
        assert_eq!(
            anyip_proxy_url(
                "u,type_mobile,session_mine",
                "x",
                NetworkType::Residential,
                "other"
            )
            .unwrap(),
            "http://u%2Ctype_mobile%2Csession_mine:x@portal.anyip.io:1080"
        );
        assert_eq!(
            anyip_proxy_url("u", "x", NetworkType::Mixed, "  my session!! ").unwrap(),
            "http://u%2Csession_my_session:x@portal.anyip.io:1080"
        );
        assert_eq!(
            anyip_proxy_url("u", "x", NetworkType::Mobile, "__").unwrap(),
            "http://u%2Ctype_mobile:x@portal.anyip.io:1080"
        );
        assert!(anyip_proxy_url("", "x", NetworkType::Mixed, "s").is_none());
        assert!(anyip_proxy_url("u", "", NetworkType::Mixed, "s").is_none());
    }
}
