// Contract between the UI and the Rust backend. Every type here mirrors a
// Rust type exactly (serde output); the Rust symbol is named in each comment.
// The UI renders what the backend reports and never derives task state.

// ---- option vocabularies (src-tauri/src/options.rs) ----------------------
// Values are the English keys the old app persisted; localise only the keys
// in LOCALIZED_OPTIONS via `option.<key lowercased>`.

export type Mode = "video" | "audio";

export const VIDEO_QUALITIES = ["Best", "4K (2160p)", "1440p", "1080p", "720p", "480p", "360p", "Worst"] as const;
export const AUDIO_QUALITIES = ["Best", "320 kbps", "256 kbps", "192 kbps", "128 kbps", "96 kbps"] as const;
/** options::Quality */
export type Quality = (typeof VIDEO_QUALITIES)[number] | (typeof AUDIO_QUALITIES)[number];

/** options::VideoCodec */
export const VIDEO_CODECS = ["Auto", "MP4 (H.264)", "WebM (VP9)", "WebM (AV1)"] as const;
export type VideoCodec = (typeof VIDEO_CODECS)[number];

/** options::AudioCodec */
export const AUDIO_CODECS = ["m4a (AAC)", "mp3", "opus", "vorbis (ogg)", "flac", "wav", "alac", "ac3", "Original"] as const;
export type AudioCodec = (typeof AUDIO_CODECS)[number];
/** AudioCodec::has_bitrate — Quality is disabled for the others (spec item 50). */
export const AUDIO_CODECS_WITH_BITRATE: readonly AudioCodec[] = ["m4a (AAC)", "mp3", "opus", "vorbis (ogg)", "ac3"];

/** options::Container ("None" displays as option.none = "None (keep original)") */
export const CONTAINERS = ["None", "MP4", "MKV", "WebM", "MOV", "AVI", "FLV", "MPEG", "WMV"] as const;
export type Container = (typeof CONTAINERS)[number];

/** options::Preset */
export const PRESETS = ["Fast", "Balanced", "Quality"] as const;
export type Preset = (typeof PRESETS)[number];

/** options::CookieChoice */
export const COOKIE_CHOICES = ["Auto", "Off", "Firefox", "Chrome", "Brave", "Chromium", "Edge", "Opera", "Safari", "Vivaldi"] as const;
export type CookieChoice = (typeof COOKIE_CHOICES)[number];

/** options::Browser */
export type Browser = "firefox" | "chrome" | "brave" | "chromium" | "edge" | "opera" | "safari" | "vivaldi";

/** options::NetworkType (never localised) */
export const NETWORK_TYPES = ["Residential", "Mixed", "Mobile"] as const;
export type NetworkType = (typeof NETWORK_TYPES)[number];

/** Option keys that get translated (spec item 56). */
export const LOCALIZED_OPTIONS = ["Best", "Worst", "Auto", "Original", "Off", "None", "Fast", "Balanced", "Quality"] as const;

/** Max-concurrent presets; 0 = unlimited (shown as "∞"). */
export const CONCURRENCY_PRESETS = [1, 2, 3, 4, 5, 10, 0] as const;
/** Parallel-parts presets. */
export const FRAGMENT_PRESETS = [1, 2, 4, 8, 16, 32, 64] as const;

/** options::parse_concurrency — editable box text → setting (0 = ∞). */
export function parseConcurrency(text: string): number {
  const t = text.trim();
  if (["∞", "inf", "infinity", "unlimited"].includes(t.toLowerCase())) return 0;
  if (!/^[+-]?\d+$/.test(t)) return 2;
  const n = Number(t);
  return n >= 9999 ? 0 : Math.max(1, n);
}

/** options::parse_fragments */
export function parseFragments(text: string): number {
  const t = text.trim();
  if (!/^[+-]?\d+$/.test(t)) return 4;
  return Math.min(Math.max(1, Number(t)), 4294967295);
}

// ---- settings (settings.rs: Settings) -------------------------------------

