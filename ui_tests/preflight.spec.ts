import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test.describe("preflight flow", () => {
  test("preflight form renders on /", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/Pre-flight/i);
    await expect(page.locator("form[action='/preflight']")).toBeVisible();
    await expect(page.locator("textarea[name='meeting_about']")).toBeVisible();
    await expect(page.locator("textarea[name='corpus_paths']")).toBeVisible();
    await expect(page.locator("input[name='participants']")).toBeVisible();
  });

  test("submit preflight redirects and shows live view", async ({ page }) => {
    await page.goto("/");
    await page
      .locator("textarea[name='meeting_about']")
      .fill("Enterprise contract renewal review");
    await page.locator("input[name='participants']").fill("Alice, Bob");
    await page.locator("textarea[name='corpus_paths']").fill("/tmp/hydra-ui-corpus");
    await page
      .locator("form[action='/preflight'] button[type='submit']")
      .click();

    // After 303 redirect we land back on `/` rendering live.html.
    await expect(page).toHaveURL("/");
    await expect(page).toHaveTitle(/Live/i);
    await expect(page.locator("section#banners")).toBeAttached();
    await expect(page.locator("section#questions")).toBeAttached();
    await expect(page.locator("h1")).toContainText(/live/i);
    await expect(page.locator("#ask-form")).toBeVisible();
  });
});
