import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

// Task 6.1 didn't ship any keyboard shortcuts in hydra.js, so this file
// covers the closest analog: a form input can be focused and accepts text.
// When Task 6.2+ adds real shortcuts (e.g., "/" focuses ask), replace these
// placeholder assertions with the real keybindings.
test.describe("keyboard / focus baseline (placeholder for future shortcuts)", () => {
  test("review export input accepts focus and typed text", async ({ page }) => {
    await page.goto("/");
    // The review project leaves us in phase=review, so the export form is visible.
    await expect(page).toHaveTitle(/Review/i);
    const destInput = page.locator("input[name='destination']");
    await expect(destInput).toBeVisible();

    await destInput.focus();
    await expect(destInput).toBeFocused();
    await page.keyboard.type("/tmp/hydra-ui-test-report.md");
    await expect(destInput).toHaveValue("/tmp/hydra-ui-test-report.md");
  });

  test("tab navigation reaches the export submit button", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/Review/i);
    const destInput = page.locator("input[name='destination']");
    await destInput.focus();
    await page.keyboard.press("Tab");
    const exportBtn = page.locator(
      "form[action='/export'] button[type='submit']",
    );
    await expect(exportBtn).toBeFocused();
  });
});
