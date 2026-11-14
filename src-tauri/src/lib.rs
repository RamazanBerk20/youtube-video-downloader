pub mod args;
pub mod backend;
pub mod browser;
pub mod commands;
pub mod log;
pub mod options;
pub mod preview;
pub mod process;
pub mod progress;
pub mod rescue;
pub mod secrets;
pub mod settings;
pub mod supervisor;
pub mod task;
pub mod tools;
pub mod transcode;
pub mod updater;
pub mod ytdlp;

use backend::{AppEvent, Backend, Banner};
use log::{Level, now_ms};
use rescue::BannerKind;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tauri::{Emitter, Manager};

/// The one event channel; payload is `AppEvent` (tagged by `type`).
pub const EVENT: &str = "app-event";

#[cfg(windows)]
fn set_app_user_model_id() {
    // Taskbar grouping/icon belong to the app (spec 1.1 item 1).
    let id: Vec<u16> = "RamazanBerk20.YouTubeDownloader\0".encode_utf16().collect();
    // SAFETY: null-terminated UTF-16 string; errors are ignored as before.
    unsafe {
        windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(id.as_ptr());
    }
}

/// Work that must not delay the first paint: secret migration, tool probes,
/// update checks and the restored queue.
fn start_background(b: Arc<Backend>, legacy_password: Option<String>) {
    std::thread::spawn(move || {
        if let Some(pw) = legacy_password {
            match secrets::set(&b.paths, &pw) {
                Ok(_) => {
                    let _ = settings::scrub_legacy_password(&b.paths);
                    b.log_text(
                        None,
                        Level::Info,
                        "Moved the saved anyIP password into secure storage.",
                    );
                }
                Err(e) => b.log_text(None, Level::Warning, &e),
            }
        }
        b.password_set
            .store(secrets::is_set(&b.paths), Ordering::SeqCst);
        b.emit_settings();

        if tools::ffmpeg().is_none() {
            b.show_banner(Banner {
                install: Some(tools::plan(tools::Tool::Ffmpeg)),
                ..Banner::simple(BannerKind::FfmpegMissing)
            });
        }
        b.ytdlp.cleanup_old();
        b.schedule();
        if b.settings().auto_update_check {
            commands::check_app(&b, false);
        }
        // yt-dlp: install when missing, then keep it current daily.
        loop {
            let missing = b.ytdlp.status().source == ytdlp::YtdlpSource::Missing;
            if missing || (b.settings().ytdlp_auto_update && b.ytdlp.auto_check_due(now_ms())) {
                let _ = commands::ytdlp_update(&b);
            }
            std::thread::sleep(Duration::from_secs(3600));
        }
    });
}

/// The first of `candidates` that has a `cursors` folder under one of `data_dirs`.
#[cfg(target_os = "linux")]
fn loadable_cursor_theme(
    candidates: impl IntoIterator<Item = String>,
    data_dirs: &[std::path::PathBuf],
) -> Option<String> {
    candidates.into_iter().find(|name| {
        !name.is_empty()
            && !name.contains('/')
            && data_dirs
                .iter()
                .any(|d| d.join("icons").join(name).join("cursors").is_dir())
    })
}

/// GTK 3.24.33, the copy inside the AppImage, aborts on Wayland as soon as it needs a cursor when
/// the desktop's cursor settings can't be loaded: a size of 0, or a theme with no `cursors` folder
/// in the system data dirs (KDE leaves both GNOME settings empty; a theme may live only in
/// `~/.icons`). Give it a size and a theme that load before any window exists. Newer GTK falls
/// back by itself, so this changes nothing there.
#[cfg(target_os = "linux")]
fn ensure_cursor_theme() {
    use gtk::prelude::*;
    if gtk::init().is_err() {
        return;
    }
    let Some(settings) = gtk::Settings::default() else {
        return;
    };
    if settings.gtk_cursor_theme_size() <= 0 {
        settings.set_gtk_cursor_theme_size(24);
    }
    let data_dirs = gtk::glib::system_data_dirs();
    let current = settings
        .gtk_cursor_theme_name()
        .map(|s| s.to_string())
        .unwrap_or_default();
    if loadable_cursor_theme([current], &data_dirs).is_some() {
        return;
    }
    let installed = data_dirs.iter().flat_map(|d| {
        std::fs::read_dir(d.join("icons"))
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| e.file_name().into_string().ok())
    });
    let candidates = std::env::var("XCURSOR_THEME")
        .ok()
        .into_iter()
        .chain(["default", "breeze_cursors", "Adwaita"].map(String::from))
        .chain(installed);
    if let Some(name) = loadable_cursor_theme(candidates, &data_dirs) {
        settings.set_gtk_cursor_theme_name(Some(&name));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // SAFETY: first thing in main, before any thread exists.
    unsafe { transcode::capture_and_strip_nvidia_env() };
    #[cfg(windows)]
    set_app_user_model_id();
    #[cfg(target_os = "linux")]
    ensure_cursor_theme();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let (b, legacy_password) = Backend::new(
                settings::Paths::detect(),
                Box::new(move |e: &AppEvent| {
                    let _ = handle.emit(EVENT, e);
                }),
            );
            app.manage(b.clone());
            start_background(b, legacy_password);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let b = window.state::<Arc<Backend>>();
                let active = b.active_count();
                if active > 0 && !b.exit_confirmed.load(Ordering::SeqCst) {
                    api.prevent_close();
                    b.emit(AppEvent::CloseRequested {
                        active_tasks: active,
                    });
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_bootstrap,
            commands::update_settings,
            commands::set_anyip_password,
            commands::add_tasks,
            commands::cancel_task,
            commands::remove_task,
            commands::retry_task,
            commands::cancel_all,
            commands::clear_finished,
            commands::open_output_folder,
            commands::preview_url,
            commands::dismiss_banner,
            commands::get_tools_status,
            commands::install_tool,
            commands::update_ytdlp,
            commands::check_app_update,
            commands::apply_app_update,
            commands::open_release_page,
            commands::open_sponsor_page,
            commands::restart_app,
            commands::confirm_exit,
        ])
        .build(tauri::generate_context!());
    let app = match app {
        Ok(a) => a,
        Err(e) => {
            eprintln!("Failed to start GUI: {e}");
            std::process::exit(1);
        }
    };
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event
            && let Some(b) = handle.try_state::<Arc<Backend>>()
        {
            b.shutdown();
        }
    });
}

#[cfg(all(test, target_os = "linux"))]
mod cursor_theme_tests {
    use super::loadable_cursor_theme;

    #[test]
    fn picks_first_theme_that_has_a_cursors_folder() {
        let dir = std::env::temp_dir().join(format!("ytdl-cursor-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("icons/good/cursors")).unwrap();
        std::fs::create_dir_all(dir.join("icons/no-cursors")).unwrap();
        let names = ["", "../good", "missing", "no-cursors", "good", "later"].map(String::from);
        let dirs = [dir.clone()];
        assert_eq!(loadable_cursor_theme(names, &dirs), Some("good".into()));
        assert_eq!(loadable_cursor_theme(["".to_string()], &dirs), None);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
