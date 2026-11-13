//! One attempt of one task: run yt-dlp, translate its output into task
//! events, run our postprocessing, and clean up on cancel.

use crate::args::{self, AttemptArgs, CookieSource, PostPlan, TaskSpec};
use crate::backend::{AppEvent, Backend};
use crate::log::{Level, strip_ansi};
use crate::process::{self, Line};
use crate::progress::{PostKind, YtLine, parse_yt_line};
use crate::task::{ErrorClass, ItemPos, TaskEvent, classify, friendly_message};
use crate::transcode::{self, Ctx, FfError, Sink};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, SystemTime};

const KILL_GRACE: Duration = Duration::from_secs(2);

pub fn run(
    b: Arc<Backend>,
    id: u64,
    spec: TaskSpec,
    cookie: CookieSource,
    use_proxy: bool,
    cancel: Arc<AtomicBool>,
) {
    let started = SystemTime::now();
    let notify = |s: &crate::ytdlp::YtdlpStatus| b.emit(AppEvent::Ytdlp { status: s.clone() });
    let exe = match b.ytdlp.resolve(&notify) {
        Ok(p) => p,
        Err(e) => {
            if cancel.load(Ordering::SeqCst) {
                return b.finish(id, TaskEvent::Killed { cleaned_up: true }, None, vec![]);
            }
            return b.finish(
                id,
                TaskEvent::Failed {
                    class: ErrorClass::YtdlpMissing,
                    message: e,
                },
                None,
                vec![],
            );
        }
    };
    if cancel.load(Ordering::SeqCst) {
        return b.finish(id, TaskEvent::Killed { cleaned_up: true }, None, vec![]);
    }
    let s = b.settings();
    let out_dir = PathBuf::from(&spec.out_dir);
    // Private copy of cookies.txt: yt-dlp writes the jar back on exit.
    let cookie_copy = match &cookie {
        CookieSource::File { path } => copy_private(
            Path::new(path),
            &b.paths.tmp_dir().join(format!("cookies-{id}.txt")),
        ),
        _ => None,
    };
    let a = AttemptArgs {
        cookie_browser: match cookie {
            CookieSource::Browser { browser } => Some(browser),
            _ => None,
        },
        cookie_copy: cookie_copy.clone(),
        js_runtimes: crate::tools::js_runtimes(),
        ffmpeg_dir: crate::tools::ffmpeg().and_then(|p| p.parent().map(Path::to_path_buf)),
        archive_file: spec.extras.download_archive.then(|| b.paths.archive_file()),
    };
    let mut cmd = process::command(&exe);
    cmd.args(args::build(&spec, &a))
        .current_dir(&out_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if use_proxy {
        let password = crate::secrets::get(&b.paths).unwrap_or_default();
        if let Some(url) = args::anyip_proxy_url(
            &s.anyip_username,
            &password,
            s.anyip_network_type,
            &s.anyip_session_name,
        ) {
            for (k, v) in args::proxy_env(&url) {
                cmd.env(k, v);
            }
        }
    }
    let mut sup = match process::spawn(cmd) {
        Ok(s) => s,
        Err(e) => {
            remove_opt(&cookie_copy);
            return b.finish(
                id,
                TaskEvent::Failed {
                    class: ErrorClass::YtdlpMissing,
                    message: format!("cannot run yt-dlp: {e}"),
                },
                None,
                vec![],
            );
        }
    };
    b.apply(id, TaskEvent::Spawned);
    let rx = process::read_lines(&mut sup.child);

    let mut files: Vec<PathBuf> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    let mut tail: Vec<String> = Vec::new();
    let mut touched: BTreeSet<PathBuf> = BTreeSet::new();
    let mut stream: u32 = 0;
    let mut cur_file: Option<String> = None;
    let mut cur_finished = false;
    let mut eofs = 0;
    let mut cancelled = false;
    while eofs < 2 {
        if cancel.load(Ordering::SeqCst) {
            sup.kill_tree(KILL_GRACE);
            cancelled = true;
            break;
        }
        let line = match rx.recv_timeout(Duration::from_millis(200)) {
            Ok(Line::Text(_, l)) => l,
            Ok(Line::Eof(_)) => {
                eofs += 1;
                continue;
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(_) => break,
        };
        match parse_yt_line(&line) {
            YtLine::Item {
                index,
                count,
                title,
                ..
            } => {
                b.set_title(id, &title);
                stream = 0;
                cur_file = None;
                cur_finished = false;
                b.apply(
                    id,
                    TaskEvent::Item(index.map(|index| ItemPos { index, count })),
                );
            }
            YtLine::Progress(p) => {
                if let Some(f) = &p.filename {
                    if cur_file.as_deref() != Some(f) {
                        if cur_file.is_some() && cur_finished {
                            stream += 1;
                        }
                        cur_file = Some(f.clone());
                        cur_finished = false;
                    }
                    touched.insert(out_dir.join(f));
                }
                if let Some(t) = &p.tmpfilename {
                    touched.insert(out_dir.join(t));
                }
                if p.status == "finished" && !cur_finished {
                    cur_finished = true;
                    if let Some(f) = &p.filename {
                        // spec 1.8 item 79: finished stream file name.
                        let name = Path::new(f)
                            .file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_default();
                        b.log_text(Some(id), Level::Info, &name);
                    }
                }
                b.apply(
                    id,
                    TaskEvent::Progress {
                        stream,
                        percent: p.percent(),
                        downloaded: p.downloaded,
                        total: p.total,
                        speed: p.speed,
                        eta: p.eta,
                    },
                );
            }
            YtLine::PostStarted(kind) => b.apply(id, TaskEvent::PostStarted(kind)),
            YtLine::PostFinished => {}
            YtLine::File(f) => {
                let p = out_dir.join(&f);
                touched.insert(p.clone());
                if !files.contains(&p) {
                    files.push(p);
                }
            }
            YtLine::Warning(w) => b.log_text(Some(id), Level::Warning, &format!("warning: {w}")),
            YtLine::Error(e) => {
                b.log_text(Some(id), Level::Error, &format!("error: {e}"));
                errors.push(crate::log::redact(&e));
            }
            YtLine::Other(o) => {
                tail.push(crate::log::redact(&strip_ansi(&o)));
                if tail.len() > 20 {
                    tail.remove(0);
                }
            }
        }
    }
    remove_opt(&cookie_copy);
    if cancelled {
        let cleaned = cleanup_partials(&touched, &files, started);
        return b.finish(
            id,
            TaskEvent::Killed {
                cleaned_up: cleaned,
            },
            None,
            tail,
        );
    }
    let status = sup.wait().ok();
    let code = status.and_then(|s| s.code());
    let ok = status.is_some_and(|s| s.success());
    let joined = errors.join("\n");
    let class = classify(&joined);
    let rescue_class = matches!(class, ErrorClass::BotCheck | ErrorClass::CookieDecrypt);
    // Playlists continue past bad entries: partial success is Done with
    // per-item errors, unless the failure is one the rescue chain can fix.
    let partial_ok = !ok && spec.playlist && !files.is_empty() && !rescue_class;
    if !ok && !partial_ok {
        let raw = if joined.is_empty() {
            format!(
                "yt-dlp exited with code {}{}",
                code.map_or("?".into(), |c| c.to_string()),
                tail.last().map(|l| format!(": {l}")).unwrap_or_default()
            )
        } else {
            joined
        };
        return b.finish(
            id,
            TaskEvent::Failed {
                class,
                message: friendly_message(class, &raw),
            },
            code,
            tail,
        );
    }

    // Our postprocessing, per final file.
    let plan = args::post_plan(&spec);
    let mut out_files = Vec::new();
    if plan != PostPlan::None {
        let Some(ffmpeg) = crate::tools::ffmpeg() else {
            let raw = "ffmpeg not found; it is needed for the selected container/codec";
            return b.finish(
                id,
                TaskEvent::Failed {
                    class: ErrorClass::FfmpegMissing,
                    message: friendly_message(ErrorClass::FfmpegMissing, raw),
                },
                code,
                tail,
            );
        };
        let sink = TaskSink { b: &b, id };
        let cx = Ctx {
            ffmpeg,
            ffprobe: crate::tools::ffprobe(),
            cancel: &cancel,
            sink: &sink,
            session: &b.encoders,
        };
        for f in &files {
            let r = match &plan {
                PostPlan::Transcode {
                    target,
                    preset,
                    remux_first,
                } => transcode::transcode(&cx, f, target, *preset, *remux_first),
                PostPlan::Ac3 { bitrate_k } => transcode::to_ac3(&cx, f, *bitrate_k),
                PostPlan::None => Ok(f.clone()),
            };
            match r {
                Ok(p) => out_files.push(p),
                Err(FfError::Cancelled) => {
                    let cleaned = cleanup_partials(&touched, &files, started);
                    return b.finish(
                        id,
                        TaskEvent::Killed {
                            cleaned_up: cleaned,
                        },
                        code,
                        tail,
                    );
                }
                Err(e) => {
                    return b.finish(
                        id,
                        TaskEvent::Failed {
                            class: ErrorClass::EncoderFailed,
                            message: e.to_string(),
                        },
                        code,
                        tail,
                    );
                }
            }
        }
    } else {
        out_files = files.clone();
    }
    let item_errors = if partial_ok { errors } else { vec![] };
    b.finish(
        id,
        TaskEvent::Finished {
            files: out_files
                .iter()
                .map(|p| p.to_string_lossy().into_owned())
                .collect(),
            item_errors,
        },
        code,
        tail,
    );
}

struct TaskSink<'a> {
    b: &'a Arc<Backend>,
    id: u64,
}

impl Sink for TaskSink<'_> {
    fn log(&self, text: &str) {
        self.b.log_text(Some(self.id), Level::Info, text);
    }
    fn warn(&self, text: &str) {
        self.b
            .log_text(Some(self.id), Level::Warning, &format!("warning: {text}"));
    }
    fn phase(&self, kind: PostKind) {
        self.b.apply(self.id, TaskEvent::PostStarted(kind));
        self.b.apply(
            self.id,
            TaskEvent::PostProgress {
                percent: Some(0.0),
                speed_x: None,
                eta: None,
            },
        );
    }
    fn progress(&self, percent: Option<f64>, speed_x: Option<f64>, eta: Option<f64>) {
        self.b.apply(
            self.id,
            TaskEvent::PostProgress {
                percent,
                speed_x,
                eta,
            },
        );
    }
}

