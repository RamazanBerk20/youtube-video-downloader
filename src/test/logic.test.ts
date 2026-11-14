import { describe, expect, it } from "vitest";
import { initialUi, uiReducer, type Dialog, type UiAction, type UiState } from "../state/ui";
import { codecPatch, codecsFor, concurrencyLabel, currentCodec, modePatch, presetEnabled, qualitiesFor, qualityEnabled, subtitleLangsEnabled, videoOnlyVisible } from "../lib/options";
import { DEFAULT_SETTINGS } from "../lib/mockBackend";
import { AUDIO_CODECS, AUDIO_QUALITIES, VIDEO_QUALITIES, type Settings, type Task, type TaskState } from "../lib/types";
import { appendPasted, scanUrls, urlAtCaret } from "../lib/urls";
import { wordEndAfter, wordStartBefore } from "../lib/textEdit";
import { barPercent, detailLine, ellipsize, formatBytes, formatEta, formatSpeed, logLine, queueSummary, statusKey, summaryLine } from "../lib/format";
import { countTasks } from "../state/store";
import type { Preview } from "../lib/types";

const S = (p: Partial<Settings> = {}): Settings => ({ ...DEFAULT_SETTINGS, ...p });

describe("ui reducer", () => {
  const preview = { url: "https://x" } as Preview;
  const base = initialUi();
  const withAll: UiState = {
    ...base,
    draft: "x",
    panel: { kind: "settings", section: "cookies" },
    dialog: { kind: "noUrls" },
    menu: { x: 1, y: 2, canCut: true, canCopy: true, canPaste: true }
  };
  // Every action type, with the expected effect on a base state.
  const cases: [UiAction, (s: UiState) => void][] = [
    [{ type: "draft", text: "abc" }, (s) => expect(s.draft).toBe("abc")],
    [{ type: "addStarted" }, (s) => expect(s.adding).toBe(true)],
    [{ type: "addSucceeded" }, (s) => expect([s.adding, s.draft, s.preview.kind]).toEqual([false, "", "idle"])],
    [{ type: "addFailed", dialog: { kind: "noUrls" } }, (s) => expect([s.adding, s.dialog?.kind]).toEqual([false, "noUrls"])],
    [{ type: "addFailed", dialog: null }, (s) => expect(s.dialog).toBeNull()],
    [{ type: "openSettings" }, (s) => expect(s.panel).toEqual({ kind: "settings", section: null })],
    [{ type: "openSettings", section: "anyip" }, (s) => expect(s.panel).toEqual({ kind: "settings", section: "anyip" })],
    [{ type: "closeSettings" }, (s) => expect(s).toBe(base)],
    [{ type: "toggleDrawer" }, (s) => expect(s.drawer.open).toBe(true)],
    [{ type: "drawerTab", tab: "tools" }, (s) => expect(s.drawer).toEqual({ open: true, tab: "tools" })],
    [{ type: "showDialog", dialog: { kind: "restart" } }, (s) => expect(s.dialog).toEqual({ kind: "restart" })],
    [{ type: "closeDialog" }, (s) => expect(s).toBe(base)],
    [{ type: "openMenu", menu: { x: 3, y: 4, canCut: false, canCopy: true, canPaste: false } }, (s) => expect(s.menu?.x).toBe(3)],
    [{ type: "closeMenu" }, (s) => expect(s).toBe(base)],
    [{ type: "previewStart", url: "u", seq: 1 }, (s) => expect(s.preview).toEqual({ kind: "loading", url: "u", seq: 1 })],
    [{ type: "previewDone", seq: 1, preview, more: 0 }, (s) => expect(s).toBe(base)],
    [{ type: "previewFailed", seq: 1, message: "e" }, (s) => expect(s).toBe(base)],
    [{ type: "previewClose" }, (s) => expect(s.preview.kind).toBe("idle")],
    [{ type: "theme", theme: "dark" }, (s) => expect(s.theme).toBe("dark")],
    [{ type: "escape" }, (s) => expect(s).toBe(base)]
  ];
  it.each(cases.map(([a, check], i) => [i, a.type, a, check] as const))("%i %s", (_i, _t, a, check) => check(uiReducer(base, a)));

  it("covers every action type", () => {
    const types = new Set(cases.map(([a]) => a.type));
    // Compile-time exhaustiveness: adding an action type without a case here fails this list.
    const all: Record<UiAction["type"], true> = {
      draft: true, addStarted: true, addSucceeded: true, addFailed: true, openSettings: true, closeSettings: true,
      toggleDrawer: true, drawerTab: true, showDialog: true, closeDialog: true, openMenu: true, closeMenu: true,
      previewStart: true, previewDone: true, previewFailed: true, previewClose: true, theme: true, escape: true
    };
    expect([...types].sort()).toEqual(Object.keys(all).sort());
  });

  it("escape closes the innermost surface first", () => {
    let s = uiReducer(withAll, { type: "escape" });
    expect([s.menu, s.dialog?.kind, s.panel.kind]).toEqual([null, "noUrls", "settings"]);
    s = uiReducer(s, { type: "escape" });
    expect([s.dialog, s.panel.kind]).toEqual([null, "settings"]);
    s = uiReducer(s, { type: "escape" });
    expect(s.panel.kind).toBe("none");
    expect(uiReducer(s, { type: "escape" })).toBe(s);
  });

  it("drops stale preview answers and keeps the matching one", () => {
    let s = uiReducer(base, { type: "previewStart", url: "a", seq: 1 });
    s = uiReducer(s, { type: "previewStart", url: "b", seq: 2 });
    expect(uiReducer(s, { type: "previewDone", seq: 1, preview, more: 0 })).toBe(s);
    expect(uiReducer(s, { type: "previewDone", seq: 2, preview, more: 3 }).preview).toEqual({ kind: "ready", preview, more: 3 });
    expect(uiReducer(s, { type: "previewFailed", seq: 2, message: "x" }).preview).toEqual({ kind: "error", url: "b", message: "x" });
    expect(uiReducer(uiReducer(s, { type: "previewClose" }), { type: "previewDone", seq: 2, preview, more: 0 }).preview.kind).toBe("idle");
  });

  it("toggles, opens dialogs over the sheet, keeps the draft on failure", () => {
    const d: Dialog = { kind: "badFolder", error: "e" };
    const s = uiReducer({ ...withAll, adding: true }, { type: "addFailed", dialog: d });
    expect([s.draft, s.panel.kind, s.dialog]).toEqual(["x", "settings", d]);
    expect(uiReducer(uiReducer(base, { type: "toggleDrawer" }), { type: "toggleDrawer" }).drawer.open).toBe(false);
    expect(uiReducer(withAll, { type: "showDialog", dialog: d }).menu).toBeNull();
    expect(uiReducer(base, { type: "draft", text: "" })).toBe(base);
  });
});

