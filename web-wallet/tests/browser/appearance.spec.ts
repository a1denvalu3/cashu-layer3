import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.addInitScript(() => {
    localStorage.setItem("layer3-mint", "http://127.0.0.1:3339");
  });
});

test("explains the backing lifecycle and remembers dismissal without losing access to the guide", async ({
  page,
}) => {
  await page.goto("/");
  const guide = page.getByRole("region", {
    name: "How cashu3 differs from ordinary ecash",
  });
  await expect(
    guide.getByRole("heading", { name: "Your tokens have a deadline." }),
  ).toBeVisible();
  await expect(
    guide.getByRole("button", { name: "Previous guide slide" }),
  ).toBeDisabled();
  const next = guide.getByRole("button", { name: "Next guide slide" });
  await next.click();
  await expect(guide).toContainText("the entire backing payment early");
  await next.press("ArrowRight");
  await expect(guide).toContainText("keeps the original backing and deadline");
  await next.click();
  await expect(guide).toContainText(
    "Any tokens left at settlement are forfeited",
  );
  await guide.getByRole("button", { name: "Got it" }).click();
  const reopen = page.getByRole("button", { name: "How cashu³ works" });
  await expect(guide).toHaveCount(0);
  await expect(reopen).toBeFocused();
  await page.reload();
  await expect(guide).toHaveCount(0);
  await reopen.click();
  await expect(
    guide.getByRole("button", { name: "Dismiss wallet guide" }),
  ).toBeFocused();
  await guide.getByRole("button", { name: "Private transfers" }).click();
  await expect(guide).toContainText("Fresh tokens, the same backing.");
  await guide.getByRole("button", { name: "Dismiss wallet guide" }).click();
  await page.reload();
  await expect(reopen).toBeVisible();
});

test("follows the device theme until a saved preference overrides it", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Use light mode" }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".theme-option").filter({ hasText: "Night" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator(".theme-option").filter({ hasText: "System" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute(
    "content",
    "#171b16",
  );
});

test("keeps the balance badge and wallet controls inside narrow screens in both themes", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  for (const width of [320, 390, 740, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await expect(page.locator("html")).toHaveAttribute(
        "data-theme",
        colorScheme,
      );
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      const main = (await page.locator(".balance-main").boundingBox())!;
      const badge = (await page.locator(".balance-status").boundingBox())!;
      expect(badge.x).toBeGreaterThan(main.x);
      expect(badge.x + badge.width).toBeLessThan(main.x + main.width);
      for (const name of ["Add sats", "Receive", "Send"]) {
        const bounds = (await page
          .getByRole("button", { name, exact: true })
          .boundingBox())!;
        expect(bounds.x).toBeGreaterThanOrEqual(main.x);
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(
          main.x + main.width,
        );
      }
    }
  }
});
