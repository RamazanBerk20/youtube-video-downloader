//! App self-update (replaces the git-based updater.py): check the app's
//! GitHub releases, and where the install format allows it (AppImage,
//! Windows installer) download, verify against the release's SHA256SUMS and
//! apply; otherwise offer the release page. Restart spawns a detached new
//! instance that exit cleanup never touches (fixes BUG #1).

use crate::ytdlp::{get_bytes, get_text, is_newer, sha256_hex, sum_for};
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;

pub const APP_REPO: &str = "RamazanBerk20/youtube-video-downloader";
pub const CURRENT: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(
    tag = "phase",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum UpdateState {
    Idle,
    Checking,
    UpToDate {
        current: String,
    },
    Available {
        current: String,
        latest: String,
        can_apply: bool,
    },
    Downloading {
        percent: Option<f64>,
    },
    /// Applied; `msg.restart.title` / `msg.restart.body` prompt.
    ReadyToRestart {
        version: String,
    },
    Failed {
        error: String,
    },
}

impl UpdateState {
    /// Nothing runs twice at once (fixes the concurrency half of BUG #13).
    pub fn busy(&self) -> bool {
        matches!(
            self,
            UpdateState::Checking
                | UpdateState::Downloading { .. }
                | UpdateState::ReadyToRestart { .. }
        )
    }
}

/// How this copy of the app was installed, which decides whether it can
/// replace itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InstallKind {
    AppImage(PathBuf),
    WindowsInstaller,
    /// deb/rpm/AUR/dev build: the package manager owns the files.
    Other,
}

pub fn install_kind() -> InstallKind {
    if cfg!(windows) {
        return InstallKind::WindowsInstaller;
    }
    match std::env::var_os("APPIMAGE").map(PathBuf::from) {
        Some(p) if p.is_file() => InstallKind::AppImage(p),
        _ => InstallKind::Other,
    }
}

pub fn release_page() -> String {
    format!("https://github.com/{APP_REPO}/releases/latest")
}

/// Release asset that can update this install, if any.
pub fn pick_asset(assets: &[String], kind: &InstallKind) -> Option<String> {
    let arch = if cfg!(target_arch = "aarch64") {
        "aarch64"
    } else {
        "amd64"
    };
    let warch = if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "x64"
    };
    assets
        .iter()
        .find(|a| match kind {
            InstallKind::AppImage(_) => a.ends_with(".AppImage") && a.contains(arch),
            InstallKind::WindowsInstaller => a.ends_with("-setup.exe") && a.contains(warch),
            InstallKind::Other => false,
        })
        .cloned()
}

pub struct Release {
    pub tag: String,
    pub page: String,
    pub assets: Vec<(String, String)>,
}

pub fn parse_release(json: &str) -> Result<Release, String> {
    let v: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
    let tag = v
        .get("tag_name")
        .and_then(Value::as_str)
        .ok_or("no tag_name")?
        .to_owned();
    let page = v
        .get("html_url")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let assets = v
        .get("assets")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|a| {
            Some((
                a.get("name")?.as_str()?.to_owned(),
                a.get("browser_download_url")?.as_str()?.to_owned(),
            ))
        })
        .collect();
    Ok(Release { tag, page, assets })
}

/// Pure evaluation of a release against this build.
pub fn evaluate(rel: &Release, current: &str, kind: &InstallKind) -> UpdateState {
    let latest = rel.tag.trim_start_matches('v').to_owned();
    if !is_newer(&latest, current) {
        return UpdateState::UpToDate {
            current: current.into(),
        };
    }
    let names: Vec<String> = rel.assets.iter().map(|(n, _)| n.clone()).collect();
    let has_sums = names.iter().any(|n| n == "SHA256SUMS");
    UpdateState::Available {
        current: current.into(),
        latest,
        can_apply: has_sums && pick_asset(&names, kind).is_some(),
    }
}

pub fn fetch_release() -> Result<Release, String> {
    parse_release(&get_text(&format!(
        "https://api.github.com/repos/{APP_REPO}/releases/latest"
    ))?)
}

