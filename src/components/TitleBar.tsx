// The window's own title bar (the window is undecorated): mark, name, a quiet live
// summary of the queue with a thin overall-progress line along the bottom edge, the
// app's header tools, then minimise / maximise-restore / close. Idle, it is still.
import { useEffect, useState, type ReactNode } from "react";
import { t } from "../i18n";
import { api, type WindowAction } from "../lib/api";
import { queueSummary, summaryLine } from "../lib/format";
import { useData } from "../state/store";
import { Icon } from "./icons";

/** Follows the window's maximised state; also mirrored on <html data-maximized> for the CSS. */
export function useMaximized(): boolean {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    let live = true;
    let unlisten: (() => void) | undefined;
    const sync = () =>
      void api.isMaximized().then(
        (m) => live && setMaximized(m),
        () => {}
      );
    sync();
    void api.onResized(sync).then(
      (u) => (live ? (unlisten = u) : u()),
      () => {}
    );
    return () => {
      live = false;
      unlisten?.();
    };
  }, []);
  useEffect(() => {
    document.documentElement.toggleAttribute("data-maximized", maximized);
  }, [maximized]);
  return maximized;
}

/** The app mark at title-bar size (the 16 px drawing of src-tauri/icons/icon-16.svg). */
function Mark() {
  return (
    <svg className="titlebar-mark" width="18" height="18" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <rect x="0.5" y="0.5" width="15" height="15" rx="3.5" fill="#2f6fdb" />
      <path d="M4.8 4.8H11.2L8 9.4Z" fill="#fff" stroke="#fff" strokeWidth="1.6" strokeLinejoin="round" />
      <rect x="4" y="11" width="8" height="2" rx="1" fill="#fff" />
    </svg>
  );
}

export function TitleBar({ children }: { children: ReactNode }) {
  const lang = useData((s) => s.lang);
  // Primitive snapshots, so the bar re-renders only when what it shows changes.
  const summary = useData((s) => {
    const q = queueSummary(s.tasks.values());
    return q ? summaryLine(s.lang, q) : "";
  });
  const progress = useData((s) => queueSummary(s.tasks.values())?.progress ?? null);
  const maximized = useMaximized();
  const run = (action: WindowAction) => void api.windowAction(action).catch(() => {});
  const maxLabel = t(lang, maximized ? "window.restore" : "window.maximize");

  return (
    <header className="topbar" data-tauri-drag-region="deep" data-testid="titlebar" data-busy={progress != null}>
      <Mark />
      <h1 className="ellipsis">{t(lang, "header.title")}</h1>
      <span className="titlebar-summary ellipsis" data-testid="titlebar-summary">
        {summary}
      </span>
      <div className="topbar-tools">{children}</div>
      <div className="window-buttons">
        <button type="button" className="window-btn" data-testid="window-minimize" aria-label={t(lang, "window.minimize")} title={t(lang, "window.minimize")} onClick={() => run("minimize")}>
          <Icon name="minimize" />
        </button>
        <button type="button" className="window-btn" data-testid="window-maximize" aria-label={maxLabel} title={maxLabel} onClick={() => run("toggleMaximize")}>
          <Icon name={maximized ? "restore" : "maximize"} />
        </button>
        <button type="button" className="window-btn close" data-testid="window-close" aria-label={t(lang, "window.close")} title={t(lang, "window.close")} onClick={() => run("close")}>
          <Icon name="x" />
        </button>
      </div>
      {progress != null && (
        <span className="titlebar-progress" aria-hidden="true">
          <i style={{ inlineSize: `${(progress * 100).toFixed(2)}%` }} />
        </span>
      )}
    </header>
  );
}
