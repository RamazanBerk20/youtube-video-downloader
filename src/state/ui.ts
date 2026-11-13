// State that belongs to the interface alone: the URL draft, which surface is
// open, the dialog, the context menu, the preview request and the theme.
// One reducer, discriminated unions throughout; nothing here mirrors backend data.
import type { Preview } from "../lib/types";

export type ThemePref = "system" | "light" | "dark";
export type DrawerTab = "log" | "tools";
export type SettingsSection = "download" | "cookies" | "anyip" | "extras" | "appearance";

export type Dialog =
  | { kind: "noUrls" }
  | { kind: "badFolder"; error: string }
  | { kind: "openFolder"; error: string }
  | { kind: "error"; message: string }
  | { kind: "restart" }
  | { kind: "confirmClose"; activeTasks: number; resumes: boolean };

/** The surface above the main view. Settings sheet and dialogs can stack. */
export type Panel = { kind: "none" } | { kind: "settings"; section: SettingsSection | null };

export type PreviewState =
  | { kind: "idle" }
  | { kind: "loading"; url: string; seq: number }
  | { kind: "ready"; preview: Preview; more: number }
  | { kind: "error"; url: string; message: string };

export interface MenuState {
  x: number;
  y: number;
  /** Which commands make sense for the target. */
  canCut: boolean;
  canCopy: boolean;
  canPaste: boolean;
}

export interface UiState {
  draft: string;
  adding: boolean;
  panel: Panel;
  drawer: { open: boolean; tab: DrawerTab };
  dialog: Dialog | null;
  menu: MenuState | null;
  preview: PreviewState;
  theme: ThemePref;
}

export type UiAction =
  | { type: "draft"; text: string }
  | { type: "addStarted" }
  | { type: "addSucceeded" }
  | { type: "addFailed"; dialog: Dialog | null }
  | { type: "openSettings"; section?: SettingsSection }
  | { type: "closeSettings" }
  | { type: "toggleDrawer" }
  | { type: "drawerTab"; tab: DrawerTab }
  | { type: "showDialog"; dialog: Dialog }
  | { type: "closeDialog" }
  | { type: "openMenu"; menu: MenuState }
  | { type: "closeMenu" }
  | { type: "previewStart"; url: string; seq: number }
  | { type: "previewDone"; seq: number; preview: Preview; more: number }
  | { type: "previewFailed"; seq: number; message: string }
  | { type: "previewClose" }
  | { type: "theme"; theme: ThemePref }
  | { type: "escape" };

export function initialUi(theme: ThemePref = "system"): UiState {
  return {
    draft: "",
    adding: false,
    panel: { kind: "none" },
    drawer: { open: false, tab: "log" },
    dialog: null,
    menu: null,
    preview: { kind: "idle" },
    theme
  };
}

export function uiReducer(s: UiState, a: UiAction): UiState {
  switch (a.type) {
    case "draft":
      return s.draft === a.text ? s : { ...s, draft: a.text };
    case "addStarted":
      return { ...s, adding: true };
    case "addSucceeded":
      // Item 41: clear only once Add resolved; the preview belonged to that text.
      return { ...s, adding: false, draft: "", preview: { kind: "idle" } };
    case "addFailed":
      return { ...s, adding: false, dialog: a.dialog ?? s.dialog };
    case "openSettings":
      return { ...s, menu: null, panel: { kind: "settings", section: a.section ?? null } };
    case "closeSettings":
      return s.panel.kind === "none" ? s : { ...s, panel: { kind: "none" } };
    case "toggleDrawer":
      return { ...s, drawer: { ...s.drawer, open: !s.drawer.open } };
    case "drawerTab":
      return { ...s, drawer: { open: true, tab: a.tab } };
    case "showDialog":
      return { ...s, menu: null, dialog: a.dialog };
    case "closeDialog":
      return s.dialog ? { ...s, dialog: null } : s;
    case "openMenu":
      return { ...s, menu: a.menu };
    case "closeMenu":
      return s.menu ? { ...s, menu: null } : s;
    case "previewStart":
      return { ...s, preview: { kind: "loading", url: a.url, seq: a.seq } };
    case "previewDone":
      // A stale answer (the user started another preview or closed it) is dropped.
      return s.preview.kind === "loading" && s.preview.seq === a.seq ? { ...s, preview: { kind: "ready", preview: a.preview, more: a.more } } : s;
    case "previewFailed":
      return s.preview.kind === "loading" && s.preview.seq === a.seq ? { ...s, preview: { kind: "error", url: s.preview.url, message: a.message } } : s;
    case "previewClose":
      return { ...s, preview: { kind: "idle" } };
    case "theme":
      return { ...s, theme: a.theme };
    case "escape":
      // Innermost first: menu, dialog, settings sheet.
      if (s.menu) return { ...s, menu: null };
      if (s.dialog) return { ...s, dialog: null };
      if (s.panel.kind !== "none") return { ...s, panel: { kind: "none" } };
      return s;
  }
}
