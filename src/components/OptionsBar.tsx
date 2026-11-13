// The common options on the main surface: mode, quality, codec, playlist,
// Advanced (opens the settings sheet), and the output folder (items 17-21, 43-57).
import { memo } from "react";
import { optionLabel } from "../i18n";
import { api } from "../lib/api";
import { codecPatch, codecsFor, currentCodec, modePatch, qualitiesFor, qualityEnabled } from "../lib/options";
import type { Mode, Settings } from "../lib/types";
import { updateSettings, useData } from "../state/store";
import { useDispatch, useLang, useT } from "./context";
import { Check, Field, Select, TextSetting } from "./controls";
import { Icon } from "./icons";

export const OptionsBar = memo(function OptionsBar({ settingsOpen }: { settingsOpen: boolean }) {
  const t = useT();
  const lang = useLang();
  const dispatch = useDispatch();
  const s = useData((d) => d.settings) as Settings;
  const defaultDir = useData((d) => d.defaultOutputDir);
  const label = (v: string) => optionLabel(lang, v);

  return (
    <section className="options" aria-label={t("label.format")}>
      <div className="options-row">
        <div className="field">
          <span className="label" id="mode-label">{t("label.format")}</span>
          <div className="segmented" role="radiogroup" aria-labelledby="mode-label" data-testid="mode">
            {(["video", "audio"] as Mode[]).map((m) => (
              <label key={m} className={s.mode === m ? "on" : undefined}>
                <input type="radio" name="mode" value={m} checked={s.mode === m} data-testid={`mode-${m}`} onChange={() => void updateSettings(modePatch(s, m))} />
                {t(`radio.${m}`)}
              </label>
            ))}
          </div>
        </div>
        <Field label={t("label.quality")}>
          {(id) => <Select id={id} testId="quality" value={s.quality as string} options={qualitiesFor(s.mode)} label={label} disabled={!qualityEnabled(s)} onChange={(quality) => void updateSettings({ quality: quality as Settings["quality"] })} />}
        </Field>
        <Field label={t("label.codec")}>
          {(id) => <Select id={id} testId="codec" value={currentCodec(s)} options={codecsFor(s.mode)} label={label} onChange={(c) => void updateSettings(codecPatch(s, c))} />}
        </Field>
        <div className="field end">
          <Check testId="playlist" checked={s.playlist} onChange={(playlist) => void updateSettings({ playlist })}>
            {t("check.playlist")}
          </Check>
        </div>
        <button
          type="button"
          className={settingsOpen ? "btn subtle on" : "btn subtle"}
          data-testid="advanced"
          aria-expanded={settingsOpen}
          onClick={() => dispatch(settingsOpen ? { type: "closeSettings" } : { type: "openSettings" })}
        >
          <Icon name="sliders" /> {t(settingsOpen ? "button.hide_advanced" : "button.advanced")}
        </button>
      </div>
      <div className="options-row folder">
        <Field label={t("field.save_to")} wide>
          {(id) => (
            <div className="input-with-button">
              <TextSetting id={id} testId="output-dir" dir="ltr" value={s.outputDir} placeholder={defaultDir} onCommit={(outputDir) => void updateSettings({ outputDir })} />
              <button
                type="button"
                className="btn"
                data-testid="browse-folder"
                onClick={async () => {
                  const dir = await api.pickFolder(s.outputDir || defaultDir);
                  if (dir) void updateSettings({ outputDir: dir });
                }}
              >
                <Icon name="folder" /> {t("button.browse")}
              </button>
              <button
                type="button"
                className="btn"
                data-testid="open-folder"
                onClick={() => api.openOutputFolder().catch((e) => dispatch({ type: "showDialog", dialog: { kind: "openFolder", error: String(e) } }))}
              >
                {t("button.open_folder")}
              </button>
            </div>
          )}
        </Field>
      </div>
    </section>
  );
});
