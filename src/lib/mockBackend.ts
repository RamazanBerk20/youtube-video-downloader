// In-browser fake of the whole backend, used automatically outside Tauri.
// It follows the same contract (types.ts) and the same rules as the Rust
// backend: queue order, live concurrency, cancel in every phase, rescue
// chain logs, banners, session log keys.
//
// Scripted behaviour is chosen by words in the URL:
//   (default)  fetch → video stream → audio stream → merge → done
//   "list"     a 3-item playlist ("item n of m")
//   "fail"     fails: Video unavailable
//   "bot"      bot-check failure, rescued with Firefox cookies, then done
//   "botx"     bot-check that is never rescued (bot-detection banner)
//   "noformat" "Requested format is not available" (JS runtime banner)
//   "slow"     10× slower, handy for cancel testing
// Re-encode / legacy containers add a remux/encode phase with ffmpeg-like
// progress; audio mode shows "extractAudio".
//
// Test hooks: `window.__ytdlMock` (also exported as `mockControls`) can show
// banners, emit closeRequested, reset state and change the speed.

import type {
  AddError,
  AppEvent,
  Banner,
  BannerKind,
  Bootstrap,
  ItemPos,
  LogEntry,
  LogMessage,
  Preview,
  Settings,
  Task,
  TaskState,
  ToolsStatus,
  UpdateState,
  YtdlpStatus,
  Level
} from "./types";
import { ACTIVE_STATES, TERMINAL_STATES } from "./types";
import type { WindowAction } from "./api";

export const DEFAULT_SETTINGS: Settings = {
  language: "en",
  maxConcurrent: 2,
  outputDir: "",
  mode: "video",
  quality: "Best",
  videoCodec: "Auto",
  audioCodec: "m4a (AAC)",
  container: "None",
  reencode: false,
  preset: "Balanced",
  playlist: false,
  autoUpdateCheck: true,
  ytdlpAutoUpdate: true,
  concurrentFragments: 4,
  cookiesBrowser: "Auto",
  cookieFile: "",
  anyipEnabled: false,
  anyipUsername: "",
  anyipNetworkType: "Residential",
  anyipSessionName: "youtube_downloader",
  restoreQueue: false,
  useDownloadArchive: false,
  embedSubtitles: false,
  subtitleLangs: "en.*",
  embedChapters: false,
  embedThumbnail: false,
  embedMetadata: false,
  sponsorblockRemove: false
};

type Handler = (e: AppEvent) => void;

export const SPONSOR_URL = "https://github.com/sponsors/RamazanBerk20";

class MockBackend {
  settings: Settings = { ...DEFAULT_SETTINGS };
  passwordSet = false;
  tasks: Task[] = [];
  nextId = 0;
  log: LogEntry[] = [];
  opened: string[] = [];
  seq = 0;
  banners: Banner[] = [];
  shownOnce = new Set<BannerKind>();
  handlers = new Set<Handler>();
  timers = new Map<number, ReturnType<typeof setTimeout>>();
  autoBrowserTried = false;
  /** Multiplies every scripted delay; tests set it to 0 for instant runs. */
  speed = 1;
  appUpdate: UpdateState = { phase: "idle" };
  ytdlp: YtdlpStatus = {
    source: "managed",
    version: "2026.10.26",
    path: "/home/user/.local/share/youtube-downloader/bin/yt-dlp",
    latest: "2026.10.26",
    lastCheckMs: Date.now(),
    phase: "idle"
  };

  emit(e: AppEvent) {
    // Deliver asynchronously like the real IPC; clone so the UI cannot mutate state.
    const copy = structuredClone(e);
    queueMicrotask(() => this.handlers.forEach((h) => h(copy)));
  }

