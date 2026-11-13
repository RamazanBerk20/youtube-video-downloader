//! Managed yt-dlp: the official release binary, downloaded into the app's
//! data dir, verified against the release's SHA2-256SUMS before first use,
//! re-verified once per session, updated on demand and on a daily schedule.
//! A yt-dlp on PATH is the fallback when that is impossible.

use crate::log::now_ms;
use crate::settings::{Paths, write_atomic};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

const REPO: &str = "yt-dlp/yt-dlp";
pub const AUTO_CHECK_INTERVAL_MS: u64 = 24 * 3600 * 1000;
const USER_AGENT: &str = "youtube-downloader (yt-dlp updater)";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum YtdlpSource {
    /// Our verified copy.
    Managed,
    /// Fallback found on PATH.
    System,
    Missing,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "phase", rename_all = "camelCase")]
pub enum YtdlpPhase {
    Idle,
    Checking,
    Downloading { percent: Option<f64> },
    UpToDate,
    Updated { version: String },
    Failed { error: String },
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct YtdlpStatus {
    pub source: YtdlpSource,
    pub version: Option<String>,
    pub path: Option<String>,
    pub latest: Option<String>,
    pub last_check_ms: Option<u64>,
    #[serde(flatten)]
    pub phase: YtdlpPhase,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct Record {
    version: String,
    sha256: String,
    last_check_ms: u64,
}

/// Release asset for this platform.
pub fn asset_name() -> &'static str {
    if cfg!(windows) {
        if cfg!(target_arch = "aarch64") {
            "yt-dlp_arm64.exe"
        } else {
            "yt-dlp.exe"
        }
    } else if cfg!(target_os = "macos") {
        "yt-dlp_macos"
    } else if cfg!(target_arch = "aarch64") {
        "yt-dlp_linux_aarch64"
    } else {
        "yt-dlp_linux"
    }
}

/// Find `name`'s hash in a SHA2-256SUMS file (`<hex>  <name>` lines).
pub fn sum_for(sums: &str, name: &str) -> Option<String> {
    sums.lines().find_map(|l| {
        let mut it = l.split_whitespace();
        let hash = it.next()?;
        let file = it.next()?.trim_start_matches('*');
        (file == name && hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit()))
            .then(|| hash.to_lowercase())
    })
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// yt-dlp versions are dates (2026.10.26[.123456]); compare numerically.
pub fn is_newer(latest: &str, current: &str) -> bool {
    let parse = |v: &str| {
        v.trim_start_matches('v')
            .split('.')
            .map(|p| p.parse::<u64>().unwrap_or(0))
            .collect::<Vec<_>>()
    };
    parse(latest) > parse(current)
}

pub fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(600)))
        .timeout_connect(Some(Duration::from_secs(15)))
        .user_agent(USER_AGENT)
        .build()
        .into()
}

pub fn get_text(url: &str) -> Result<String, String> {
    let mut resp = agent().get(url).call().map_err(|e| e.to_string())?;
    resp.body_mut().read_to_string().map_err(|e| e.to_string())
}

/// Download with progress, at most `limit` bytes.
pub fn get_bytes(
    url: &str,
    limit: u64,
    progress: &mut dyn FnMut(Option<f64>),
) -> Result<Vec<u8>, String> {
    let mut resp = agent().get(url).call().map_err(|e| e.to_string())?;
    let total: Option<u64> = resp
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok());
    let mut reader = resp.body_mut().with_config().limit(limit).reader();
    let mut out = Vec::with_capacity(total.unwrap_or(0) as usize);
    let mut buf = vec![0u8; 64 * 1024];
    let mut last = -1.0;
    loop {
        let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        out.extend_from_slice(&buf[..n]);
        let pct = total.map(|t| out.len() as f64 / t as f64 * 100.0);
        if pct.is_none_or(|p| p - last >= 1.0) {
            last = pct.unwrap_or(last);
            progress(pct);
        }
    }
    Ok(out)
}

pub fn latest_release_tag(repo: &str) -> Result<String, String> {
    let body = get_text(&format!(
        "https://api.github.com/repos/{repo}/releases/latest"
    ))?;
    let v: serde_json::Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
    v.get("tag_name")
        .and_then(|t| t.as_str())
        .map(str::to_owned)
        .ok_or_else(|| "no tag_name in release".into())
}

pub struct Ytdlp {
    paths: Paths,
    status: Mutex<YtdlpStatus>,
    /// Serialises installs/updates (no two concurrent runs).
    busy: Mutex<()>,
    verified: Mutex<bool>,
    install_failed: Mutex<bool>,
}

