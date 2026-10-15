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

test("shows the frozen deposit and hidden ownership changes without connecting a wallet", async ({
  page,
}) => {
  const mintRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/v1/"))
      mintRequests.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    /Ecash from a\s*frozen deposit\./,
  );
  await expect(
    page.getByRole("heading", {
      name: "The mint cannot collect your deposit.",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Ecash transfers are private." }),
  ).toBeVisible();
  const expiry = page.getByRole("complementary", {
    name: "Ecash has a deadline.",
  });
  await expect(expiry).toBeVisible();
  await expect(expiry).toContainText("There is no remaining balance to claim.");
  await expect(expiry).toContainText("does not reset the clock");
  const diagram = page.getByRole("region", {
    name: /Alice, the mint,\s*and Carol/,
  });
  const mint = diagram.getByRole("group", { name: "What the mint sees" });
  await expect(mint).toContainText("Frozen in Lightning");
  await expect(mint).toContainText("The mint cannot collect the deposit.");
  const deposit = (await mint.locator(".example-backing").textContent())!;
  await diagram.getByRole("button", { name: /Transfer/ }).focus();
  await diagram.getByRole("button", { name: /Transfer/ }).press("Enter");
  await expect(
    diagram.getByRole("group", { name: "Carol", exact: true }),
  ).toContainText("Has the ecash");
  await expect(
    diagram.getByRole("group", { name: "Alice’s ecash", exact: true }),
  ).toContainText("750 sats");
  await expect(
    diagram.getByRole("group", { name: "Carol’s ecash", exact: true }),
  ).toContainText("250 sats");
  await expect(mint.locator(".example-backing")).toHaveText(deposit);
  await expect(mint).toContainText("Request: refresh ecash");
  await expect(mint).not.toContainText(/Alice|Carol/);
  await expect(diagram.getByRole("status")).toContainText(
    "cannot tell that ownership changed from Alice to Carol",
  );
  await diagram.screenshot({ path: "test-results/diagram-transfer.png" });
  await diagram.getByRole("button", { name: /Redeem/ }).click();
  await expect(mint).toContainText("Unlocked by redemption");
  await expect(mint).toContainText("The mint can now collect the deposit.");
  await expect(diagram.getByRole("status")).toContainText(
    "redeems and burns her 250 sats of ecash",
  );
  await expect(
    diagram.getByRole("group", { name: "Carol’s ecash", exact: true }),
  ).toContainText("Burned");
  await expect(
    diagram.getByRole("group", { name: "Alice’s ecash", exact: true }),
  ).not.toContainText("Burned");
  await diagram.screenshot({ path: "test-results/diagram-redeem.png" });
  await diagram.getByRole("button", { name: /miss the deadline/i }).click();
  await expect(
    diagram.getByRole("group", { name: "Alice’s ecash", exact: true }),
  ).toContainText("Expired");
  await expect(
    diagram.getByRole("group", { name: "Carol’s ecash", exact: true }),
  ).toContainText("Expired");
  await expect(diagram.getByRole("status")).toContainText(
    "can no longer be spent",
  );
  await expect(mint).toContainText("Reverted to sender");
  await expect(mint).toContainText("Lightning hold timed out");
  await expect(mint).toContainText("The mint never collected this deposit.");
  await expect(
    diagram.getByRole("group", { name: "Alice", exact: true }),
  ).toContainText("Deposit returned");
  await expect(mint).not.toContainText("Unlocked");
  await diagram.screenshot({ path: "test-results/diagram-expiry.png" });
  await page
    .getByText("What changes when someone redeems?", { exact: true })
    .click();
  await expect(
    page.getByText(
      /the mint can collect the whole deposit, even if other people/,
    ),
  ).toBeVisible();
  await page
    .getByText("Can the mint see anything during a transfer?", { exact: true })
    .click();
  await expect(
    page.getByText(/Those requests do not tell it whether ownership changed/),
  ).toBeVisible();
  expect(mintRequests).toEqual([]);
  expect(
    await page.evaluate(async () => (await indexedDB.databases()).length),
  ).toBe(0);
});

test("plays, pauses, and completes the animated explanation", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.clock.install();
  await page.goto("/");
  const diagram = page.getByRole("region", {
    name: /Alice, the mint,\s*and Carol/,
  });
  await diagram.getByRole("button", { name: "Play explanation" }).click();
  await page.clock.runFor(4500);
  await expect(
    diagram.getByRole("button", { name: /Transfer/ }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(diagram.locator(".diagram-private-line")).toHaveCSS(
    "animation-play-state",
    "running",
  );
  await diagram.getByRole("button", { name: "Pause explanation" }).click();
  await expect(diagram.locator(".diagram-private-line")).toHaveCSS(
    "animation-play-state",
    "paused",
  );
  await page.clock.runFor(9000);
  await expect(
    diagram.getByRole("button", { name: /Transfer/ }),
  ).toHaveAttribute("aria-pressed", "true");
  await diagram.getByRole("button", { name: "Play explanation" }).click();
  await page.clock.runFor(4500);
  await expect(
    diagram.getByRole("group", { name: "What the mint sees" }),
  ).toContainText("Frozen in Lightning");
  await page.clock.runFor(4500);
  await expect(
    diagram.getByRole("group", { name: "What the mint sees" }),
  ).toContainText("Unlocked by redemption");
  await page.clock.runFor(4500);
  await expect(
    diagram.getByRole("button", { name: "Play explanation" }),
  ).toHaveAttribute("aria-pressed", "false");
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
    "<title>cashu3 — Ecash from a frozen deposit</title>",
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
      await page.getByRole("button", { name: /miss the deadline/i }).click();
      await expect(
        page.getByRole("group", { name: "Alice’s ecash", exact: true }),
      ).toContainText("Expired");
      await expect(
        page.getByRole("group", { name: "Carol’s ecash", exact: true }),
      ).toContainText("Expired");
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
  await page.getByRole("button", { name: /Deposit/ }).click();
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
