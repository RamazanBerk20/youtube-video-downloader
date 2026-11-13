//! External tools: ffmpeg/ffprobe and JS runtimes on PATH, plus in-app
//! installs through the system package manager (installer.py), with a hard
//! overall timeout and no hidden sudo prompt (fixes BUG #8).

use crate::process::{self, Line};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Tool {
    Ffmpeg,
    Deno,
}

impl Tool {
    pub fn package(self) -> &'static str {
        match self {
            Tool::Ffmpeg => "ffmpeg",
            Tool::Deno => "deno",
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolsStatus {
    pub ffmpeg: Option<String>,
    pub ffprobe: Option<String>,
    /// deno / node / bun found on PATH, in yt-dlp's preference order.
    pub js_runtimes: Vec<String>,
}

pub fn ffmpeg() -> Option<PathBuf> {
    process::which("ffmpeg")
}
pub fn ffprobe() -> Option<PathBuf> {
    process::which("ffprobe")
}

/// (name, path) for every JS runtime yt-dlp can use (spec 2.1).
pub fn js_runtimes() -> Vec<(String, PathBuf)> {
    ["deno", "node", "bun"]
        .iter()
        .filter_map(|n| process::which(n).map(|p| (n.to_string(), p)))
        .collect()
}

pub fn status() -> ToolsStatus {
    let s = |p: Option<PathBuf>| p.map(|p| p.to_string_lossy().into_owned());
    ToolsStatus {
        ffmpeg: s(ffmpeg()),
        ffprobe: s(ffprobe()),
        js_runtimes: js_runtimes().into_iter().map(|(n, _)| n).collect(),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pm {
    Winget,
    Brew,
    Pacman,
    Apt,
    Dnf,
    Zypper,
}

/// How to install a tool on this machine.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum InstallPlan {
    /// Runnable from the app. `display` is the command line shown in the log.
    Auto {
        #[serde(skip)]
        program: String,
        #[serde(skip)]
        args: Vec<String>,
        display: String,
    },
    /// Show `hint` (and maybe a command) to the user.
    Manual { hint: String },
}

/// Pure planner (installer.py:30-132). `elevation` = "pkexec" | "sudo" | None.
pub fn plan_for(tool: Tool, pm: Option<Pm>, elevation: Option<&str>) -> InstallPlan {
    let Some(pm) = pm else {
        return InstallPlan::Manual {
            hint: "No supported package manager found on this system.".into(),
        };
    };
    let pkg = tool.package();
    let (prog, args): (&str, Vec<&str>) = match (tool, pm) {
        (Tool::Ffmpeg, Pm::Winget) => (
            "winget",
            vec![
                "install",
                "--silent",
                "--accept-source-agreements",
                "--accept-package-agreements",
                "Gyan.FFmpeg",
            ],
        ),
        (Tool::Deno, Pm::Winget) => (
            "winget",
            vec![
                "install",
                "--silent",
                "--accept-source-agreements",
                "--accept-package-agreements",
                "DenoLand.Deno",
            ],
        ),
        (_, Pm::Brew) => ("brew", vec!["install", pkg]),
        (_, Pm::Pacman) => ("pacman", vec!["-S", "--noconfirm", pkg]),
        (Tool::Ffmpeg, Pm::Apt) => ("apt-get", vec!["install", "-y", "ffmpeg"]),
        (Tool::Ffmpeg, Pm::Dnf) => ("dnf", vec!["install", "-y", "ffmpeg"]),
        (Tool::Ffmpeg, Pm::Zypper) => ("zypper", vec!["--non-interactive", "install", "ffmpeg"]),
        // No deno package there; never suggest `curl | sh` (BUG #26).
        (Tool::Deno, _) => {
            return InstallPlan::Manual {
                hint: "Install Deno with your package manager or from https://docs.deno.com/runtime/getting_started/installation/ (or install Node.js), then restart the app.".into(),
            };
        }
    };
    let needs_root = matches!(pm, Pm::Pacman | Pm::Apt | Pm::Dnf | Pm::Zypper);
    let mut full: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    let program = if needs_root {
        match elevation {
            Some("pkexec") => {
                full.insert(0, prog.into());
                "pkexec".to_owned()
            }
            Some("sudo") => {
                // -n: fail instead of waiting for a password nobody can type.
                full.splice(0..0, ["-n".to_owned(), prog.to_owned()]);
                "sudo".to_owned()
            }
            _ => {
                return InstallPlan::Manual {
                    hint: format!(
                        "neither pkexec nor sudo is available — run this command yourself: sudo {prog} {}",
                        args.join(" ")
                    ),
                };
            }
        }
    } else {
        prog.to_owned()
    };
    let display = std::iter::once(program.clone())
        .chain(full.iter().cloned())
        .collect::<Vec<_>>()
        .join(" ");
    InstallPlan::Auto {
        program,
        args: full,
        display,
    }
}

pub fn detect_pm() -> Option<Pm> {
    if cfg!(windows) {
        return process::which("winget").map(|_| Pm::Winget);
    }
    if cfg!(target_os = "macos") {
        return process::which("brew").map(|_| Pm::Brew);
    }
    [
        ("pacman", Pm::Pacman),
        ("apt-get", Pm::Apt),
        ("dnf", Pm::Dnf),
        ("zypper", Pm::Zypper),
    ]
    .into_iter()
    .find(|(n, _)| process::which(n).is_some())
    .map(|(_, p)| p)
}

pub fn plan(tool: Tool) -> InstallPlan {
    let elevation = ["pkexec", "sudo"]
        .into_iter()
        .find(|e| process::which(e).is_some());
    plan_for(tool, detect_pm(), elevation)
}

pub const INSTALL_TIMEOUT: Duration = Duration::from_secs(600);

/// Run an Auto plan, streaming each output line to `on_line`. Returns
/// Ok(()) on exit code 0 or Err(summary). The timeout covers the whole run.
pub fn run_plan(plan: &InstallPlan, on_line: &mut dyn FnMut(&str)) -> Result<(), String> {
    let InstallPlan::Auto { program, args, .. } = plan else {
        return Err("manual install".into());
    };
    let exe = process::which(program)
        .ok_or_else(|| format!("failed to launch installer: {program} not found"))?;
    let mut cmd = process::command(&exe);
    cmd.args(args).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut sup = process::spawn(cmd).map_err(|e| format!("failed to launch installer: {e}"))?;
    let rx = process::read_lines(&mut sup.child);
    let deadline = Instant::now() + INSTALL_TIMEOUT;
    let mut eofs = 0;
    while eofs < 2 {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            sup.kill_tree(Duration::from_secs(2));
            return Err(format!("timed out after {} s", INSTALL_TIMEOUT.as_secs()));
        }
        match rx.recv_timeout(left.min(Duration::from_millis(500))) {
            Ok(Line::Text(_, l)) => on_line(l.trim()),
            Ok(Line::Eof(_)) => eofs += 1,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(_) => break,
        }
    }
    let status = sup.wait().map_err(|e| e.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("exit code {}", status.code().unwrap_or(-1)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn display(p: InstallPlan) -> String {
        match p {
            InstallPlan::Auto { display, .. } => display,
            InstallPlan::Manual { hint } => format!("MANUAL: {hint}"),
        }
    }

    #[test]
    fn recipes() {
        assert_eq!(
            display(plan_for(Tool::Ffmpeg, Some(Pm::Winget), None)),
            "winget install --silent --accept-source-agreements --accept-package-agreements Gyan.FFmpeg"
        );
        assert_eq!(
            display(plan_for(Tool::Ffmpeg, Some(Pm::Pacman), Some("pkexec"))),
            "pkexec pacman -S --noconfirm ffmpeg"
        );
        assert_eq!(
            display(plan_for(Tool::Ffmpeg, Some(Pm::Apt), Some("sudo"))),
            "sudo -n apt-get install -y ffmpeg"
        );
        assert_eq!(
            display(plan_for(Tool::Ffmpeg, Some(Pm::Zypper), Some("pkexec"))),
            "pkexec zypper --non-interactive install ffmpeg"
        );
        assert_eq!(
            display(plan_for(Tool::Deno, Some(Pm::Brew), None)),
            "brew install deno"
        );
        assert_eq!(
            display(plan_for(Tool::Deno, Some(Pm::Pacman), Some("pkexec"))),
            "pkexec pacman -S --noconfirm deno"
        );
        assert!(
            display(plan_for(Tool::Deno, Some(Pm::Apt), Some("pkexec")))
                .starts_with("MANUAL: Install Deno")
        );
        assert!(
            display(plan_for(Tool::Ffmpeg, Some(Pm::Dnf), None))
                .contains("neither pkexec nor sudo")
        );
        assert!(
            display(plan_for(Tool::Ffmpeg, None, None)).contains("No supported package manager")
        );
        assert!(!display(plan_for(Tool::Deno, Some(Pm::Dnf), Some("sudo"))).contains("curl"));
    }

    #[cfg(unix)]
    #[test]
    fn run_plan_streams_and_reports_exit_code() {
        let plan = InstallPlan::Auto {
            program: "sh".into(),
            args: vec!["-c".into(), "echo one; echo two >&2; exit 3".into()],
            display: String::new(),
        };
        let mut lines = Vec::new();
        let r = run_plan(&plan, &mut |l| lines.push(l.to_owned()));
        assert_eq!(r, Err("exit code 3".into()));
        lines.sort();
        assert_eq!(lines, ["one", "two"]);
    }
}
