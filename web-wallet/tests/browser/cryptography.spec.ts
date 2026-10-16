import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
});

test("opens the cryptography from the landing page without initializing a wallet", async ({
  page,
}) => {
  const mintRequests: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/v1/"))
      mintRequests.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page
    .getByRole("link", { name: /How does the cryptography work/ })
    .click();
  await expect(page).toHaveURL(/\/how-it-works\/\?view=cryptography$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    /The cryptography\s*behind temporary cash\./,
  );
  await expect(
    page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Cryptography" }),
  ).toHaveAttribute("aria-current", "page");
  await page
    .getByRole("navigation", { name: "On this page" })
    .getByRole("link", { name: /A private swap/ })
    .click();
  await expect(page).toHaveURL(/#swaps$/);
  await expect(
    page.getByRole("heading", { name: "04 A private swap" }),
  ).toBeInViewport();

  const example = page.getByRole("figure", {
    name: "Same backing. Fresh credentials.",
  });
  const inputs = example.getByRole("group", { name: "Input credentials" });
  const outputs = example.getByRole("group", { name: "Output credentials" });
  await expect(inputs.locator(".crypto-note")).toHaveCount(1);
  await expect(outputs.locator(".crypto-note")).toHaveCount(3);
  await example.getByRole("button", { name: "Combine" }).focus();
  await page.keyboard.press("Enter");
  await expect(
    example.getByRole("button", { name: "Combine" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(inputs.locator(".crypto-note")).toHaveCount(3);
  await expect(outputs.locator(".crypto-note")).toHaveCount(1);
  await expect(example.getByRole("status")).toContainText(
    "Three credentials become one",
  );
  await example.getByRole("button", { name: "Split" }).click();
  await expect(example.getByRole("status")).toContainText(
    "One credential becomes three",
  );

  await page
    .getByText("How blind reissuance preserves that backing", { exact: true })
    .click();
  await expect(
    page.locator(".crypto-equation").filter({ hasText: "v_raw,j" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Use night mode" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("link", { name: "The basics", exact: true }).click();
  await expect(page).toHaveURL(/\/#how-it-works$/);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(mintRequests).toEqual([]);
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(async () => (await indexedDB.databases()).length),
  ).toBe(0);
});

test("serves a standalone explainer on direct visits and reloads", async ({
  page,
  request,
}) => {
  const response = await request.get("/how-it-works/");
  expect(response.ok()).toBe(true);
  const html = await response.text();
  expect(html).toContain(
    "<title>tmp.cash — The cryptography behind temporary cash</title>",
  );
  expect(html).toContain('data-page="cryptography"');
  if (process.env.WALLET_E2E_PRODUCTION)
    expect(html).toContain("Content-Security-Policy");

  for (const entry of [
    "/how-it-works/",
    "/how-it-works/index.html",
    "/how-it-works?view=cryptography#redemption",
  ]) {
    await page.goto(entry);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "temporary cash.",
    );
    await expect(page).toHaveTitle(
      "tmp.cash — The cryptography behind temporary cash",
    );
    await page.reload();
    await expect(page.getByRole("heading", { level: 1 })).toBeAttached();
  }
  await expect(page).toHaveURL(
    /\/how-it-works\/\?view=cryptography#redemption$/,
  );
  await expect(
    page.getByRole("heading", { name: "05 Back to Lightning" }),
  ).toBeInViewport();
  await expect(
    page.locator(".landing-header").getByRole("link", { name: "Open wallet" }),
  ).toHaveAttribute("href", "/wallet/");
});

test("keeps the equations and example readable across screen sizes and themes", async ({
  page,
}) => {
  await page.goto("/how-it-works/?view=cryptography");
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  for (const width of [320, 390, 740, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await expect(page.locator("html")).toHaveAttribute(
        "data-theme",
        colorScheme,
      );
      if (width <= 900) {
        const toc = (await page
          .getByRole("navigation", { name: "On this page" })
          .boundingBox())!;
        const article = (await page.getByRole("article").boundingBox())!;
        expect(toc.y + toc.height).toBeLessThan(article.y);
      }
      for (const detail of await page.locator(".crypto-details").all()) {
        if ((await detail.getAttribute("open")) === null)
          await detail.locator("summary").click();
      }
      for (const mode of ["Split", "Combine"]) {
        await page.getByRole("button", { name: mode, exact: true }).click();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBe(true);
        const overflow = await page
          .locator(".crypto-equation")
          .evaluateAll(
            (elements) =>
              elements.filter((el) => el.scrollWidth > el.clientWidth).length,
          );
        expect(overflow).toBe(0);
      }
      const open = page
        .locator(".landing-header")
        .getByRole("link", { name: "Open wallet" });
      const bounds = (await open.boundingBox())!;
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    }
  }
  await page.getByRole("button", { name: "Split", exact: true }).click();
  for (const colorScheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement)
        document.activeElement.blur();
      window.scrollTo(0, 0);
    });
    await page.screenshot({
      path: `test-results/cryptography-${colorScheme}-desktop.png`,
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: `test-results/cryptography-${colorScheme}-mobile.png`,
      fullPage: true,
    });
  }
});
