//! Session log: a bounded ring of entries (fixes BUG #17). Localisable lines
//! carry a message key + args; technical lines (yt-dlp, ffmpeg, installer)
//! are plain text, as in the old app. Everything passes through `redact`.

use serde::Serialize;
use std::collections::{BTreeMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

pub const LOG_CAPACITY: usize = 5000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum Level {
    Info,
    Warning,
    Error,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum LogMessage {
    /// Localise with the UI catalogs: `t(key, args)`.
    Key {
        key: String,
        args: BTreeMap<String, String>,
    },
    /// Shown verbatim (never localised in the old app either).
    Text { text: String },
}

impl LogMessage {
    pub fn key(key: &str) -> Self {
        LogMessage::Key {
            key: key.into(),
            args: BTreeMap::new(),
        }
    }
    pub fn key_with(key: &str, args: &[(&str, &str)]) -> Self {
        LogMessage::Key {
            key: key.into(),
            args: args
                .iter()
                .map(|(k, v)| (k.to_string(), redact(v)))
                .collect(),
        }
    }
    pub fn text(t: impl AsRef<str>) -> Self {
        LogMessage::Text {
            text: redact(t.as_ref()),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    pub seq: u64,
    /// Unix epoch milliseconds; the UI renders `[HH:MM:SS]` in local time.
    pub ts_ms: u64,
    /// Per-task lines render as `#<id>  <message>`.
    pub task_id: Option<u64>,
    pub level: Level,
    pub message: LogMessage,
}

#[derive(Default)]
pub struct SessionLog {
    entries: VecDeque<LogEntry>,
    next_seq: u64,
}

impl SessionLog {
    pub fn push(&mut self, task_id: Option<u64>, level: Level, message: LogMessage) -> LogEntry {
        let entry = LogEntry {
            seq: self.next_seq,
            ts_ms: now_ms(),
            task_id,
            level,
            message,
        };
        self.next_seq += 1;
        if self.entries.len() == LOG_CAPACITY {
            self.entries.pop_front();
        }
        self.entries.push_back(entry.clone());
        entry
    }
    pub fn snapshot(&self) -> Vec<LogEntry> {
        self.entries.iter().cloned().collect()
    }
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Remove ANSI colour sequences (`\x1b[...X`), as the old code did.
pub fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for n in chars.by_ref() {
                if n.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Hide `user:pass@` in any URL-ish text (BUG #4: credentials must never
/// reach the log, errors or the UI).
pub fn redact(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find("://") {
        let (head, tail) = rest.split_at(i + 3);
        out.push_str(head);
        let end = tail
            .find(|c: char| c.is_whitespace() || c == '/' || c == '"' || c == '\'')
            .unwrap_or(tail.len());
        match tail[..end].rfind('@') {
            Some(at) => {
                out.push_str("***@");
                rest = &tail[at + 1..];
            }
            None => rest = tail,
        }
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_proxy_credentials() {
        assert_eq!(
            redact(
                "Unable to connect to proxy http://u%2Ctype_mobile:p%40ss@portal.anyip.io:1080 now"
            ),
            "Unable to connect to proxy http://***@portal.anyip.io:1080 now"
        );
        assert_eq!(
            redact("https://youtube.com/watch?v=1"),
            "https://youtube.com/watch?v=1"
        );
        assert_eq!(redact("a://x@y b://z@w"), "a://***@y b://***@w");
    }

    #[test]
    fn strips_ansi() {
        assert_eq!(strip_ansi("\x1b[0;31mERROR:\x1b[0m bad"), "ERROR: bad");
    }

    #[test]
    fn ring_is_bounded() {
        let mut log = SessionLog::default();
        for i in 0..LOG_CAPACITY + 10 {
            log.push(None, Level::Info, LogMessage::text(i.to_string()));
        }
        let snap = log.snapshot();
        assert_eq!(snap.len(), LOG_CAPACITY);
        assert_eq!(snap[0].seq, 10);
    }
}
