//! Child-process supervision. Every yt-dlp/ffmpeg/installer child runs in its
//! own process group (Unix) or kill-on-close Job Object (Windows) so that
//! cancel kills the whole tree, and is registered so app exit can kill what
//! is left. Restart spawns are deliberately *not* registered (BUG #1).

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// A running child plus the handle that kills its whole tree.
pub struct Supervised {
    pub child: Child,
    #[cfg(windows)]
    job: Option<JobHandle>,
}

fn registry() -> &'static Mutex<HashMap<u32, ()>> {
    static R: OnceLock<Mutex<HashMap<u32, ()>>> = OnceLock::new();
    R.get_or_init(Default::default)
}

/// Build a `Command` for a child we supervise. No console window on Windows.
pub fn command(program: impl AsRef<OsStr>) -> Command {
    let mut cmd = Command::new(program);
    cmd.stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
        #[cfg(target_os = "linux")]
        // SAFETY: prctl is async-signal-safe; nothing else runs between fork and exec.
        unsafe {
            cmd.pre_exec(|| {
                // If the app dies (even SIGKILL), the direct child is killed
                // too (fixes the orphan half of BUG #22).
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                Ok(())
            });
        }
    }
    cmd
}

/// Spawn and register for exit cleanup.
pub fn spawn(mut cmd: Command) -> std::io::Result<Supervised> {
    let child = cmd.spawn()?;
    registry().lock().unwrap().insert(child.id(), ());
    #[cfg(windows)]
    {
        // shortcut: the child runs briefly before it joins the job; yt-dlp
        // spawns ffmpeg much later, so the race is harmless in practice.
        let job = JobHandle::for_child(&child);
        Ok(Supervised { child, job })
    }
    #[cfg(not(windows))]
    Ok(Supervised { child })
}

impl Supervised {
    /// Kill the whole tree: SIGTERM to the group, `grace` to exit, then
    /// SIGKILL. On Windows the job is terminated at once.
    pub fn kill_tree(&mut self, grace: Duration) {
        #[cfg(unix)]
        {
            let pgid = self.child.id() as i32;
            // SAFETY: plain syscalls on a process group we created.
            unsafe { libc::kill(-pgid, libc::SIGTERM) };
            let deadline = Instant::now() + grace;
            while Instant::now() < deadline {
                if matches!(self.child.try_wait(), Ok(Some(_))) {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            // Also reaps stragglers (ffmpeg) whose leader already exited.
            unsafe { libc::kill(-pgid, libc::SIGKILL) };
        }
        #[cfg(windows)]
        {
            let _ = grace;
            match &self.job {
                Some(job) => job.terminate(),
                None => {
                    let _ = self.child.kill();
                }
            }
        }
        let _ = self.child.wait();
    }

    pub fn wait(&mut self) -> std::io::Result<ExitStatus> {
        let status = self.child.wait();
        // The leader is gone; make sure no grandchild survives it.
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        #[cfg(windows)]
        if let Some(job) = &self.job {
            job.terminate();
        }
        status
    }
}

impl Drop for Supervised {
    fn drop(&mut self) {
        registry().lock().unwrap().remove(&self.child.id());
    }
}

/// Kill every registered child tree (app exit).
pub fn kill_all() {
    let ids: Vec<u32> = registry().lock().unwrap().keys().copied().collect();
    for id in ids {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(id as i32), libc::SIGKILL);
        }
        // Windows: closing the job handles at process exit kills the trees.
        #[cfg(windows)]
        let _ = id;
    }
}

#[cfg(windows)]
struct JobHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for JobHandle {}
#[cfg(windows)]
unsafe impl Sync for JobHandle {}

#[cfg(windows)]
impl JobHandle {
    fn for_child(child: &Child) -> Option<JobHandle> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::*;
        // SAFETY: standard Job Object setup; handles are checked and owned.
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            let handle = JobHandle(job);
            if ok == 0 || AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0 {
                return None;
            }
            Some(handle)
        }
    }
    fn terminate(&self) {
        // SAFETY: valid job handle owned by self.
        unsafe { windows_sys::Win32::System::JobObjects::TerminateJobObject(self.0, 1) };
    }
}

