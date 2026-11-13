// Option inter-dependencies (spec 6.7, PARITY items 23, 46, 49, 50, 52, 54).
// Pure functions so they are tested once and every control agrees.
import type { Mode, Settings } from "./types";
import { AUDIO_CODECS, AUDIO_CODECS_WITH_BITRATE, AUDIO_QUALITIES, VIDEO_CODECS, VIDEO_QUALITIES } from "./types";

export const qualitiesFor = (mode: Mode): readonly string[] => (mode === "audio" ? AUDIO_QUALITIES : VIDEO_QUALITIES);
export const codecsFor = (mode: Mode): readonly string[] => (mode === "audio" ? AUDIO_CODECS : VIDEO_CODECS);

/** Item 49: the codec of the current mode. */
export const currentCodec = (s: Settings): string => (s.mode === "audio" ? s.audioCodec : s.videoCodec);
export const codecPatch = (s: Settings, codec: string): Partial<Settings> =>
  s.mode === "audio" ? { audioCodec: codec as Settings["audioCodec"] } : { videoCodec: codec as Settings["videoCodec"] };

/** Item 46: one patch with mode and the (possibly reset) shared quality. */
export function modePatch(s: Settings, mode: Mode): Partial<Settings> {
  const quality = qualitiesFor(mode).includes(s.quality) ? s.quality : "Best";
  return { mode, quality };
}

/** Item 50. */
export const qualityEnabled = (s: Settings): boolean => s.mode !== "audio" || AUDIO_CODECS_WITH_BITRATE.includes(s.audioCodec);
/** Items 23/54: container, re-encode and preset exist in video mode only. */
export const videoOnlyVisible = (s: Settings): boolean => s.mode === "video";
/** Item 52. */
export const presetEnabled = (s: Settings): boolean => s.reencode;
/** Subtitle languages only matter when subtitles are embedded. */
export const subtitleLangsEnabled = (s: Settings): boolean => s.embedSubtitles;

/** Items 58/61: 0 (or ≥ 9999) shows as ∞. */
export const concurrencyLabel = (n: number): string => (n <= 0 || n >= 9999 ? "∞" : String(n));
