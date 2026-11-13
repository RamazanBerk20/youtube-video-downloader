// URL box parsing for the live hint and the preview. Mirrors args::extract_urls
// (the backend still validates on Add): trim lines, strip control characters,
// drop empties, keep http(s) only, no de-dup.

export interface UrlScan {
  urls: string[];
  /** Non-empty lines that will be dropped. */
  skipped: number;
}

// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/g;

export function scanUrls(text: string): UrlScan {
  const urls: string[] = [];
  let skipped = 0;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(CONTROL, "").trim();
    if (!line) continue;
    if (/^https?:\/\//i.test(line)) urls.push(line);
    else skipped++;
  }
  return { urls, skipped };
}

/** The URL on the caret's line if it is one, else the first URL. */
export function urlAtCaret(text: string, caret: number): string | null {
  const start = text.lastIndexOf("\n", caret - 1) + 1;
  const end = text.indexOf("\n", caret);
  const line = scanUrls(text.slice(start, end < 0 ? undefined : end)).urls[0];
  return line ?? scanUrls(text).urls[0] ?? null;
}

/** Paste button (item 14): append on a new line, then a trailing newline. */
export function appendPasted(current: string, clip: string | null): string {
  const c = clip?.trim();
  if (!c) return current;
  // app.py:1738-1741: existing text is stripped, then joined by one newline.
  const existing = current.trim();
  return `${existing ? existing + "\n" : ""}${c}\n`;
}
