// Message catalogs: the 106 keys converted from the old i18n.py plus the
// rewrite's own strings, in all 10 languages. Arabic is NOT pre-shaped: the
// webview shapes bidi natively; the app sets dir="rtl" for "ar".

import en from "./locales/en.json";
import languages from "./locales/languages.json";

export type MessageKey = keyof typeof en;
export type Lang = "en" | "tr" | "es" | "fr" | "de" | "ru" | "ar" | "zh" | "ja" | "ko";

// English is bundled (it is the fallback); the other nine load on demand so
// the first paint does not carry ~100 KB of tables nobody is reading.
const loaders = import.meta.glob<{ default: Record<string, string> }>(["./locales/*.json", "!./locales/en.json", "!./locales/languages.json"]);
const loaded: Partial<Record<Lang, Record<string, string>>> = { en };

/** Load a language's catalog; resolves at once when it is already there or unknown. */
export async function loadLanguage(lang: string): Promise<void> {
  if (!isLang(lang) || loaded[lang]) return;
  loaded[lang] = (await loaders[`./locales/${lang}.json`]()).default;
}
export const isLoaded = (lang: string): boolean => !isLang(lang) || !!loaded[lang];

/** Picker order (spec 5.1). */
export const LANGUAGES = languages.order as Lang[];
/** Native names shown in the language picker. */
export const NATIVE_NAMES = languages.nativeNames as Record<Lang, string>;

const isLang = (l: string): l is Lang => (LANGUAGES as string[]).includes(l);

/**
 * Lookup: current table, else English, else the key itself; `{name}`
 * placeholders are filled, unknown ones left as-is (i18n.py:1192-1204).
 */
export function t(lang: string, key: string, args: Record<string, string | number> = {}): string {
  const table = (isLang(lang) && loaded[lang]) || (en as Record<string, string>);
  const raw = table[key] ?? (en as Record<string, string>)[key] ?? key;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) => (name in args ? String(args[name]) : m));
}

/** Display string for an option key: only LOCALIZED_OPTIONS are translated. */
export function optionLabel(lang: string, key: string): string {
  return (languages.localizedOptions as string[]).includes(key) ? t(lang, `option.${key.toLowerCase()}`) : key;
}
