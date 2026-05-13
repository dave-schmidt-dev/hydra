import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test.describe("live SSE updates", () => {
  test("manual /ask appears in the question list via SSE", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    // The preflight project already set phase=live, so the live view loads.
    await expect(page).toHaveTitle(/Live/i);
    await expect(page.locator("#question-list")).toBeAttached();

    // Wait for hydra.js to receive the SSE "connected" handshake before we
    // post — otherwise the broadcast can race ahead of subscriber registration.
    await page.waitForFunction(
      "window.hydraSSE && window.hydraSSE.connected === true",
      undefined,
      { timeout: 5000 },
    );

    const topic = `Playwright manual ask ${Date.now()}`;
    const resp = await request.post("/ask", {
      form: { topic },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.status).toBe("queued");
    expect(body.q_id).toMatch(/^q-manual-\d{4,}$/);

    // hydra.js prepends a <li id="q-<q_id>"> on SSE broadcast.
    const qLocator = page.locator(`#q-${body.q_id}`);
    await expect(qLocator).toBeVisible({ timeout: 5000 });
    await expect(qLocator.locator(".q-topic")).toContainText(topic);
  });
});