  pushLog(taskId: number | null, level: Level, message: LogMessage) {
    const entry: LogEntry = { seq: this.seq++, tsMs: Date.now(), taskId, level, message };
    this.log.push(entry);
    if (this.log.length > 5000) this.log.shift();
    this.emit({ type: "log", entry });
  }
  key(taskId: number | null, level: Level, key: string, args: Record<string, string> = {}) {
    this.pushLog(taskId, level, { kind: "key", key, args });
  }
  text(taskId: number | null, level: Level, text: string) {
    this.pushLog(taskId, level, { kind: "text", text });
  }

  showBanner(b: Banner) {
    const i = this.banners.findIndex((x) => x.kind === b.kind);
    if (i >= 0) this.banners[i] = b;
    else this.banners.push(b);
    this.emit({ type: "bannerShown", banner: b });
  }
  once(kind: BannerKind, extra: Partial<Banner> = {}) {
    if (this.shownOnce.has(kind)) return;
    this.shownOnce.add(kind);
    this.showBanner({ kind, install: null, version: null, canApply: false, ...extra });
  }

  task(id: number) {
    return this.tasks.find((t) => t.id === id);
  }
  update(t: Task, state: TaskState) {
    t.state = state;
    this.emit({ type: "taskUpdated", task: t });
  }
  title(t: Task) {
    return t.title ?? t.spec.url;
  }

  bootstrap(): Bootstrap {
    return structuredClone({
      settings: this.settings,
      anyipPasswordSet: this.passwordSet,
      version: "2.0.0",
      platform: "linux",
      defaultOutputDir: "/home/user/Downloads",
      tasks: this.tasks,
      banners: this.banners,
      log: this.log,
      ytdlp: this.ytdlp,
      appUpdate: this.appUpdate
    });
  }

  updateSettings(patch: Partial<Settings>): Settings {
    for (const k of Object.keys(patch)) {
      if (!(k in this.settings)) throw `unknown setting: ${k}`;
    }
    const before = this.settings.autoUpdateCheck;
    this.settings = { ...this.settings, ...patch };
    if (this.settings.autoUpdateCheck !== before) {
      this.key(null, "info", "log.auto_update_toggled", {
        state: this.settings.autoUpdateCheck ? "auto_update.on" : "auto_update.off"
      });
    }
    this.schedule();
    return { ...this.settings };
  }