#[cfg(windows)]
impl Drop for JobHandle {
    fn drop(&mut self) {
        // SAFETY: closing our own handle; KILL_ON_JOB_CLOSE ends the tree.
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}

/// Which pipe a line came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pipe {
    Out,
    Err,
}

pub enum Line {
    Text(Pipe, String),
    Eof(Pipe),
}

/// Read both pipes line by line into one channel (lossy UTF-8, CR or LF
/// terminated so in-place progress lines are seen too).
pub fn read_lines(child: &mut Child) -> Receiver<Line> {
    let (tx, rx) = mpsc::channel();
    let pipes: [(Pipe, Option<Box<dyn Read + Send>>); 2] = [
        (
            Pipe::Out,
            child
                .stdout
                .take()
                .map(|p| Box::new(p) as Box<dyn Read + Send>),
        ),
        (
            Pipe::Err,
            child
                .stderr
                .take()
                .map(|p| Box::new(p) as Box<dyn Read + Send>),
        ),
    ];
    for (pipe, reader) in pipes {
        let tx = tx.clone();
        thread::spawn(move || {
            if let Some(r) = reader {
                let mut r = BufReader::new(r);
                let mut buf = Vec::new();
                loop {
                    buf.clear();
                    match read_until_eol(&mut r, &mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&buf)
                                .trim_end_matches(['\r', '\n'])
                                .to_owned();
                            if !line.is_empty() && tx.send(Line::Text(pipe, line)).is_err() {
                                return;
                            }
                        }
                    }
                }
            }
            let _ = tx.send(Line::Eof(pipe));
        });
    }
    rx
}

fn read_until_eol(r: &mut impl BufRead, buf: &mut Vec<u8>) -> std::io::Result<usize> {
    let mut n = 0;
    loop {
        let available = r.fill_buf()?;
        if available.is_empty() {
            return Ok(n);
        }
        match available.iter().position(|&b| b == b'\n' || b == b'\r') {
            Some(i) => {
                buf.extend_from_slice(&available[..=i]);
                r.consume(i + 1);
                return Ok(n + i + 1);
            }
            None => {
                let len = available.len();
                buf.extend_from_slice(available);
                r.consume(len);
                n += len;
            }
        }
    }
}

/// Run a short helper (ffprobe, `--version`, xdg-settings…) with a timeout,
/// returning (exit success, stdout) or None on spawn failure/timeout.
pub fn run_capture(program: &Path, args: &[&str], timeout: Duration) -> Option<(bool, String)> {
    let mut cmd = command(program);
    cmd.args(args).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut sup = spawn(cmd).ok()?;
    let mut out = sup.child.stdout.take()?;
    let reader = thread::spawn(move || {
        let mut s = Vec::new();
        let _ = out.read_to_end(&mut s);
        String::from_utf8_lossy(&s).into_owned()
    });
    let deadline = Instant::now() + timeout;
    loop {
        match sup.child.try_wait() {
            Ok(Some(status)) => {
                let text = reader.join().unwrap_or_default();
                return Some((status.success(), text));
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                sup.kill_tree(Duration::ZERO);
                return None;
            }
        }
    }
}

/// Extra places tools land that a running process's PATH may not include
/// yet (winget user-scope shims on Windows).
fn extra_dirs() -> Vec<PathBuf> {
    #[cfg(windows)]
    {
        std::env::var_os("LOCALAPPDATA")
            .map(|l| {
                vec![
                    PathBuf::from(l)
                        .join("Microsoft")
                        .join("WinGet")
                        .join("Links"),
                ]
            })
            .unwrap_or_default()
    }
    #[cfg(not(windows))]
    {
        let mut v = Vec::new();
        if let Some(h) = dirs::home_dir() {
            v.push(h.join(".deno").join("bin"));
            v.push(h.join(".local").join("bin"));
        }
        v
    }
}

