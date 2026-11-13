import { describe, expect, it } from "vitest";
import { api, onEvent } from "../lib/api";
import { mockControls } from "../lib/mockBackend";
import { LANGUAGES, loadLanguage, optionLabel, t } from "../i18n";

const CATALOGS = Object.fromEntries(
  Object.entries(import.meta.glob<Record<string, string>>("../i18n/locales/*.json", { eager: true, import: "default" })).map(([p, m]) => [p.match(/(\w+)\.json$/)![1], m])
);
import { parseConcurrency, parseFragments, type AppEvent, type Task } from "../lib/types";

describe("i18n catalogs", () => {
  it("every language has every English key with the same placeholders", () => {
    const en = CATALOGS.en as Record<string, string>;
    const ph = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join();
    expect(Object.keys(en)).toHaveLength(206);
    for (const lang of LANGUAGES) {
      const table = CATALOGS[lang] as Record<string, string>;
      expect(Object.keys(table).sort()).toEqual(Object.keys(en).sort());
      for (const k of Object.keys(en)) expect(ph(table[k]), `${lang} ${k}`).toBe(ph(en[k]));
    }
  });
  it("formats and localises options", async () => {
    expect(t("tr", "button.add")).toBe("Add to queue"); // not loaded yet: English fallback
    await loadLanguage("tr");
    expect(t("tr", "button.add")).not.toBe("Add to queue");
    expect(t("tr", "log.task_added", { url: "u" })).toContain("u");
    expect(t("xx", "button.add")).toBe("Add to queue");
    expect(t("en", "missing.key")).toBe("missing.key");
    expect(optionLabel("en", "None")).toBe("None (keep original)");
    expect(optionLabel("de", "1080p")).toBe("1080p");
  });
});

describe("option parsing mirrors Rust", () => {
  it("concurrency and fragments", () => {
    expect(["∞", "Unlimited", "3", "0", "9999", "abc", "2.5"].map(parseConcurrency)).toEqual([0, 0, 3, 1, 0, 2, 2]);
    expect(["16", "0", "x"].map(parseFragments)).toEqual([16, 1, 4]);
  });
});

describe("mock backend", () => {
  const until = async (pred: () => boolean) => {
    for (let i = 0; i < 400 && !pred(); i++) await new Promise((r) => setTimeout(r, 5));
    expect(pred()).toBe(true);
  };

  it("runs, rescues, fails and cancels scripted tasks", async () => {
    mockControls.reset();
    mockControls.setSpeed(0.01);
    const tasks = new Map<number, Task>();
    const events: AppEvent[] = [];
    await onEvent((e) => {
      events.push(e);
      if (e.type === "taskUpdated") tasks.set(e.task.id, e.task);
    });
    await expect(api.addTasks("nothing here")).rejects.toEqual({ kind: "noUrls" });
    await api.updateSettings({ maxConcurrent: 0 });
    const [ok, bot, bad, slow] = await api.addTasks(
      "https://y/ok\nhttps://y/bot\nhttps://y/fail\nhttps://y/slow"
    );
    await until(() => tasks.get(slow)?.state.state === "downloading");
    await api.cancelTask(slow);
    await until(() => ["done", "failed", "cancelled"].every((s) => [ok, bot, bad, slow].some((id) => tasks.get(id)?.state.state === s)));
    expect(tasks.get(ok)?.state.state).toBe("done");
    expect(tasks.get(bot)?.state.state).toBe("done");
    expect(tasks.get(bot)?.attempts).toHaveLength(2);
    expect(tasks.get(bad)?.state).toMatchObject({ state: "failed", errorClass: "unavailable" });
    expect(tasks.get(slow)?.state).toEqual({ state: "cancelled", cleanedUp: true });
    const keys = events.flatMap((e) => (e.type === "log" && e.entry.message.kind === "key" ? [e.entry.message.key] : []));
    expect(keys).toContain("log.auto_cookies_switched");
    expect(await api.clearFinished()).toHaveLength(4);
  });
});
