//! Compatibility transcode around ffmpeg (spec 2.6): smart skip, remux,
//! copy-video/convert-audio, full encode with hardware encoders, CUDA decode
//! fallback, session encoder blacklist, 60 s hang watchdog and real progress.

use crate::options::Preset;
use crate::process::{self, Line, Pipe};
use crate::progress::{FfProgress, PostKind};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

pub const WATCHDOG: Duration = Duration::from_secs(60);
const NV_KEYS: [&str; 3] = [
    "__NV_PRIME_RENDER_OFFLOAD",
    "__GLX_VENDOR_LIBRARY_NAME",
    "__VK_LAYER_NV_optimus",
];
const NV_DEFAULTS: [&str; 3] = ["1", "nvidia", "NVIDIA_only"];

/// PRIME offload vars removed from our own environment at start-up
/// (spec 1.1 item 2), re-injected only into ffmpeg.
static NV_VARS: OnceLock<Vec<(String, String)>> = OnceLock::new();

/// Strip the PRIME offload vars before any thread or webview exists.
///
/// # Safety
/// Must run before any other thread is started.
pub unsafe fn capture_and_strip_nvidia_env() {
    let mut found = Vec::new();
    for k in NV_KEYS {
        if let Ok(v) = std::env::var(k) {
            found.push((k.to_owned(), v));
            // SAFETY: caller guarantees single-threaded start-up.
            unsafe { std::env::remove_var(k) };
        }
    }
    let _ = NV_VARS.set(found);
}

fn nv_var(k: &str) -> Option<&'static str> {
    NV_VARS
        .get()?
        .iter()
        .find(|(n, _)| n == k)
        .map(|(_, v)| v.as_str())
}

/// Linux: NVENC is plausible only with an NVIDIA driver/device visible.
pub fn has_nvidia_runtime() -> bool {
    if !cfg!(target_os = "linux") {
        return true;
    }
    nv_var("__NV_PRIME_RENDER_OFFLOAD") == Some("1")
        || nv_var("__GLX_VENDOR_LIBRARY_NAME") == Some("nvidia")
        || ["/dev/nvidiactl", "/dev/nvidia0", "/dev/dxg"]
            .iter()
            .any(|p| Path::new(p).exists())
}

fn ffmpeg_env() -> Vec<(String, String)> {
    if !cfg!(target_os = "linux") || !has_nvidia_runtime() {
        return vec![];
    }
    NV_KEYS
        .iter()
        .zip(NV_DEFAULTS)
        .map(|(k, d)| {
            (
                k.to_string(),
                nv_var(k).filter(|v| !v.is_empty()).unwrap_or(d).to_owned(),
            )
        })
        .collect()
}

