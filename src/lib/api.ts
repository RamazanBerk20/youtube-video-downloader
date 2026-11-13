// Typed wrappers over every backend command and the single event channel.
// Outside Tauri (plain browser, tests, agents) the in-browser mock backend is
// used automatically, so the UI can be built and driven without Rust.

import type {
  AddError,
  AppEvent,
  BannerKind,
  Bootstrap,
  Preview,
  Settings,
  Tool,
  ToolsStatus,
  UpdateState
} from "./types";

export const EVENT = "app-event";

export const isTauri = (): boolean => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

type Invoke = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
type Listen = (handler: (e: AppEvent) => void) => Promise<() => void>;

interface Transport {
  invoke: Invoke;
  listen: Listen;
  pickFolder(initial?: string): Promise<string | null>;
  pickCookieFile(initial: string | undefined, title: string, typeName: string): Promise<string | null>;
  readClipboard(): Promise<string | null>;
  setWindowTitle(title: string): Promise<void>;
  /** null = follow the system. */
  setWindowTheme(theme: "light" | "dark" | null): Promise<void>;
}

let transport: Promise<Transport> | null = null;

async function tauriTransport(): Promise<Transport> {
  const core = await import("@tauri-apps/api/core");
  const event = await import("@tauri-apps/api/event");
  const dialog = await import("@tauri-apps/plugin-dialog");
  const clipboard = await import("@tauri-apps/plugin-clipboard-manager");
  const win = await import("@tauri-apps/api/window");
  return {
    invoke: core.invoke,
    listen: (handler) => event.listen<AppEvent>(EVENT, (e) => handler(e.payload)),
    pickFolder: async (initial) => {
      const r = await dialog.open({ directory: true, multiple: false, defaultPath: initial });
      return typeof r === "string" ? r : null;
    },
    pickCookieFile: async (initial, title, typeName) => {
      const r = await dialog.open({
        multiple: false,
        title,
        defaultPath: initial,
        // app.py:1749-1761: localised title and type, "All files" never localised.
        filters: [
          { name: typeName, extensions: ["txt", "cookies"] },
          { name: "All files", extensions: ["*"] }
        ]
      });
      return typeof r === "string" ? r : null;
    },
    readClipboard: async () => {
      try {
        return await clipboard.readText();
      } catch {
        return null;
      }
    },
    setWindowTitle: (title) => win.getCurrentWindow().setTitle(title),
    setWindowTheme: (theme) => win.getCurrentWindow().setTheme(theme).catch(() => {})
  };
}

function getTransport(): Promise<Transport> {
  transport ??= isTauri() ? tauriTransport() : import("./mockBackend").then((m) => m.mockTransport());
  return transport;
}

const call = async <T>(cmd: string, args?: Record<string, unknown>): Promise<T> => (await getTransport()).invoke<T>(cmd, args);

/** Subscribe to every backend event. Returns an unsubscribe function. */
export async function onEvent(handler: (e: AppEvent) => void): Promise<() => void> {
  return (await getTransport()).listen(handler);
}

/** Subscribe to one event type. */
export async function on<K extends AppEvent["type"]>(
  type: K,
  handler: (e: Extract<AppEvent, { type: K }>) => void
): Promise<() => void> {
  return onEvent((e) => {
    if (e.type === type) handler(e as Extract<AppEvent, { type: K }>);
  });
}

export const api = {
  /** Everything needed for the first render; subscribe to events first. */
  getBootstrap: () => call<Bootstrap>("get_bootstrap"),
  /** Partial update; saved at once; maxConcurrent applies to the running queue. Rejects unknown keys/invalid values. */
  updateSettings: (patch: Partial<Settings>) => call<Settings>("update_settings", { patch }),
  /** Stores in the OS keyring (0600 file fallback); "" clears. Resolves to "a password is stored". */
  setAnyipPassword: (password: string) => call<boolean>("set_anyip_password", { password }),
  /** Add: validates URLs, creates the folder, snapshots current settings. Rejects with AddError. */
  addTasks: (text: string) => call<number[]>("add_tasks", { text }) as Promise<number[]>,
  cancelTask: (id: number) => call<void>("cancel_task", { id }),
  /** Removes a done/failed/cancelled row; false otherwise. */
  removeTask: (id: number) => call<boolean>("remove_task", { id }),
  /** Re-queue a failed/cancelled task. */
  retryTask: (id: number) => call<boolean>("retry_task", { id }),
  cancelAll: () => call<void>("cancel_all"),
  clearFinished: () => call<number[]>("clear_finished"),
  /** Opens this session's last Add folder, else the setting; no-op if missing. */
  openOutputFolder: () => call<void>("open_output_folder"),
  /** Metadata before queueing (title, thumbnail, duration, sizes, playlist count). */
  previewUrl: (url: string) => call<Preview>("preview_url", { url }),
  dismissBanner: (kind: BannerKind) => call<void>("dismiss_banner", { kind }),
  getToolsStatus: () => call<ToolsStatus>("get_tools_status"),
  /** Banner Install button; progress arrives as log + install events. */
  installTool: (tool: Tool) => call<void>("install_tool", { tool }),
  /** Check and install a newer yt-dlp; resolves to the new version or null. */
  updateYtdlp: () => call<string | null>("update_ytdlp"),
  /** "Check for updates" button (logged). */
  checkAppUpdate: () => call<UpdateState>("check_app_update"),
  /** "Update now" when the appUpdate banner has canApply. */
  applyAppUpdate: () => call<UpdateState>("apply_app_update"),
  /** "Update now" when canApply is false. */
  openReleasePage: () => call<void>("open_release_page"),
  /** Quiet "Support development" link in the Tools tab. */
  openSponsorPage: () => call<void>("open_sponsor_page"),
  /** After the restart prompt is answered yes. */
  restartApp: () => call<void>("restart_app"),
  /** After a closeRequested event is confirmed. */
  confirmExit: () => call<void>("confirm_exit"),

  pickFolder: async (initial?: string) => (await getTransport()).pickFolder(initial),
  pickCookieFile: async (initial: string | undefined, title: string, typeName: string) => (await getTransport()).pickCookieFile(initial, title, typeName),
  readClipboard: async () => (await getTransport()).readClipboard(),
  setWindowTitle: async (title: string) => (await getTransport()).setWindowTitle(title),
  /** Native title bar / webview colour scheme for the manual theme override. */
  setWindowTheme: async (theme: "light" | "dark" | null) => (await getTransport()).setWindowTheme(theme)
};

/** Narrow a rejected addTasks() value. */
export function asAddError(e: unknown): AddError | null {
  if (e && typeof e === "object" && "kind" in e) {
    const k = (e as { kind: unknown }).kind;
    if (k === "noUrls" || k === "badFolder") return e as AddError;
  }
  return null;
}
