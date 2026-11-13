// Collapsible bottom drawer: session log (items 29, 35, 90) and tools/updates
// (items 26, 101-105 and the yt-dlp status). Nothing inside renders while closed.
import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { logLine } from "../lib/format";
import type { LogEntry, ToolsStatus } from "../lib/types";
import { getState, updateSettings, useData } from "../state/store";
import type { DrawerTab } from "../state/ui";
import { useDispatch, useLang, useT } from "./context";
import { Check } from "./controls";
import { Icon } from "./icons";

export const Drawer = memo(function Drawer({ open, tab }: { open: boolean; tab: DrawerTab }) {
  const t = useT();
  const dispatch = useDispatch();
  const tabs: DrawerTab[] = ["log", "tools"];
  return (
    <section className={open ? "drawer open" : "drawer"} data-testid="drawer" data-state={open ? tab : "closed"}>
      <header className="drawer-head">
        <div role="tablist" aria-label={t("label.log")}>
          {tabs.map((k) => (
            <button
              key={k}
              type="button"
              role="tab"
              id={`tab-${k}`}
              aria-selected={open && tab === k}
              aria-controls="drawer-panel"
              data-testid={`drawer-tab-${k}`}
              className={open && tab === k ? "tab on" : "tab"}
              onClick={() => dispatch(open && tab === k ? { type: "toggleDrawer" } : { type: "drawerTab", tab: k })}
            >
              <Icon name={k === "log" ? "terminal" : "gear"} size={14} /> {t(k === "log" ? "label.log" : "label.tools")}
            </button>
          ))}
        </div>
        <button type="button" className="icon-btn" data-testid="drawer-toggle" aria-expanded={open} aria-label={t(open ? "drawer.hide" : "drawer.show")} title={t(open ? "drawer.hide" : "drawer.show")} onClick={() => dispatch({ type: "toggleDrawer" })}>
          <Icon name={open ? "chevronDown" : "chevronUp"} />
        </button>
      </header>
      {open && (
        <div className="drawer-panel" id="drawer-panel" role="tabpanel" aria-labelledby={`tab-${tab}`}>
          {tab === "log" ? <Log /> : <Tools />}
        </div>
      )}
    </section>
  );
});

const LogRow = memo(function LogRow({ e, lang }: { e: LogEntry; lang: string }) {
  return <div className={`log-line l-${e.level}`}>{logLine(lang, e)}</div>;
});

function Log() {
  const t = useT();
  const lang = useLang();
  useData((s) => s.logVersion); // re-render when the log grows
  const log = getState().log;
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight; // auto-scroll unless the user scrolled up
  });
  return (
    <div
      className="log"
      ref={box}
      tabIndex={0}
      data-log=""
      data-testid="log"
      role="log"
      aria-label={t("label.log")}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
    >
      {log.length === 0 ? <div className="muted">{t("log.empty")}</div> : log.map((e) => <LogRow key={e.seq} e={e} lang={lang} />)}
    </div>
  );
}