describe("option dependencies (PARITY 23, 46, 49, 50, 52, 54)", () => {
  it("quality is disabled in audio mode for codecs without a bitrate only", () => {
    const off = AUDIO_CODECS.filter((c) => !qualityEnabled(S({ mode: "audio", audioCodec: c })));
    expect(off).toEqual(["flac", "wav", "alac", "Original"]);
    for (const c of AUDIO_CODECS) expect(qualityEnabled(S({ mode: "video", audioCodec: c }))).toBe(true);
  });
  it("re-encode enables preset; video-only row hidden in audio mode", () => {
    expect(presetEnabled(S({ reencode: false }))).toBe(false);
    expect(presetEnabled(S({ reencode: true }))).toBe(true);
    expect(videoOnlyVisible(S({ mode: "video" }))).toBe(true);
    expect(videoOnlyVisible(S({ mode: "audio" }))).toBe(false);
    expect(subtitleLangsEnabled(S({ embedSubtitles: false }))).toBe(false);
    expect(subtitleLangsEnabled(S({ embedSubtitles: true }))).toBe(true);
  });
  it("mode switch sends mode and quality together, resetting to Best when needed", () => {
    expect(modePatch(S({ quality: "1080p" }), "audio")).toEqual({ mode: "audio", quality: "Best" });
    expect(modePatch(S({ mode: "audio", quality: "320 kbps" }), "video")).toEqual({ mode: "video", quality: "Best" });
    expect(modePatch(S({ quality: "Best" }), "audio")).toEqual({ mode: "audio", quality: "Best" });
    expect(qualitiesFor("audio")).toBe(AUDIO_QUALITIES);
    expect(qualitiesFor("video")).toBe(VIDEO_QUALITIES);
  });
  it("codec is per mode", () => {
    const s = S({ videoCodec: "WebM (VP9)", audioCodec: "mp3" });
    expect(currentCodec(s)).toBe("WebM (VP9)");
    expect(currentCodec({ ...s, mode: "audio" })).toBe("mp3");
    expect(codecPatch(s, "Auto")).toEqual({ videoCodec: "Auto" });
    expect(codecPatch({ ...s, mode: "audio" }, "opus")).toEqual({ audioCodec: "opus" });
    expect(codecsFor("audio")).toContain("ac3");
  });
  it("concurrency shows 0 and ≥ 9999 as ∞", () => {
    expect([0, -1, 9999, 1, 10].map(concurrencyLabel)).toEqual(["∞", "∞", "∞", "1", "10"]);
  });
});

