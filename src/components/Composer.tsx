// URL box, Paste / Preview / Add (items 13-16, 36-41) and the preview card.
import { memo, useMemo, useRef } from "react";
import { api, asAddError } from "../lib/api";
import { formatBytes, formatDuration } from "../lib/format";
import { scanUrls, appendPasted, urlAtCaret } from "../lib/urls";
import { setPreview, settled } from "../state/store";
import type { PreviewState } from "../state/ui";
import { useDispatch, useT } from "./context";
import { Icon } from "./icons";

let previewSeq = 0;

export const Composer = memo(function Composer({ draft, adding, preview }: { draft: string; adding: boolean; preview: PreviewState }) {
  const t = useT();
  const dispatch = useDispatch();
  const box = useRef<HTMLTextAreaElement>(null);
  const scan = useMemo(() => scanUrls(draft), [draft]);

  const add = async () => {
    if (adding) return;
    dispatch({ type: "addStarted" });
    await settled(); // item 40: the snapshot is taken from the backend's settings
    try {
      await api.addTasks(draft);
      dispatch({ type: "addSucceeded" });
    } catch (e) {
      const err = asAddError(e);
      dispatch({
        type: "addFailed",
        dialog: err ? (err.kind === "noUrls" ? { kind: "noUrls" } : { kind: "badFolder", error: err.error }) : { kind: "error", message: String(e) }
      });
    }
  };

  const paste = async () => {
    const clip = await api.readClipboard();
    const next = appendPasted(box.current?.value ?? draft, clip);
    if (next !== draft) dispatch({ type: "draft", text: next });
    box.current?.focus();
  };

  const runPreview = async () => {
    const url = urlAtCaret(draft, box.current?.selectionStart ?? 0);
    if (!url) return dispatch({ type: "showDialog", dialog: { kind: "noUrls" } });
    const seq = ++previewSeq;
    dispatch({ type: "previewStart", url, seq });
    try {
      const p = await api.previewUrl(url);
      setPreview(p);
      dispatch({ type: "previewDone", seq, preview: p, more: Math.max(0, scan.urls.length - 1) });
    } catch (e) {
      dispatch({ type: "previewFailed", seq, message: String(e) });
    }
  };

  return (
    <section className="composer" aria-label={t("field.url")}>
      <div className="composer-main">
        <label htmlFor="url-box" className="visually-hidden">
          {t("field.url")}
        </label>
        <textarea
          id="url-box"
          ref={box}
          data-testid="url-input"
          wrap="off"
          rows={3}
          spellCheck={false}
          autoComplete="off"
          placeholder={t("field.url.placeholder")}
          value={draft}
          onChange={(e) => dispatch({ type: "draft", text: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void add();
            }
          }}
        />
        <div className="composer-actions">
          <button type="button" className="btn" data-testid="paste" onClick={() => void paste()}>
            <Icon name="clipboard" /> {t("button.paste")}
          </button>
          <button type="button" className="btn" data-testid="preview" disabled={preview.kind === "loading"} onClick={() => void runPreview()}>
            <Icon name="eye" /> {t("button.preview")}
          </button>
          <button type="button" className="btn primary" data-testid="add" title={t("url.shortcut")} aria-busy={adding} disabled={adding} onClick={() => void add()}>
            <Icon name="plus" /> {t("button.add")}
          </button>
        </div>
      </div>
      <p className="composer-hint" aria-live="polite" data-testid="url-hint">
        {scan.urls.length > 0 && <span className="ok">{t("url.ready", { n: scan.urls.length })}</span>}
        {scan.skipped > 0 && <span className="warn">{t("url.skipped", { n: scan.skipped })}</span>}
        {draft.trim() !== "" && <span className="muted">{t("url.shortcut")}</span>}
      </p>
      {preview.kind !== "idle" && <PreviewCard state={preview} />}
    </section>
  );
});

function PreviewCard({ state }: { state: PreviewState }) {
  const t = useT();
  const dispatch = useDispatch();
  const close = (
    <button type="button" className="icon-btn" aria-label={t("button.close")} onClick={() => dispatch({ type: "previewClose" })}>
      <Icon name="x" />
    </button>
  );
  if (state.kind === "loading")
    return (
      <div className="preview loading" data-testid="preview-card" data-state="loading">
        <div className="thumb skeleton" />
        <div className="preview-body">
          <span className="spinner" aria-hidden="true" /> {t("preview.loading")}
          <div className="muted ellipsis">{state.url}</div>
        </div>
        {close}
      </div>
    );
  if (state.kind === "error")
    return (
      <div className="preview error" data-testid="preview-card" data-state="error" role="alert">
        <div className="preview-body">
          <strong>{t("preview.failed", { error: state.message })}</strong>
          <div className="muted ellipsis">{state.url}</div>
        </div>
        {close}
      </div>
    );
  if (state.kind !== "ready") return null;
  const p = state.preview;
  const duration = formatDuration(p.duration);
  const size = formatBytes(p.filesizeApprox);
  const audio = formatBytes(p.audioBytes);
  return (
    <div className="preview" data-testid="preview-card" data-state="ready">
      {p.thumbnail ? <img className="thumb" src={p.thumbnail} alt="" referrerPolicy="no-referrer" decoding="async" /> : <div className="thumb placeholder"><Icon name={p.isPlaylist ? "sliders" : "play"} size={20} /></div>}
      <div className="preview-body">
        <strong className="ellipsis" title={p.title ?? undefined}>{p.title ?? t("preview.untitled")}</strong>
        <div className="muted">
          {[p.uploader, duration, p.isPlaylist && p.entryCount != null ? t("preview.playlist", { n: p.entryCount }) : null, size ? t("preview.size", { size }) : null]
            .filter(Boolean)
            .join("  ·  ")}
        </div>
        {(p.sizes.length > 0 || audio || p.heights.length > 0) && (
          <div className="chips">
            {p.sizes.map((s) => (
              <span key={s.height} className="chip">
                {s.height}p · {formatBytes(s.bytes)}
              </span>
            ))}
            {p.sizes.length === 0 && p.heights.slice(0, 6).map((h) => <span key={h} className="chip">{h}p</span>)}
            {audio && <span className="chip">{t("preview.audio_size", { size: audio })}</span>}
          </div>
        )}
        {p.entries.length > 0 && (
          <ol className="entries">
            {p.entries.slice(0, 50).map((e, i) => (
              <li key={e.id ?? i}>
                <span className="ellipsis">{e.title ?? e.id ?? "—"}</span>
                <span className="muted">{formatDuration(e.duration)}</span>
              </li>
            ))}
          </ol>
        )}
        {state.more > 0 && <div className="muted">{t("preview.more", { n: state.more })}</div>}
      </div>
      {close}
    </div>
  );
}
