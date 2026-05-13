import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test.describe("post-session review", () => {
  test("finalize transitions phase and / renders the review template", async ({
    page,
    request,
  }) => {
    // Submit at least one manual ask so the review table has a row to render.
    const topic = `Playwright review ask ${Date.now()}`;
    const askResp = await request.post("/ask", { form: { topic } });
    expect(askResp.status()).toBe(200);
    const askBody = await askResp.json();
    const qId = askBody.q_id as string;

    // Give the mock worker ~150ms to write the artifact and flip status to
    // "answered". The mock sleeps 50ms; we leave a generous buffer.
    await page.waitForTimeout(200);

    // Trigger finalize (the live view has a form posting to /finalize).
    const finalizeResp = await request.post("/finalize");
    expect(finalizeResp.status()).toBe(200);
    const finalizeBody = await finalizeResp.json();
    expect(finalizeBody.status).toBe("review");

    // Reload `/`; should now serve review.html.
    await page.goto("/");
    await expect(page).toHaveTitle(/Review/i);
    await expect(page.locator(".review-table")).toBeVisible();
    await expect(page.locator("#export")).toBeVisible();

    // The manual q_id should appear somewhere in the review table.
    await expect(page.locator(".review-table")).toContainText(qId);
  });
});