describe("URL box", () => {
  it("keeps http(s) lines, trims, strips control chars, counts the rest, no de-dup", () => {
    const r = scanUrls("  https://a \n\nftp://b\nHTTP://C\nhello\nhttps://a\n\u0007https://d\u0000");
    expect(r.urls).toEqual(["https://a", "HTTP://C", "https://a", "https://d"]);
    expect(r.skipped).toBe(2);
    expect(scanUrls("")).toEqual({ urls: [], skipped: 0 });
    expect(scanUrls("   \n \r\n").urls).toEqual([]);
  });
  it("previews the caret's line, else the first URL", () => {
    const text = "https://a\nnope\nhttps://c";
    expect(urlAtCaret(text, text.length)).toBe("https://c");
    expect(urlAtCaret(text, 12)).toBe("https://a");
    expect(urlAtCaret("nothing", 0)).toBeNull();
  });
  it("Paste appends on a new line with a trailing newline; empty clipboard does nothing", () => {
    expect(appendPasted("", " https://a ")).toBe("https://a\n");
    expect(appendPasted("x", "y")).toBe("x\ny\n");
    expect(appendPasted("x\n", "y")).toBe("x\ny\n");
    expect(appendPasted("x", "  ")).toBe("x");
    expect(appendPasted("x", null)).toBe("x");
    expect(appendPasted("  x\n\n", "y")).toBe("x\ny\n");
  });
});

describe("word deletion (items 32, 33)", () => {
  it("skips whitespace then word characters, stops at punctuation", () => {
    expect(wordStartBefore("hello world  ", 13)).toBe(6);
    expect(wordStartBefore("a/b_c", 5)).toBe(2);
    expect(wordStartBefore("x.", 2)).toBe(2);
    expect(wordStartBefore("çay şeker", 9)).toBe(4);
    expect(wordEndAfter("  foo bar", 0)).toBe(5);
    expect(wordEndAfter("foo-bar", 0)).toBe(3);
    expect(wordEndAfter("abc", 3)).toBe(3);
  });
});

const task = (state: TaskState, extra: Partial<Task> = {}): Task => ({
  id: 7,
  spec: { url: "https://u" } as Task["spec"],
  title: null,
  state,
  attempts: [],
  rescue: { cookieFileRetryAttempted: false, anyipRetryAttempted: false },
  createdMs: 0,
  ...extra
});

