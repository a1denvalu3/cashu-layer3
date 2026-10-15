import { test, expect } from "@playwright/test";
import { psInvoiceDetails } from "@cashu/cashu-ts/layer3";

const mintUrl = "http://127.0.0.1:3339";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.addInitScript((url) => {
    if (!localStorage.getItem("layer3-mint"))
      localStorage.setItem("layer3-mint", url);
  }, mintUrl);
});

test("explains sharing, redemption, and expiry without connecting a wallet", async ({
  page,
}) => {
  const mintRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/v1/"))
      mintRequests.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    /Lightning in\.\s*Cash around\.\s*Lightning out\./,
  );
  const example = page.getByRole("region", {
    name: /One hold\.\s*Two pockets\./,
  });
  const slider = example.getByRole("slider", { name: "Send to a friend" });
  await slider.focus();
  await slider.press("Home");
  await slider.press("ArrowRight");
  await expect(slider).toHaveValue("100");
  await expect(example.locator(".example-pocket").first()).toContainText(
    "900 sats",
  );
  await example.getByRole("button", { name: "Pay a 99 sat invoice" }).click();
  await expect(slider).toBeDisabled();
  await expect(example.getByRole("status")).toContainText(
    "revealed the backing preimage",
  );
  await expect(example.locator(".friend")).toContainText("99 paid + 1 sat fee");
  await example
    .getByRole("button", { name: "Reach the redemption cutoff" })
    .click();
  await expect(example.getByRole("status")).toContainText(
    "remaining 900 sats expire",
  );
  await expect(example.locator(".example-pocket").first()).toContainText(
    "900 sats expired",
  );
  await example.getByRole("button", { name: "Start again" }).click();
  await expect(slider).toBeEnabled();
  await expect(example.locator(".friend")).toContainText("100 sats");
  await expect(
    page.getByRole("heading", {
      name: "A held payment still needs a trusted mint.",
    }),
  ).toBeVisible();
  expect(mintRequests).toEqual([]);
  expect(
    await page.evaluate(async () => (await indexedDB.databases()).length),
  ).toBe(0);
});

test("opens the wallet and keeps its funds and theme across visits to the explainer", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Use night mode" }).click();
  await page
    .locator(".landing-hero")
    .getByRole("link", { name: "Open wallet" })
    .click();
  await expect(page).toHaveURL(/\/wallet\/$/);
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.getByRole("region", {
      name: "How cashu3 differs from ordinary ecash",
    }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("100");
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
  const hash = psInvoiceDetails(
    await page.getByLabel("Funding invoice").inputValue(),
  ).paymentHash;
  await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: hash },
  });
  await page.getByRole("button", { name: "Check payment" }).click();
  await expect(
    page.getByRole("heading", { name: "100 sats received" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to wallet" }).click();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("link", { name: "How cashu³ works" }).click();
  await expect(page).toHaveURL(/\/#how-it-works$/);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page
    .locator(".landing-header")
    .getByRole("link", { name: "Open wallet" })
    .click();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
});

test("serves a direct wallet entry and distinct page metadata", async ({
  page,
  request,
}) => {
  const landing = await request.get("/");
  const wallet = await request.get("/wallet/");
  expect(landing.ok()).toBe(true);
  expect(wallet.ok()).toBe(true);
  expect(await landing.text()).toContain(
    "<title>cashu3 — Lightning in. Cash around. Lightning out.</title>",
  );
  expect(await wallet.text()).toContain(
    "<title>cashu3 wallet — Cash with a deadline</title>",
  );
  if (process.env.WALLET_E2E_PRODUCTION) {
    expect(await landing.text()).toContain("Content-Security-Policy");
    expect(await wallet.text()).toContain("Content-Security-Policy");
  }
  for (const entry of ["/wallet", "/wallet/index.html"]) {
    await page.goto(entry);
    await expect(
      page.getByText("Mint connected", { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "Cash, with a deadline.",
    );
  }
});

test("keeps the landing page usable on narrow screens in both themes", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  for (const width of [320, 390, 740, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
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
      const open = page
        .locator(".landing-header")
        .getByRole("link", { name: "Open wallet" });
      await expect(open).toBeVisible();
      const bounds = (await open.boundingBox())!;
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/landing-desktop.png",
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({
    path: "test-results/landing-night.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/landing-mobile.png",
    fullPage: true,
  });
});
