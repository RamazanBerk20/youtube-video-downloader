// Pure display helpers (spec items 67-69, 77, 78, 90). No React here.
import { t } from "../i18n";
import type { LogEntry, PostKind, Task } from "./types";

const UNITS = ["B", "KB", "MB", "GB", "TB"];

/** Base-1024 size with one decimal; null when unknown. */
export function formatBytes(n: number | null | undefined): string | null {
  if (n == null || !Number.isFinite(n) || n < 0) return null;
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(1)} ${UNITS[i]}`;
}

/** Item 77: "4.3 MB/s"; missing/0/non-finite/negative → "—". */
export function formatSpeed(bps: number | null | undefined): string {
  if (bps == null || !Number.isFinite(bps) || bps <= 0) return "—";
  return `${formatBytes(bps)}/s`;
}

/** Item 78: "{h}h {mm}m" from one hour, else "{m}m {ss}s"; missing → "—". */
export function formatEta(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (x: number) => String(x).padStart(2, "0");
  return h >= 1 ? `${h}h ${pad(m)}m` : `${m}m ${pad(s % 60)}s`;
}

/** Media duration "1:02:03" / "3:33". */
export function formatDuration(sec: number | null | undefined): string | null {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return null;
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export const ellipsize = (s: string, max: number) => (s.length > max ? s.slice(0, max) + "…" : s);

/** Item 68: status text key. */
export function statusKey(task: Task): string {
  const s = task.state;
  switch (s.state) {
    case "queued":
      return "status.queued";
    case "starting":
    case "fetching":
    case "downloading":
      return "status.running";
    case "postProcessing":
      return POST_KEYS[s.kind];
    case "cancelling":
      return "status.cancelling";
    default:
      return `status.${s.state}`;
  }
}

const POST_KEYS: Record<PostKind, string> = {
  encode: "status.encoding",
  remux: "status.remuxing",
  merge: "status.merging",
  extractAudio: "status.extracting",
  convert: "status.converting",
  embed: "status.embedding"
};

const SEP = "  ·  ";

/** Item 69 detail line, plus the optional extras the backend now reports. */
export function detailLine(lang: string, task: Task): string {
  const s = task.state;
  const pos = "item" in s ? s.item : null;
  const item = pos ? (pos.count ? t(lang, "detail.item", { index: pos.index, count: pos.count }) : t(lang, "detail.item_open", { index: pos.index })) : null;
  const parts: (string | null)[] = [];
  switch (s.state) {
    case "queued":
      break;
    case "starting":
      parts.push(t(lang, "phase.starting"));
      break;
    case "fetching":
      parts.push(t(lang, "phase.fetching"), item);
      break;
    case "downloading": {
      parts.push(`${s.percent.toFixed(1)}%`, formatSpeed(s.speed), `ETA ${formatEta(s.eta)}`);
      const done = formatBytes(s.downloaded);
      const total = formatBytes(s.total);
      if (total) parts.push(done ? `${done} / ${total}` : total);
      if (s.stream > 0) parts.push(t(lang, "detail.file", { n: s.stream + 1 }));
      parts.push(item);
      break;
    }
    case "postProcessing":
      if (s.percent != null) parts.push(`${s.percent.toFixed(1)}%`, s.speedX != null ? `${s.speedX.toFixed(2)}x` : "—", `ETA ${formatEta(s.eta)}`);
      parts.push(item);
      break;
    case "cancelling":
      parts.push(t(lang, "phase.cancelling"));
      break;
    case "done":
      parts.push("100%");
      if (s.itemErrors.length) parts.push(t(lang, "detail.item_errors", { n: s.itemErrors.length }));
      break;
    case "failed":
      parts.push(ellipsize(s.message.replace(/\s*\n\s*/g, " "), 110));
      break;
    case "cancelled":
      if (s.cleanedUp && task.attempts.length) parts.push(t(lang, "detail.cleaned_up"));
      break;
  }
  // Rescue visibility: which attempt and through what.
  const a = task.attempts[task.attempts.length - 1];
  if (a && task.attempts.length > 1 && s.state !== "queued") {
    parts.push(t(lang, "detail.attempt", { n: a.n }));
    if (a.cookieSource.type === "browser") parts.push(t(lang, "detail.via_cookies", { browser: a.cookieSource.browser }));
    if (a.cookieSource.type === "file") parts.push(t(lang, "label.cookie_file"));
    if (a.proxy) parts.push(t(lang, "detail.via_proxy"));
  }
  return parts.filter(Boolean).join(SEP);
}

/** Bar fill 0..100, or null for an indeterminate phase. */
export function barPercent(task: Task): number | null {
  const s = task.state;
  switch (s.state) {
    case "downloading":
      return s.percent;
    case "postProcessing":
      return s.percent;
    case "starting":
    case "fetching":
    case "cancelling":
      return null;
    case "done":
      return 100;
    default:
      return 0;
  }
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Item 90: "[HH:MM:SS] #id  message" in local time. */
export function logLine(lang: string, e: LogEntry): string {
  const d = new Date(e.tsMs);
  const ts = `[${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}] `;
  const id = e.taskId != null ? `#${e.taskId}  ` : "";
  let msg: string;
  if (e.message.kind === "text") msg = e.message.text;
  else {
    const args: Record<string, string> = { ...e.message.args };
    // Arg values that are message keys are translated too (auto_update.on/off).
    if (e.message.key === "log.auto_update_toggled" && args.state) args.state = t(lang, args.state);
    msg = t(lang, e.message.key, args);
    // Item 91: further lines of a failure are indented by three spaces.
    msg = msg.replace(/\n/g, "\n   ");
  }
  return ts + id + msg;
}
