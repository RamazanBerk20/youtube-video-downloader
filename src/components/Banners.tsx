// Banner stack (items 11, 100-102, 105): collapsed when empty, one per kind.
import { memo } from "react";
import { api } from "../lib/api";
import type { Banner, BannerKind } from "../lib/types";
import { useData } from "../state/store";
import { useDispatch, useT } from "./context";
import { Icon } from "./icons";

const TONE: Record<BannerKind, "danger" | "warning" | "info"> = {
  ffmpegMissing: "danger",
  jsRuntimeMissing: "danger",
  ytdlpMissing: "danger",
  chromiumCookies: "warning",
  anyipNotConfigured: "warning",
  botDetection: "warning",
  appUpdate: "info"
};

const TEXT: Record<BannerKind, string> = {
  ffmpegMissing: "banner.ffmpeg_missing",
  jsRuntimeMissing: "banner.js_runtime_missing",
  ytdlpMissing: "banner.ytdlp_missing",
  chromiumCookies: "banner.chromium_cookies",
  anyipNotConfigured: "banner.anyip_not_configured",
  botDetection: "banner.bot_detection",
  appUpdate: "banner.app_update"
};

export const Banners = memo(function Banners() {
  const banners = useData((s) => s.banners);
  if (banners.length === 0) return null;
  return (
    <div className="banners" data-testid="banners">
      {banners.map((b) => (
        <BannerView key={b.kind} b={b} />
      ))}
    </div>
  );
});

function BannerView({ b }: { b: Banner }) {
  const t = useT();
  const dispatch = useDispatch();
  const plan = b.install;
  const noPm = plan?.type === "manual" && plan.hint.startsWith("No supported package manager");
  let action: { label: string; run: () => void } | null = null;
  if (plan?.type === "auto") action = { label: t("button.install"), run: () => void api.installTool(b.kind === "ffmpegMissing" ? "ffmpeg" : "deno") };
  else if (b.kind === "appUpdate") action = { label: t("button.update_now"), run: () => void (b.canApply ? api.applyAppUpdate() : api.openReleasePage()) };
  else if (b.kind === "ytdlpMissing") action = { label: t("button.download_ytdlp"), run: () => void api.updateYtdlp() };
  else if (b.kind === "botDetection" || b.kind === "chromiumCookies") action = { label: t("button.open_settings"), run: () => dispatch({ type: "openSettings", section: "cookies" }) };
  else if (b.kind === "anyipNotConfigured") action = { label: t("button.open_settings"), run: () => dispatch({ type: "openSettings", section: "anyip" }) };

  return (
    <div className={`banner ${TONE[b.kind]}`} role="status" data-testid={`banner-${b.kind}`}>
      <div className="banner-text">
        <p>{noPm ? t("banner.no_pm") : t(TEXT[b.kind], { version: b.version ?? "" })}</p>
        {plan?.type === "manual" && !noPm && (
          <p>
            {t("banner.manual_install")} <code dir="ltr">{plan.hint}</code>
          </p>
        )}
      </div>
      {action && (
        <button type="button" className="btn small solid" onClick={action.run}>
          {action.label}
        </button>
      )}
      <button type="button" className="icon-btn" aria-label={t("button.dismiss")} title={t("button.dismiss")} onClick={() => void api.dismissBanner(b.kind)}>
        <Icon name="x" />
      </button>
    </div>
  );
}
