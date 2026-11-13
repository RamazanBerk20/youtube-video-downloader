const COMMANDS: &[&str] = &[
    "get_bootstrap",
    "update_settings",
    "set_anyip_password",
    "add_tasks",
    "cancel_task",
    "remove_task",
    "retry_task",
    "cancel_all",
    "clear_finished",
    "open_output_folder",
    "preview_url",
    "dismiss_banner",
    "get_tools_status",
    "install_tool",
    "update_ytdlp",
    "check_app_update",
    "apply_app_update",
    "open_release_page",
    "open_sponsor_page",
    "restart_app",
    "confirm_exit",
];

fn main() {
    // An app manifest makes every command opt-in: only those granted in
    // capabilities/main.json are callable from the webview.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