/// Download, verify and put the new version in place. Returns the version.
pub fn apply(
    rel: &Release,
    kind: &InstallKind,
    progress: &mut dyn FnMut(Option<f64>),
) -> Result<String, String> {
    let names: Vec<String> = rel.assets.iter().map(|(n, _)| n.clone()).collect();
    let asset = pick_asset(&names, kind).ok_or("no release asset for this install")?;
    let url = |name: &str| {
        rel.assets
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, u)| u.clone())
    };
    let sums = get_text(&url("SHA256SUMS").ok_or("release has no SHA256SUMS")?)?;
    let expected = sum_for(&sums, &asset).ok_or("asset missing from SHA256SUMS")?;
    let bytes = get_bytes(
        &url(&asset).ok_or("asset url missing")?,
        500 << 20,
        progress,
    )?;
    let got = sha256_hex(&bytes);
    if got != expected {
        return Err(format!("checksum mismatch for {asset}"));
    }
    match kind {
        InstallKind::AppImage(path) => {
            let tmp = path.with_extension("AppImage.new");
            std::fs::write(&tmp, &bytes).map_err(|e| e.to_string())?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755))
                    .map_err(|e| e.to_string())?;
            }
            std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
        }
        InstallKind::WindowsInstaller => {
            // The installer replaces the app files; it is started detached and
            // the app exits on restart_app.
            let dir = std::env::temp_dir().join("youtube-downloader-update");
            std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
            let exe = dir.join(&asset);
            std::fs::write(&exe, &bytes).map_err(|e| e.to_string())?;
            *PENDING_INSTALLER.lock().unwrap() = Some(exe);
        }
        InstallKind::Other => return Err("this install cannot update itself".into()),
    }
    Ok(rel.tag.trim_start_matches('v').to_owned())
}

static PENDING_INSTALLER: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);

/// What to launch on restart: the downloaded Windows installer, else the
/// (possibly replaced) app itself with the same arguments.
pub fn restart_target() -> std::io::Result<(PathBuf, Vec<std::ffi::OsString>)> {
    if let Some(inst) = PENDING_INSTALLER.lock().unwrap().take() {
        // Interactive installer: its finish page relaunches the app.
        return Ok((inst, vec![]));
    }
    let exe = match install_kind() {
        InstallKind::AppImage(p) => p,
        _ => std::env::current_exe()?,
    };
    Ok((exe, std::env::args_os().skip(1).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(tag: &str, assets: &[&str]) -> Release {
        Release {
            tag: tag.into(),
            page: "p".into(),
            assets: assets
                .iter()
                .map(|a| (a.to_string(), format!("u/{a}")))
                .collect(),
        }
    }

    #[test]
    fn evaluation() {
        let img = InstallKind::AppImage("/x.AppImage".into());
        assert_eq!(
            evaluate(&rel("v2.0.0", &[]), "2.0.0", &img),
            UpdateState::UpToDate {
                current: "2.0.0".into()
            }
        );
        let r = rel(
            "v2.1.0",
            &["YouTube Downloader_2.1.0_amd64.AppImage", "SHA256SUMS"],
        );
        assert_eq!(
            evaluate(&r, "2.0.0", &img),
            UpdateState::Available {
                current: "2.0.0".into(),
                latest: "2.1.0".into(),
                can_apply: !cfg!(target_arch = "aarch64")
            }
        );
        assert!(matches!(
            evaluate(&r, "2.0.0", &InstallKind::Other),
            UpdateState::Available {
                can_apply: false,
                ..
            }
        ));
        let no_sums = rel("v2.1.0", &["YouTube Downloader_2.1.0_amd64.AppImage"]);
        assert!(
            matches!(
                evaluate(&no_sums, "2.0.0", &img),
                UpdateState::Available {
                    can_apply: false,
                    ..
                }
            ),
            "no checksum, no apply"
        );
    }

    #[test]
    fn busy_states() {
        assert!(UpdateState::Checking.busy());
        assert!(!UpdateState::Idle.busy());
        assert!(!UpdateState::Failed { error: "x".into() }.busy());
    }

    #[test]
    fn release_json() {
        let r = parse_release(r#"{"tag_name":"v2.1.0","html_url":"h","assets":[{"name":"a","browser_download_url":"u"}]}"#).unwrap();
        assert_eq!(
            (r.tag.as_str(), r.page.as_str(), r.assets.len()),
            ("v2.1.0", "h", 1)
        );
    }
}
