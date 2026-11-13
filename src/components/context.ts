import { createContext, useCallback, useContext, type Dispatch } from "react";
import { t } from "../i18n";
import { useData } from "../state/store";
import type { UiAction } from "../state/ui";

export const DispatchContext = createContext<Dispatch<UiAction>>(() => {});
export const useDispatch = () => useContext(DispatchContext);

export const useLang = () => useData((s) => s.lang);

/** Translator bound to the current language; stable until the language changes. */
export function useT() {
  const lang = useLang();
  return useCallback((key: string, args?: Record<string, string | number>) => t(lang, key, args), [lang]);
}
