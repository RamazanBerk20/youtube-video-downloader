// Everything that comes from the backend lives here. Events are applied to the
// data at once (cheap map writes) but subscribers are notified at most once per
// animation frame, so a few hundred tasks streaming progress cost one React
// pass per frame, and each row re-renders only when its own Task object changed.
import { useSyncExternalStore } from "react";
import { isLoaded, loadLanguage } from "../i18n";
import { api, onEvent } from "../lib/api";
import type {
  AppEvent,
  Banner,
  Bootstrap,
  InstallState,
  LogEntry,
  Preview,
  Settings,
  Task,
  Tool,
  UpdateState,
  YtdlpStatus
} from "../lib/types";
import { ACTIVE_STATES, TERMINAL_STATES } from "../lib/types";

export const LOG_LIMIT = 5000;

export interface Counts {
  total: number;
  active: number;
  queued: number;
  done: number;
  failed: number;
  cancelled: number;
}

export interface DataState {
  phase: "loading" | "ready" | "error";
  /** The language the UI renders in: settings.language once its catalog is loaded. */
  lang: string;
  error: string | null;
  version: string;
  platform: string;
  defaultOutputDir: string;
  settings: Settings | null;
  passwordSet: boolean;
  /** Task ids in id order (item 62a); a new array only when membership changes. */
  ids: number[];
  tasks: Map<number, Task>;
  counts: Counts;
  banners: Banner[];
  /** Mutated in place; `logVersion` changes when it does. */
  log: LogEntry[];
  logVersion: number;
  ytdlp: YtdlpStatus | null;
  appUpdate: UpdateState;
  installs: Partial<Record<Tool, InstallState>>;
  /** Preview results by URL, used by the preview card and by queue rows (thumbnail/title). */
  previews: Map<string, Preview>;
  previewsVersion: number;
}

/** One-off signals the UI turns into dialogs. */
export type Signal = { type: "closeRequested"; activeTasks: number } | { type: "restartReady" } | { type: "settingsError"; error: string };

const EMPTY_COUNTS: Counts = { total: 0, active: 0, queued: 0, done: 0, failed: 0, cancelled: 0 };

function initial(): DataState {
  return {
    phase: "loading",
    lang: "en",
    error: null,
    version: "",
    platform: "",
    defaultOutputDir: "",
    settings: null,
    passwordSet: false,
    ids: [],
    tasks: new Map(),
    counts: EMPTY_COUNTS,
    banners: [],
    log: [],
    logVersion: 0,
    ytdlp: null,
    appUpdate: { phase: "idle" },
    installs: {},
    previews: new Map(),
    previewsVersion: 0
  };
}

let state = initial();
const listeners = new Set<() => void>();
let signalHandler: (s: Signal) => void = () => {};
let scheduled = false;
let idsDirty = false;

const raf: (cb: () => void) => void =
  typeof requestAnimationFrame === "function" ? (cb) => requestAnimationFrame(cb) : (cb) => setTimeout(cb, 16);

function flush() {
  if (!scheduled) return;
  scheduled = false;
  syncLang();
  if (idsDirty) {
    idsDirty = false;
    state.ids = [...state.tasks.keys()].sort((a, b) => a - b);
  }
  const c = countTasks(state.tasks);
  const o = state.counts;
  if (c.total !== o.total || c.active !== o.active || c.queued !== o.queued || c.done !== o.done || c.failed !== o.failed || c.cancelled !== o.cancelled) {
    state.counts = c;
  }
  listeners.forEach((l) => l());
}

/** Keep showing the current language until the chosen one's catalog has loaded. */
function syncLang() {
  const want = state.settings?.language || "en";
  if (state.lang === want) return;
  if (isLoaded(want)) state.lang = want;
  else
    void loadLanguage(want).then(() => {
      if ((state.settings?.language || "en") === want) changedNow();
    });
}

/** Coalesce to the next frame; the timer bounds latency when frames are throttled (occluded window). */
function changed() {
  if (scheduled) return;
  scheduled = true;
  raf(flush);
  setTimeout(flush, 100);
}

/** User-initiated changes are shown at once, not on the next frame. */
function changedNow() {
  scheduled = true;
  flush();
}

export function countTasks(tasks: Map<number, Task>): Counts {
  const c = { ...EMPTY_COUNTS, total: tasks.size };
  for (const t of tasks.values()) {
    const s = t.state.state;
    if (ACTIVE_STATES.includes(s)) c.active++;
    else if (s === "queued") c.queued++;
    else if (s === "done") c.done++;
    else if (s === "failed") c.failed++;
    else if (s === "cancelled") c.cancelled++;
  }
  return c;
}

function upsertTask(task: Task) {
  if (!state.tasks.has(task.id)) idsDirty = true;
  state.tasks.set(task.id, task);
}

function pushLog(e: LogEntry) {
  state.log.push(e);
  if (state.log.length > LOG_LIMIT) state.log.splice(0, state.log.length - LOG_LIMIT);
  state.logVersion++;
}