fn copy_private(src: &Path, dst: &Path) -> Option<PathBuf> {
    let bytes = std::fs::read(src).ok()?;
    crate::settings::write_atomic(dst, &bytes, true).ok()?;
    Some(dst.to_path_buf())
}

fn remove_opt(p: &Option<PathBuf>) {
    if let Some(p) = p {
        let _ = std::fs::remove_file(p);
    }
}

/// `Title [id].f137.mp4.part` → `Title [id]`: strip partial suffixes, the
/// extension and a yt-dlp format id.
pub fn item_base(name: &str) -> String {
    let mut n = name.to_owned();
    loop {
        let before = n.clone();
        if let Some(i) = n.rfind(".part-Frag") {
            n.truncate(i);
        }
        for suf in [".part", ".ytdl", ".temp"] {
            if let Some(s) = n.strip_suffix(suf) {
                n = s.to_owned();
            }
        }
        if n == before {
            break;
        }
    }
    // Drop the extension.
    if let Some(i) = n.rfind('.') {
        n.truncate(i);
    }
    // Drop `.fNNN` / `.temp` / `.reencoding` markers.
    loop {
        match n.rfind('.') {
            Some(i) if is_marker(&n[i + 1..]) => n.truncate(i),
            _ => break,
        }
    }
    n
}

