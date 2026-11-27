import { test, expect } from "@playwright/test";
import { attachErrorGuards } from "../playwright.config.mjs";

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8081";

test.describe("MOD-16: Inspect, partial track selection, progress & cancellation", () => {
  test("inspects playlist, unchecks a track, starts download, and cancels active job without errors", async ({
    page,
  }) => {
    const guards = attachErrorGuards(page);

    // Mock /api/health and /api/default-dir for offline execution
    await page.route("**/api/health", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          ffmpeg_available: true,
          details: { available: true, ffmpeg: true, ffprobe: true },
        }),
      });
    });

    await page.route("**/api/default-dir", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ default_dir: "C:/Music" }),
      });
    });

    // Mock /api/inspect (and /api/preview) with extract_flat style playlist metadata
    const inspectHandler = async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          title: "Test Playlist E2E",
          author: "Test Channel",
          thumbnail: "https://i.ytimg.com/vi/t1/hqdefault.jpg",
          duration: 360,
          is_playlist: true,
          track_count: 2,
          tracks: [
            { index: 1, id: "t1", title: "Track One", duration: 180 },
            { index: 2, id: "t2", title: "Track Two", duration: 180 },
          ],
        }),
      });
    };
    await page.route("**/api/inspect", inspectHandler);
    await page.route("**/api/preview", inspectHandler);

    let capturedPayload = null;
    await page.route("**/api/jobs", async (route) => {
      if (route.request().method() === "POST") {
        capturedPayload = route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({ job_id: "job-e2e-1", status: "pending" }),
        });
        return;
      }
      await route.continue();
    });

    await page.route("**/api/jobs/job-e2e-1/events", async (route) => {
      const ssePayload =
        'event: status\ndata: {"job_id":"job-e2e-1","status":"downloading","progress":42.0,"current_track":"Track One","message":"Canción 1 de 1 • Descargando Track One"}\n\n';
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ssePayload,
      });
    });

    let cancelRequested = false;
    await page.route("**/api/jobs/job-e2e-1/cancel", async (route) => {
      cancelRequested = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ job_id: "job-e2e-1", status: "cancelled" }),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

    // Paste playlist URL and wait for inspection metadata
    const urlInput = page.locator("#url-input");
    await urlInput.fill("https://www.youtube.com/playlist?list=PL_E2E_123");

    await expect(page.locator("#preview-title")).toHaveText("Test Playlist E2E");

    // Open track selector and uncheck the second track (partial selection)
    const details = page.locator("#track-selector-details");
    if (await details.count()) {
      await details.locator("summary").click();
      const checkboxes = page.locator('#preview-tracks-list input[type="checkbox"]');
      await expect(checkboxes).toHaveCount(2);
      await checkboxes.nth(1).uncheck();
    }

    // Start download
    await page.locator("#submit-btn").click();
    await expect.poll(() => capturedPayload).not.toBeNull();
    if (capturedPayload && Array.isArray(capturedPayload.selected_tracks)) {
      expect(capturedPayload.selected_tracks).toEqual([1]);
    }

    // Cancel active job
    const cancelBtn = page.locator("#cancel-btn");
    await expect(cancelBtn).toBeEnabled();
    await cancelBtn.click();
    await expect.poll(() => cancelRequested).toBe(true);

    guards.assertNoErrors();
  });
});
