// One window, no navigation: the title bar (window chrome + header tools), banners, composer + options, the queue as
// the main area, and the log/tools drawer. Backend data comes from the store;
// the UI's own state is the reducer in state/ui.ts.
import { useCallback, useEffect, useReducer } from "react";
import { LANGUAGES, NATIVE_NAMES, t, type Lang } from "./i18n";
import { api, isTauri } from "./lib/api";
import { deleteWord, isTextField, replaceRange, selectContents } from "./lib/textEdit";
import { getState, retryBootstrap, start, updateSettings, useData, type Signal } from "./state/store";
import { initialUi, uiReducer, type ThemePref, type UiAction } from "./state/ui";
import { Banners } from "./components/Banners";
import { Composer } from "./components/Composer";
import { DispatchContext } from "./components/context";
import { ContextMenu, DialogView, setMenuTarget } from "./components/Dialogs";
import { Drawer } from "./components/Drawer";
import { Icon } from "./components/icons";
import { OptionsBar } from "./components/OptionsBar";
import { Queue } from "./components/Queue";
import { SettingsSheet } from "./components/SettingsSheet";
import { TitleBar } from "./components/TitleBar";

const THEME_KEY = "ytdl.theme";
const THEME_ORDER: ThemePref[] = ["system", "light", "dark"];

