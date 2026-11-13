//! Tauri commands. None takes an executable path or shell string from the
//! webview; anything slow runs off the main thread.

use crate::backend::{AddError, AppEvent, Backend, Banner, InstallState};
use crate::log::{Level, LogEntry};
use crate::preview::Preview;
use crate::rescue::BannerKind;
use crate::settings::Settings;
use crate::task::Task;
use crate::tools::{InstallPlan, Tool, ToolsStatus};
use crate::updater::{self, UpdateState};
use crate::ytdlp::YtdlpStatus;
use serde::Serialize;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tauri::{AppHandle, State};

type B<'a> = State<'a, Arc<Backend>>;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Bootstrap {
    pub settings: Settings,
    pub anyip_password_set: bool,
    pub version: String,
    /// "linux" | "windows" | "macos"
    pub platform: String,
    pub default_output_dir: String,
    pub tasks: Vec<Task>,
    pub banners: Vec<Banner>,
    pub log: Vec<LogEntry>,
    pub ytdlp: YtdlpStatus,
    pub app_update: UpdateState,
}

#[tauri::command]
pub fn get_bootstrap(b: B) -> Bootstrap {
    Bootstrap {
        settings: b.settings(),
        anyip_password_set: b.password_set.load(Ordering::SeqCst),
        version: updater::CURRENT.into(),
        platform: std::env::consts::OS.into(),
        default_output_dir: crate::settings::default_output_dir()
            .to_string_lossy()
            .into_owned(),
        tasks: b.tasks(),
        banners: b.banners(),
        log: b.log_snapshot(),
        ytdlp: b.ytdlp.status(),
        app_update: b.update.lock().unwrap().clone(),
    }
}

#[tauri::command]
pub fn update_settings(
    b: B,
    patch: serde_json::Map<String, serde_json::Value>,
) -> Result<Settings, String> {
    let before = b.settings().auto_update_check;
    let s = b.update_settings(&patch)?;
    if s.auto_update_check != before {
        let state = if s.auto_update_check {
            "auto_update.on"
        } else {
            "auto_update.off"
        };
        b.log_key(
            None,
            Level::Info,
            "log.auto_update_toggled",
            &[("state", state)],
        );
    }
    Ok(s)
}

