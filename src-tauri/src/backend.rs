//! The single owner of app state: settings, queue, session rescue state,
//! banners and the session log. Every change is emitted as an `AppEvent`.

use crate::args::{CookieSource, TaskSpec};
use crate::log::{Level, LogEntry, LogMessage, SessionLog, now_ms};
use crate::options::{Browser, CookieChoice};
use crate::rescue::{self, AutoSource, BannerKind, Failure, Inputs, Note, SessionRescue};
use crate::settings::{self, Paths, Settings};
use crate::task::{Attempt, ErrorClass, Task, TaskEvent, TaskState, transition};
use crate::tools::{InstallPlan, Tool};
use crate::updater::UpdateState;
use crate::ytdlp::{Ytdlp, YtdlpStatus};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Banner {
    pub kind: BannerKind,
    /// FfmpegMissing / JsRuntimeMissing: how the Install button works.
    pub install: Option<InstallPlan>,
    /// AppUpdate: the version on offer.
    pub version: Option<String>,
    /// AppUpdate: true = "Update now" installs in place; false = open the release page.
    pub can_apply: bool,
}

impl Banner {
    pub fn simple(kind: BannerKind) -> Banner {
        Banner {
            kind,
            install: None,
            version: None,
            can_apply: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(
    tag = "state",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum InstallState {
    Running,
    Done,
    Failed { error: String },
}

#[derive(Clone, Debug, Serialize)]
#[serde(
    tag = "type",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum AppEvent {
    TaskUpdated {
        task: Task,
    },
    TasksRemoved {
        ids: Vec<u64>,
    },
    Log {
        entry: LogEntry,
    },
    BannerShown {
        banner: Banner,
    },
    BannerDismissed {
        kind: BannerKind,
    },
    Settings {
        settings: Settings,
        anyip_password_set: bool,
    },
    Ytdlp {
        status: YtdlpStatus,
    },
    AppUpdate {
        state: UpdateState,
    },
    Install {
        tool: Tool,
        state: InstallState,
    },
    /// The window close was held because tasks are active; the UI asks and
    /// then calls `confirm_exit` (fixes BUG #16).
    CloseRequested {
        active_tasks: u32,
    },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum AddError {
    /// `msg.no_url.title` / `msg.no_url.body`.
    NoUrls,
    /// `msg.bad_folder.title` / `msg.bad_folder.body` with {error}.
    BadFolder { error: String },
}

pub type Emitter = Box<dyn Fn(&AppEvent) + Send + Sync>;

#[derive(Default)]
pub struct Queue {
    pub tasks: Vec<Task>,
    pub next_id: u64,
    pub session: SessionRescue,
    cancels: HashMap<u64, Arc<AtomicBool>>,
    last_emit: HashMap<u64, Instant>,
}

pub struct Backend {
    pub paths: Paths,
    emitter: Emitter,
    pub settings: Mutex<Settings>,
    pub password_set: AtomicBool,
    pub q: Mutex<Queue>,
    log: Mutex<SessionLog>,
    banners: Mutex<Vec<Banner>>,
    pub ytdlp: Ytdlp,
    pub encoders: Mutex<crate::transcode::EncoderSession>,
    pub update: Mutex<UpdateState>,
    candidates: OnceLock<Vec<Browser>>,
    last_out_dir: Mutex<Option<PathBuf>>,
    pub exiting: AtomicBool,
    pub exit_confirmed: AtomicBool,
}

const PROGRESS_EMIT_INTERVAL: Duration = Duration::from_millis(100);

impl Backend {
    pub fn new(paths: Paths, emitter: Emitter) -> (Arc<Backend>, Option<String>) {
        let loaded = settings::load(&paths);
        let mut s = loaded.settings;
        if s.language.is_empty() {
            // Written on the next save and fixed from then on (spec 1.11 item 99).
            s.language = settings::detect_language().into();
        }
        if loaded.migrated {
            let _ = settings::save(&paths, &s);
        }
        // Opt-in (an addition): without it every launch starts with an empty
        // queue and ids from 1, as before.
        let (tasks, next_id) = if s.restore_queue {
            load_queue(&paths)
        } else {
            let _ = std::fs::remove_file(paths.queue_file());
            (vec![], 0)
        };
        let b = Arc::new(Backend {
            ytdlp: Ytdlp::new(paths.clone()),
            paths,
            emitter,
            settings: Mutex::new(s),
            password_set: AtomicBool::new(false),
            q: Mutex::new(Queue {
                tasks,
                next_id,
                ..Default::default()
            }),
            log: Mutex::new(SessionLog::default()),
            banners: Mutex::new(Vec::new()),
            encoders: Mutex::new(Default::default()),
            update: Mutex::new(UpdateState::Idle),
            candidates: OnceLock::new(),
            last_out_dir: Mutex::new(None),
            exiting: AtomicBool::new(false),
            exit_confirmed: AtomicBool::new(false),
        });
        b.log_key(None, Level::Info, "log.ready", &[]);
        (b, loaded.legacy_password)
    }

    pub fn emit(&self, ev: AppEvent) {
        (self.emitter)(&ev);
    }

    pub fn log(&self, task: Option<u64>, level: Level, message: LogMessage) {
        let entry = self.log.lock().unwrap().push(task, level, message);
        self.emit(AppEvent::Log { entry });
    }
    pub fn log_key(&self, task: Option<u64>, level: Level, key: &str, args: &[(&str, &str)]) {
        self.log(task, level, LogMessage::key_with(key, args));
    }
    pub fn log_text(&self, task: Option<u64>, level: Level, text: &str) {
        self.log(task, level, LogMessage::text(text));
    }
    pub fn log_snapshot(&self) -> Vec<LogEntry> {
        self.log.lock().unwrap().snapshot()
    }

    // ---- banners -------------------------------------------------------

    /// Show or replace a banner; replacing keeps its place (fixes BUG #10).
    pub fn show_banner(&self, banner: Banner) {
        {
            let mut v = self.banners.lock().unwrap();
            match v.iter_mut().find(|b| b.kind == banner.kind) {
                Some(slot) => *slot = banner.clone(),
                None => v.push(banner.clone()),
            }
        }
        self.emit(AppEvent::BannerShown { banner });
    }
    pub fn dismiss_banner(&self, kind: BannerKind) {
        self.banners.lock().unwrap().retain(|b| b.kind != kind);
        self.emit(AppEvent::BannerDismissed { kind });
    }
    pub fn banners(&self) -> Vec<Banner> {
        self.banners.lock().unwrap().clone()
    }
    /// One-shot-per-session banner; `rearm` forgets that it was shown.
    pub fn banner_once(&self, banner: Banner) {
        let first = self.q.lock().unwrap().session.once(banner.kind).is_some();
        if first {
            self.show_banner(banner);
        }
    }
    pub fn rearm_banner(&self, kind: BannerKind) {
        self.q.lock().unwrap().session.shown.retain(|k| *k != kind);
    }

    // ---- settings ------------------------------------------------------

    pub fn settings(&self) -> Settings {
        self.settings.lock().unwrap().clone()
    }

    pub fn update_settings(
        self: &Arc<Self>,
        patch: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<Settings, String> {
        let (new, restore_changed) = {
            let mut s = self.settings.lock().unwrap();
            let new = s.patched(patch)?;
            let changed = new.restore_queue != s.restore_queue;
            *s = new.clone();
            (new, changed)
        };
        settings::save(&self.paths, &new).map_err(|e| format!("cannot save settings: {e}"))?;
        if restore_changed {
            self.persist_queue();
        }
        // Max-concurrent changes apply to the running queue at once.
        self.schedule();
        Ok(new)
    }

    pub fn emit_settings(&self) {
        self.emit(AppEvent::Settings {
            settings: self.settings(),
            anyip_password_set: self.password_set.load(Ordering::SeqCst),
        });
    }

    // ---- queue ---------------------------------------------------------

    pub fn tasks(&self) -> Vec<Task> {
        self.q.lock().unwrap().tasks.clone()
    }

    pub fn last_out_dir(&self) -> PathBuf {
        self.last_out_dir
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_else(|| self.output_dir())
    }

    fn output_dir(&self) -> PathBuf {
        let raw = self.settings().output_dir;
        if raw.trim().is_empty() {
            settings::default_output_dir()
        } else {
            settings::expand_user(&raw)
        }
    }

    /// Add (spec 1.4): validate, create the folder, snapshot the options.
    pub fn add_tasks(self: &Arc<Self>, text: &str) -> Result<Vec<u64>, AddError> {
        let urls = crate::args::extract_urls(text);
        if urls.is_empty() {
            return Err(AddError::NoUrls);
        }
        let dir = self.output_dir();
        let dir = if dir.is_absolute() {
            dir
        } else {
            settings::home_dir().join(dir)
        };
        std::fs::create_dir_all(&dir).map_err(|e| AddError::BadFolder {
            error: e.to_string(),
        })?;
        *self.last_out_dir.lock().unwrap() = Some(dir.clone());
        let s = self.settings();
        let mut added = Vec::new();
        {
            let mut q = self.q.lock().unwrap();
            for url in &urls {
                q.next_id += 1;
                let task = Task {
                    id: q.next_id,
                    spec: TaskSpec::snapshot(url, &dir, &s),
                    title: None,
                    state: TaskState::Queued,
                    attempts: vec![],
                    rescue: Default::default(),
                    created_ms: now_ms(),
                };
                q.tasks.push(task.clone());
                added.push(task);
            }
        }
        for t in &added {
            self.emit(AppEvent::TaskUpdated { task: t.clone() });
            self.log_key(
                Some(t.id),
                Level::Info,
                "log.task_added",
                &[("url", &t.spec.url)],
            );
        }
        self.persist_queue();
        self.schedule();
        Ok(added.iter().map(|t| t.id).collect())
    }

    /// Start queued tasks in id order while slots are free (spec 1.7 item 63).
    pub fn schedule(self: &Arc<Self>) {
        if self.exiting.load(Ordering::SeqCst) {
            return;
        }
        let limit = match self.settings().max_concurrent {
            0 => usize::MAX,
            n => n as usize,
        };
        let mut starts = Vec::new();
        {
            let mut q = self.q.lock().unwrap();
            let active = q.tasks.iter().filter(|t| t.state.is_active()).count();
            let free = limit.saturating_sub(active);
            let ids: Vec<u64> = q
                .tasks
                .iter()
                .filter(|t| t.state == TaskState::Queued)
                .take(free)
                .map(|t| t.id)
                .collect();
            for id in ids {
                let auto = q.session.auto_source.clone();
                let proxy = q.session.anyip_active;
                let cancel = Arc::new(AtomicBool::new(false));
                q.cancels.insert(id, cancel.clone());
                let t = q
                    .tasks
                    .iter_mut()
                    .find(|t| t.id == id)
                    .expect("id from list");
                t.state = TaskState::Starting;
                let cookie = match t.spec.cookie_choice {
                    CookieChoice::Off => CookieSource::None,
                    CookieChoice::Auto => match auto {
                        AutoSource::None => CookieSource::None,
                        AutoSource::Browser { browser } => CookieSource::Browser { browser },
                        AutoSource::File { path } => CookieSource::File { path },
                    },
                    c => CookieSource::Browser {
                        browser: c.browser().expect("explicit browser"),
                    },
                };
                t.attempts.push(Attempt {
                    n: t.attempts.len() as u32 + 1,
                    cookie_source: cookie.clone(),
                    proxy,
                    started_ms: now_ms(),
                    ended_ms: None,
                    exit_code: None,
                    error_class: None,
                    stderr_tail: vec![],
                });
                starts.push((t.clone(), cookie, proxy, cancel));
            }
        }
        for (task, cookie, proxy, cancel) in starts {
            self.emit(AppEvent::TaskUpdated { task: task.clone() });
            let title = task.title.clone().unwrap_or_else(|| task.spec.url.clone());
            self.log_key(
                Some(task.id),
                Level::Info,
                "log.task_started",
                &[("title", &title)],
            );
            let b = self.clone();
            std::thread::Builder::new()
                .name(format!("task-{}", task.id))
                .spawn(move || crate::supervisor::run(b, task.id, task.spec, cookie, proxy, cancel))
                .expect("spawn task thread");
        }
    }

    /// Apply a non-terminal event from a supervisor. Progress is throttled.
    pub fn apply(&self, id: u64, ev: TaskEvent) {
        let throttle = matches!(
            ev,
            TaskEvent::Progress { .. } | TaskEvent::PostProgress { .. }
        );
        let snapshot = {
            let mut q = self.q.lock().unwrap();
            let Some(idx) = q.tasks.iter().position(|t| t.id == id) else {
                return;
            };
            let Ok(next) = transition(&q.tasks[idx].state, ev) else {
                return;
            };
            let changed_kind =
                std::mem::discriminant(&next) != std::mem::discriminant(&q.tasks[idx].state);
            q.tasks[idx].state = next;
            let now = Instant::now();
            let due = q
                .last_emit
                .get(&id)
                .is_none_or(|t| now.duration_since(*t) >= PROGRESS_EMIT_INTERVAL);
            if throttle && !changed_kind && !due {
                return;
            }
            q.last_emit.insert(id, now);
            q.tasks[idx].clone()
        };
        self.emit(AppEvent::TaskUpdated { task: snapshot });
    }

    pub fn set_title(&self, id: u64, title: &str) {
        let snap = {
            let mut q = self.q.lock().unwrap();
            let Some(t) = q.tasks.iter_mut().find(|t| t.id == id) else {
                return;
            };
            t.title = Some(title.to_owned());
            t.clone()
        };
        self.emit(AppEvent::TaskUpdated { task: snap });
    }

    fn candidates(&self) -> Vec<Browser> {
        self.candidates
            .get_or_init(crate::browser::candidates)
            .clone()
    }

    /// Terminal event of an attempt, then logs, rescue and re-scheduling.
    pub fn finish(
        self: &Arc<Self>,
        id: u64,
        ev: TaskEvent,
        exit_code: Option<i32>,
        stderr_tail: Vec<String>,
    ) {
        if self.exiting.load(Ordering::SeqCst) {
            return;
        }
        let failure_class = match &ev {
            TaskEvent::Failed { class, .. } => Some(*class),
            _ => None,
        };
        // Gather slow inputs (browser probe, keyring) before taking the lock.
        let rescue_inputs = failure_class
            .filter(|c| matches!(c, ErrorClass::BotCheck | ErrorClass::CookieDecrypt))
            .map(|_| {
                let s = self.settings();
                // Only touch the keyring when anyIP rescue is on.
                let anyip_configured = s.anyip_enabled
                    && !s.anyip_username.trim().is_empty()
                    && crate::secrets::is_set(&self.paths);
                (s, anyip_configured, self.candidates())
            });
        let (task, outcome) = {
            let mut q = self.q.lock().unwrap();
            q.cancels.remove(&id);
            q.last_emit.remove(&id);
            let Queue { tasks, session, .. } = &mut *q;
            let Some(t) = tasks.iter_mut().find(|t| t.id == id) else {
                return;
            };
            let Ok(next) = transition(&t.state, ev) else {
                return;
            };
            t.state = next;
            if let Some(a) = t.attempts.last_mut() {
                a.ended_ms = Some(now_ms());
                a.exit_code = exit_code;
                a.error_class = failure_class;
                a.stderr_tail = stderr_tail;
            }
            let mut outcome = None;
            if let (Some(class), Some((s, anyip_configured, cands))) =
                (failure_class, &rescue_inputs)
            {
                let last = t.attempts.last().cloned();
                let file = if !t.spec.cookie_file.is_empty() {
                    t.spec.cookie_file.clone()
                } else {
                    s.cookie_file.trim().to_owned()
                };
                let file = (!file.is_empty())
                    .then(|| settings::expand_user(&file).to_string_lossy().into_owned());
                let exists = file
                    .as_ref()
                    .is_some_and(|f| std::path::Path::new(f).is_file());
                let f = Failure {
                    class,
                    used_cookies: last
                        .as_ref()
                        .map(|a| a.cookie_source.clone())
                        .unwrap_or(CookieSource::None),
                    used_proxy: last.as_ref().is_some_and(|a| a.proxy),
                };
                let inputs = Inputs {
                    task_choice: t.spec.cookie_choice,
                    cookie_file: file.as_deref(),
                    cookie_file_exists: exists,
                    candidates: cands,
                    anyip_enabled: s.anyip_enabled,
                    anyip_configured: *anyip_configured,
                };
                let o = rescue::decide(session, &mut t.rescue, &f, &inputs);
                outcome = Some(o);
            }
            (t.clone(), outcome)
        };
        self.emit(AppEvent::TaskUpdated { task: task.clone() });
        let title = task.title.clone().unwrap_or_else(|| task.spec.url.clone());
        match &task.state {
            TaskState::Done { item_errors, .. } => {
                for e in item_errors {
                    self.log_text(Some(id), Level::Warning, &format!("error: {e}"));
                }
                self.log_key(Some(id), Level::Info, "log.task_done", &[("title", &title)]);
            }
            TaskState::Cancelled { .. } => self.log_key(
                Some(id),
                Level::Info,
                "log.task_cancelled",
                &[("title", &title)],
            ),
            TaskState::Failed {
                error_class,
                message,
            } => {
                let mut lines = message.lines().filter(|l| !l.trim().is_empty());
                let first = lines.next().unwrap_or_default();
                self.log_key(
                    Some(id),
                    Level::Error,
                    "log.task_failed",
                    &[("title", &title), ("error", first)],
                );
                for l in lines {
                    self.log_text(Some(id), Level::Error, &format!("   {l}"));
                }
                self.after_failure(*error_class);
            }
            _ => {}
        }
        if let Some(o) = outcome {
            for n in &o.notes {
                let (key, arg): (&str, Option<(&str, String)>) = match n {
                    Note::AutoCookiesSwitched(b) => {
                        ("log.auto_cookies_switched", Some(("browser", b.display())))
                    }
                    Note::AutoCookiesNoBrowser => ("log.auto_cookies_no_browser", None),
                    Note::CookieFileMissing(p) => {
                        ("log.cookie_file_missing", Some(("path", p.clone())))
                    }
                    Note::CookieFileRetry(p) => {
                        ("log.cookie_file_retry", Some(("path", p.clone())))
                    }
                    Note::AnyipActivated => ("log.anyip_activated", None),
                };
                match arg {
                    Some((k, v)) => self.log_key(None, Level::Warning, key, &[(k, &v)]),
                    None => self.log_key(None, Level::Warning, key, &[]),
                }
            }
            if let Some(kind) = o.banner {
                self.show_banner(Banner::simple(kind));
            }
            if o.retry {
                self.requeue(id);
            }
        }
        self.persist_queue();
        self.schedule();
    }

    fn after_failure(&self, class: ErrorClass) {
        match class {
            ErrorClass::NoFormats if crate::tools::js_runtimes().is_empty() => {
                self.banner_once(Banner {
                    install: Some(crate::tools::plan(Tool::Deno)),
                    ..Banner::simple(BannerKind::JsRuntimeMissing)
                })
            }
            ErrorClass::FfmpegMissing => self.show_banner(Banner {
                install: Some(crate::tools::plan(Tool::Ffmpeg)),
                ..Banner::simple(BannerKind::FfmpegMissing)
            }),
            ErrorClass::YtdlpMissing => self.show_banner(Banner::simple(BannerKind::YtdlpMissing)),
            _ => {}
        }
    }

    fn requeue(&self, id: u64) -> bool {
        let snap = {
            let mut q = self.q.lock().unwrap();
            let Some(t) = q.tasks.iter_mut().find(|t| t.id == id) else {
                return false;
            };
            let Ok(next) = transition(&t.state, TaskEvent::Requeue) else {
                return false;
            };
            t.state = next;
            t.clone()
        };
        self.emit(AppEvent::TaskUpdated { task: snap });
        true
    }

    /// Per-row retry (new; the old API allowed it, the old UI never did).
    pub fn retry(self: &Arc<Self>, id: u64) -> bool {
        let ok = self.requeue(id);
        if ok {
            self.persist_queue();
            self.schedule();
        }
        ok
    }

    /// Cancel works in every non-terminal state (spec 6.8).
    pub fn cancel(self: &Arc<Self>, id: u64) {
        let (snap, queued) = {
            let mut q = self.q.lock().unwrap();
            let flag = q.cancels.get(&id).cloned();
            let Some(t) = q.tasks.iter_mut().find(|t| t.id == id) else {
                return;
            };
            let queued = t.state == TaskState::Queued;
            let Ok(next) = transition(&t.state, TaskEvent::CancelRequested) else {
                return;
            };
            t.state = next;
            if let Some(f) = flag {
                f.store(true, Ordering::SeqCst);
            }
            (t.clone(), queued)
        };
        self.emit(AppEvent::TaskUpdated { task: snap.clone() });
        if queued {
            let title = snap.title.clone().unwrap_or_else(|| snap.spec.url.clone());
            self.log_key(
                Some(id),
                Level::Info,
                "log.task_cancelled",
                &[("title", &title)],
            );
            self.persist_queue();
        }
    }

    pub fn cancel_all(self: &Arc<Self>) {
        let ids: Vec<u64> = self
            .q
            .lock()
            .unwrap()
            .tasks
            .iter()
            .filter(|t| !t.state.is_terminal())
            .map(|t| t.id)
            .collect();
        for id in ids {
            self.cancel(id);
        }
    }

    /// `×` on a finished row; ignored for non-terminal tasks.
    pub fn remove(&self, id: u64) -> bool {
        let removed = {
            let mut q = self.q.lock().unwrap();
            let before = q.tasks.len();
            q.tasks.retain(|t| !(t.id == id && t.state.is_terminal()));
            q.tasks.len() != before
        };
        if removed {
            self.emit(AppEvent::TasksRemoved { ids: vec![id] });
            self.persist_queue();
        }
        removed
    }

    pub fn clear_finished(&self) -> Vec<u64> {
        let ids: Vec<u64> = {
            let mut q = self.q.lock().unwrap();
            let ids = q
                .tasks
                .iter()
                .filter(|t| t.state.is_terminal())
                .map(|t| t.id)
                .collect();
            q.tasks.retain(|t| !t.state.is_terminal());
            ids
        };
        if !ids.is_empty() {
            self.emit(AppEvent::TasksRemoved { ids: ids.clone() });
            self.persist_queue();
        }
        ids
    }

    pub fn active_count(&self) -> u32 {
        self.q
            .lock()
            .unwrap()
            .tasks
            .iter()
            .filter(|t| t.state.is_active())
            .count() as u32
    }

    /// Queue + history survive a restart; interrupted tasks come back
    /// queued (yt-dlp resumes their .part files).
    pub fn persist_queue(&self) {
        if !self.settings().restore_queue {
            let _ = std::fs::remove_file(self.paths.queue_file());
            return;
        }
        let tasks: Vec<Task> = self
            .q
            .lock()
            .unwrap()
            .tasks
            .iter()
            .cloned()
            .map(|mut t| {
                if !t.state.is_terminal() {
                    t.state = TaskState::Queued;
                }
                t
            })
            .collect();
        if let Ok(json) = serde_json::to_vec(&tasks)
            && let Err(e) = settings::write_atomic(&self.paths.queue_file(), &json, false)
        {
            self.log_text(None, Level::Warning, &format!("Cannot save the queue: {e}"));
        }
    }

    /// App exit: keep the queue, kill every child tree.
    pub fn shutdown(&self) {
        self.persist_queue();
        self.exiting.store(true, Ordering::SeqCst);
        crate::process::kill_all();
    }
}

fn load_queue(paths: &Paths) -> (Vec<Task>, u64) {
    let tasks: Vec<Task> = std::fs::read(paths.queue_file())
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default();
    let tasks: Vec<Task> = tasks
        .into_iter()
        .map(|mut t| {
            if !t.state.is_terminal() {
                t.state = TaskState::Queued;
            }
            t
        })
        .collect();
    let next = tasks.iter().map(|t| t.id).max().unwrap_or(0);
    (tasks, next)
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::progress::PostKind;

    pub fn test_backend(tag: &str) -> (Arc<Backend>, Arc<Mutex<Vec<AppEvent>>>, PathBuf) {
        let dir = std::env::temp_dir().join(format!("ytdl-backend-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let paths = Paths {
            config_dir: dir.join("cfg"),
            data_dir: dir.join("data"),
        };
        let events = Arc::new(Mutex::new(Vec::new()));
        let ev2 = events.clone();
        let (b, _) = Backend::new(
            paths,
            Box::new(move |e| ev2.lock().unwrap().push(e.clone())),
        );
        (b, events, dir)
    }

    fn put_task(b: &Backend, state: TaskState) -> u64 {
        let mut q = b.q.lock().unwrap();
        q.next_id += 1;
        let id = q.next_id;
        let spec = TaskSpec::snapshot(
            "https://x/1",
            std::path::Path::new("/tmp"),
            &Settings::default(),
        );
        q.tasks.push(Task {
            id,
            spec,
            title: None,
            state,
            attempts: vec![],
            rescue: Default::default(),
            created_ms: 0,
        });
        id
    }

    #[test]
    fn add_validates_and_snapshots() {
        let (b, events, dir) = test_backend("add");
        assert_eq!(b.add_tasks("not a url\n"), Err(AddError::NoUrls));
        let out = dir.join("out");
        let patch = serde_json::json!({ "outputDir": out.to_string_lossy(), "maxConcurrent": 1, "mode": "audio", "container": "MKV" });
        // Hold the queue at zero slots so nothing actually starts.
        b.exiting.store(true, Ordering::SeqCst);
        b.update_settings(patch.as_object().unwrap()).unwrap();
        let ids = b.add_tasks("https://a/1\nhttps://a/2").unwrap();
        assert_eq!(ids, [1, 2]);
        assert!(out.is_dir(), "output folder created");
        let tasks = b.tasks();
        assert_eq!(
            tasks[0].spec.container,
            crate::options::Container::None,
            "forced None in audio mode"
        );
        assert_eq!(tasks[0].spec.out_dir, out.to_string_lossy());
        let ev = events.lock().unwrap();
        assert!(
            ev.iter()
                .any(|e| matches!(e, AppEvent::TaskUpdated { task } if task.id == 2))
        );
        drop(ev);
        let file = dir.join("file");
        std::fs::write(&file, b"x").unwrap();
        let patch = serde_json::json!({ "outputDir": file.join("sub").to_string_lossy() });
        b.update_settings(patch.as_object().unwrap()).unwrap();
        assert!(matches!(
            b.add_tasks("https://a/3"),
            Err(AddError::BadFolder { .. })
        ));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn cancel_queued_is_immediate_and_remove_only_terminal() {
        let (b, _, dir) = test_backend("cancel");
        let a = put_task(&b, TaskState::Queued);
        let r = put_task(
            &b,
            TaskState::Downloading {
                item: None,
                stream: 0,
                percent: 1.0,
                downloaded: None,
                total: None,
                speed: None,
                eta: None,
            },
        );
        assert!(!b.remove(a), "queued rows are cancelled, not removed");
        b.cancel(a);
        b.cancel(r);
        let t = b.tasks();
        assert_eq!(t[0].state, TaskState::Cancelled { cleaned_up: true });
        assert_eq!(t[1].state, TaskState::Cancelling);
        assert!(b.remove(a));
        assert_eq!(
            b.clear_finished(),
            Vec::<u64>::new(),
            "cancelling task is not finished"
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn queue_is_not_kept_unless_restore_queue_is_on() {
        let (b, _, dir) = test_backend("nopersist");
        put_task(&b, TaskState::Queued);
        b.persist_queue();
        assert!(
            !b.paths.queue_file().exists(),
            "off by default: nothing kept"
        );
        let patch = serde_json::json!({ "restoreQueue": true });
        b.update_settings(patch.as_object().unwrap()).unwrap();
        b.exiting.store(true, Ordering::SeqCst);
        b.persist_queue();
        assert!(b.paths.queue_file().exists());
        // Turning it off again drops the saved queue; a new backend starts empty.
        let patch = serde_json::json!({ "restoreQueue": false });
        b.update_settings(patch.as_object().unwrap()).unwrap();
        b.persist_queue();
        let (again, _) = Backend::new(b.paths.clone(), Box::new(|_| {}));
        assert!(again.tasks().is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn queue_survives_restart_with_interrupted_tasks_requeued() {
        let (b, _, dir) = test_backend("persist");
        b.exiting.store(true, Ordering::SeqCst);
        let patch = serde_json::json!({ "restoreQueue": true });
        b.update_settings(patch.as_object().unwrap()).unwrap();
        put_task(
            &b,
            TaskState::Done {
                files: vec!["/f".into()],
                item_errors: vec![],
            },
        );
        put_task(
            &b,
            TaskState::PostProcessing {
                kind: PostKind::Encode,
                item: None,
                percent: None,
                speed_x: None,
                eta: None,
            },
        );
        b.persist_queue();
        let (tasks, next) = load_queue(&b.paths);
        assert_eq!(next, 2);
        assert!(matches!(tasks[0].state, TaskState::Done { .. }));
        assert_eq!(tasks[1].state, TaskState::Queued);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn banners_replace_in_place() {
        let (b, _, dir) = test_backend("banner");
        b.show_banner(Banner::simple(BannerKind::FfmpegMissing));
        b.show_banner(Banner::simple(BannerKind::BotDetection));
        b.show_banner(Banner {
            version: Some("2".into()),
            ..Banner::simple(BannerKind::FfmpegMissing)
        });
        let v = b.banners();
        assert_eq!(
            v.iter().map(|b| b.kind).collect::<Vec<_>>(),
            [BannerKind::FfmpegMissing, BannerKind::BotDetection]
        );
        assert_eq!(v[0].version.as_deref(), Some("2"));
        b.dismiss_banner(BannerKind::FfmpegMissing);
        assert_eq!(b.banners().len(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn failed_bot_check_on_auto_is_rescued_with_logs() {
        let (b, events, dir) = test_backend("rescue");
        let id = put_task(&b, TaskState::Starting);
        b.q.lock().unwrap().tasks[0].attempts.push(Attempt {
            n: 1,
            cookie_source: CookieSource::None,
            proxy: false,
            started_ms: 0,
            ended_ms: None,
            exit_code: None,
            error_class: None,
            stderr_tail: vec![],
        });
        // Pin the candidate list instead of probing this machine's browsers.
        let _ = b.candidates.set(vec![Browser::Firefox]);
        // A second active task fills the only slot, so the re-queued task
        // is not actually started (no yt-dlp in unit tests).
        let patch = serde_json::json!({ "maxConcurrent": 1 });
        b.update_settings(patch.as_object().unwrap()).unwrap();
        let filler = Task {
            id: 99,
            state: TaskState::Starting,
            ..b.tasks()[0].clone()
        };
        b.q.lock().unwrap().tasks.push(filler);
        b.finish(
            id,
            TaskEvent::Failed {
                class: ErrorClass::BotCheck,
                message: "Sign in to confirm you're not a bot\nmore".into(),
            },
            Some(1),
            vec![],
        );
        let t = b.tasks();
        assert_eq!(
            t[0].state,
            TaskState::Queued,
            "re-queued by the rescue chain"
        );
        assert_eq!(
            b.q.lock().unwrap().session.auto_source,
            AutoSource::Browser {
                browser: Browser::Firefox
            }
        );
        let keys: Vec<String> = events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                AppEvent::Log { entry } => match &entry.message {
                    LogMessage::Key { key, .. } => Some(key.clone()),
                    LogMessage::Text { text } => Some(text.clone()),
                },
                _ => None,
            })
            .collect();
        let i_failed = keys.iter().position(|k| k == "log.task_failed").unwrap();
        assert_eq!(keys[i_failed + 1], "   more");
        assert_eq!(keys[i_failed + 2], "log.auto_cookies_switched");
        let _ = std::fs::remove_dir_all(dir);
    }
}
