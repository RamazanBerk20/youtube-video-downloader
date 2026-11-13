//! Task model (spec 6.8): state enum, pure transition function, error
//! classification. The queue is the single owner that applies transitions
//! and emits them; the UI only renders what it receives.

use crate::args::{CookieSource, TaskSpec};
use crate::progress::PostKind;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ItemPos {
    /// 1-based playlist index.
    pub index: u32,
    pub count: Option<u32>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum ErrorClass {
    BotCheck,
    CookieDecrypt,
    NoFormats,
    FfmpegMissing,
    Network,
    Unavailable,
    EncoderFailed,
    YtdlpMissing,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "camelCase")]
pub enum TaskState {
    Queued,
    /// Resolving yt-dlp and spawning.
    Starting,
    /// Extraction, before the first byte.
    Fetching {
        item: Option<ItemPos>,
    },
    #[serde(rename_all = "camelCase")]
    Downloading {
        item: Option<ItemPos>,
        /// 0-based stream file within the item (video, then audio…).
        stream: u32,
        percent: f64,
        downloaded: Option<u64>,
        total: Option<u64>,
        /// Bytes per second.
        speed: Option<f64>,
        /// Seconds.
        eta: Option<f64>,
    },
    #[serde(rename_all = "camelCase")]
    PostProcessing {
        kind: PostKind,
        item: Option<ItemPos>,
        /// None = indeterminate (yt-dlp's own postprocessors).
        percent: Option<f64>,
        /// ffmpeg speed multiplier ("2.5x").
        speed_x: Option<f64>,
        eta: Option<f64>,
    },
    /// Cancel accepted; the tree kill and cleanup are in flight.
    Cancelling,
    #[serde(rename_all = "camelCase")]
    Done {
        files: Vec<String>,
        /// Playlist entries that failed while the rest continued.
        item_errors: Vec<String>,
    },
    #[serde(rename_all = "camelCase")]
    Failed {
        error_class: ErrorClass,
        message: String,
    },
    #[serde(rename_all = "camelCase")]
    Cancelled {
        cleaned_up: bool,
    },
}