impl Ytdlp {
    pub fn new(paths: Paths) -> Ytdlp {
        let rec = read_record(&paths);
        let exe = paths.bin_dir().join(asset_file());
        let (source, version, path) = match &rec {
            Some(r) if exe.is_file() => (
                YtdlpSource::Managed,
                Some(r.version.clone()),
                Some(exe.to_string_lossy().into_owned()),
            ),
            _ => (YtdlpSource::Missing, None, None),
        };
        Ytdlp {
            status: Mutex::new(YtdlpStatus {
                source,
                version,
                path,
                latest: None,
                last_check_ms: rec.map(|r| r.last_check_ms).filter(|t| *t > 0),
                phase: YtdlpPhase::Idle,
            }),
            paths,
            busy: Mutex::new(()),
            verified: Mutex::new(false),
            install_failed: Mutex::new(false),
        }
    }

    pub fn status(&self) -> YtdlpStatus {
        self.status.lock().unwrap().clone()
    }

    fn set(&self, notify: &dyn Fn(&YtdlpStatus), f: impl FnOnce(&mut YtdlpStatus)) {
        let s = {
            let mut s = self.status.lock().unwrap();
            f(&mut s);
            s.clone()
        };
        notify(&s);
    }

    fn managed_exe(&self) -> PathBuf {
        self.paths.bin_dir().join(asset_file())
    }

    /// The executable to run for a task. May download on first use (the
    /// calling task shows Starting meanwhile); never runs on the UI thread.
    pub fn resolve(&self, notify: &dyn Fn(&YtdlpStatus)) -> Result<PathBuf, String> {
        let _guard = self.busy.lock().unwrap();
        let exe = self.managed_exe();
        if let Some(rec) = read_record(&self.paths)
            && exe.is_file()
        {
            let mut verified = self.verified.lock().unwrap();
            if *verified {
                return Ok(exe);
            }
            match fs::read(&exe) {
                Ok(bytes) if sha256_hex(&bytes) == rec.sha256 => {
                    *verified = true;
                    return Ok(exe);
                }
                // Corrupt or tampered: discard and reinstall below.
                _ => {
                    let _ = fs::remove_file(&exe);
                }
            }
        }
        let tried = *self.install_failed.lock().unwrap();
        if !tried {
            match self.install_latest(notify, None) {
                Ok(_) => return Ok(exe),
                Err(_) => *self.install_failed.lock().unwrap() = true,
            }
        }
        match crate::process::which("yt-dlp") {
            Some(p) => {
                let path = p.to_string_lossy().into_owned();
                self.set(notify, |s| {
                    s.source = YtdlpSource::System;
                    s.path = Some(path);
                });
                Ok(p)
            }
            None => {
                self.set(notify, |s| s.source = YtdlpSource::Missing);
                Err("yt-dlp is not available: the download from GitHub failed and no yt-dlp is on PATH".into())
            }
        }
    }

    /// Check GitHub and install when newer (or when nothing is installed).
    /// Returns the installed version on change, None when up to date.
    pub fn update(&self, notify: &dyn Fn(&YtdlpStatus)) -> Result<Option<String>, String> {
        let _guard = self.busy.lock().unwrap();
        self.set(notify, |s| s.phase = YtdlpPhase::Checking);
        let tag = match latest_release_tag(REPO) {
            Ok(t) => t,
            Err(e) => {
                self.set(notify, |s| {
                    s.phase = YtdlpPhase::Failed { error: e.clone() }
                });
                return Err(e);
            }
        };
        let rec = read_record(&self.paths);
        let current = rec
            .as_ref()
            .filter(|_| self.managed_exe().is_file())
            .map(|r| r.version.clone());
        if current.as_deref().is_some_and(|c| !is_newer(&tag, c)) {
            let mut r = rec.unwrap_or_default();
            r.last_check_ms = now_ms();
            let _ = write_record(&self.paths, &r);
            self.set(notify, |s| {
                s.latest = Some(tag.clone());
                s.last_check_ms = Some(r.last_check_ms);
                s.phase = YtdlpPhase::UpToDate;
            });
            return Ok(None);
        }
        self.install_latest(notify, Some(tag)).map(Some)
    }

    fn install_latest(
        &self,
        notify: &dyn Fn(&YtdlpStatus),
        tag: Option<String>,
    ) -> Result<String, String> {
        let r = (|| {
            let tag = match tag {
                Some(t) => t,
                None => {
                    self.set(notify, |s| s.phase = YtdlpPhase::Checking);
                    latest_release_tag(REPO)?
                }
            };
            let base = format!("https://github.com/{REPO}/releases/download/{tag}");
            let sums = get_text(&format!("{base}/SHA2-256SUMS"))?;
            let expected = sum_for(&sums, asset_name()).ok_or("asset missing from SHA2-256SUMS")?;
            self.set(notify, |s| {
                s.phase = YtdlpPhase::Downloading { percent: Some(0.0) }
            });
            let bytes = get_bytes(&format!("{base}/{}", asset_name()), 300 << 20, &mut |p| {
                self.set(notify, |s| s.phase = YtdlpPhase::Downloading { percent: p })
            })?;
            let got = sha256_hex(&bytes);
            if got != expected {
                return Err(format!(
                    "checksum mismatch for {} (expected {expected}, got {got})",
                    asset_name()
                ));
            }
            self.place(&bytes)
                .map_err(|e| format!("cannot install yt-dlp: {e}"))?;
            write_record(
                &self.paths,
                &Record {
                    version: tag.clone(),
                    sha256: got,
                    last_check_ms: now_ms(),
                },
            )
            .map_err(|e| e.to_string())?;
            *self.verified.lock().unwrap() = true;
            *self.install_failed.lock().unwrap() = false;
            Ok(tag)
        })();
        match &r {
            Ok(tag) => {
                let path = self.managed_exe().to_string_lossy().into_owned();
                self.set(notify, |s| {
                    s.source = YtdlpSource::Managed;
                    s.version = Some(tag.clone());
                    s.latest = Some(tag.clone());
                    s.path = Some(path);
                    s.last_check_ms = Some(now_ms());
                    s.phase = YtdlpPhase::Updated {
                        version: tag.clone(),
                    };
                })
            }
            Err(e) => self.set(notify, |s| {
                s.phase = YtdlpPhase::Failed { error: e.clone() }
            }),
        }
        r
    }