describe("formatting (items 66-69, 77, 78, 90)", () => {
  it("speed, eta, sizes", () => {
    expect([null, 0, -1, NaN, Infinity].map(formatSpeed)).toEqual(["—", "—", "—", "—", "—"]);
    expect(formatSpeed(512)).toBe("512.0 B/s");
    expect(formatSpeed(4_500_000)).toBe("4.3 MB/s");
    expect(formatEta(null)).toBe("—");
    expect(formatEta(65)).toBe("1m 05s");
    expect(formatEta(3725)).toBe("1h 02m");
    expect(formatBytes(1024 ** 3)).toBe("1.0 GB");
    expect(ellipsize("x".repeat(81), 80)).toBe("x".repeat(80) + "…");
  });
  it("status text and bar", () => {
    const pp = (kind: "encode" | "remux" | "merge") => task({ state: "postProcessing", kind, item: null, percent: null, speedX: null, eta: null });
    expect([pp("encode"), pp("remux"), pp("merge")].map(statusKey)).toEqual(["status.encoding", "status.remuxing", "status.merging"]);
    expect(statusKey(task({ state: "fetching", item: null }))).toBe("status.running");
    expect(statusKey(task({ state: "cancelling" }))).toBe("status.cancelling");
    expect(statusKey(task({ state: "done", files: [], itemErrors: [] }))).toBe("status.done");
    expect(barPercent(task({ state: "done", files: [], itemErrors: [] }))).toBe(100);
    expect(barPercent(pp("merge"))).toBeNull();
    expect(barPercent(task({ state: "queued" }))).toBe(0);
  });
  it("detail lines", () => {
    const dl = task({ state: "downloading", item: { index: 2, count: 3 }, stream: 1, percent: 42.25, downloaded: 1024 * 1024, total: 2 * 1024 * 1024, speed: 2048, eta: 61 });
    expect(detailLine("en", dl)).toBe("42.3%  ·  2.0 KB/s  ·  ETA 1m 01s  ·  1.0 MB / 2.0 MB  ·  file 2  ·  item 2 of 3");
    expect(detailLine("en", task({ state: "postProcessing", kind: "encode", item: null, percent: 50, speedX: 2.5, eta: 4 }))).toBe("50.0%  ·  2.50x  ·  ETA 0m 04s");
    expect(detailLine("en", task({ state: "queued" }))).toBe("");
    expect(detailLine("en", task({ state: "done", files: [], itemErrors: ["e"] }))).toBe("100%  ·  1 playlist item(s) failed");
    const long = "a\nb " + "x".repeat(200);
    expect(detailLine("en", task({ state: "failed", errorClass: "unknown", message: long }))).toBe(("a b " + "x".repeat(200)).slice(0, 110) + "…");
    const rescued = task({ state: "done", files: [], itemErrors: [] }, {
      attempts: [1, 2].map((n) => ({ n, cookieSource: n === 2 ? { type: "browser", browser: "firefox" } : { type: "none" }, proxy: n === 2, startedMs: 0, endedMs: 0, exitCode: 0, errorClass: null, stderrTail: [] }))
    });
    expect(detailLine("en", rescued)).toBe("100%  ·  attempt 2  ·  firefox cookies  ·  anyIP proxy");
  });
  it("log lines are timestamped, task-tagged and translated (state arg too)", () => {
    const ts = new Date(2026, 0, 1, 9, 5, 7).getTime();
    expect(logLine("en", { seq: 1, tsMs: ts, taskId: 3, level: "info", message: { kind: "text", text: "<b>x</b>" } })).toBe("[09:05:07] #3  <b>x</b>");
    expect(logLine("de", { seq: 1, tsMs: ts, taskId: null, level: "info", message: { kind: "key", key: "log.auto_update_toggled", args: { state: "auto_update.off" } } })).not.toContain("auto_update.off");
    expect(logLine("en", { seq: 1, tsMs: ts, taskId: 1, level: "error", message: { kind: "key", key: "log.task_failed", args: { title: "t", error: "a\nb" } } })).toBe("[09:05:07] #1  Failed: t  —  a\n   b");
  });
  it("counts tasks by state", () => {
    const m = new Map([task({ state: "queued" }), task({ state: "downloading", item: null, stream: 0, percent: 0, downloaded: null, total: null, speed: null, eta: null }), task({ state: "failed", errorClass: "unknown", message: "" })].map((t, i) => [i, t]));
    expect(countTasks(m)).toEqual({ total: 3, active: 1, queued: 1, done: 0, failed: 1, cancelled: 0 });
  });
  it("title-bar summary: count, total speed and overall progress of the current run", () => {
    const dl = (percent: number, speed: number | null, createdMs: number) => task({ state: "downloading", item: null, stream: 0, percent, downloaded: null, total: null, speed, eta: null }, { createdMs });
    const done = (createdMs: number) => task({ state: "done", files: [], itemErrors: [] }, { createdMs });
    // Idle: nothing running or waiting, whatever finished before.
    expect(queueSummary([])).toBeNull();
    expect(queueSummary([done(1), task({ state: "failed", errorClass: "unknown", message: "" })])).toBeNull();
    // Two downloads (one with no speed yet) and one waiting; the done task from before the run is ignored.
    const q = queueSummary([done(1), dl(50, 3 * 1024 * 1024, 10), dl(30, null, 11), task({ state: "queued" }, { createdMs: 12 })])!;
    expect(q).toEqual({ active: 2, queued: 1, speed: 3 * 1024 * 1024, progress: 80 / 300 });
    expect(summaryLine("en", q)).toBe("2 active  ·  3.0 MB/s");
    expect(summaryLine("de", { ...q, speed: 0 })).not.toContain("MB/s");
    // A task finished during the run counts as whole; post-processing counts as transferred; failures leave the run.
    const later = queueSummary([done(20), task({ state: "postProcessing", kind: "merge", item: null, percent: null, speedX: null, eta: null }, { createdMs: 21 }), dl(0, 0, 10), task({ state: "failed", errorClass: "unknown", message: "" }, { createdMs: 15 })])!;
    expect(later.progress).toBeCloseTo(200 / 300);
    expect(later.speed).toBe(0);
    // Waiting only: says how many are queued.
    expect(summaryLine("en", queueSummary([task({ state: "queued" }), task({ state: "queued" })])!)).toBe("2 queued");
  });
});