impl TaskState {
    pub fn is_active(&self) -> bool {
        matches!(
            self,
            TaskState::Starting
                | TaskState::Fetching { .. }
                | TaskState::Downloading { .. }
                | TaskState::PostProcessing { .. }
                | TaskState::Cancelling
        )
    }
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            TaskState::Done { .. } | TaskState::Failed { .. } | TaskState::Cancelled { .. }
        )
    }
    fn item(&self) -> Option<ItemPos> {
        match self {
            TaskState::Fetching { item }
            | TaskState::Downloading { item, .. }
            | TaskState::PostProcessing { item, .. } => *item,
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum TaskEvent {
    Start,
    Spawned,
    Item(Option<ItemPos>),
    Progress {
        stream: u32,
        percent: f64,
        downloaded: Option<u64>,
        total: Option<u64>,
        speed: Option<f64>,
        eta: Option<f64>,
    },
    PostStarted(PostKind),
    PostProgress {
        percent: Option<f64>,
        speed_x: Option<f64>,
        eta: Option<f64>,
    },
    CancelRequested,
    Finished {
        files: Vec<String>,
        item_errors: Vec<String>,
    },
    Failed {
        class: ErrorClass,
        message: String,
    },
    Killed {
        cleaned_up: bool,
    },
    Requeue,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Invalid;

/// The only way a task's state changes.
pub fn transition(s: &TaskState, ev: TaskEvent) -> Result<TaskState, Invalid> {
    use TaskEvent as E;
    use TaskState as S;
    let running = matches!(
        s,
        S::Starting | S::Fetching { .. } | S::Downloading { .. } | S::PostProcessing { .. }
    );
    Ok(match (s, ev) {
        (S::Queued, E::Start) => S::Starting,
        (S::Queued, E::CancelRequested) => S::Cancelled { cleaned_up: true },
        (S::Starting, E::Spawned) => S::Fetching { item: None },
        // A new playlist item resets the phase (fixes BUG #6).
        (_, E::Item(item)) if running => S::Fetching { item },
        (
            S::Fetching { .. } | S::Downloading { .. } | S::PostProcessing { .. },
            E::Progress {
                stream,
                percent,
                downloaded,
                total,
                speed,
                eta,
            },
        ) => S::Downloading {
            item: s.item(),
            stream,
            percent,
            downloaded,
            total,
            speed,
            eta,
        },
        (_, E::PostStarted(kind)) if running && !matches!(s, S::Starting) => S::PostProcessing {
            kind,
            item: s.item(),
            percent: None,
            speed_x: None,
            eta: None,
        },
        (
            S::PostProcessing { kind, item, .. },
            E::PostProgress {
                percent,
                speed_x,
                eta,
            },
        ) => S::PostProcessing {
            kind: *kind,
            item: *item,
            percent,
            speed_x,
            eta,
        },
        (_, E::CancelRequested) if running => S::Cancelling,
        // Late progress while the kill is in flight changes nothing.
        (
            S::Cancelling,
            E::Item(_)
            | E::Progress { .. }
            | E::PostStarted(_)
            | E::PostProgress { .. }
            | E::CancelRequested,
        ) => S::Cancelling,
        (_, E::Finished { files, item_errors }) if running => S::Done { files, item_errors },
        (_, E::Failed { class, message }) if running => S::Failed {
            error_class: class,
            message,
        },
        // Once cancel was accepted, every outcome is Cancelled (fixes BUG #7).
        (S::Cancelling, E::Finished { .. } | E::Failed { .. }) => {
            S::Cancelled { cleaned_up: false }
        }
        (S::Cancelling, E::Killed { cleaned_up }) => S::Cancelled { cleaned_up },
        (_, E::Killed { cleaned_up }) if running => S::Cancelled { cleaned_up },
        (S::Failed { .. } | S::Cancelled { .. }, E::Requeue) => S::Queued,
        _ => return Err(Invalid),
    })
}

/// One run of yt-dlp (+ our postprocessing) for a task.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Attempt {
    pub n: u32,
    pub cookie_source: CookieSource,
    pub proxy: bool,
    pub started_ms: u64,
    pub ended_ms: Option<u64>,
    pub exit_code: Option<i32>,
    pub error_class: Option<ErrorClass>,
    /// Last stderr lines (redacted).
    pub stderr_tail: Vec<String>,
}

/// Per-task rescue bookkeeping (spec 3.3).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskRescue {
    pub cookie_file_retry_attempted: bool,
    pub anyip_retry_attempted: bool,
}

/// What the UI renders for one row. Sent whole on every change, so the UI
/// never shows a mixed snapshot.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Task {
    pub id: u64,
    pub spec: TaskSpec,
    /// yt-dlp's title once known; the UI shows the URL until then.
    pub title: Option<String>,
    pub state: TaskState,
    pub attempts: Vec<Attempt>,
    pub rescue: TaskRescue,
    pub created_ms: u64,
}

const FFMPEG_FRIENDLY: &str = "ffmpeg is required (merging streams or extracting mp3) but is not on PATH. Install:\n  Windows: winget install Gyan.FFmpeg\n  Arch:    sudo pacman -S ffmpeg\n  Debian:  sudo apt install ffmpeg\n  macOS:   brew install ffmpeg";

