//! Parsing of yt-dlp's machine-readable lines and ffmpeg `-progress` output
//! into typed events.

use crate::args::{P_FILE, P_ITEM, P_POST, P_PROGRESS};
use crate::log::strip_ansi;
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub struct DlProgress {
    pub status: String,
    pub downloaded: Option<u64>,
    pub total: Option<u64>,
    pub speed: Option<f64>,
    pub eta: Option<f64>,
    pub filename: Option<String>,
    pub tmpfilename: Option<String>,
}

impl DlProgress {
    /// spec 1.8 item 76: per stream file, 0 when no total is known.
    pub fn percent(&self) -> f64 {
        match (self.downloaded, self.total) {
            (Some(d), Some(t)) if t > 0 => (d as f64 / t as f64 * 100.0).clamp(0.0, 100.0),
            _ if self.status == "finished" => 100.0,
            _ => 0.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum PostKind {
    Merge,
    ExtractAudio,
    Convert,
    Remux,
    Encode,
    Embed,
}

#[derive(Clone, Debug, PartialEq)]
pub enum YtLine {
    Progress(DlProgress),
    Item {
        index: Option<u32>,
        count: Option<u32>,
        id: String,
        title: String,
    },
    File(String),
    PostStarted(PostKind),
    PostFinished,
    Warning(String),
    Error(String),
    Other(String),
}

fn opt_num(s: &str) -> Option<u32> {
    s.trim().parse().ok().filter(|n| *n > 0)
}

fn json_str(s: &str) -> String {
    serde_json::from_str::<String>(s.trim())
        .unwrap_or_else(|_| s.trim().trim_matches('"').to_owned())
}

/// yt-dlp postprocessor key → our phase. Unknown/housekeeping ones → None.
pub fn post_kind(pp: &str) -> Option<PostKind> {
    Some(match pp {
        "Merger" => PostKind::Merge,
        "ExtractAudio" => PostKind::ExtractAudio,
        "VideoConvertor" => PostKind::Convert,
        "VideoRemuxer" => PostKind::Remux,
        "EmbedSubtitle" | "EmbedThumbnail" | "FFmpegMetadata" | "Metadata" | "ModifyChapters"
        | "SponsorBlock" => PostKind::Embed,
        _ => return None,
    })
}

pub fn parse_yt_line(raw: &str) -> YtLine {
    let line = strip_ansi(raw);
    if let Some(rest) = line.strip_prefix(P_PROGRESS) {
        if let Some(p) = parse_progress_json(rest) {
            return YtLine::Progress(p);
        }
    } else if let Some(rest) = line.strip_prefix(P_ITEM) {
        let f: Vec<&str> = rest.splitn(5, '\t').collect();
        if f.len() == 5 {
            return YtLine::Item {
                index: opt_num(f[0]),
                count: opt_num(f[1]).or_else(|| opt_num(f[2])),
                id: json_str(f[3]),
                title: json_str(f[4]),
            };
        }
    } else if let Some(rest) = line.strip_prefix(P_FILE) {
        return YtLine::File(json_str(rest));
    } else if let Some(rest) = line.strip_prefix(P_POST) {
        let mut it = rest.splitn(2, ' ');
        let status = it.next().unwrap_or_default();
        let pp = it.next().unwrap_or_default().trim();
        return match status {
            "started" => post_kind(pp)
                .map(YtLine::PostStarted)
                .unwrap_or(YtLine::Other(line.clone())),
            "finished" => YtLine::PostFinished,
            _ => YtLine::Other(line.clone()),
        };
    } else if let Some(rest) = line.strip_prefix("WARNING:") {
        return YtLine::Warning(rest.trim().to_owned());
    } else if let Some(rest) = line.strip_prefix("ERROR:") {
        return YtLine::Error(rest.trim().to_owned());
    }
    YtLine::Other(line)
}

fn parse_progress_json(s: &str) -> Option<DlProgress> {
    let v: Value = serde_json::from_str(s.trim())
        .or_else(|_| {
            // Python's json.dumps may emit NaN/Infinity.
            let fixed = s
                .replace("NaN", "null")
                .replace("-Infinity", "null")
                .replace("Infinity", "null");
            serde_json::from_str(fixed.trim())
        })
        .ok()?;
    let num = |k: &str| {
        v.get(k)
            .and_then(Value::as_f64)
            .filter(|f| f.is_finite() && *f >= 0.0)
    };
    let text = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_owned);
    Some(DlProgress {
        status: text("status").unwrap_or_default(),
        downloaded: num("downloaded_bytes").map(|f| f as u64),
        total: num("total_bytes")
            .or_else(|| num("total_bytes_estimate"))
            .map(|f| f as u64)
            .filter(|t| *t > 0),
        speed: num("speed").filter(|s| *s > 0.0),
        eta: num("eta"),
        filename: text("filename"),
        tmpfilename: text("tmpfilename"),
    })
}

/// State of one ffmpeg `-progress pipe:1` block.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FfProgress {
    pub out_time_us: Option<u64>,
    pub speed_x: Option<f64>,
    pub end: bool,
}

impl FfProgress {
    /// Feed one `key=value` line; returns true at the end of a block.
    /// Only known keys are consumed (stderr is read separately; fixes BUG #21).
    pub fn feed(&mut self, line: &str) -> bool {
        let Some((k, v)) = line.trim().split_once('=') else {
            return false;
        };
        match k {
            "out_time_us" | "out_time_ms" => self.out_time_us = v.trim().parse().ok(),
            "speed" => {
                self.speed_x = v
                    .trim()
                    .trim_end_matches('x')
                    .trim()
                    .parse()
                    .ok()
                    .filter(|s: &f64| s.is_finite() && *s > 0.0)
            }
            "progress" => {
                self.end = v.trim() == "end";
                return true;
            }
            _ => {}
        }
        false
    }