export interface Settings {
  /** "" only before autodetect; the backend fills it at start-up. */
  language: string;
  /** 0 = unlimited. */
  maxConcurrent: number;
  /** "" = ~/Downloads (Bootstrap.defaultOutputDir). */
  outputDir: string;
  mode: Mode;
  /** Shared by both modes; reset to "Best" if not in the new mode's list. */
  quality: Quality;
  videoCodec: VideoCodec;
  audioCodec: AudioCodec;
  container: Container;
  reencode: boolean;
  preset: Preset;
  playlist: boolean;
  /** App update check at start-up. */
  autoUpdateCheck: boolean;
  /** Daily yt-dlp update. */
  ytdlpAutoUpdate: boolean;
  concurrentFragments: number;
  cookiesBrowser: CookieChoice;
  cookieFile: string;
  anyipEnabled: boolean;
  anyipUsername: string;
  anyipNetworkType: NetworkType;
  anyipSessionName: string;
  /** Keep the queue across restarts (opt-in addition). */
  restoreQueue: boolean;
  useDownloadArchive: boolean;
  embedSubtitles: boolean;
  subtitleLangs: string;
  embedChapters: boolean;
  embedThumbnail: boolean;
  embedMetadata: boolean;
  sponsorblockRemove: boolean;
}

// ---- tasks (args.rs, task.rs, progress.rs) --------------------------------

/** args::Extras */
export interface Extras {
  downloadArchive: boolean;
  embedSubtitles: boolean;
  subtitleLangs: string;
  embedChapters: boolean;
  embedThumbnail: boolean;
  embedMetadata: boolean;
  sponsorblockRemove: boolean;
}

/** args::TaskSpec — immutable snapshot taken at Add. */
export interface TaskSpec {
  url: string;
  outDir: string;
  mode: Mode;
  quality: Quality;
  videoCodec: VideoCodec;
  audioCodec: AudioCodec;
  container: Container;
  reencode: boolean;
  preset: Preset;
  playlist: boolean;
  fragments: number;
  cookieChoice: CookieChoice;
  cookieFile: string;
  extras: Extras;
}

/** args::CookieSource */
export type CookieSource = { type: "none" } | { type: "browser"; browser: Browser } | { type: "file"; path: string };

/** progress::PostKind */
export type PostKind = "merge" | "extractAudio" | "convert" | "remux" | "encode" | "embed";

/** task::ErrorClass */
export type ErrorClass =
  | "botCheck"
  | "cookieDecrypt"
  | "noFormats"
  | "ffmpegMissing"
  | "network"
  | "unavailable"
  | "encoderFailed"
  | "ytdlpMissing"
  | "unknown";

/** task::ItemPos — playlist "item n of m". */
export interface ItemPos {
  index: number;
  count: number | null;
}

/** task::TaskState */
export type TaskState =
  | { state: "queued" }
  | { state: "starting" }
  | { state: "fetching"; item: ItemPos | null }
  | {
      state: "downloading";
      item: ItemPos | null;
      /** 0-based stream file within the item (video, then audio). */
      stream: number;
      percent: number;
      downloaded: number | null;
      total: number | null;
      /** bytes/s */
      speed: number | null;
      /** seconds */
      eta: number | null;
    }
  | {
      state: "postProcessing";
      kind: PostKind;
      item: ItemPos | null;
      /** null = indeterminate (yt-dlp's own postprocessors). */
      percent: number | null;
      /** ffmpeg speed multiplier, show as "2.50x". */
      speedX: number | null;
      eta: number | null;
    }
  | { state: "cancelling" }
  | { state: "done"; files: string[]; itemErrors: string[] }
  | { state: "failed"; errorClass: ErrorClass; message: string }
  | { state: "cancelled"; cleanedUp: boolean };

export type TaskStateName = TaskState["state"];

/** task::Attempt */
export interface Attempt {
  n: number;
  cookieSource: CookieSource;
  proxy: boolean;
  startedMs: number;
  endedMs: number | null;
  exitCode: number | null;
  errorClass: ErrorClass | null;
  stderrTail: string[];
}

/** task::TaskRescue */
export interface TaskRescue {
  cookieFileRetryAttempted: boolean;
  anyipRetryAttempted: boolean;
}

/** task::Task — sent whole on every change. */
export interface Task {
  id: number;
  spec: TaskSpec;
  /** null until yt-dlp reports it; show spec.url meanwhile. */
  title: string | null;
  state: TaskState;
  attempts: Attempt[];
  rescue: TaskRescue;
  createdMs: number;
}

export const ACTIVE_STATES: readonly TaskStateName[] = ["starting", "fetching", "downloading", "postProcessing", "cancelling"];
export const TERMINAL_STATES: readonly TaskStateName[] = ["done", "failed", "cancelled"];