/// Classify yt-dlp's error text (spec 3.2; both ffmpeg messages, BUG #18).
pub fn classify(error_text: &str) -> ErrorClass {
    let e = error_text.to_lowercase();
    let any = |pats: &[&str]| pats.iter().any(|p| e.contains(p));
    if e.contains("sign in to confirm you") {
        ErrorClass::BotCheck
    } else if e.contains("failed to decrypt") {
        ErrorClass::CookieDecrypt
    } else if e.contains("requested format is not available") {
        ErrorClass::NoFormats
    } else if any(&[
        "ffmpeg is not installed",
        "ffmpeg not found",
        "ffprobe and ffmpeg not found",
        "ffprobe not found",
    ]) {
        ErrorClass::FfmpegMissing
    } else if any(&[
        "video unavailable",
        "private video",
        "has been removed",
        "not available in your country",
        "this video is not available",
        "members-only",
        "http error 404",
        "unsupported url",
    ]) {
        ErrorClass::Unavailable
    } else if any(&[
        "unable to download",
        "timed out",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "name resolution",
        "getaddrinfo",
        "nodename nor servname",
        "http error 5",
        "ssl:",
        "proxy",
    ]) {
        ErrorClass::Network
    } else {
        ErrorClass::Unknown
    }
}

/// The message shown for a failure: the friendly ffmpeg text keeps the raw
/// error as `Original:` (spec 3.1).
pub fn friendly_message(class: ErrorClass, raw: &str) -> String {
    match class {
        ErrorClass::FfmpegMissing => format!("{FFMPEG_FRIENDLY}\nOriginal: {raw}"),
        _ => raw.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ErrorClass::*;

    fn all_states() -> Vec<TaskState> {
        let item = Some(ItemPos {
            index: 2,
            count: Some(5),
        });
        vec![
            TaskState::Queued,
            TaskState::Starting,
            TaskState::Fetching { item },
            TaskState::Downloading {
                item,
                stream: 1,
                percent: 10.0,
                downloaded: None,
                total: None,
                speed: None,
                eta: None,
            },
            TaskState::PostProcessing {
                kind: PostKind::Merge,
                item,
                percent: None,
                speed_x: None,
                eta: None,
            },
            TaskState::Cancelling,
            TaskState::Done {
                files: vec![],
                item_errors: vec![],
            },
            TaskState::Failed {
                error_class: Unknown,
                message: "x".into(),
            },
            TaskState::Cancelled { cleaned_up: true },
        ]
    }

    fn all_events() -> Vec<TaskEvent> {
        vec![
            TaskEvent::Start,
            TaskEvent::Spawned,
            TaskEvent::Item(Some(ItemPos {
                index: 3,
                count: Some(5),
            })),
            TaskEvent::Progress {
                stream: 0,
                percent: 50.0,
                downloaded: Some(5),
                total: Some(10),
                speed: Some(1.0),
                eta: Some(5.0),
            },
            TaskEvent::PostStarted(PostKind::Encode),
            TaskEvent::PostProgress {
                percent: Some(10.0),
                speed_x: Some(2.0),
                eta: Some(3.0),
            },
            TaskEvent::CancelRequested,
            TaskEvent::Finished {
                files: vec!["f".into()],
                item_errors: vec![],
            },
            TaskEvent::Failed {
                class: BotCheck,
                message: "m".into(),
            },
            TaskEvent::Killed { cleaned_up: true },
            TaskEvent::Requeue,
        ]
    }

    fn kind(s: &TaskState) -> &'static str {
        match s {
            TaskState::Queued => "Q",
            TaskState::Starting => "S",
            TaskState::Fetching { .. } => "F",
            TaskState::Downloading { .. } => "D",
            TaskState::PostProcessing { .. } => "P",
            TaskState::Cancelling => "C",
            TaskState::Done { .. } => "done",
            TaskState::Failed { .. } => "fail",
            TaskState::Cancelled { .. } => "X",
        }
    }

    /// Exhaustive table: rows = states, columns = events in `all_events`
    /// order; "-" = rejected.
    #[test]
    fn transition_table_is_exactly_as_designed() {
        #[rustfmt::skip]
        let expected: [[&str; 11]; 9] = [
            //      Start Spawn Item Prog  PostS PostP Cancel Finish Fail   Killed Requeue
            /*Q*/  ["S",  "-",  "-", "-",  "-",  "-",  "X",   "-",   "-",   "-",   "-"],
            /*S*/  ["-",  "F",  "F", "-",  "-",  "-",  "C",   "done","fail","X",   "-"],
            /*F*/  ["-",  "-",  "F", "D",  "P",  "-",  "C",   "done","fail","X",   "-"],
            /*D*/  ["-",  "-",  "F", "D",  "P",  "-",  "C",   "done","fail","X",   "-"],
            /*P*/  ["-",  "-",  "F", "D",  "P",  "P",  "C",   "done","fail","X",   "-"],
            /*C*/  ["-",  "-",  "C", "C",  "C",  "C",  "C",   "X",   "X",   "X",   "-"],
            /*done*/["-", "-",  "-", "-",  "-",  "-",  "-",   "-",   "-",   "-",   "-"],
            /*fail*/["-", "-",  "-", "-",  "-",  "-",  "-",   "-",   "-",   "-",   "Q"],
            /*X*/  ["-",  "-",  "-", "-",  "-",  "-",  "-",   "-",   "-",   "-",   "Q"],
        ];
        for (si, s) in all_states().iter().enumerate() {
            for (ei, e) in all_events().into_iter().enumerate() {
                let got = transition(s, e.clone()).map(|n| kind(&n)).unwrap_or("-");
                assert_eq!(got, expected[si][ei], "state {} + event {:?}", kind(s), e);
            }
        }
    }

    #[test]
    fn item_resets_phase_and_progress_keeps_item() {
        let s = TaskState::PostProcessing {
            kind: PostKind::Encode,
            item: None,
            percent: Some(80.0),
            speed_x: None,
            eta: None,
        };
        let item = Some(ItemPos {
            index: 2,
            count: Some(3),
        });
        let f = transition(&s, TaskEvent::Item(item)).unwrap();
        assert_eq!(f, TaskState::Fetching { item });
        let d = transition(
            &f,
            TaskEvent::Progress {
                stream: 0,
                percent: 1.0,
                downloaded: None,
                total: None,
                speed: None,
                eta: None,
            },
        )
        .unwrap();
        assert!(matches!(d, TaskState::Downloading { item: i, .. } if i == item));
        let p = transition(&d, TaskEvent::PostStarted(PostKind::Merge)).unwrap();
        assert!(
            matches!(p, TaskState::PostProcessing { kind: PostKind::Merge, item: i, percent: None, .. } if i == item)
        );
    }

    #[test]
    fn killed_while_cancelling_keeps_cleanup_flag() {
        assert_eq!(
            transition(
                &TaskState::Cancelling,
                TaskEvent::Killed { cleaned_up: false }
            ),
            Ok(TaskState::Cancelled { cleaned_up: false })
        );
    }

    #[test]
    fn classification() {
        assert_eq!(
            classify("[youtube] x: Sign in to confirm you’re not a bot. Use --cookies"),
            BotCheck
        );
        assert_eq!(classify("Failed to decrypt with DPAPI"), CookieDecrypt);
        assert_eq!(
            classify("Requested format is not available. Use --list-formats"),
            NoFormats
        );
        assert_eq!(
            classify("Postprocessing: ffmpeg not found. Please install"),
            FfmpegMissing
        );
        assert_eq!(
            classify("You have requested merging of multiple formats but ffmpeg is not installed"),
            FfmpegMissing
        );
        assert_eq!(
            classify("[youtube] x: Video unavailable. This video is private"),
            Unavailable
        );
        assert_eq!(
            classify(
                "Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>"
            ),
            Network
        );
        assert_eq!(classify("something odd"), Unknown);
        assert!(friendly_message(FfmpegMissing, "raw").ends_with("\nOriginal: raw"));
        assert_eq!(friendly_message(Network, "raw"), "raw");
    }
}