/// Codec sets per target (downloader.py:286-295).
pub fn target_codecs(ext: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    Some(match ext {
        "mp4" | "mov" | "mkv" => (&["h264"], &["aac"]),
        "webm" => (&["vp9"], &["opus"]),
        "avi" => (&["mpeg4", "msmpeg4v3"], &["mp3"]),
        "flv" => (&["flv1", "h264"], &["mp3", "aac"]),
        "mpeg" => (&["mpeg2video"], &["mp2"]),
        "wmv" => (&["wmv1", "wmv2", "wmv3"], &["wmav2", "wmav1"]),
        _ => return None,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Decision {
    Skip,
    Remux,
    CopyVideoConvertAudio,
    Encode,
}

/// Pure decision (spec 2.6 steps 2-6). Audio None counts as matching.
pub fn decide(target: &str, src_ext: &str, v: Option<&str>, a: Option<&str>) -> Decision {
    let Some((vs, as_)) = target_codecs(target) else {
        return Decision::Encode;
    };
    let v_ok = v.is_some_and(|v| vs.contains(&v));
    let a_ok = a.is_none_or(|a| as_.contains(&a));
    match (v_ok, a_ok) {
        (true, true) if src_ext == target => Decision::Skip,
        (true, true) => Decision::Remux,
        (true, false) => Decision::CopyVideoConvertAudio,
        _ => Decision::Encode,
    }
}

const MAP: [&str; 7] = [
    "-map",
    "0:v:0",
    "-map",
    "0:a:0?",
    "-sn",
    "-dn",
    "-ignore_unknown",
];

fn s(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| x.to_string()).collect()
}

fn tail_flags(target: &str) -> Vec<String> {
    let mut v = vec![];
    if matches!(target, "mp4" | "mov" | "m4a") {
        v.extend(s(&["-movflags", "+faststart"]));
    }
    if target == "mpeg" {
        v.extend(s(&["-f", "mpeg"]));
    }
    v
}

pub fn remux_args(target: &str) -> Vec<String> {
    let mut v = s(&MAP);
    v.extend(s(&["-c", "copy"]));
    v.extend(tail_flags(target));
    v
}

/// Container-only change (re-encode off): stream copy, except that Opus or
/// Vorbis audio is never copied into MP4/MOV, which some players play silently
/// (the old app avoided that on purpose, downloader.py:74-78). Returns the
/// args and whether the audio is converted.
pub fn container_change_args(target: &str, a: Option<&str>) -> (Vec<String>, bool) {
    let mp4_audio_ok = |a: &str| matches!(a, "aac" | "mp3" | "alac" | "ac3" | "eac3");
    if matches!(target, "mp4" | "mov") && a.is_some_and(|a| !mp4_audio_ok(a)) {
        (copy_video_args(target), true)
    } else {
        (remux_args(target), false)
    }
}

fn audio_args(target: &str) -> &'static [&'static str] {
    match target {
        "webm" => &["-c:a", "libopus", "-b:a", "160k"],
        "avi" | "flv" => &["-c:a", "libmp3lame", "-b:a", "192k"],
        "mpeg" => &["-c:a", "mp2", "-b:a", "192k"],
        "wmv" => &["-c:a", "wmav2", "-b:a", "192k"],
        _ => &["-c:a", "aac", "-b:a", "192k"],
    }
}

pub fn copy_video_args(target: &str) -> Vec<String> {
    let mut v = s(&MAP);
    v.extend(s(&["-c:v", "copy"]));
    v.extend(s(audio_args(target)));
    v.extend(tail_flags(target));
    v
}

pub fn h264_encoder_args(enc: &str, p: Preset) -> Vec<String> {
    let i = p.idx();
    let q = ["24", "22", "20"][i];
    match enc {
        "h264_nvenc" => s(&[
            "-c:v",
            "h264_nvenc",
            "-preset",
            ["fast", "fast", "medium"][i],
            "-rc",
            "constqp",
            "-qp",
            q,
        ]),
        "h264_qsv" => s(&[
            "-c:v",
            "h264_qsv",
            "-preset",
            ["veryfast", "medium", "slow"][i],
            "-global_quality",
            q,
        ]),
        "h264_amf" => s(&[
            "-c:v",
            "h264_amf",
            "-quality",
            ["speed", "balanced", "quality"][i],
            "-rc",
            "cqp",
            "-qp_i",
            q,
            "-qp_p",
            q,
        ]),
        "h264_videotoolbox" => s(&["-c:v", "h264_videotoolbox", "-q:v", ["75", "60", "45"][i]]),
        _ => s(&[
            "-c:v",
            "libx264",
            "-preset",
            ["ultrafast", "medium", "slow"][i],
            "-crf",
            ["23", "21", "19"][i],
        ]),
    }
}

/// Full re-encode args (downloader.py `_build_transcode_args`).
pub fn encode_args(target: &str, enc: &str, p: Preset, cuda_input: bool) -> Vec<String> {
    let i = p.idx();
    let mut v = s(&MAP);
    match target {
        "webm" => v.extend(s(&[
            "-c:v",
            "libvpx-vp9",
            "-crf",
            ["35", "32", "28"][i],
            "-b:v",
            "0",
            "-cpu-used",
            ["6", "4", "1"][i],
            "-c:a",
            "libopus",
            "-b:a",
            "160k",
        ])),
        "avi" => v.extend(s(&[
            "-c:v",
            "libxvid",
            "-q:v",
            ["6", "4", "2"][i],
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
        ])),
        "flv" => v.extend(s(&[
            "-c:v",
            "flv",
            "-q:v",
            ["7", "5", "3"][i],
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
        ])),
        "mpeg" => v.extend(s(&[
            "-c:v",
            "mpeg2video",
            "-q:v",
            ["5", "3", "2"][i],
            "-c:a",
            "mp2",
            "-b:a",
            "192k",
            "-f",
            "mpeg",
        ])),
        "wmv" => v.extend(s(&[
            "-c:v",
            "wmv2",
            "-q:v",
            ["6", "4", "2"][i],
            "-c:a",
            "wmav2",
            "-b:a",
            "192k",
        ])),
        _ => {
            v.extend(h264_encoder_args(enc, p));
            if !cuda_input {
                v.extend(s(&["-pix_fmt", "yuv420p"]));
            }
            v.extend(s(&["-c:a", "aac", "-b:a", "192k"]));
            if matches!(target, "mp4" | "mov") {
                v.extend(s(&["-movflags", "+faststart"]));
            }
        }
    }
    v
}

pub fn is_h264_target(t: &str) -> bool {
    matches!(t, "mp4" | "mov" | "mkv")
}

pub fn encoder_label(target: &str, h264: &str) -> String {
    match target {
        "webm" => "libvpx-vp9",
        "avi" => "libxvid",
        "flv" => "flv",
        "mpeg" => "mpeg2video",
        "wmv" => "wmv2",
        _ => h264,
    }
    .to_owned()
}

/// OS priority (spec 2.6 step 6), filtered by availability and blacklist.
pub fn encoder_order(
    available: &BTreeSet<String>,
    blacklist: &BTreeSet<String>,
    nvidia: bool,
) -> Vec<String> {
    let priority: &[&str] = if cfg!(windows) {
        &["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]
    } else if cfg!(target_os = "macos") {
        &["h264_videotoolbox", "libx264"]
    } else {
        &["h264_nvenc", "libx264"]
    };
    let avail: BTreeSet<String> = if available.is_empty() {
        ["libx264".to_owned()].into()
    } else {
        available.clone()
    };
    let out: Vec<String> = priority
        .iter()
        .filter(|e| avail.contains(**e) && !blacklist.contains(**e))
        .filter(|e| **e != "h264_nvenc" || nvidia)
        .map(|e| e.to_string())
        .collect();
    if out.is_empty() {
        vec!["libx264".into()]
    } else {
        out
    }
}

pub fn cuda_decoder_for(v: Option<&str>) -> Option<&'static str> {
    match v? {
        "vp9" => Some("vp9_cuvid"),
        "av1" => Some("av1_cuvid"),
        "h264" => Some("h264_cuvid"),
        "hevc" => Some("hevc_cuvid"),
        _ => None,
    }
}

/// Session state (spec 6.3): encoder blacklist + cached probes.
#[derive(Default)]
pub struct EncoderSession {
    pub blacklist: BTreeSet<String>,
    encoders: Option<BTreeSet<String>>,
    cuda: Option<BTreeSet<String>>,
}

fn probe_names(ffmpeg: &Path, flag: &str, names: &[&str]) -> BTreeSet<String> {
    let out = process::run_capture(
        ffmpeg,
        &[flag, "-hide_banner", "-loglevel", "error"],
        Duration::from_secs(8),
    )
    .map(|(_, o)| o)
    .unwrap_or_default();
    names
        .iter()
        .filter(|n| out.contains(**n))
        .map(|n| n.to_string())
        .collect()
}

impl EncoderSession {
    fn encoders(&mut self, ffmpeg: &Path) -> BTreeSet<String> {
        self.encoders
            .get_or_insert_with(|| {
                probe_names(
                    ffmpeg,
                    "-encoders",
                    &[
                        "h264_nvenc",
                        "h264_qsv",
                        "h264_amf",
                        "h264_videotoolbox",
                        "libx264",
                    ],
                )
            })
            .clone()
    }
    fn cuda(&mut self, ffmpeg: &Path) -> BTreeSet<String> {
        self.cuda
            .get_or_insert_with(|| {
                if has_nvidia_runtime() {
                    probe_names(
                        ffmpeg,
                        "-decoders",
                        &["vp9_cuvid", "av1_cuvid", "h264_cuvid", "hevc_cuvid"],
                    )
                } else {
                    BTreeSet::new()
                }
            })
            .clone()
    }
}

/// Callbacks into the owning task.
pub trait Sink {
    fn log(&self, text: &str);
    fn warn(&self, text: &str);
    fn phase(&self, kind: PostKind);
    fn progress(&self, percent: Option<f64>, speed_x: Option<f64>, eta: Option<f64>);
}

#[derive(Debug, PartialEq)]
pub enum FfError {
    Cancelled,
    /// Hang watchdog fired.
    Timeout,
    Failed {
        code: Option<i32>,
        stderr: Vec<String>,
    },
    Spawn(String),
}

impl std::fmt::Display for FfError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            FfError::Cancelled => write!(f, "cancelled"),
            FfError::Timeout => write!(
                f,
                "TimeoutError: ffmpeg produced no output for {} s",
                WATCHDOG.as_secs()
            ),
            FfError::Failed { code, stderr } => {
                write!(
                    f,
                    "ffmpeg exited with code {}",
                    code.map_or("?".into(), |c| c.to_string())
                )?;
                if let Some(l) = stderr.last() {
                    write!(f, ": {l}")?;
                }
                Ok(())
            }
            FfError::Spawn(e) => write!(f, "cannot run ffmpeg: {e}"),
        }
    }
}