// ---- log (log.rs) ---------------------------------------------------------

export type Level = "info" | "warning" | "error";

/** log::LogMessage — `key` lines are localised with the catalogs. */
export type LogMessage = { kind: "key"; key: string; args: Record<string, string> } | { kind: "text"; text: string };

/** log::LogEntry — render `[HH:MM:SS] ` + (`#<taskId>  ` if set) + message. */
export interface LogEntry {
  seq: number;
  tsMs: number;
  taskId: number | null;
  level: Level;
  message: LogMessage;
}

// ---- banners, tools, updates ----------------------------------------------

/** rescue::BannerKind */
export type BannerKind =
  | "ffmpegMissing"
  | "jsRuntimeMissing"
  | "chromiumCookies"
  | "anyipNotConfigured"
  | "botDetection"
  | "appUpdate"
  | "ytdlpMissing";

/** tools::Tool */
export type Tool = "ffmpeg" | "deno";

/** tools::InstallPlan */
export type InstallPlan = { type: "auto"; display: string } | { type: "manual"; hint: string };

/** backend::Banner */
export interface Banner {
  kind: BannerKind;
  install: InstallPlan | null;
  version: string | null;
  canApply: boolean;
}

/** backend::InstallState */
export type InstallState = { state: "running" } | { state: "done" } | { state: "failed"; error: string };

/** tools::ToolsStatus */
export interface ToolsStatus {
  ffmpeg: string | null;
  ffprobe: string | null;
  jsRuntimes: string[];
}

/** ytdlp::YtdlpSource */
export type YtdlpSource = "managed" | "system" | "missing";

/** ytdlp::YtdlpPhase */
export type YtdlpPhase =
  | { phase: "idle" }
  | { phase: "checking" }
  | { phase: "downloading"; percent: number | null }
  | { phase: "upToDate" }
  | { phase: "updated"; version: string }
  | { phase: "failed"; error: string };

/** ytdlp::YtdlpStatus (phase fields are flattened into the object) */
export type YtdlpStatus = {
  source: YtdlpSource;
  version: string | null;
  path: string | null;
  latest: string | null;
  lastCheckMs: number | null;
} & YtdlpPhase;

/** updater::UpdateState */
export type UpdateState =
  | { phase: "idle" }
  | { phase: "checking" }
  | { phase: "upToDate"; current: string }
  | { phase: "available"; current: string; latest: string; canApply: boolean }
  | { phase: "downloading"; percent: number | null }
  | { phase: "readyToRestart"; version: string }
  | { phase: "failed"; error: string };

// ---- preview (preview.rs) -------------------------------------------------

export interface PreviewEntry {
  id: string | null;
  title: string | null;
  duration: number | null;
}

export interface SizeAtHeight {
  height: number;
  bytes: number;
}

export interface Preview {
  url: string;
  isPlaylist: boolean;
  title: string | null;
  uploader: string | null;
  /** https only */
  thumbnail: string | null;
  duration: number | null;
  filesizeApprox: number | null;
  heights: number[];
  sizes: SizeAtHeight[];
  audioBytes: number | null;
  entryCount: number | null;
  entries: PreviewEntry[];
}

// ---- commands and events --------------------------------------------------

/** commands::Bootstrap */
export interface Bootstrap {
  settings: Settings;
  anyipPasswordSet: boolean;
  version: string;
  platform: "linux" | "windows" | "macos" | string;
  defaultOutputDir: string;
  tasks: Task[];
  banners: Banner[];
  log: LogEntry[];
  ytdlp: YtdlpStatus;
  appUpdate: UpdateState;
}

/** backend::AddError — show as a dialog. */
export type AddError = { kind: "noUrls" } | { kind: "badFolder"; error: string };

/** backend::AppEvent, delivered on the "app-event" channel. */
export type AppEvent =
  | { type: "taskUpdated"; task: Task }
  | { type: "tasksRemoved"; ids: number[] }
  | { type: "log"; entry: LogEntry }
  | { type: "bannerShown"; banner: Banner }
  | { type: "bannerDismissed"; kind: BannerKind }
  | { type: "settings"; settings: Settings; anyipPasswordSet: boolean }
  | { type: "ytdlp"; status: YtdlpStatus }
  | { type: "appUpdate"; state: UpdateState }
  | { type: "install"; tool: Tool; state: InstallState }
  | { type: "closeRequested"; activeTasks: number };
