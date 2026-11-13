// Modal dialogs (items 38, 39, 74, 107, 108) and the text context menu (34, 35).
import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import { isTextField, replaceRange, selectContents, trapTab } from "../lib/textEdit";
import type { Dialog, MenuState } from "../state/ui";
import { useDispatch, useT } from "./context";

export function DialogView({ dialog }: { dialog: Dialog }) {
  const t = useT();
  const dispatch = useDispatch();
  const primary = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    primary.current?.focus();
    return () => opener?.focus?.();
  }, []);
  const close = () => dispatch({ type: "closeDialog" });

  let title: string;
  let body: string;
  let tone: "warning" | "danger" | "info" = "danger";
  let buttons: { label: string; run: () => void; kind?: string }[] = [{ label: t("button.ok"), run: close, kind: "primary" }];
  switch (dialog.kind) {
    case "noUrls":
      title = t("msg.no_url.title");
      body = t("msg.no_url.body");
      tone = "warning";
      break;
    case "badFolder":
      title = t("msg.bad_folder.title");
      body = t("msg.bad_folder.body", { error: dialog.error });
      break;
    case "openFolder":
      title = "Open folder"; // item 74: not localised
      body = dialog.error;
      break;
    case "error":
      title = t("msg.error.title");
      body = dialog.message;
      break;
    case "restart":
      title = t("msg.restart.title");
      body = t("msg.restart.body");
      tone = "info";
      buttons = [
        { label: t("button.later"), run: close },
        { label: t("button.restart_now"), run: () => void api.restartApp().catch(close), kind: "primary" }
      ];
      break;
    case "confirmClose":
      title = t("msg.close.title");
      body = t(dialog.resumes ? "msg.close.body" : "msg.close.body_stop", { n: dialog.activeTasks });
      tone = "warning";
      buttons = [
        { label: t("button.cancel"), run: close, kind: "primary" },
        { label: t("button.quit"), run: () => void api.confirmExit(), kind: "danger" }
      ];
      break;
  }
  return (
    <div className="modal-layer">
      <div className="scrim" onClick={close} />
      <div className={`modal ${tone}`} role="alertdialog" aria-modal="true" aria-labelledby="dlg-title" aria-describedby="dlg-body" data-testid={`dialog-${dialog.kind}`} onKeyDown={trapTab}>
        <h2 id="dlg-title">{title}</h2>
        <p id="dlg-body">{body}</p>
        <div className="modal-actions">
          {buttons.map((b) => (
            <button key={b.label} type="button" ref={b.kind === "primary" ? primary : undefined} className={`btn ${b.kind ?? ""}`} onClick={b.run}>
              {b.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/** Target of the open context menu (the field or the log element). */
let menuTarget: HTMLElement | null = null;
export const setMenuTarget = (el: HTMLElement | null) => (menuTarget = el);

export function ContextMenu({ menu }: { menu: MenuState }) {
  const t = useT();
  const dispatch = useDispatch();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const close = (e: Event) => {
      if (!ref.current?.contains(e.target as Node)) dispatch({ type: "closeMenu" });
    };
    const blur = () => dispatch({ type: "closeMenu" });
    document.addEventListener("mousedown", close, true);
    window.addEventListener("blur", blur);
    window.addEventListener("resize", blur);
    return () => {
      document.removeEventListener("mousedown", close, true);
      window.removeEventListener("blur", blur);
      window.removeEventListener("resize", blur);
    };
  }, [dispatch]);

  const run = async (cmd: "cut" | "copy" | "paste" | "selectAll") => {
    const el = menuTarget;
    dispatch({ type: "closeMenu" });
    if (!el) return;
    if (isTextField(el)) {
      const a = el.selectionStart ?? 0;
      const b = el.selectionEnd ?? a;
      const sel = el.value.slice(a, b);
      if (cmd === "selectAll") return el.focus(), el.select();
      if (cmd === "copy" || cmd === "cut") {
        await navigator.clipboard?.writeText(sel).catch(() => document.execCommand("copy"));
        if (cmd === "cut" && !el.readOnly) replaceRange(el, a, b, "");
        return;
      }
      const clip = await api.readClipboard();
      if (clip != null) replaceRange(el, a, b, clip);
      return;
    }
    if (cmd === "selectAll") return selectContents(el);
    if (cmd === "copy") {
      const text = window.getSelection()?.toString() ?? "";
      await navigator.clipboard?.writeText(text).catch(() => document.execCommand("copy"));
    }
  };

  // Keep the menu inside the window.
  const x = Math.min(menu.x, window.innerWidth - 180);
  const y = Math.min(menu.y, window.innerHeight - 170);
  return (
    <div
      className="menu"
      role="menu"
      ref={ref}
      data-testid="context-menu"
      style={{ insetInlineStart: document.dir === "rtl" ? window.innerWidth - x : x, insetBlockStart: y }}
      onKeyDown={(e) => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          const items = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
          const i = items.indexOf(document.activeElement as HTMLButtonElement);
          items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
        }
      }}
    >
      <button type="button" role="menuitem" disabled={!menu.canCut} onClick={() => void run("cut")}>{t("menu.cut")}</button>
      <button type="button" role="menuitem" disabled={!menu.canCopy} onClick={() => void run("copy")}>{t("menu.copy")}</button>
      <button type="button" role="menuitem" disabled={!menu.canPaste} onClick={() => void run("paste")}>{t("menu.paste")}</button>
      <hr />
      <button type="button" role="menuitem" onClick={() => void run("selectAll")}>{t("menu.select_all")}</button>
    </div>
  );
}
