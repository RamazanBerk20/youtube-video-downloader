// The queue: toolbar (items 26, 58-61) and one memoised row per task (65-70, 76-82).
import { memo } from "react";
import { api } from "../lib/api";
import { barPercent, detailLine, ellipsize, statusKey } from "../lib/format";
import { concurrencyLabel } from "../lib/options";
import { CONCURRENCY_PRESETS, parseConcurrency, type Task, type TaskStateName } from "../lib/types";
import { isTerminal, updateSettings, useData } from "../state/store";
import { useLang, useT } from "./context";
import { Combo } from "./controls";
import { Icon, type IconName } from "./icons";
import { t as tr } from "../i18n";

const ICONS: Record<TaskStateName, IconName> = {
  queued: "pause",
  starting: "play",
  fetching: "play",
  downloading: "play",
  postProcessing: "play",
  cancelling: "play",
  done: "check",
  failed: "fail",
  cancelled: "ban"
};

export const Queue = memo(function Queue() {
  const t = useT();
  const ids = useData((s) => s.ids);
  const phase = useData((s) => s.phase);
  const c = useData((s) => s.counts);
  const allDone = c.total > 0 && c.active === 0 && c.queued === 0;

  return (
    <section className="queue" aria-labelledby="queue-title" data-testid="queue" data-state={phase !== "ready" ? phase : c.total === 0 ? "empty" : allDone ? "finished" : "running"}>
      <QueueToolbar />
      <div className="queue-scroll">
        {phase === "loading" ? (
          <div className="empty" data-testid="queue-loading">
            <span className="spinner" aria-hidden="true" /> {t("queue.loading")}
          </div>
        ) : ids.length === 0 ? (
          <div className="empty" data-testid="queue-empty">
            <Icon name="download" size={28} />
            <p>{t("queue.empty")}</p>
          </div>
        ) : (
          <ol className="rows">
            {ids.map((id) => (
              <TaskRow key={id} id={id} />
            ))}
          </ol>
        )}
      </div>
      {allDone && (
        <div className="all-done" data-testid="all-done" role="status">
          <Icon name="check" /> {t("queue.all_done")}
          <button type="button" className="btn small" onClick={() => void api.clearFinished()}>
            {t("button.clear_done")}
          </button>
        </div>
      )}
    </section>
  );
});

function QueueToolbar() {
  const t = useT();
  const c = useData((s) => s.counts);
  const max = useData((s) => s.settings?.maxConcurrent ?? 2);
  const busy = c.active + c.queued > 0;
  const finished = c.done + c.failed + c.cancelled > 0;
  return (
    <header className="queue-head">
      <h2 id="queue-title">{t("label.queue")}</h2>
      <div className="counts" data-testid="queue-counts">
        {c.active > 0 && <span className="count running">{t("queue.count_active", { n: c.active })}</span>}
        {c.queued > 0 && <span className="count">{t("queue.count_queued", { n: c.queued })}</span>}
        {c.done > 0 && <span className="count done">{t("queue.count_done", { n: c.done })}</span>}
        {c.failed > 0 && <span className="count failed">{t("queue.count_failed", { n: c.failed })}</span>}
      </div>
      <div className="queue-tools">
        <label className="inline-label" htmlFor="max-concurrent">
          {t("label.max_concurrent")}
        </label>
        <Combo
          id="max-concurrent"
          testId="max-concurrent"
          label={t("label.max_concurrent")}
          value={concurrencyLabel(max)}
          presets={CONCURRENCY_PRESETS.map(concurrencyLabel)}
          onCommit={(text) => void updateSettings({ maxConcurrent: parseConcurrency(text) })}
        />
        <button type="button" className="btn danger" data-testid="cancel-all" disabled={!busy} onClick={() => void api.cancelAll()}>
          <Icon name="stop" /> {t("button.cancel_all")}
        </button>
        <button type="button" className="btn" data-testid="clear-finished" disabled={!finished} onClick={() => void api.clearFinished()}>
          <Icon name="sweep" /> {t("button.clear_done")}
        </button>
      </div>
    </header>
  );
}

/** One row; re-renders only when its Task object, its preview or the language changes. */
export const TaskRow = memo(function TaskRow({ id }: { id: number }) {
  const task = useData((s) => s.tasks.get(id));
  const lang = useLang();
  const preview = useData((s) => (task ? s.previews.get(task.spec.url) : undefined));
  if (!task) return null;
  return <RowView task={task} lang={lang} thumb={preview?.thumbnail ?? null} previewTitle={preview?.title ?? null} />;
});

const RowView = memo(function RowView({ task, lang, thumb, previewTitle }: { task: Task; lang: string; thumb: string | null; previewTitle: string | null }) {
  const t = (k: string, a?: Record<string, string | number>) => tr(lang, k, a);
  const s = task.state;
  const name = s.state;
  const terminal = isTerminal(task);
  const title = task.title ?? previewTitle ?? task.spec.url;
  const pct = barPercent(task);
  const detail = detailLine(lang, task);
  const fullDetail =
    s.state === "failed" ? s.message : s.state === "done" ? [...s.files.map((f) => t("detail.saved_to", { path: f })), ...s.itemErrors].join("\n") : undefined;
  return (
    <li className="row" data-testid={`task-${task.id}`} data-state={name} data-task-id={task.id}>
      <span className={`status-icon s-${name}`} aria-hidden="true">
        <Icon name={ICONS[name]} size={14} />
      </span>
      {thumb && <img className="row-thumb" src={thumb} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" />}
      <div className="row-main">
        <div className="row-top">
          <span className="row-id">#{task.id}</span>
          <span className="row-title" title={title} dir="auto">
            {ellipsize(title, 80)}
          </span>
          <span className={`row-status s-${name}`} data-testid="task-status">
            {t(statusKey(task))}
          </span>
        </div>
        <div className={`bar s-${name}`} role="progressbar" aria-label={title} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? undefined}>
          {pct == null ? <i className="indeterminate" /> : <i style={{ inlineSize: `${Math.min(100, Math.max(0, pct))}%` }} />}
        </div>
        <div className="row-detail" title={fullDetail} data-testid="task-detail">
          {detail || " "}
        </div>
      </div>
      <div className="row-actions">
        {(name === "failed" || name === "cancelled") && (
          <button type="button" className="icon-btn" data-testid="task-retry" aria-label={t("button.retry")} title={t("button.retry")} onClick={() => void api.retryTask(task.id)}>
            <Icon name="retry" />
          </button>
        )}
        <button
          type="button"
          className="icon-btn"
          data-testid="task-x"
          aria-label={terminal ? t("button.remove") : t("button.cancel")}
          title={terminal ? t("button.remove") : t("button.cancel")}
          onClick={() => void (terminal ? api.removeTask(task.id) : api.cancelTask(task.id))}
        >
          <Icon name="x" />
        </button>
      </div>
    </li>
  );
});