    /// Atomically put the verified bytes in place. A running yt-dlp keeps its
    /// old inode on Unix; on Windows the running exe is renamed aside.
    fn place(&self, bytes: &[u8]) -> std::io::Result<()> {
        let dir = self.paths.bin_dir();
        fs::create_dir_all(&dir)?;
        let exe = self.managed_exe();
        let tmp = dir.join(format!("{}.new", asset_file()));
        {
            let mut f = fs::File::create(&tmp)?;
            f.write_all(bytes)?;
            f.sync_all()?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o755))?;
        }
        #[cfg(windows)]
        if exe.exists() {
            let old = dir.join(format!("{}.old", asset_file()));
            let _ = fs::remove_file(&old);
            fs::rename(&exe, &old)?;
        }
        fs::rename(&tmp, &exe)
    }

    pub fn auto_check_due(&self, now: u64) -> bool {
        self.status()
            .last_check_ms
            .is_none_or(|t| now.saturating_sub(t) >= AUTO_CHECK_INTERVAL_MS)
    }

    /// Remove leftovers of a Windows in-place update.
    pub fn cleanup_old(&self) {
        let _ = fs::remove_file(self.paths.bin_dir().join(format!("{}.old", asset_file())));
    }
}

fn asset_file() -> &'static str {
    if cfg!(windows) {
        "yt-dlp.exe"
    } else {
        "yt-dlp"
    }
}

fn record_path(paths: &Paths) -> PathBuf {
    paths.bin_dir().join("yt-dlp.json")
}

fn read_record(paths: &Paths) -> Option<Record> {
    serde_json::from_str(&fs::read_to_string(record_path(paths)).ok()?).ok()
}

fn write_record(paths: &Paths, r: &Record) -> std::io::Result<()> {
    write_atomic(
        &record_path(paths),
        serde_json::to_string_pretty(r)?.as_bytes(),
        false,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_parsing() {
        let h = "a".repeat(64);
        let sums = format!(
            "{h}  yt-dlp\n{}  yt-dlp_linux\n{}  *yt-dlp.exe\n",
            "B".repeat(64),
            "c".repeat(64)
        );
        assert_eq!(sum_for(&sums, "yt-dlp_linux"), Some("b".repeat(64)));
        assert_eq!(sum_for(&sums, "yt-dlp.exe"), Some("c".repeat(64)));
        assert_eq!(sum_for(&sums, "yt-dlp_macos"), None);
        assert_eq!(sum_for("zz  yt-dlp_linux", "yt-dlp_linux"), None);
    }

    #[test]
    fn version_compare() {
        assert!(is_newer("2026.11.01", "2026.10.26"));
        assert!(is_newer("2026.10.26.1", "2026.10.26"));
        assert!(!is_newer("2026.10.26", "2026.10.26"));
        assert!(!is_newer("2025.12.31", "2026.01.01"));
    }

    #[test]
    fn sha256() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn tampered_binary_is_not_used() {
        let dir = std::env::temp_dir().join(format!("ytdl-mgr-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let paths = Paths {
            config_dir: dir.clone(),
            data_dir: dir.clone(),
        };
        let m = Ytdlp::new(paths.clone());
        m.place(b"#!/bin/sh\necho 1\n").unwrap();
        write_record(
            &paths,
            &Record {
                version: "2026.01.01".into(),
                sha256: sha256_hex(b"other"),
                last_check_ms: 1,
            },
        )
        .unwrap();
        // Pretend the download already failed this session so no network is used.
        *m.install_failed.lock().unwrap() = true;
        let r = m.resolve(&|_| {});
        assert!(
            r.as_ref().map_or(true, |p| *p != m.managed_exe()),
            "unverified binary must not be returned"
        );
        assert!(!m.managed_exe().exists(), "tampered binary removed");
        fs::remove_dir_all(&dir).unwrap();
    }
}