  addTasks(text: string): number[] {
    const urls = text
      .split(/\r?\n/)
      // eslint-disable-next-line no-control-regex
      .map((l) => l.replace(/[\u0000-\u001f\u007f]/g, "").trim())
      .filter((l) => /^https?:\/\//i.test(l));
    if (urls.length === 0) throw { kind: "noUrls" } satisfies AddError;
    if (this.settings.outputDir.includes("\0") || this.settings.outputDir.startsWith("/proc")) {
      throw { kind: "badFolder", error: "Permission denied (os error 13)" } satisfies AddError;
    }
    const s = this.settings;
    const audio = s.mode === "audio";
    const ids: number[] = [];
    for (const url of urls) {
      const t: Task = {
        id: ++this.nextId,
        spec: {
          url,
          outDir: s.outputDir || "/home/user/Downloads",
          mode: s.mode,
          quality: s.quality,
          videoCodec: s.videoCodec,
          audioCodec: s.audioCodec,
          container: audio ? "None" : s.container,
          reencode: !audio && s.reencode,
          preset: s.preset,
          playlist: s.playlist,
          fragments: s.concurrentFragments,
          cookieChoice: s.cookiesBrowser,
          cookieFile: s.cookieFile,
          extras: {
            downloadArchive: s.useDownloadArchive,
            embedSubtitles: s.embedSubtitles,
            subtitleLangs: s.subtitleLangs,
            embedChapters: s.embedChapters,
            embedThumbnail: s.embedThumbnail,
            embedMetadata: s.embedMetadata,
            sponsorblockRemove: s.sponsorblockRemove
          }
        },
        title: null,
        state: { state: "queued" },
        attempts: [],
        rescue: { cookieFileRetryAttempted: false, anyipRetryAttempted: false },
        createdMs: Date.now()
      };
      this.tasks.push(t);
      ids.push(t.id);
      this.emit({ type: "taskUpdated", task: t });
      this.key(t.id, "info", "log.task_added", { url });
    }
    this.schedule();
    return ids;
  }

  schedule() {
    const limit = this.settings.maxConcurrent === 0 ? Infinity : this.settings.maxConcurrent;
    let active = this.tasks.filter((t) => ACTIVE_STATES.includes(t.state.state)).length;
    for (const t of this.tasks) {
      if (active >= limit) break;
      if (t.state.state !== "queued") continue;
      active++;
      const auto = this.autoBrowserTried && t.spec.cookieChoice === "Auto";
      t.attempts.push({
        n: t.attempts.length + 1,
        cookieSource: auto ? { type: "browser", browser: "firefox" } : { type: "none" },
        proxy: false,
        startedMs: Date.now(),
        endedMs: null,
        exitCode: null,
        errorClass: null,
        stderrTail: []
      });
      this.update(t, { state: "starting" });
      this.key(t.id, "info", "log.task_started", { title: this.title(t) });
      void this.run(t);
    }
  }

  wait(t: Task, ms: number): Promise<boolean> {
    const slow = t.spec.url.includes("slow") ? 10 : 1;
    return new Promise((resolve) => {
      const h = setTimeout(() => {
        this.timers.delete(t.id);
        resolve(t.state.state !== "cancelling");
      }, ms * slow * this.speed);
      this.timers.set(t.id, h);
    });
  }

  async run(t: Task) {
    const url = t.spec.url;
    const attempt = t.attempts[t.attempts.length - 1];
    if (!(await this.wait(t, 200))) return this.killed(t);
    this.update(t, { state: "fetching", item: null });
    if (!(await this.wait(t, 500))) return this.killed(t);

    const rescued = attempt.cookieSource.type === "browser";
    if ((url.includes("bot") && !rescued) || url.includes("botx")) {
      return this.fail(t, "botCheck", "[youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.");
    }
    if (url.includes("fail")) return this.fail(t, "unavailable", "[youtube] abc: Video unavailable. This video is private");
    if (url.includes("noformat")) return this.fail(t, "noFormats", "[youtube] abc: Requested format is not available. Use --list-formats for a list of available formats");

    const items = t.spec.playlist || url.includes("list") ? 3 : 1;
    const files: string[] = [];
    for (let i = 1; i <= items; i++) {
      const item: ItemPos | null = items > 1 ? { index: i, count: items } : null;
      const name = items > 1 ? `Mock video ${i}` : "Mock video";
      t.title = name;
      this.update(t, { state: "fetching", item });
      const streams = t.spec.mode === "audio" ? 1 : 2;
      for (let stream = 0; stream < streams; stream++) {
        const total = stream === 0 ? 48_000_000 : 3_200_000;
        for (let pct = 0; pct <= 100; pct += 10) {
          if (!(await this.wait(t, 120))) return this.killed(t);
          this.update(t, {
            state: "downloading",
            item,
            stream,
            percent: pct,
            downloaded: Math.round((total * pct) / 100),
            total,
            speed: 4_500_000,
            eta: ((100 - pct) / 100) * 10
          });
        }
        this.text(t.id, "info", `${name} [abc${i}].f${stream === 0 ? 137 : 140}.${stream === 0 ? "mp4" : "m4a"}`);
      }
      const pp = t.spec.mode === "audio" ? "extractAudio" : "merge";
      this.update(t, { state: "postProcessing", kind: pp, item, percent: null, speedX: null, eta: null });
      if (!(await this.wait(t, 400))) return this.killed(t);
      const encode = t.spec.reencode || ["AVI", "FLV", "MPEG", "WMV"].includes(t.spec.container);
      if (encode || t.spec.container !== "None") {
        const kind = encode ? "encode" : "remux";
        this.text(t.id, "info", `Inspecting ${name} [abc${i}].mp4…`);
        this.text(t.id, "info", "Detected source codecs: vp9/opus");
        if (encode) this.text(t.id, "info", "Re-encoding to mp4 with libx264 (balanced)");
        for (let pct = 0; pct <= 100; pct += encode ? 5 : 50) {
          if (!(await this.wait(t, encode ? 150 : 100))) return this.killed(t);
          this.update(t, { state: "postProcessing", kind, item, percent: pct, speedX: 2.5, eta: ((100 - pct) / 100) * 8 });
        }
      }
      files.push(`${t.spec.outDir}/${name} [abc${i}].${t.spec.mode === "audio" ? "m4a" : "mp4"}`);
    }
    attempt.endedMs = Date.now();
    attempt.exitCode = 0;
    this.update(t, { state: "done", files, itemErrors: [] });
    this.key(t.id, "info", "log.task_done", { title: this.title(t) });
    this.schedule();
  }

  killed(t: Task) {
    const a = t.attempts[t.attempts.length - 1];
    if (a) a.endedMs = Date.now();
    this.update(t, { state: "cancelled", cleanedUp: true });
    this.key(t.id, "info", "log.task_cancelled", { title: this.title(t) });
    this.schedule();
  }

  fail(t: Task, errorClass: Extract<TaskState, { state: "failed" }>["errorClass"], message: string) {
    const a = t.attempts[t.attempts.length - 1];
    a.endedMs = Date.now();
    a.exitCode = 1;
    a.errorClass = errorClass;
    this.text(t.id, "error", `error: ${message}`);
    this.update(t, { state: "failed", errorClass, message });
    this.key(t.id, "error", "log.task_failed", { title: this.title(t), error: message.split("\n")[0] });
    if (errorClass === "botCheck") {
      if (t.spec.cookieChoice === "Auto" && !this.autoBrowserTried && !t.spec.url.includes("botx")) {
        this.autoBrowserTried = true;
        this.key(null, "warning", "log.auto_cookies_switched", { browser: "Firefox" });
        this.update(t, { state: "queued" });
      } else {
        if (t.spec.cookieChoice === "Auto") this.key(null, "warning", "log.auto_cookies_no_browser");
        if (t.spec.cookieChoice === "Auto" || t.spec.cookieChoice === "Off") this.once("botDetection");
      }
    } else if (errorClass === "noFormats") {
      this.once("jsRuntimeMissing", { install: { type: "auto", display: "pkexec pacman -S --noconfirm deno" } });
    }
    this.schedule();
  }

  cancel(id: number) {
    const t = this.task(id);
    if (!t || TERMINAL_STATES.includes(t.state.state) || t.state.state === "cancelling") return;
    if (t.state.state === "queued") {
      this.update(t, { state: "cancelled", cleanedUp: true });
      this.key(id, "info", "log.task_cancelled", { title: this.title(t) });
      return;
    }
    this.update(t, { state: "cancelling" });
    // The scripted run notices at its next step, like the supervisor's poll.
    const h = this.timers.get(id);
    if (h) {
      clearTimeout(h);
      this.timers.delete(id);
      setTimeout(() => this.killed(t), 150 * this.speed);
    }
  }

  remove(id: number): boolean {
    const t = this.task(id);
    if (!t || !TERMINAL_STATES.includes(t.state.state)) return false;
    this.tasks = this.tasks.filter((x) => x.id !== id);
    this.emit({ type: "tasksRemoved", ids: [id] });
    return true;
  }

  retry(id: number): boolean {
    const t = this.task(id);
    if (!t || !(t.state.state === "failed" || t.state.state === "cancelled")) return false;
    this.update(t, { state: "queued" });
    this.schedule();
    return true;
  }

  clearFinished(): number[] {
    const ids = this.tasks.filter((t) => TERMINAL_STATES.includes(t.state.state)).map((t) => t.id);
    this.tasks = this.tasks.filter((t) => !ids.includes(t.id));
    if (ids.length) this.emit({ type: "tasksRemoved", ids });
    return ids;
  }

  preview(url: string): Preview {
    if (!/^https?:\/\//i.test(url.trim())) throw "not an http(s) URL";
    if (url.includes("fail")) throw "[youtube] abc: Video unavailable";
    const list = url.includes("list");
    return {
      url,
      isPlaylist: list,
      title: list ? "Mock playlist" : "Mock video",
      uploader: "Mock channel",
      thumbnail: null,
      duration: list ? null : 213,
      filesizeApprox: list ? null : 52_000_000,
      heights: list ? [] : [2160, 1440, 1080, 720, 480, 360, 240, 144],
      sizes: list ? [] : [{ height: 1080, bytes: 51_200_000 }, { height: 720, bytes: 28_000_000 }],
      audioBytes: list ? null : 3_200_000,
      entryCount: list ? 3 : null,
      entries: list ? [1, 2, 3].map((i) => ({ id: `abc${i}`, title: `Mock video ${i}`, duration: 60 * i })) : []
    };
  }

  async install(tool: "ffmpeg" | "deno") {
    const kind: BannerKind = tool === "ffmpeg" ? "ffmpegMissing" : "jsRuntimeMissing";
    this.banners = this.banners.filter((b) => b.kind !== kind);
    this.emit({ type: "bannerDismissed", kind });
    this.key(null, "info", "log.installing", { package: tool });
    this.key(null, "info", "log.install_running", { cmd: `pkexec pacman -S --noconfirm ${tool}` });
    this.emit({ type: "install", tool, state: { state: "running" } });
    await new Promise((r) => setTimeout(r, 600 * this.speed));
    this.text(null, "info", `installing ${tool}...`);
    this.key(null, "info", "log.install_done", { package: tool });
    this.emit({ type: "install", tool, state: { state: "done" } });
  }

  setUpdate(s: UpdateState) {
    this.appUpdate = s;
    this.emit({ type: "appUpdate", state: s });
  }
}

let backend = new MockBackend();

/** Test/agent hooks for the mock. */
export const mockControls = {
  get backend() {
    return backend;
  },
  reset() {
    backend.timers.forEach((h) => clearTimeout(h));
    const handlers = backend.handlers;
    backend = new MockBackend();
    backend.handlers = handlers;
    backend.key(null, "info", "log.ready");
    Object.assign(mockControls.window, { maximized: false, minimized: false, closed: false });
  },
  setSpeed(multiplier: number) {
    backend.speed = multiplier;
  },
  showBanner(kind: BannerKind, extra: Partial<Banner> = {}) {
    backend.showBanner({ kind, install: null, version: null, canApply: false, ...extra });
  },
  requestClose() {
    const active = backend.tasks.filter((t) => ACTIVE_STATES.includes(t.state.state)).length;
    backend.emit({ type: "closeRequested", activeTasks: active });
  },
  /** The fake window behind the custom title bar. */
  window: { maximized: false, minimized: false, closed: false, resized: new Set<() => void>() },
  offerAppUpdate(canApply = true) {
    backend.setUpdate({ phase: "available", current: "2.0.0", latest: "2.1.0", canApply });
    backend.showBanner({ kind: "appUpdate", install: null, version: "2.1.0", canApply });
  }
};

if (typeof window !== "undefined") {
  (window as unknown as { __ytdlMock: typeof mockControls }).__ytdlMock = mockControls;
}

backend.key(null, "info", "log.ready");
// Like the real start-up probe on a machine without ffmpeg (handy for UI work).
backend.showBanner({ kind: "ffmpegMissing", install: { type: "auto", display: "pkexec pacman -S --noconfirm ffmpeg" }, version: null, canApply: false });

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function invoke<T>(cmd: string, args: Record<string, unknown> = {}): Promise<T> {
  const b = backend;
  await Promise.resolve();
  const r = ((): unknown => {
    switch (cmd) {
      case "get_bootstrap":
        return b.bootstrap();
      case "update_settings":
        return b.updateSettings(args.patch as Partial<Settings>);
      case "set_anyip_password":
        b.passwordSet = (args.password as string) !== "";
        b.emit({ type: "settings", settings: { ...b.settings }, anyipPasswordSet: b.passwordSet });
        return b.passwordSet;
      case "add_tasks":
        return b.addTasks(args.text as string);
      case "cancel_task":
        return b.cancel(args.id as number);
      case "remove_task":
        return b.remove(args.id as number);
      case "retry_task":
        return b.retry(args.id as number);
      case "cancel_all":
        return b.tasks.filter((t) => !TERMINAL_STATES.includes(t.state.state)).forEach((t) => b.cancel(t.id));
      case "clear_finished":
        return b.clearFinished();
      case "open_output_folder":
        return undefined;
      case "preview_url":
        return sleep(400 * b.speed).then(() => b.preview(args.url as string));
      case "dismiss_banner":
        b.banners = b.banners.filter((x) => x.kind !== args.kind);
        b.emit({ type: "bannerDismissed", kind: args.kind as BannerKind });
        return undefined;
      case "get_tools_status":
        return { ffmpeg: null, ffprobe: null, jsRuntimes: ["deno"] } satisfies ToolsStatus;
      case "install_tool":
        return b.install(args.tool as "ffmpeg" | "deno");
      case "update_ytdlp":
        b.ytdlp = { ...b.ytdlp, phase: "upToDate" };
        b.emit({ type: "ytdlp", status: b.ytdlp });
        b.text(null, "info", "yt-dlp is up to date.");
        return null;
      case "check_app_update":
        b.key(null, "info", "log.update_checking");
        b.setUpdate({ phase: "upToDate", current: "2.0.0" });
        b.key(null, "info", "log.update_uptodate");
        return b.appUpdate;
      case "apply_app_update":
        b.setUpdate({ phase: "readyToRestart", version: "2.1.0" });
        b.key(null, "info", "log.update_done");
        return b.appUpdate;
      case "open_sponsor_page":
        b.opened.push(SPONSOR_URL);
        return undefined;
      case "open_release_page":
      case "restart_app":
      case "confirm_exit":
        return undefined;
      default:
        throw `mock: unknown command ${cmd}`;
    }
  })();
  return (await r) as T;
}

export function mockTransport() {
  return {
    invoke,
    listen: async (handler: Handler) => {
      backend.handlers.add(handler);
      return () => backend.handlers.delete(handler);
    },
    pickFolder: async (initial?: string) => initial || "/home/user/Videos",
    pickCookieFile: async () => "/home/user/cookies.txt",
    readClipboard: async () => (typeof navigator !== "undefined" && navigator.clipboard ? navigator.clipboard.readText().catch(() => null) : null),
    setWindowTitle: async (title: string) => {
      if (typeof document !== "undefined") document.title = title;
    },
    setWindowTheme: async () => {},
    windowAction: async (action: WindowAction) => {
      const w = mockControls.window;
      if (action === "minimize") w.minimized = true;
      else if (action === "toggleMaximize") {
        w.maximized = !w.maximized;
        w.resized.forEach((h) => h());
      } else if (backend.tasks.some((t) => ACTIVE_STATES.includes(t.state.state))) mockControls.requestClose();
      else w.closed = true; // like the backend: nothing running, the window just closes
    },
    isMaximized: async () => mockControls.window.maximized,
    onResized: async (handler: () => void) => {
      mockControls.window.resized.add(handler);
      return () => mockControls.window.resized.delete(handler);
    }
  };
}
