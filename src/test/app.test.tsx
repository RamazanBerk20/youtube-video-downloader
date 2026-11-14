import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { App } from "../App";
import { mockControls, SPONSOR_URL } from "../lib/mockBackend";

beforeAll(() => {
  mockControls.setSpeed(0.01);
});
afterEach(cleanup);

const row = (id: number) => document.querySelector<HTMLElement>(`[data-task-id="${id}"]`);
const rowState = (id: number) => row(id)?.dataset.state;

async function addUrls(text: string) {
  const box = screen.getByTestId("url-input");
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
  await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(""));
}

describe("App against the mock backend", () => {
  it("drives the queue through success, failure, rescue and cancel", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("app").dataset.state).toBe("ready"));
    expect(screen.getByTestId("queue-empty")).toBeTruthy();

    // No URL → warning dialog, nothing queued.
    fireEvent.change(screen.getByTestId("url-input"), { target: { value: "not a link" } });
    fireEvent.click(screen.getByTestId("add"));
    await screen.findByTestId("dialog-noUrls");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("dialog-noUrls")).toBeNull();

    await addUrls("https://y/ok\nhttps://y/bot\nhttps://y/fail\nhttps://y/slow");
    await waitFor(() => expect(row(4)).toBeTruthy());
    // Unlimited concurrency, changed while the queue runs.
    const combo = screen.getByTestId("max-concurrent");
    fireEvent.change(combo, { target: { value: "unlimited" } });
    fireEvent.keyDown(combo, { key: "Enter" });
    await waitFor(() => expect((combo as HTMLInputElement).value).toBe("∞"));

    await waitFor(() => expect(rowState(4)).toBe("downloading"), { timeout: 4000 });
    fireEvent.click(within(row(4)!).getByTestId("task-x")); // cancel
    await waitFor(() => expect(rowState(4)).toBe("cancelled"), { timeout: 4000 });

    await waitFor(() => expect(rowState(1)).toBe("done"), { timeout: 4000 });
    expect(within(row(1)!).getByTestId("task-detail").textContent).toBe("100%");
    await waitFor(() => expect(rowState(3)).toBe("failed"), { timeout: 4000 });
    expect(within(row(3)!).getByTestId("task-detail").textContent).toContain("Video unavailable");
    // Rescue: the bot-check task is retried with browser cookies and finishes.
    await waitFor(() => expect(rowState(2)).toBe("done"), { timeout: 4000 });
    expect(within(row(2)!).getByTestId("task-detail").textContent).toContain("attempt 2");
    expect(within(row(2)!).getByTestId("task-detail").textContent).toContain("firefox cookies");

    // Retry the failed one; × on a terminal row removes it.
    const failures = () => mockControls.backend.log.filter((e) => e.taskId === 3 && e.message.kind === "key" && e.message.key === "log.task_failed").length;
    fireEvent.click(within(row(3)!).getByTestId("task-retry"));
    await waitFor(() => expect(failures()).toBe(2), { timeout: 4000 });
    fireEvent.click(within(row(4)!).getByTestId("task-x"));
    await waitFor(() => expect(row(4)).toBeNull());
    await waitFor(() => expect(screen.getByTestId("queue").dataset.state).toBe("finished"));
    expect(rowState(3)).toBe("failed");
    expect(screen.getByTestId("all-done")).toBeTruthy();

    // Unrescued bot check shows the amber banner with a way to the cookie settings.
    await addUrls("https://y/botx");
    await screen.findByTestId("banner-botDetection", {}, { timeout: 4000 });
    fireEvent.click(within(screen.getByTestId("banner-botDetection")).getByText("Open settings"));
    expect(screen.getByTestId("app").dataset.panel).toBe("settings");
    expect(screen.getByTestId("cookies-browser")).toBeTruthy();
    fireEvent.click(screen.getByTestId("settings-close"));

    // Clear finished empties the queue.
    fireEvent.click(screen.getByTestId("clear-finished"));
    await screen.findByTestId("queue-empty");

    // Closing with active tasks asks first.
    await addUrls("https://y/slow");
    await waitFor(() => expect(row(6)?.dataset.state).not.toBe("queued"));
    // The title bar shows the run and its close button goes through the same confirmation.
    expect(screen.getByTestId("titlebar-summary").textContent).toContain("1 active");
    expect(screen.getByTestId("titlebar").dataset.busy).toBe("true");
    fireEvent.click(screen.getByTestId("window-close"));
    const dlg = await screen.findByTestId("dialog-confirmClose");
    // Queue restore is opt-in, so by default the dialog says downloads stop.
    expect(dlg.textContent).toContain("They will be stopped.");
    fireEvent.click(screen.getByTestId("cancel-all"));
    expect(mockControls.window.closed).toBe(false);
    await waitFor(() => expect(screen.getByTestId("titlebar").dataset.busy).toBe("false"));
    expect(screen.getByTestId("titlebar-summary").textContent).toBe("");
  });

  it("title bar window buttons: maximise state, labels, close when idle", async () => {
    Object.assign(mockControls.window, { maximized: false, minimized: false, closed: false });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("app").dataset.state).toBe("ready"));
    const max = screen.getByTestId("window-maximize");
    expect(max.getAttribute("aria-label")).toBe("Maximize");
    fireEvent.click(max);
    await waitFor(() => expect(max.getAttribute("aria-label")).toBe("Restore"));
    expect(document.documentElement.hasAttribute("data-maximized")).toBe(true);
    fireEvent.click(max);
    await waitFor(() => expect(max.getAttribute("aria-label")).toBe("Maximize"));
    expect(document.documentElement.hasAttribute("data-maximized")).toBe(false);
    fireEvent.click(screen.getByTestId("window-minimize"));
    await waitFor(() => expect(mockControls.window.minimized).toBe(true));
    // Translated with the rest of the UI.
    fireEvent.change(screen.getByTestId("language"), { target: { value: "de" } });
    await waitFor(() => expect(screen.getByTestId("window-close").getAttribute("aria-label")).toBe("Schließen"));
    fireEvent.change(screen.getByTestId("language"), { target: { value: "en" } });
    await waitFor(() => expect(screen.getByTestId("window-close").getAttribute("aria-label")).toBe("Close"));
    // Nothing running: no question, the window closes.
    fireEvent.click(screen.getByTestId("window-close"));
    await waitFor(() => expect(mockControls.window.closed).toBe(true));
    expect(screen.queryByTestId("dialog-confirmClose")).toBeNull();
  });

  it("renders right-to-left in Arabic and back", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("app").dataset.state).toBe("ready"));
    fireEvent.change(screen.getByTestId("language"), { target: { value: "ar" } });
    await waitFor(() => expect(document.documentElement.dir).toBe("rtl"));
    expect(document.documentElement.lang).toBe("ar");
    expect(screen.getByTestId("add").textContent).toContain("إضافة");
    expect(document.title).toBe("محمّل يوتيوب");
    fireEvent.change(screen.getByTestId("language"), { target: { value: "en" } });
    await waitFor(() => expect(document.documentElement.dir).toBe("ltr"));
    expect(screen.getByTestId("add").textContent).toContain("Add to queue");
  });

  it("offers the sponsor link only in the Tools tab", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("app").dataset.state).toBe("ready"));
    expect(screen.queryByTestId("sponsor-link")).toBeNull();
    fireEvent.click(screen.getByTestId("drawer-toggle"));
    fireEvent.click(screen.getByTestId("drawer-tab-tools"));
    fireEvent.click(await screen.findByTestId("sponsor-link"));
    await waitFor(() => expect(mockControls.backend.opened).toEqual([SPONSOR_URL]));
  });

  it("applies the option dependencies on screen", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("app").dataset.state).toBe("ready"));
    fireEvent.click(screen.getByTestId("advanced"));
    expect((screen.getByTestId("preset") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("reencode"));
    expect((screen.getByTestId("preset") as HTMLSelectElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("mode-audio"));
    expect(screen.queryByTestId("video-only")).toBeNull();
    fireEvent.change(screen.getByTestId("codec"), { target: { value: "flac" } });
    expect((screen.getByTestId("quality") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.change(screen.getByTestId("codec"), { target: { value: "mp3" } });
    expect((screen.getByTestId("quality") as HTMLSelectElement).disabled).toBe(false);
    await waitFor(() => expect(mockControls.backend.settings).toMatchObject({ mode: "audio", audioCodec: "mp3", reencode: true }));
    fireEvent.click(screen.getByTestId("mode-video"));
    expect((screen.getByTestId("codec") as HTMLSelectElement).value).toBe("Auto");
  });
});