    /// spec 1.8 item 80: capped at 99.5 until `progress=end`.
    pub fn percent(&self, duration_s: Option<f64>) -> Option<f64> {
        if self.end {
            return Some(100.0);
        }
        let d = duration_s.filter(|d| *d > 0.0)?;
        Some((self.out_time_us? as f64 / 1e6 / d * 100.0).clamp(0.0, 99.5))
    }

    pub fn eta(&self, duration_s: Option<f64>) -> Option<f64> {
        let pct = self.percent(duration_s)?;
        Some(duration_s? * (100.0 - pct) / 100.0 / self.speed_x?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_download_progress() {
        let l = r#"YTDL-P {"status": "downloading", "downloaded_bytes": 1048576, "total_bytes": null, "total_bytes_estimate": 4194304.0, "speed": 524288.5, "eta": 6, "filename": "T [x].f137.mp4", "tmpfilename": "T [x].f137.mp4.part", "fragment_index": 3}"#;
        let YtLine::Progress(p) = parse_yt_line(l) else {
            panic!()
        };
        assert_eq!(p.status, "downloading");
        assert_eq!(p.downloaded, Some(1048576));
        assert_eq!(p.total, Some(4194304));
        assert_eq!(p.percent(), 25.0);
        assert_eq!(p.speed, Some(524288.5));
        assert_eq!(p.eta, Some(6.0));
        assert_eq!(p.tmpfilename.as_deref(), Some("T [x].f137.mp4.part"));
    }

    #[test]
    fn progress_without_total_is_zero_and_nan_tolerated() {
        let YtLine::Progress(p) = parse_yt_line(
            r#"YTDL-P {"status":"downloading","downloaded_bytes":10,"speed":NaN,"eta":null}"#,
        ) else {
            panic!()
        };
        assert_eq!((p.percent(), p.speed, p.eta), (0.0, None, None));
        let YtLine::Progress(p) = parse_yt_line(
            r#"YTDL-P {"status":"finished","downloaded_bytes":10,"filename":"a.webm"}"#,
        ) else {
            panic!()
        };
        assert_eq!(p.percent(), 100.0);
    }

    #[test]
    fn parses_item_file_post_and_diagnostics() {
        assert_eq!(
            parse_yt_line("YTDL-ITEM\t3\t12\t12\t\"abc\"\t\"Tab\\tTitle \\\"q\\\"\""),
            YtLine::Item {
                index: Some(3),
                count: Some(12),
                id: "abc".into(),
                title: "Tab\tTitle \"q\"".into()
            }
        );
        assert_eq!(
            parse_yt_line("YTDL-ITEM\tNA\tNA\tNA\t\"abc\"\t\"T\""),
            YtLine::Item {
                index: None,
                count: None,
                id: "abc".into(),
                title: "T".into()
            }
        );
        assert_eq!(
            parse_yt_line(r#"YTDL-FILE "/d/é [x].mp4""#),
            YtLine::File("/d/é [x].mp4".into())
        );
        assert_eq!(
            parse_yt_line("YTDL-PP started Merger"),
            YtLine::PostStarted(PostKind::Merge)
        );
        assert_eq!(
            parse_yt_line("YTDL-PP started ExtractAudio"),
            YtLine::PostStarted(PostKind::ExtractAudio)
        );
        assert_eq!(
            parse_yt_line("YTDL-PP finished Merger"),
            YtLine::PostFinished
        );
        assert!(matches!(
            parse_yt_line("YTDL-PP started MoveFiles"),
            YtLine::Other(_)
        ));
        assert_eq!(
            parse_yt_line("\x1b[0;33mWARNING:\x1b[0m slow"),
            YtLine::Warning("slow".into())
        );
        assert_eq!(
            parse_yt_line("ERROR: [youtube] x: Sign in to confirm you’re not a bot"),
            YtLine::Error("[youtube] x: Sign in to confirm you’re not a bot".into())
        );
        assert!(matches!(
            parse_yt_line("[download] something"),
            YtLine::Other(_)
        ));
    }

    #[test]
    fn ffmpeg_progress_blocks() {
        let mut p = FfProgress::default();
        for l in ["frame=10", "out_time_us=5000000", "speed=2.50x"] {
            assert!(!p.feed(l));
        }
        assert!(p.feed("progress=continue"));
        assert_eq!(p.percent(Some(10.0)), Some(50.0));
        assert_eq!(p.eta(Some(10.0)), Some(2.0));
        p.feed("out_time_us=10000000");
        assert_eq!(p.percent(Some(10.0)), Some(99.5));
        p.feed("progress=end");
        assert_eq!(p.percent(None), Some(100.0));
        let mut q = FfProgress::default();
        q.feed("speed=N/A");
        assert_eq!(q.speed_x, None);
        assert_eq!(q.percent(None), None);
    }
}