function storedTheme(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function App() {
  const [ui, dispatch] = useReducer(uiReducer, undefined, () => initialUi(storedTheme()));
  const phase = useData((s) => s.phase);
  const error = useData((s) => s.error);
  const hasSettings = useData((s) => s.settings != null);
  const chosen = useData((s) => s.settings?.language || "en");
  const language = (LANGUAGES as string[]).includes(chosen) ? chosen : "en";
  const lang = useData((s) => s.lang);

  // Subscribe, then bootstrap; backend signals become dialogs.
  useEffect(() => {
    const onSignal = (s: Signal) => {
      if (s.type === "closeRequested") dispatch({ type: "showDialog", dialog: { kind: "confirmClose", activeTasks: s.activeTasks, resumes: !!getState().settings?.restoreQueue } });
      else if (s.type === "restartReady") dispatch({ type: "showDialog", dialog: { kind: "restart" } });
      else dispatch({ type: "showDialog", dialog: { kind: "error", message: t(lang, "msg.settings_failed", { error: s.error }) } });
    };
    void start(onSignal);
  }, [lang]);

  // Language: document lang/dir (full RTL by direction alone) and window title (item 8).
  useEffect(() => {
    const el = document.documentElement;
    el.lang = lang;
    el.dir = lang === "ar" ? "rtl" : "ltr";
    void api.setWindowTitle(t(lang, "app.title"));
  }, [lang]);

  // Theme: follows the system unless overridden; remembered per machine.
  useEffect(() => {
    const el = document.documentElement;
    if (ui.theme === "system") el.removeAttribute("data-theme");
    else el.dataset.theme = ui.theme;
    try {
      localStorage.setItem(THEME_KEY, ui.theme);
    } catch {
      /* private mode: the choice lasts for this session */
    }
    void api.setWindowTheme(ui.theme === "system" ? null : ui.theme);
  }, [ui.theme]);

  // Global keys: Escape, text-editing shortcuts (items 30-33) and the context menu (34-35).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") return dispatch({ type: "escape" });
      const ctrl = e.ctrlKey || e.metaKey;
      if (!ctrl) return;
      const el = e.target;
      if (isTextField(el)) {
        if (e.key === "Backspace" || e.key === "Delete") {
          e.preventDefault();
          deleteWord(el, e.key === "Delete");
        } else if (e.shiftKey && e.key.toLowerCase() === "v") {
          e.preventDefault();
          const a = el.selectionStart ?? 0;
          const b = el.selectionEnd ?? a;
          void api.readClipboard().then((clip) => clip != null && replaceRange(el, a, b, clip));
        } else if (e.shiftKey && e.key.toLowerCase() === "a") {
          e.preventDefault();
          el.select();
        }
      } else if (el instanceof HTMLElement && el.closest("[data-log]") && e.key.toLowerCase() === "a") {
        e.preventDefault();
        selectContents(el.closest("[data-log]")!);
      }
    };
    const onMenu = (e: MouseEvent) => {
      const el = e.target as HTMLElement;
      const field = isTextField(el) ? el : null;
      const log = el.closest?.("[data-log]") as HTMLElement | null;
      if (!field && !log) return;
      e.preventDefault();
      setMenuTarget(field ?? log);
      const hasSel = field ? (field.selectionEnd ?? 0) > (field.selectionStart ?? 0) : !!window.getSelection()?.toString();
      const editable = !!field && !field.readOnly && !field.disabled;
      const secret = field?.type === "password";
      dispatch({ type: "openMenu", menu: { x: e.clientX, y: e.clientY, canCut: editable && hasSel && !secret, canCopy: hasSel && !secret, canPaste: editable } });
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("contextmenu", onMenu);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("contextmenu", onMenu);
    };
  }, []);

  const stableDispatch = useCallback((a: UiAction) => dispatch(a), []);
  const settingsOpen = ui.panel.kind === "settings";

  return (
    <DispatchContext.Provider value={stableDispatch}>
      <div
        className="app"
        data-testid="app"
        data-state={phase}
        data-panel={ui.panel.kind}
        data-dialog={ui.dialog?.kind ?? "none"}
        data-drawer={ui.drawer.open ? ui.drawer.tab : "closed"}
        data-theme-pref={ui.theme}
        data-backend={isTauri() ? "tauri" : "mock"}
      >
        <TitleBar>
          <span className="select compact">
            <select aria-label={t(lang, "label.language")} data-testid="language" value={language} disabled={!hasSettings} onChange={(e) => void updateSettings({ language: e.target.value as Lang })}>
              {LANGUAGES.map((l) => (
                <option key={l} value={l} lang={l}>
                  {NATIVE_NAMES[l]}
                </option>
              ))}
            </select>
            <Icon name="chevronDown" size={14} />
          </span>
          <button
            type="button"
            className="icon-btn"
            data-testid="theme-toggle"
            aria-label={`${t(lang, "label.theme")}: ${t(lang, `theme.${ui.theme}`)}`}
            title={`${t(lang, "label.theme")}: ${t(lang, `theme.${ui.theme}`)}`}
            onClick={() => dispatch({ type: "theme", theme: THEME_ORDER[(THEME_ORDER.indexOf(ui.theme) + 1) % 3] })}
          >
            <Icon name={ui.theme === "light" ? "sun" : ui.theme === "dark" ? "moon" : "monitor"} />
          </button>
          <button type="button" className="icon-btn" data-testid="settings-open" aria-label={t(lang, "settings.title")} title={t(lang, "settings.title")} disabled={!hasSettings} onClick={() => dispatch(settingsOpen ? { type: "closeSettings" } : { type: "openSettings" })}>
            <Icon name="gear" />
          </button>
        </TitleBar>

        <Banners />

        {phase === "error" ? (
          <div className="fatal" role="alert" data-testid="bootstrap-error">
            <p>{t(lang, "error.backend", { error: error ?? "" })}</p>
            <button type="button" className="btn primary" onClick={() => void retryBootstrap()}>
              {t(lang, "button.try_again")}
            </button>
          </div>
        ) : (
          <>
            <Composer draft={ui.draft} adding={ui.adding || !hasSettings} preview={ui.preview} />
            {hasSettings ? <OptionsBar settingsOpen={settingsOpen} /> : <div className="options skeleton-block" aria-hidden="true" />}
          </>
        )}

        <Queue />
        <Drawer open={ui.drawer.open} tab={ui.drawer.tab} />

        {settingsOpen && hasSettings && <SettingsSheet section={ui.panel.kind === "settings" ? ui.panel.section : null} theme={ui.theme} />}
        {ui.dialog && <DialogView key={ui.dialog.kind} dialog={ui.dialog} />}
        {ui.menu && <ContextMenu menu={ui.menu} />}
      </div>
    </DispatchContext.Provider>
  );
}
