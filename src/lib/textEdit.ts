// Text-editing quality of life (spec 1.3, items 30-35), installed once on the
// document so every text field and the log get it without per-field wiring.

const isWordChar = (ch: string) => ch === "_" || /[\p{L}\p{N}]/u.test(ch);
const isSpace = (ch: string) => ch.trim() === "";

/** Item 32: start of the range Ctrl+Backspace deletes: whitespace, then word chars. */
export function wordStartBefore(text: string, pos: number): number {
  let i = pos;
  while (i > 0 && isSpace(text[i - 1])) i--;
  while (i > 0 && isWordChar(text[i - 1])) i--;
  return i;
}

/** Item 33: end of the range Ctrl+Delete deletes. */
export function wordEndAfter(text: string, pos: number): number {
  let i = pos;
  while (i < text.length && isSpace(text[i])) i++;
  while (i < text.length && isWordChar(text[i])) i++;
  return i;
}

export type TextField = HTMLInputElement | HTMLTextAreaElement;

export function isTextField(el: EventTarget | null): el is TextField {
  if (el instanceof HTMLTextAreaElement) return true;
  return el instanceof HTMLInputElement && ["text", "password", "search", "url", ""].includes(el.type);
}

/** Replace [start, end) with text, keeping undo where the engine supports it, and notify React. */
export function replaceRange(el: TextField, start: number, end: number, text: string) {
  el.focus();
  el.setSelectionRange(start, end);
  let ok = false;
  try {
    ok = document.execCommand("insertText", false, text);
  } catch {
    ok = false;
  }
  if (!ok || el.value.slice(start, start + text.length) !== text) {
    el.setRangeText(text, start, end, "end");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

export function deleteWord(el: TextField, forward: boolean) {
  const a = el.selectionStart ?? 0;
  const b = el.selectionEnd ?? a;
  if (a !== b) return replaceRange(el, a, b, "");
  const [s, e] = forward ? [a, wordEndAfter(el.value, a)] : [wordStartBefore(el.value, a), a];
  if (s !== e) replaceRange(el, s, e, "");
}

/** Read-only log element: select only its own text (items 30, 35). */
export function selectContents(el: Element) {
  const sel = window.getSelection();
  if (!sel) return;
  const r = document.createRange();
  r.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(r);
}

/** Keep Tab inside a modal surface (settings sheet, dialogs). */
export function trapTab(e: { key: string; shiftKey: boolean; currentTarget: HTMLElement; preventDefault(): void }) {
  if (e.key !== "Tab") return;
  const items = [...e.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea, [tabindex='0']")];
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}