fn is_marker(s: &str) -> bool {
    s == "temp"
        || s == "reencoding"
        || (s.len() > 1
            && s.starts_with('f')
            && s[1..]
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-'))
}

fn is_partial_name(name: &str) -> bool {
    name.ends_with(".part")
        || name.ends_with(".ytdl")
        || name.contains(".part-Frag")
        || name.contains(".temp.")
        || name.contains(".reencoding.")
}

/// Files to delete after a cancel. `entries` = (path, modified) in the item
/// directories. Partial-pattern files of the cancelled items always go;
/// other files with the item's base name only when this attempt created
/// them (never a finished file from an earlier download). `keep` = final
/// files of items that completed.
pub fn partials_to_remove(
    touched: &BTreeSet<PathBuf>,
    keep: &[PathBuf],
    entries: &[(PathBuf, Option<SystemTime>)],
    since: SystemTime,
) -> Vec<PathBuf> {
    let bases: BTreeSet<(PathBuf, String)> = touched
        .iter()
        .filter(|p| !keep.contains(p))
        .filter_map(|p| {
            Some((
                p.parent()?.to_path_buf(),
                item_base(&p.file_name()?.to_string_lossy()),
            ))
        })
        .filter(|(_, b)| !b.is_empty())
        .collect();
    let kept_bases: BTreeSet<String> = keep
        .iter()
        .filter_map(|p| Some(item_base(&p.file_name()?.to_string_lossy())))
        .collect();
    entries
        .iter()
        .filter(|(p, _)| !keep.contains(p))
        .filter(|(p, mtime)| {
            let (Some(dir), Some(name)) = (
                p.parent(),
                p.file_name().map(|n| n.to_string_lossy().into_owned()),
            ) else {
                return false;
            };
            bases.iter().any(|(d, base)| {
                d == dir
                    && name.starts_with(&format!("{base}."))
                    && (is_partial_name(&name)
                        || (!kept_bases.contains(base) && mtime.is_some_and(|m| m >= since)))
            })
        })
        .map(|(p, _)| p.clone())
        .collect()
}

fn cleanup_partials(touched: &BTreeSet<PathBuf>, keep: &[PathBuf], started: SystemTime) -> bool {
    let since = started - Duration::from_secs(2);
    let dirs: BTreeSet<PathBuf> = touched
        .iter()
        .filter_map(|p| p.parent().map(Path::to_path_buf))
        .collect();
    let mut entries = Vec::new();
    for d in dirs {
        for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
            let m = e.metadata().ok();
            if m.as_ref().is_some_and(|m| m.is_file()) {
                entries.push((e.path(), m.and_then(|m| m.modified().ok())));
            }
        }
    }
    let mut ok = true;
    for p in partials_to_remove(touched, keep, &entries, since) {
        // Windows may hold the handle briefly after the kill.
        let mut removed = false;
        for _ in 0..10 {
            if std::fs::remove_file(&p).is_ok() || !p.exists() {
                removed = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        ok &= removed;
    }
    ok
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn item_base_names() {
        assert_eq!(item_base("T [id].f137.mp4.part"), "T [id]");
        assert_eq!(item_base("T [id].f251.webm.part-Frag12"), "T [id]");
        assert_eq!(item_base("T [id].mp4"), "T [id]");
        assert_eq!(item_base("T [id].temp.mp4"), "T [id]");
        assert_eq!(item_base("T [id].reencoding.mp4"), "T [id]");
        assert_eq!(
            item_base("001 - My.Title v1.0 [id].mkv"),
            "001 - My.Title v1.0 [id]"
        );
        assert_eq!(item_base("T [id].f399-1.mp4.ytdl"), "T [id]");
    }

    #[test]
    fn partial_selection() {
        let d = PathBuf::from("/out");
        let old = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        let since = SystemTime::UNIX_EPOCH + Duration::from_secs(2000);
        let new = SystemTime::UNIX_EPOCH + Duration::from_secs(3000);
        let touched: BTreeSet<PathBuf> = [
            d.join("A [a].f137.mp4"),
            d.join("A [a].f137.mp4.part"),
            d.join("B [b].mp4"),
        ]
        .into();
        let keep = vec![d.join("B [b].mp4")];
        let entries = vec![
            (d.join("A [a].f137.mp4.part"), Some(new)),
            (d.join("A [a].f251.webm"), Some(new)),
            (d.join("A [a].f137.mp4.ytdl"), Some(old)),
            (d.join("A [a].mp4"), Some(old)), // finished earlier: keep
            (d.join("B [b].mp4"), Some(new)), // completed item: keep
            (d.join("C [c].mp4.part"), Some(new)), // other download: keep
            (PathBuf::from("/elsewhere/A [a].mp4.part"), Some(new)),
        ];
        let mut got = partials_to_remove(&touched, &keep, &entries, since);
        got.sort();
        assert_eq!(
            got,
            [
                d.join("A [a].f137.mp4.part"),
                d.join("A [a].f137.mp4.ytdl"),
                d.join("A [a].f251.webm")
            ]
        );
    }
}