/// `shutil.which`, but on Windows only real `.exe` files are accepted: never
/// a `.bat`/`.cmd` shim, which cmd.exe would re-parse (BUG #24, BatBadBut).
pub fn which(name: &str) -> Option<PathBuf> {
    let file = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    };
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .chain(extra_dirs())
        .filter(|d| d.is_absolute())
        .map(|d| d.join(&file))
        .find(|p| is_executable(p))
}

pub fn is_executable(p: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.metadata()
            .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    }
    #[cfg(not(unix))]
    {
        // symlink_metadata: winget's app-execution aliases are reparse
        // points that a following metadata() call cannot open.
        p.symlink_metadata().is_ok_and(|m| !m.is_dir())
    }
}

/// Open a folder or URL with the desktop's default handler. The opener is
/// reaped on a thread so it never becomes a zombie (BUG #22).
pub fn open_with_system(target: &OsStr) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
        let wide: Vec<u16> = target.encode_wide().chain(Some(0)).collect();
        let verb: Vec<u16> = "open\0".encode_utf16().collect();
        // SAFETY: null-terminated UTF-16 buffers outlive the call.
        let r = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                verb.as_ptr(),
                wide.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            )
        };
        if (r as isize) <= 32 {
            return Err(std::io::Error::other(format!(
                "ShellExecuteW failed ({})",
                r as isize
            )));
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let opener = if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        };
        let mut child = Command::new(opener)
            .arg(target)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        thread::spawn(move || {
            let _ = child.wait();
        });
        Ok(())
    }
}

/// Start a new instance of the app that outlives this one: its own session
/// (Unix) / detached and outside any job (Windows), and never registered for
/// exit cleanup. Fixes BUG #1.
pub fn spawn_detached_restart(exe: &Path, args: &[std::ffi::OsString]) -> std::io::Result<()> {
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(dir) = exe.parent() {
        cmd.current_dir(dir);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: setsid is async-signal-safe.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        thread::spawn(move || {
            let _ = child.wait();
        });
        Ok(())
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB);
        match cmd.spawn() {
            Ok(_) => Ok(()),
            // Breakaway is refused inside a job that forbids it; we are not in
            // a kill-on-close job of our own, so plain detaching is enough.
            Err(_) => {
                cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
                cmd.spawn().map(|_| ())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn kill_tree_kills_grandchildren() {
        let mut cmd = command("sh");
        cmd.args(["-c", "sleep 30 & echo $!; wait"])
            .stdout(Stdio::piped());
        let mut sup = spawn(cmd).unwrap();
        let mut out = BufReader::new(sup.child.stdout.take().unwrap());
        let mut pid = String::new();
        out.read_line(&mut pid).unwrap();
        let grandchild: i32 = pid.trim().parse().unwrap();
        sup.kill_tree(Duration::from_millis(500));
        thread::sleep(Duration::from_millis(100));
        // SAFETY: signal 0 only probes for existence.
        let alive = unsafe { libc::kill(grandchild, 0) } == 0;
        assert!(!alive, "grandchild survived the tree kill");
    }

    #[test]
    fn run_capture_times_out() {
        #[cfg(unix)]
        {
            let r = run_capture(Path::new("sleep"), &["5"], Duration::from_millis(200));
            assert!(r.is_none());
            let r = run_capture(Path::new("echo"), &["hi"], Duration::from_secs(5)).unwrap();
            assert_eq!(r, (true, "hi\n".to_owned()));
        }
    }

    #[test]
    fn line_reader_splits_on_cr_and_lf() {
        let mut r = BufReader::new(&b"a\rb\nc"[..]);
        let mut buf = Vec::new();
        let mut lines = Vec::new();
        while read_until_eol(&mut r, &mut buf).unwrap() > 0 {
            lines.push(String::from_utf8(std::mem::take(&mut buf)).unwrap());
        }
        assert_eq!(lines, ["a\r", "b\n", "c"]);
    }
}