/// Empty string clears it. Returns whether a password is now stored.
#[tauri::command]
pub async fn set_anyip_password(b: B<'_>, password: String) -> Result<bool, String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let r = crate::secrets::set(&b.paths, &password)?;
        b.password_set.store(r.is_some(), Ordering::SeqCst);
        b.emit_settings();
        Ok(r.is_some())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn add_tasks(b: B, text: String) -> Result<Vec<u64>, AddError> {
    b.add_tasks(&text)
}

#[tauri::command]
pub fn cancel_task(b: B, id: u64) {
    b.cancel(id)
}

#[tauri::command]
pub fn remove_task(b: B, id: u64) -> bool {
    b.remove(id)
}

#[tauri::command]
pub fn retry_task(b: B, id: u64) -> bool {
    b.retry(id)
}

#[tauri::command]
pub fn cancel_all(b: B) {
    b.cancel_all()
}

#[tauri::command]
pub fn clear_finished(b: B) -> Vec<u64> {
    b.clear_finished()
}

/// The last Add folder of this session, else the setting (spec 1.7 item 74).
/// A missing folder does nothing.
#[tauri::command]
pub fn open_output_folder(b: B) -> Result<(), String> {
    let dir = b.last_out_dir();
    if !dir.is_dir() {
        return Ok(());
    }
    crate::process::open_with_system(dir.as_os_str()).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn preview_url(b: B<'_>, url: String) -> Result<Preview, String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || run_preview(&b, &url))
        .await
        .map_err(|e| e.to_string())?
}

fn run_preview(b: &Arc<Backend>, url: &str) -> Result<Preview, String> {
    let url = crate::args::extract_urls(url)
        .into_iter()
        .next()
        .ok_or("not an http(s) URL")?;
    let exe = b
        .ytdlp
        .resolve(&|s| b.emit(AppEvent::Ytdlp { status: s.clone() }))?;
    let mut args: Vec<String> = [
        "--ignore-config",
        "--color",
        "never",
        "-J",
        "--flat-playlist",
        "--no-warnings",
        "--remote-components",
        "ejs:github",
    ]
    .map(String::from)
    .into();
    for (n, p) in crate::tools::js_runtimes() {
        args.push("--js-runtimes".into());
        args.push(format!("{n}:{}", p.display()));
    }
    if let Some(br) = b.settings().cookies_browser.browser() {
        args.push("--cookies-from-browser".into());
        args.push(br.id().into());
    }
    args.push("--".into());
    args.push(url.clone());
    let mut cmd = crate::process::command(&exe);
    cmd.args(&args)
        .env("PYTHONIOENCODING", "utf-8")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut sup = crate::process::spawn(cmd).map_err(|e| e.to_string())?;
    let rx = crate::process::read_lines(&mut sup.child);
    let deadline = std::time::Instant::now() + Duration::from_secs(120);
    let (mut out, mut err, mut eofs) = (String::new(), String::new(), 0);
    while eofs < 2 {
        if std::time::Instant::now() > deadline {
            sup.kill_tree(Duration::from_secs(1));
            return Err("preview timed out".into());
        }
        match rx.recv_timeout(Duration::from_millis(250)) {
            Ok(crate::process::Line::Text(crate::process::Pipe::Out, l)) => out.push_str(&l),
            Ok(crate::process::Line::Text(crate::process::Pipe::Err, l)) => {
                err = crate::log::redact(&crate::log::strip_ansi(&l));
            }
            Ok(crate::process::Line::Eof(_)) => eofs += 1,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(_) => break,
        }
    }
    let ok = sup.wait().is_ok_and(|s| s.success());
    if !ok {
        return Err(if err.is_empty() {
            "yt-dlp failed".into()
        } else {
            err.trim_start_matches("ERROR:").trim().to_owned()
        });
    }
    crate::preview::parse(&url, &out)
}

#[tauri::command]
pub fn dismiss_banner(b: B, kind: BannerKind) {
    b.dismiss_banner(kind)
}

#[tauri::command]
pub async fn get_tools_status() -> ToolsStatus {
    tauri::async_runtime::spawn_blocking(crate::tools::status)
        .await
        .unwrap_or_default()
}

/// Install button of the ffmpeg / JS-runtime banners (spec 1.12 item 102).
#[tauri::command]
pub async fn install_tool(b: B<'_>, tool: Tool) -> Result<(), String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || install(&b, tool))
        .await
        .map_err(|e| e.to_string())?
}

fn install(b: &Arc<Backend>, tool: Tool) -> Result<(), String> {
    let kind = match tool {
        Tool::Ffmpeg => BannerKind::FfmpegMissing,
        Tool::Deno => BannerKind::JsRuntimeMissing,
    };
    let plan = crate::tools::plan(tool);
    b.dismiss_banner(kind);
    let pkg = tool.package();
    match &plan {
        InstallPlan::Manual { hint } => {
            b.log_key(None, Level::Info, "banner.manual_install", &[]);
            b.log_text(None, Level::Info, hint);
            return Ok(());
        }
        InstallPlan::Auto { display, .. } => {
            b.log_key(None, Level::Info, "log.installing", &[("package", pkg)]);
            b.log_key(
                None,
                Level::Info,
                "log.install_running",
                &[("cmd", display)],
            );
        }
    }
    b.emit(AppEvent::Install {
        tool,
        state: InstallState::Running,
    });
    let r = crate::tools::run_plan(&plan, &mut |l| {
        if !l.is_empty() {
            b.log_text(None, Level::Info, l)
        }
    });
    let found = match tool {
        Tool::Ffmpeg => crate::tools::ffmpeg().is_some(),
        Tool::Deno => !crate::tools::js_runtimes().is_empty(),
    };
    let r = match r {
        Ok(()) if found => Ok(()),
        Ok(()) => Err("binary still not on PATH".to_owned()),
        Err(e) => Err(e),
    };
    match &r {
        Ok(()) => {
            b.log_key(None, Level::Info, "log.install_done", &[("package", pkg)]);
            b.emit(AppEvent::Install {
                tool,
                state: InstallState::Done,
            });
        }
        Err(e) => {
            b.log_key(None, Level::Error, "log.install_failed", &[("error", e)]);
            b.emit(AppEvent::Install {
                tool,
                state: InstallState::Failed { error: e.clone() },
            });
            if tool == Tool::Deno {
                b.rearm_banner(kind);
            }
            b.show_banner(Banner {
                install: Some(crate::tools::plan(tool)),
                ..Banner::simple(kind)
            });
        }
    }
    r
}

/// Check GitHub and install a newer yt-dlp. Returns the new version, or
/// None when already current.
#[tauri::command]
pub async fn update_ytdlp(b: B<'_>) -> Result<Option<String>, String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || ytdlp_update(&b))
        .await
        .map_err(|e| e.to_string())?
}

pub fn ytdlp_update(b: &Arc<Backend>) -> Result<Option<String>, String> {
    let r = b
        .ytdlp
        .update(&|s| b.emit(AppEvent::Ytdlp { status: s.clone() }));
    match &r {
        Ok(Some(v)) => b.log_text(None, Level::Info, &format!("yt-dlp updated to {v}.")),
        Ok(None) => b.log_text(None, Level::Info, "yt-dlp is up to date."),
        Err(e) => b.log_text(None, Level::Warning, &format!("yt-dlp update failed: {e}")),
    }
    r
}

#[tauri::command]
pub async fn check_app_update(b: B<'_>) -> Result<UpdateState, String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || check_app(&b, true))
        .await
        .map_err(|e| e.to_string())
}

fn set_update(b: &Backend, s: UpdateState) {
    *b.update.lock().unwrap() = s.clone();
    b.emit(AppEvent::AppUpdate { state: s });
}

/// spec 1.13: silent at start-up (`manual` = false), logged on demand.
pub fn check_app(b: &Arc<Backend>, manual: bool) -> UpdateState {
    {
        let mut st = b.update.lock().unwrap();
        if st.busy() {
            return st.clone();
        }
        *st = UpdateState::Checking;
    }
    b.emit(AppEvent::AppUpdate {
        state: UpdateState::Checking,
    });
    if manual {
        b.log_key(None, Level::Info, "log.update_checking", &[]);
    }
    let state = match updater::fetch_release() {
        Ok(rel) => updater::evaluate(&rel, updater::CURRENT, &updater::install_kind()),
        Err(error) => UpdateState::Failed { error },
    };
    match &state {
        UpdateState::Available {
            latest, can_apply, ..
        } => b.show_banner(Banner {
            version: Some(latest.clone()),
            can_apply: *can_apply,
            ..Banner::simple(BannerKind::AppUpdate)
        }),
        UpdateState::UpToDate { .. } if manual => {
            b.log_key(None, Level::Info, "log.update_uptodate", &[])
        }
        UpdateState::Failed { error } if manual => {
            b.log_key(None, Level::Error, "log.update_failed", &[("error", error)])
        }
        _ => {}
    }
    set_update(b, state.clone());
    state
}

/// "Update now": download, verify, install; then the UI shows the restart
/// prompt (state ReadyToRestart).
#[tauri::command]
pub async fn apply_app_update(b: B<'_>) -> Result<UpdateState, String> {
    let b = b.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        {
            let st = b.update.lock().unwrap();
            if !matches!(
                *st,
                UpdateState::Available {
                    can_apply: true,
                    ..
                }
            ) {
                return Err("no applicable update".to_owned());
            }
        }
        b.dismiss_banner(BannerKind::AppUpdate);
        set_update(&b, UpdateState::Downloading { percent: None });
        b.log_text(None, Level::Info, "Downloading the new version…");
        let kind = updater::install_kind();
        let r = updater::fetch_release().and_then(|rel| {
            updater::apply(&rel, &kind, &mut |p| {
                let s = UpdateState::Downloading { percent: p };
                *b.update.lock().unwrap() = s.clone();
                b.emit(AppEvent::AppUpdate { state: s });
            })
        });
        let state = match r {
            Ok(version) => {
                b.log_key(None, Level::Info, "log.update_done", &[]);
                UpdateState::ReadyToRestart { version }
            }
            Err(error) => {
                b.log_key(
                    None,
                    Level::Error,
                    "log.update_failed",
                    &[("error", &error)],
                );
                UpdateState::Failed { error }
            }
        };
        set_update(&b, state.clone());
        Ok(state)
    })
    .await
    .map_err(|e| e.to_string())?
}

const SPONSOR_URL: &str = "https://github.com/sponsors/RamazanBerk20";

#[tauri::command]
pub fn open_release_page() -> Result<(), String> {
    crate::process::open_with_system(std::ffi::OsStr::new(&updater::release_page()))
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn open_sponsor_page() -> Result<(), String> {
    crate::process::open_with_system(std::ffi::OsStr::new(SPONSOR_URL)).map_err(|e| e.to_string())
}

/// Spawn the new instance detached, then exit (spec 1.13 item 107).
#[tauri::command]
pub fn restart_app(app: AppHandle, b: B) -> Result<(), String> {
    let (exe, args) = updater::restart_target().map_err(|e| e.to_string())?;
    if let Err(e) = crate::process::spawn_detached_restart(&exe, &args) {
        b.log_text(
            None,
            Level::Error,
            &format!("Failed to spawn restart process: {e}"),
        );
        return Err(e.to_string());
    }
    b.exit_confirmed.store(true, Ordering::SeqCst);
    app.exit(0);
    Ok(())
}

/// The UI confirmed closing while downloads run.
#[tauri::command]
pub fn confirm_exit(app: AppHandle, b: B) {
    b.exit_confirmed.store(true, Ordering::SeqCst);
    app.exit(0);
}