function showBanner(b: Banner) {
  const i = state.banners.findIndex((x) => x.kind === b.kind);
  state.banners = i >= 0 ? state.banners.map((x, j) => (j === i ? b : x)) : [...state.banners, b];
}

// ---- settings: optimistic, serialised, awaited before Add -----------------

let confirmed: Settings | null = null;
let pending = 0;
let chain: Promise<unknown> = Promise.resolve();

/** Save a patch (item 97). Applied to the screen at once; calls run in order. */
export function updateSettings(patch: Partial<Settings>): Promise<void> {
  if (!state.settings) return Promise.resolve();
  state.settings = { ...state.settings, ...patch };
  changedNow();
  pending++;
  const p = chain.then(async () => {
    try {
      confirmed = await api.updateSettings(patch);
    } catch (e) {
      signalHandler({ type: "settingsError", error: String(e) });
    } finally {
      if (--pending === 0 && confirmed) {
        state.settings = confirmed;
        changed();
      }
    }
  });
  chain = p;
  return p;
}

/** Item 40: resolves once every pending updateSettings call has finished. */
export const settled = (): Promise<unknown> => chain;

export async function setPassword(password: string) {
  try {
    state.passwordSet = await api.setAnyipPassword(password);
    changed();
  } catch (e) {
    signalHandler({ type: "settingsError", error: String(e) });
  }
}

// ---- events ----------------------------------------------------------------

export function applyEvent(e: AppEvent) {
  switch (e.type) {
    case "taskUpdated":
      upsertTask(e.task);
      break;
    case "tasksRemoved":
      for (const id of e.ids) state.tasks.delete(id);
      idsDirty = true;
      break;
    case "log":
      pushLog(e.entry);
      break;
    case "bannerShown":
      showBanner(e.banner);
      break;
    case "bannerDismissed":
      state.banners = state.banners.filter((b) => b.kind !== e.kind);
      break;
    case "settings":
      confirmed = e.settings;
      if (pending === 0) state.settings = e.settings;
      state.passwordSet = e.anyipPasswordSet;
      break;
    case "ytdlp":
      state.ytdlp = e.status;
      break;
    case "appUpdate":
      if (e.state.phase === "readyToRestart" && state.appUpdate.phase !== "readyToRestart") signalHandler({ type: "restartReady" });
      state.appUpdate = e.state;
      break;
    case "install":
      state.installs = { ...state.installs, [e.tool]: e.state };
      break;
    case "closeRequested":
      signalHandler({ type: "closeRequested", activeTasks: e.activeTasks });
      break;
  }
  changed();
}

function applyBootstrap(b: Bootstrap, early: AppEvent[]) {
  state.phase = "ready";
  state.error = null;
  state.version = b.version;
  state.platform = b.platform;
  state.defaultOutputDir = b.defaultOutputDir;
  state.settings = confirmed = b.settings;
  state.passwordSet = b.anyipPasswordSet;
  state.tasks = new Map(b.tasks.map((t) => [t.id, t]));
  idsDirty = true;
  state.banners = b.banners;
  state.log = b.log.slice(-LOG_LIMIT);
  state.logVersion++;
  state.ytdlp = b.ytdlp;
  state.appUpdate = b.appUpdate;
  // Events that raced the bootstrap call: re-apply the ones newer than the snapshot.
  const lastSeq = b.log.length ? b.log[b.log.length - 1].seq : -1;
  for (const e of early) {
    if (e.type === "log" && e.entry.seq <= lastSeq) continue;
    applyEvent(e);
  }
  changed();
}

let started: Promise<void> | null = null;

/** Subscribe first, then bootstrap (backend rule). Idempotent. */
export function start(onSignal: (s: Signal) => void): Promise<void> {
  signalHandler = onSignal;
  started ??= (async () => {
    let early: AppEvent[] | null = [];
    await onEvent((e) => (early ? early.push(e) : applyEvent(e)));
    await bootstrap(() => {
      const q = early ?? [];
      early = null;
      return q;
    });
  })();
  return started;
}

async function bootstrap(drain: () => AppEvent[]) {
  try {
    const b = await api.getBootstrap();
    applyBootstrap(b, drain());
  } catch (e) {
    state.phase = "error";
    state.error = String(e);
    changed();
  }
}

/** "Try again" after a failed bootstrap. */
export function retryBootstrap() {
  state.phase = "loading";
  changed();
  return bootstrap(() => []);
}

export function setPreview(p: Preview) {
  state.previews.set(p.url, p);
  state.previewsVersion++;
  changedNow();
}

// ---- React binding -----------------------------------------------------------

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export const getState = () => state;

/** Selector must return a stable value (a field, a Map entry, a primitive). */
export function useData<T>(select: (s: DataState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

export const isTerminal = (t: Task) => TERMINAL_STATES.includes(t.state.state);

/** Tests: fresh state and synchronous notification. */
export function resetForTests() {
  state = initial();
  confirmed = null;
  pending = 0;
  chain = Promise.resolve();
  started = null;
  idsDirty = false;
}
export const flushNow = changedNow;