function Tools() {
  const t = useT();
  const lang = useLang();
  const y = useData((s) => s.ytdlp);
  const app = useData((s) => s.appUpdate);
  const version = useData((s) => s.version);
  const installs = useData((s) => s.installs);
  const autoUpdate = useData((s) => s.settings?.autoUpdateCheck ?? true);
  const ytAuto = useData((s) => s.settings?.ytdlpAutoUpdate ?? true);
  const [tools, setTools] = useState<ToolsStatus | null>(null);
  const refresh = () => {
    setTools(null);
    void api.getToolsStatus().then(setTools);
  };
  useEffect(refresh, [installs]);

  const pct = (p: number | null) => (p == null ? "" : `${p.toFixed(0)}%`);
  const ytPhase = (() => {
    if (!y) return "";
    switch (y.phase) {
      case "checking":
        return t("tools.checking");
      case "downloading":
        return t("tools.downloading", { percent: pct(y.percent) });
      case "upToDate":
        return t("tools.up_to_date");
      case "updated":
        return t("tools.updated", { version: y.version });
      case "failed":
        return t("tools.failed", { error: y.error });
      default:
        return "";
    }
  })();
  const appPhase = (() => {
    switch (app.phase) {
      case "checking":
        return t("log.update_checking");
      case "upToDate":
        return t("log.update_uptodate");
      case "available":
        return t("update.available", { version: app.latest });
      case "downloading":
        return t("update.downloading", { percent: pct(app.percent) });
      case "readyToRestart":
        return t("update.ready", { version: app.version });
      case "failed":
        return t("log.update_failed", { error: app.error });
      default:
        return "";
    }
  })();
  const installing = (tool: "ffmpeg" | "deno") => installs[tool]?.state === "running";
  const lastCheck = y?.lastCheckMs ? new Date(y.lastCheckMs).toLocaleString(lang) : null;

  return (
    <div className="tools" data-testid="tools">
      <div className="tool">
        <div className="tool-name">yt-dlp</div>
        <div className="tool-info" data-testid="tool-ytdlp">
          <span>{y?.version ?? t("tools.not_found")}</span>
          {y && <span className="muted">{t(`tools.source.${y.source}`)}</span>}
          {y?.latest && y.latest !== y.version && <span className="muted">{t("tools.latest", { version: y.latest })}</span>}
          {lastCheck && <span className="muted">{t("tools.last_check", { time: lastCheck })}</span>}
          {ytPhase && <span className={y?.phase === "failed" ? "bad" : "muted"}>{ytPhase}</span>}
        </div>
        <div className="tool-actions">
          <Check checked={ytAuto} onChange={(ytdlpAutoUpdate) => void updateSettings({ ytdlpAutoUpdate })}>
            {t("check.ytdlp_auto_update")}
          </Check>
          <button type="button" className="btn small" data-testid="update-ytdlp" disabled={y?.phase === "checking" || y?.phase === "downloading"} onClick={() => void api.updateYtdlp().catch(() => {})}>
            {t("button.update_ytdlp")}
          </button>
        </div>
      </div>
      <div className="tool">
        <div className="tool-name">ffmpeg</div>
        <div className="tool-info" data-testid="tool-ffmpeg">
          {tools ? (
            <>
              <span dir="ltr" className={tools.ffmpeg ? undefined : "bad"}>{tools.ffmpeg ?? t("tools.not_found")}</span>
              <span className="muted" dir="ltr">ffprobe: {tools.ffprobe ?? t("tools.not_found")}</span>
            </>
          ) : (
            <span className="spinner" aria-hidden="true" />
          )}
          {installing("ffmpeg") && <span className="muted">{t("tools.installing", { package: "ffmpeg" })}</span>}
          {installs.ffmpeg?.state === "failed" && <span className="bad">{t("log.install_failed", { error: installs.ffmpeg.error })}</span>}
        </div>
        <div className="tool-actions">
          {tools && !tools.ffmpeg && (
            <button type="button" className="btn small" disabled={installing("ffmpeg")} onClick={() => void api.installTool("ffmpeg").catch(() => {})}>
              {t("button.install")}
            </button>
          )}
        </div>
      </div>
      <div className="tool">
        <div className="tool-name">{t("tools.js_runtime")}</div>
        <div className="tool-info" data-testid="tool-js">
          {tools ? <span className={tools.jsRuntimes.length ? undefined : "bad"}>{tools.jsRuntimes.join(", ") || t("tools.not_found")}</span> : <span className="spinner" aria-hidden="true" />}
          {installing("deno") && <span className="muted">{t("tools.installing", { package: "deno" })}</span>}
          {installs.deno?.state === "failed" && <span className="bad">{t("log.install_failed", { error: installs.deno.error })}</span>}
        </div>
        <div className="tool-actions">
          {tools && tools.jsRuntimes.length === 0 && (
            <button type="button" className="btn small" disabled={installing("deno")} onClick={() => void api.installTool("deno").catch(() => {})}>
              {t("button.install")}
            </button>
          )}
          <button type="button" className="btn small" onClick={refresh}>
            {t("button.refresh")}
          </button>
        </div>
      </div>
      <div className="tool">
        <div className="tool-name">{t("app.title")}</div>
        <div className="tool-info" data-testid="tool-app">
          <span>{t("label.app_version", { version })}</span>
          {appPhase && <span className={app.phase === "failed" ? "bad" : "muted"}>{appPhase}</span>}
        </div>
        <div className="tool-actions">
          <Check testId="auto-update" checked={autoUpdate} onChange={(autoUpdateCheck) => void updateSettings({ autoUpdateCheck })}>
            {t("check.auto_update")}
          </Check>
          {app.phase === "available" ? (
            <button type="button" className="btn small solid" onClick={() => void (app.canApply ? api.applyAppUpdate() : api.openReleasePage())}>
              {t("button.update_now")}
            </button>
          ) : app.phase === "readyToRestart" ? (
            <button type="button" className="btn small solid" onClick={() => void api.restartApp()}>
              {t("button.restart")}
            </button>
          ) : (
            <button type="button" className="btn small" data-testid="check-updates" disabled={app.phase === "checking" || app.phase === "downloading"} onClick={() => void api.checkAppUpdate()}>
              {t("button.check_updates")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
