// Rarely-changed settings (items 22-25, 52-54, extras, theme). Opened by the
// Advanced button; never persisted open (item 21). Values apply while closed.
import { useEffect, useRef } from "react";
import { LANGUAGES, NATIVE_NAMES, optionLabel, type Lang } from "../i18n";
import { api } from "../lib/api";
import { trapTab } from "../lib/textEdit";
import { presetEnabled, subtitleLangsEnabled, videoOnlyVisible } from "../lib/options";
import { CONTAINERS, COOKIE_CHOICES, FRAGMENT_PRESETS, NETWORK_TYPES, PRESETS, parseFragments, type Settings } from "../lib/types";
import { setPassword, updateSettings, useData } from "../state/store";
import type { SettingsSection, ThemePref } from "../state/ui";
import { useDispatch, useLang, useT } from "./context";
import { Check, Combo, Field, Select, TextSetting } from "./controls";
import { Icon } from "./icons";

const THEMES: ThemePref[] = ["system", "light", "dark"];

export function SettingsSheet({ section, theme }: { section: SettingsSection | null; theme: ThemePref }) {
  const t = useT();
  const lang = useLang();
  const dispatch = useDispatch();
  const s = useData((d) => d.settings) as Settings;
  const passwordSet = useData((d) => d.passwordSet);
  const sheet = useRef<HTMLDivElement>(null);
  const label = (v: string) => optionLabel(lang, v);
  const set = (p: Partial<Settings>) => void updateSettings(p);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const target = section ? sheet.current?.querySelector<HTMLElement>(`#sec-${section}`) : null;
    target?.scrollIntoView?.({ block: "start" });
    (target?.querySelector<HTMLElement>("input,select,button") ?? sheet.current?.querySelector<HTMLElement>(".sheet-body input, .sheet-body select"))?.focus();
    return () => opener?.focus?.();
  }, [section]);

  return (
    <div className="sheet-layer">
      <div className="scrim" onClick={() => dispatch({ type: "closeSettings" })} />
      <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title" ref={sheet} data-testid="settings-sheet" onKeyDown={trapTab}>
        <header className="sheet-head">
          <h2 id="sheet-title">{t("settings.title")}</h2>
          <button type="button" className="icon-btn" aria-label={t("button.close")} data-testid="settings-close" onClick={() => dispatch({ type: "closeSettings" })}>
            <Icon name="x" />
          </button>
        </header>
        <div className="sheet-body">
          <section id="sec-download">
            <h3>{t("section.download")}</h3>
            <Field label={t("label.fragments")}>
              {(id) => (
                <Combo id={id} testId="fragments" label={t("label.fragments")} value={String(s.concurrentFragments)} presets={FRAGMENT_PRESETS.map(String)} onCommit={(v) => set({ concurrentFragments: parseFragments(v) })} />
              )}
            </Field>
            {videoOnlyVisible(s) ? (
              <div className="grid2" data-testid="video-only">
                <Field label={t("label.container")}>
                  {(id) => <Select id={id} testId="container" value={s.container} options={CONTAINERS} label={label} onChange={(container) => set({ container })} />}
                </Field>
                <Field label={t("label.reencode_preset")}>
                  {(id) => <Select id={id} testId="preset" value={s.preset} options={PRESETS} label={label} disabled={!presetEnabled(s)} onChange={(preset) => set({ preset })} />}
                </Field>
                <Check testId="reencode" checked={s.reencode} onChange={(reencode) => set({ reencode })}>
                  {t("check.reencode_h264")}
                </Check>
              </div>
            ) : (
              <p className="hint" data-testid="video-only-hint">{t("hint.video_only")}</p>
            )}
          </section>

          <section id="sec-cookies">
            <h3>{t("section.cookies")}</h3>
            <Field label={t("label.cookies")}>
              {(id) => <Select id={id} testId="cookies-browser" value={s.cookiesBrowser} options={COOKIE_CHOICES} label={label} onChange={(cookiesBrowser) => set({ cookiesBrowser })} />}
            </Field>
            <Field label={t("label.cookie_file")} wide>
              {(id) => (
                <div className="input-with-button">
                  <TextSetting id={id} testId="cookie-file" dir="ltr" value={s.cookieFile} onCommit={(cookieFile) => set({ cookieFile })} />
                  <button
                    type="button"
                    className="btn"
                    title={t("dialog.cookie_file.title")}
                    onClick={async () => {
                      const f = await api.pickCookieFile(s.cookieFile || undefined, t("dialog.cookie_file.title"), t("dialog.cookie_file.type"));
                      if (f) set({ cookieFile: f });
                    }}
                  >
                    {t("button.browse")}
                  </button>
                </div>
              )}
            </Field>
          </section>

          <section id="sec-anyip">
            <h3>{t("check.anyip_rescue")}</h3>
            <Check testId="anyip-enabled" checked={s.anyipEnabled} onChange={(anyipEnabled) => set({ anyipEnabled })}>
              {t("check.anyip_rescue")}
            </Check>
            <p className="hint">{t("hint.anyip")}</p>
            <div className="grid2">
              <Field label={t("label.anyip_username")}>
                {(id) => <TextSetting id={id} testId="anyip-username" value={s.anyipUsername} onCommit={(anyipUsername) => set({ anyipUsername })} />}
              </Field>
              <Field label={t("label.anyip_password")} hint={passwordSet ? t("hint.password_set") : undefined}>
                {(id) => (
                  <TextSetting id={id} testId="anyip-password" type="password" dirtyOnly value="" placeholder={passwordSet ? "••••••" : ""} onCommit={(v) => void setPassword(v)} />
                )}
              </Field>
              <Field label={t("label.anyip_network")}>
                {(id) => <Select id={id} testId="anyip-network" value={s.anyipNetworkType} options={NETWORK_TYPES} label={(v) => v} onChange={(anyipNetworkType) => set({ anyipNetworkType })} />}
              </Field>
              <Field label={t("label.anyip_session")}>
                {(id) => <TextSetting id={id} testId="anyip-session" value={s.anyipSessionName} onCommit={(anyipSessionName) => set({ anyipSessionName })} />}
              </Field>
            </div>
            {passwordSet && (
              <button type="button" className="btn small" data-testid="anyip-password-clear" onClick={() => void setPassword("")}>
                {t("button.remove_password")}
              </button>
            )}
          </section>

          <section id="sec-extras">
            <h3>{t("section.extras")}</h3>
            <Check testId="restore-queue" checked={s.restoreQueue} onChange={(restoreQueue) => set({ restoreQueue })}>
              {t("check.restore_queue")}
            </Check>
            <Check testId="download-archive" checked={s.useDownloadArchive} onChange={(useDownloadArchive) => set({ useDownloadArchive })}>
              {t("check.download_archive")}
            </Check>
            <Check testId="embed-subtitles" checked={s.embedSubtitles} onChange={(embedSubtitles) => set({ embedSubtitles })}>
              {t("check.embed_subtitles")}
            </Check>
            <Field label={t("label.subtitle_langs")} hint={t("hint.subtitle_langs")}>
              {(id) => <TextSetting id={id} testId="subtitle-langs" value={s.subtitleLangs} disabled={!subtitleLangsEnabled(s)} onCommit={(subtitleLangs) => set({ subtitleLangs })} />}
            </Field>
            <Check testId="embed-chapters" checked={s.embedChapters} onChange={(embedChapters) => set({ embedChapters })}>
              {t("check.embed_chapters")}
            </Check>
            <Check testId="embed-thumbnail" checked={s.embedThumbnail} onChange={(embedThumbnail) => set({ embedThumbnail })}>
              {t("check.embed_thumbnail")}
            </Check>
            <Check testId="embed-metadata" checked={s.embedMetadata} onChange={(embedMetadata) => set({ embedMetadata })}>
              {t("check.embed_metadata")}
            </Check>
            <Check testId="sponsorblock" checked={s.sponsorblockRemove} onChange={(sponsorblockRemove) => set({ sponsorblockRemove })}>
              {t("check.sponsorblock")}
            </Check>
          </section>

          <section id="sec-appearance">
            <h3>{t("section.appearance")}</h3>
            <div className="grid2">
              <Field label={t("label.theme")}>
                {(id) => <Select id={id} testId="theme" value={theme} options={THEMES} label={(v) => t(`theme.${v}`)} onChange={(v) => dispatch({ type: "theme", theme: v })} />}
              </Field>
              <Field label={t("label.language")}>
                {(id) => <Select id={id} value={(LANGUAGES as string[]).includes(lang) ? (lang as Lang) : "en"} options={LANGUAGES} label={(v) => NATIVE_NAMES[v]} onChange={(language) => set({ language })} />}
              </Field>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