pub struct Ctx<'a> {
    pub ffmpeg: PathBuf,
    pub ffprobe: Option<PathBuf>,
    pub cancel: &'a AtomicBool,
    pub sink: &'a dyn Sink,
    pub session: &'a Mutex<EncoderSession>,
}

/// One ffmpeg run with progress, cancel polling and the hang watchdog.
pub fn run_ffmpeg(
    cx: &Ctx,
    input_args: &[String],
    input: &Path,
    args: &[String],
    out: &Path,
    duration: Option<f64>,
) -> Result<(), FfError> {
    let mut cmd = process::command(&cx.ffmpeg);
    cmd.args(["-hide_banner", "-nostats", "-loglevel", "error", "-y"])
        .args(input_args)
        .arg("-i")
        .arg(input)
        .args(args)
        .args(["-progress", "pipe:1"])
        .arg(out)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in ffmpeg_env() {
        cmd.env(k, v);
    }
    let mut sup = process::spawn(cmd).map_err(|e| FfError::Spawn(e.to_string()))?;
    let rx = process::read_lines(&mut sup.child);
    let mut prog = FfProgress::default();
    let mut last_pct = -1.0_f64;
    let mut stderr: Vec<String> = Vec::new();
    let mut last_output = Instant::now();
    let mut eofs = 0;
    while eofs < 2 {
        if cx.cancel.load(Ordering::SeqCst) {
            sup.kill_tree(Duration::from_secs(2));
            return Err(FfError::Cancelled);
        }
        if last_output.elapsed() > WATCHDOG {
            sup.kill_tree(Duration::from_secs(2));
            return Err(FfError::Timeout);
        }
        match rx.recv_timeout(Duration::from_millis(250)) {
            Ok(Line::Text(Pipe::Out, l)) => {
                last_output = Instant::now();
                if prog.feed(&l) {
                    let pct = prog.percent(duration);
                    if pct.is_some_and(|p| (p - last_pct).abs() >= 0.5) || prog.end {
                        last_pct = pct.unwrap_or(last_pct);
                        cx.sink.progress(pct, prog.speed_x, prog.eta(duration));
                    }
                }
            }
            Ok(Line::Text(Pipe::Err, l)) => {
                last_output = Instant::now();
                stderr.push(crate::log::redact(&l));
                if stderr.len() > 20 {
                    stderr.remove(0);
                }
            }
            Ok(Line::Eof(_)) => eofs += 1,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(_) => break,
        }
    }
    let status = sup.wait().map_err(|e| FfError::Spawn(e.to_string()))?;
    if cx.cancel.load(Ordering::SeqCst) {
        return Err(FfError::Cancelled);
    }
    if status.success() {
        Ok(())
    } else {
        Err(FfError::Failed {
            code: status.code(),
            stderr,
        })
    }
}

pub fn probe_codecs(ffprobe: Option<&Path>, file: &Path) -> (Option<String>, Option<String>) {
    let Some(fp) = ffprobe else {
        return (None, None);
    };
    let f = file.to_string_lossy();
    let Some((true, out)) = process::run_capture(
        fp,
        &["-v", "error", "-show_streams", "-print_format", "json", &f],
        Duration::from_secs(10),
    ) else {
        return (None, None);
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&out) else {
        return (None, None);
    };
    let mut vc = None;
    let mut ac = None;
    for st in v
        .get("streams")
        .and_then(|s| s.as_array())
        .into_iter()
        .flatten()
    {
        let name = st
            .get("codec_name")
            .and_then(|n| n.as_str())
            .map(str::to_owned);
        match st.get("codec_type").and_then(|t| t.as_str()) {
            Some("video") if vc.is_none() => vc = name,
            Some("audio") if ac.is_none() => ac = name,
            _ => {}
        }
    }
    (vc, ac)
}

pub fn probe_duration(ffprobe: Option<&Path>, file: &Path) -> Option<f64> {
    let f = file.to_string_lossy();
    let (ok, out) = process::run_capture(
        ffprobe?,
        &[
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            &f,
        ],
        Duration::from_secs(10),
    )?;
    if !ok {
        return None;
    }
    out.trim()
        .parse::<f64>()
        .ok()
        .filter(|d| d.is_finite() && *d > 0.0)
}

/// `<stem>.<ext>`, or `<stem> (n).<ext>` when that already exists and is
/// not the source (never overwrite an unrelated file; fixes BUG #20).
pub fn free_output_path(src: &Path, target_ext: &str) -> PathBuf {
    let stem = src
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let dir = src.parent().unwrap_or(Path::new(""));
    let first = dir.join(format!("{stem}.{target_ext}"));
    if !first.exists() || first == src {
        return first;
    }
    (1..)
        .map(|n| dir.join(format!("{stem} ({n}).{target_ext}")))
        .find(|p| !p.exists())
        .expect("an unused name exists")
}

fn name(p: &Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn ext_of(p: &Path) -> String {
    p.extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default()
}

/// Run `args` into the final location: a temp name when the extension is
/// unchanged (then replace the source), else a free `<stem>.<target>` (then
/// delete the source). Partial output is removed on any failure.
fn produce(
    cx: &Ctx,
    src: &Path,
    target: &str,
    input_args: &[String],
    args: &[String],
    duration: Option<f64>,
) -> Result<PathBuf, FfError> {
    let same = ext_of(src) == target;
    let out = if same {
        let stem = src
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        src.with_file_name(format!("{stem}.reencoding.{target}"))
    } else {
        free_output_path(src, target)
    };
    match run_ffmpeg(cx, input_args, src, args, &out, duration) {
        Ok(()) => {
            let _ = std::fs::remove_file(src);
            if same {
                std::fs::rename(&out, src)
                    .map_err(|e| FfError::Spawn(format!("rename failed: {e}")))?;
                Ok(src.to_path_buf())
            } else {
                Ok(out)
            }
        }
        Err(e) => {
            let _ = std::fs::remove_file(&out);
            Err(e)
        }
    }
}

/// The whole compatibility pass for one file.
pub fn transcode(
    cx: &Ctx,
    src: &Path,
    target: &str,
    preset: Preset,
    remux_first: bool,
) -> Result<PathBuf, FfError> {
    let src_ext = ext_of(src);
    if remux_first && src_ext == target {
        cx.sink
            .log(&format!("Container already {target}: {}", name(src)));
        return Ok(src.to_path_buf());
    }
    cx.sink.log(&format!("Inspecting {}…", name(src)));
    let (v, a) = probe_codecs(cx.ffprobe.as_deref(), src);
    let codecs = match &a {
        Some(a) => format!("{}/{a}", v.as_deref().unwrap_or("unknown")),
        None => v.clone().unwrap_or_else(|| "unknown".into()),
    };
    cx.sink.log(&format!("Detected source codecs: {codecs}"));
    let duration = || probe_duration(cx.ffprobe.as_deref(), src);
    let result = match decide(target, &src_ext, v.as_deref(), a.as_deref()) {
        Decision::Skip => {
            cx.sink.log(&format!(
                "Re-encode skipped: {} is already {target}/{codecs}",
                name(src)
            ));
            return Ok(src.to_path_buf());
        }
        Decision::Remux => {
            cx.sink.phase(PostKind::Remux);
            cx.sink
                .log(&format!("Remuxing to {target} (codecs already compatible)"));
            produce(cx, src, target, &[], &remux_args(target), duration())
        }
        Decision::CopyVideoConvertAudio => {
            cx.sink.phase(PostKind::Remux);
            cx.sink.log(&format!(
                "Copying {} video; converting audio to fit {target}",
                v.as_deref().unwrap_or("unknown")
            ));
            produce(cx, src, target, &[], &copy_video_args(target), duration())
        }
        Decision::Encode => {
            if remux_first {
                // Container change only: try a stream copy first.
                cx.sink.phase(PostKind::Remux);
                let (args, converts) = container_change_args(target, a.as_deref());
                if converts {
                    cx.sink.log(&format!(
                        "Copying {} video; converting audio to fit {target}",
                        v.as_deref().unwrap_or("unknown")
                    ));
                } else {
                    cx.sink.log(&format!("Remuxing to {target}"));
                }
                match produce(cx, src, target, &[], &args, duration()) {
                    Ok(p) => {
                        cx.sink.log(&format!("Saved output: {}", name(&p)));
                        return Ok(p);
                    }
                    Err(FfError::Cancelled) => return Err(FfError::Cancelled),
                    Err(e) => cx.sink.warn(&format!(
                        "Remux to {target} failed ({e}); re-encoding instead"
                    )),
                }
            }
            encode(cx, src, target, preset, v.as_deref())
        }
    };
    if let Ok(p) = &result {
        cx.sink.log(&format!("Saved output: {}", name(p)));
    }
    result
}

fn encode(
    cx: &Ctx,
    src: &Path,
    target: &str,
    preset: Preset,
    v: Option<&str>,
) -> Result<PathBuf, FfError> {
    cx.sink.phase(PostKind::Encode);
    let duration = probe_duration(cx.ffprobe.as_deref(), src);
    let encoders = if is_h264_target(target) {
        let mut sess = cx.session.lock().unwrap();
        let avail = sess.encoders(&cx.ffmpeg);
        let order = encoder_order(&avail, &sess.blacklist, has_nvidia_runtime());
        cx.sink
            .log(&format!("Available H.264 encoders: {}", order.join(", ")));
        order
    } else {
        vec![encoder_label(target, "libx264")]
    };
    let mut last = FfError::Failed {
        code: None,
        stderr: vec![],
    };
    for enc in &encoders {
        let label = encoder_label(target, enc);
        let mut attempts: Vec<(Vec<String>, bool)> = Vec::new();
        if enc == "h264_nvenc" {
            let dec = cuda_decoder_for(v)
                .filter(|d| cx.session.lock().unwrap().cuda(&cx.ffmpeg).contains(*d));
            match dec {
                Some(d) => {
                    cx.sink.log(&format!("Using NVIDIA hardware decode: {d}"));
                    attempts.push((
                        s(&[
                            "-hwaccel",
                            "cuda",
                            "-hwaccel_output_format",
                            "cuda",
                            "-c:v",
                            d,
                        ]),
                        true,
                    ));
                }
                None => cx.sink.log(&format!(
                    "NVIDIA hardware decode unavailable for {}; using CPU decode + NVENC",
                    v.unwrap_or("unknown")
                )),
            }
        }
        attempts.push((vec![], false));
        for (i, (input_args, cuda)) in attempts.iter().enumerate() {
            if i > 0 {
                // downloader.py:1240: info line before the CPU-decode retry.
                cx.sink.log("Retrying NVENC with CPU decode");
            }
            cx.sink.log(&format!(
                "Re-encoding to {target} with {label} ({})",
                preset.lower()
            ));
            cx.sink.progress(Some(0.0), None, None);
            match produce(
                cx,
                src,
                target,
                input_args,
                &encode_args(target, enc, preset, *cuda),
                duration,
            ) {
                Ok(p) => return Ok(p),
                Err(FfError::Cancelled) => return Err(FfError::Cancelled),
                Err(e) => {
                    if let FfError::Failed { stderr, .. } = &e {
                        for l in stderr.iter().take(3) {
                            cx.sink.warn(&format!(
                                "{label} ffmpeg: {}",
                                l.chars().take(300).collect::<String>()
                            ));
                        }
                    }
                    if *cuda && i + 1 < attempts.len() {
                        cx.sink
                            .warn("NVIDIA hardware decode failed; retrying NVENC with CPU decode");
                    }
                    last = e;
                }
            }
        }
        if enc != "libx264" && is_h264_target(target) {
            cx.session.lock().unwrap().blacklist.insert(enc.clone());
        }
        cx.sink
            .warn(&format!("{label} failed; trying next encoder"));
    }
    Err(last)
}

/// AC-3 conversion for audio mode (yt-dlp cannot produce it; BUG #2).
pub fn to_ac3(cx: &Ctx, src: &Path, bitrate_k: u32) -> Result<PathBuf, FfError> {
    cx.sink.phase(PostKind::Convert);
    cx.sink
        .log(&format!("Converting {} to ac3 ({bitrate_k}k)", name(src)));
    let duration = probe_duration(cx.ffprobe.as_deref(), src);
    let args = s(&["-vn", "-c:a", "ac3", "-b:a", &format!("{bitrate_k}k")]);
    let out = produce(cx, src, "ac3", &[], &args, duration)?;
    cx.sink.log(&format!("Saved output: {}", name(&out)));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decisions() {
        assert_eq!(
            decide("mp4", "mp4", Some("h264"), Some("aac")),
            Decision::Skip
        );
        assert_eq!(
            decide("mp4", "mkv", Some("h264"), Some("aac")),
            Decision::Remux
        );
        assert_eq!(
            decide("mp4", "mp4", Some("h264"), None),
            Decision::Skip,
            "video-only still matches"
        );
        assert_eq!(
            decide("mov", "webm", Some("h264"), Some("opus")),
            Decision::CopyVideoConvertAudio
        );
        assert_eq!(
            decide("mp4", "webm", Some("vp9"), Some("opus")),
            Decision::Encode
        );
        assert_eq!(
            decide("webm", "webm", Some("vp9"), Some("opus")),
            Decision::Skip
        );
        assert_eq!(
            decide("flv", "mp4", Some("h264"), Some("aac")),
            Decision::Remux
        );
        assert_eq!(decide("avi", "mp4", None, None), Decision::Encode);
    }

    #[test]
    fn container_change_never_puts_opus_in_mp4() {
        let (args, conv) = container_change_args("mp4", Some("opus"));
        assert!(conv);
        assert_eq!(args, copy_video_args("mp4"));
        assert!(container_change_args("mov", Some("vorbis")).1);
        assert_eq!(container_change_args("mp4", Some("aac")), (remux_args("mp4"), false));
        assert_eq!(container_change_args("mp4", None), (remux_args("mp4"), false));
        assert_eq!(container_change_args("mkv", Some("opus")), (remux_args("mkv"), false));
        assert_eq!(container_change_args("webm", Some("opus")), (remux_args("webm"), false));
    }

    #[test]
    fn args_match_old_tables() {
        assert_eq!(
            remux_args("mp4").join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c copy -movflags +faststart"
        );
        assert_eq!(
            remux_args("mpeg").join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c copy -f mpeg"
        );
        assert_eq!(
            copy_video_args("webm").join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c:v copy -c:a libopus -b:a 160k"
        );
        assert_eq!(
            encode_args("mp4", "h264_nvenc", Preset::Quality, true).join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c:v h264_nvenc -preset medium -rc constqp -qp 20 -c:a aac -b:a 192k -movflags +faststart"
        );
        assert_eq!(
            encode_args("mkv", "libx264", Preset::Fast, false).join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 192k"
        );
        assert_eq!(
            encode_args("mov", "h264_amf", Preset::Balanced, false).join(" "),
            "-map 0:v:0 -map 0:a:0? -sn -dn -ignore_unknown -c:v h264_amf -quality balanced -rc cqp -qp_i 22 -qp_p 22 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart"
        );
        assert_eq!(
            encode_args("mp4", "h264_qsv", Preset::Fast, false)[7..13].join(" "),
            "-c:v h264_qsv -preset veryfast -global_quality 24"
        );
        assert_eq!(
            encode_args("mp4", "h264_videotoolbox", Preset::Quality, false)[7..11].join(" "),
            "-c:v h264_videotoolbox -q:v 45"
        );
        assert_eq!(
            encode_args("webm", "x", Preset::Balanced, false)[7..].join(" "),
            "-c:v libvpx-vp9 -crf 32 -b:v 0 -cpu-used 4 -c:a libopus -b:a 160k"
        );
        assert_eq!(
            encode_args("avi", "x", Preset::Quality, false)[7..].join(" "),
            "-c:v libxvid -q:v 2 -c:a libmp3lame -b:a 192k"
        );
        assert_eq!(
            encode_args("flv", "x", Preset::Fast, false)[7..].join(" "),
            "-c:v flv -q:v 7 -c:a libmp3lame -b:a 192k"
        );
        assert_eq!(
            encode_args("mpeg", "x", Preset::Balanced, false)[7..].join(" "),
            "-c:v mpeg2video -q:v 3 -c:a mp2 -b:a 192k -f mpeg"
        );
        assert_eq!(
            encode_args("wmv", "x", Preset::Balanced, false)[7..].join(" "),
            "-c:v wmv2 -q:v 4 -c:a wmav2 -b:a 192k"
        );
    }

    #[test]
    fn encoder_order_filters() {
        let set = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<BTreeSet<_>>();
        let all = set(&[
            "h264_nvenc",
            "h264_qsv",
            "h264_amf",
            "h264_videotoolbox",
            "libx264",
        ]);
        let expect_full: Vec<&str> = if cfg!(windows) {
            vec!["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]
        } else if cfg!(target_os = "macos") {
            vec!["h264_videotoolbox", "libx264"]
        } else {
            vec!["h264_nvenc", "libx264"]
        };
        assert_eq!(encoder_order(&all, &BTreeSet::new(), true), expect_full);
        assert!(!encoder_order(&all, &BTreeSet::new(), false).contains(&"h264_nvenc".to_owned()));
        assert!(
            !encoder_order(&all, &set(&["h264_nvenc"]), true).contains(&"h264_nvenc".to_owned())
        );
        assert_eq!(
            encoder_order(&BTreeSet::new(), &BTreeSet::new(), true),
            ["libx264"]
        );
        assert_eq!(
            encoder_order(
                &all,
                &set(&[
                    "libx264",
                    "h264_nvenc",
                    "h264_qsv",
                    "h264_amf",
                    "h264_videotoolbox"
                ]),
                true
            ),
            ["libx264"]
        );
    }

    #[test]
    fn free_output_never_clobbers() {
        let dir = std::env::temp_dir().join(format!("ytdl-free-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("T [id].webm");
        std::fs::write(&src, b"x").unwrap();
        assert_eq!(free_output_path(&src, "mp4"), dir.join("T [id].mp4"));
        std::fs::write(dir.join("T [id].mp4"), b"old").unwrap();
        assert_eq!(free_output_path(&src, "mp4"), dir.join("T [id] (1).mp4"));
        assert_eq!(
            free_output_path(&src, "webm"),
            src,
            "same file is replaced via temp"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Real ffmpeg, if installed: encode a 1 s test pattern and walk every
    /// path of the pipeline (remux, copy-video, encode, ac3) with progress.
    #[test]
    fn pipeline_with_real_ffmpeg() {
        let (Some(ffmpeg), Some(ffprobe)) = (crate::tools::ffmpeg(), crate::tools::ffprobe())
        else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("ytdl-pipe-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("clip [x].mkv");
        let ok = std::process::Command::new(&ffmpeg)
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x120:rate=10:duration=1",
            ])
            .args([
                "-f",
                "lavfi",
                "-i",
                "sine=duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "libopus",
                "-shortest",
            ])
            .arg(&src)
            .status()
            .is_ok_and(|s| s.success());
        if !ok {
            return; // ffmpeg build without libx264/libopus
        }
        struct Rec(Mutex<Vec<String>>);
        impl Sink for Rec {
            fn log(&self, t: &str) {
                self.0.lock().unwrap().push(format!("L {t}"));
            }
            fn warn(&self, t: &str) {
                self.0.lock().unwrap().push(format!("W {t}"));
            }
            fn phase(&self, k: PostKind) {
                self.0.lock().unwrap().push(format!("P {k:?}"));
            }
            fn progress(&self, p: Option<f64>, _: Option<f64>, _: Option<f64>) {
                self.0
                    .lock()
                    .unwrap()
                    .push(format!("% {}", p.unwrap_or(-1.0)));
            }
        }
        let rec = Rec(Mutex::new(vec![]));
        let cancel = AtomicBool::new(false);
        let session = Mutex::new(EncoderSession::default());
        let cx = Ctx {
            ffmpeg,
            ffprobe: Some(ffprobe),
            cancel: &cancel,
            sink: &rec,
            session: &session,
        };
        // h264/opus mkv → mp4: copy video, convert audio.
        let out = transcode(&cx, &src, "mp4", Preset::Fast, false).unwrap();
        assert_eq!(out, dir.join("clip [x].mp4"));
        assert!(!src.exists(), "source removed");
        let log = rec.0.lock().unwrap().join("\n");
        assert!(
            log.contains("Copying h264 video; converting audio to fit mp4"),
            "{log}"
        );
        assert!(log.contains("% 100"), "progress reached 100: {log}");
        // mp4 → mp4 is skipped.
        assert_eq!(
            transcode(&cx, &out, "mp4", Preset::Fast, false).unwrap(),
            out
        );
        // mp4 → avi needs a full encode (if libxvid exists).
        if let Ok(avi) = transcode(&cx, &out, "avi", Preset::Fast, false) {
            assert_eq!(ext_of(&avi), "avi");
            let ac3 = to_ac3(&cx, &avi, 192).unwrap();
            assert_eq!(ext_of(&ac3), "ac3");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
