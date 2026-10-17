import { test, expect, type Page } from "@playwright/test";
import { psInvoiceDetails } from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "http://127.0.0.1:3339";
// Include both sides of every layout breakpoint and short landscape viewports.
const viewports = [
  { width: 280, height: 653 },
  { width: 319, height: 576 },
  { width: 320, height: 576 },
  { width: 360, height: 640 },
  { width: 380, height: 740 },
  { width: 381, height: 740 },
  { width: 390, height: 844 },
  { width: 480, height: 320 },
  { width: 640, height: 360 },
  { width: 740, height: 576 },
  { width: 741, height: 576 },
  { width: 768, height: 1024 },
  { width: 900, height: 600 },
  { width: 901, height: 600 },
  { width: 1000, height: 768 },
  { width: 1001, height: 768 },
  { width: 1024, height: 768 },
  { width: 1440, height: 900 },
  { width: 2560, height: 1440 },
];

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.addInitScript((url) => {
    localStorage.setItem("layer3-mint", url);
  }, mintUrl);
});

async function expectContained(page: Page, selectors: string) {
  const issues = await page.locator(selectors).evaluateAll((elements) => {
    const width = document.documentElement.clientWidth;
    return elements.flatMap((element) => {
      const bounds = element.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return [];
      return bounds.left < -1 ||
        bounds.right > width + 1 ||
        element.scrollWidth > element.clientWidth + 1
        ? [element.className || element.tagName]
        : [];
    });
  });
  expect(issues, JSON.stringify(page.viewportSize())).toEqual([]);
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);
}

async function expectSeparate(page: Page, first: string, second: string) {
  const a = (await page.locator(first).boundingBox())!;
  const b = (await page.locator(second).boundingBox())!;
  expect(
    a.x + a.width <= b.x + 1 ||
      b.x + b.width <= a.x + 1 ||
      a.y + a.height <= b.y + 1 ||
      b.y + b.height <= a.y + 1,
    `${first} overlaps ${second} at ${JSON.stringify(page.viewportSize())}`,
  ).toBe(true);
}

async function fund(page: Page, amount: number) {
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill(String(amount));
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  const invoice = page.getByLabel("Funding invoice");
  await expect(invoice).toHaveValue(/^lnbcrt/);
  const paymentHash = psInvoiceDetails(await invoice.inputValue()).paymentHash;
  const response = await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: paymentHash },
  });
  expect(response.ok()).toBe(true);
  await page.getByRole("button", { name: "Check payment" }).click();
  await expect(
    page.getByRole("heading", {
      name: `${amount.toLocaleString("en-US")} sats received`,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to wallet" }).click();
}

test("resizes the landing page without clipping controls or overlapping the illustration text", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await expectContained(
        page,
        "header, header a, header button, .hero-copy, .hero-actions a, .art-held, .art-note, .art-fragment, figcaption, .diagram-controls button",
      );
      await expectSeparate(page, ".brand", ".theme-toggle");
      await expectSeparate(
        page,
        ".theme-toggle",
        ".landing-header .landing-open",
      );
      await expectSeparate(page, ".art-note-value", ".art-fragment");
      await expectSeparate(page, ".art-fragment", "figcaption");
    }
    for (const step of ["Deposit", "Transfer", "Redeem"]) {
      await page.getByRole("button", { name: new RegExp(step) }).click();
      await expectContained(page, ".diagram-node, .diagram-cash");
      await expectSeparate(page, ".diagram-alice", ".diagram-carol");
    }
  }
  await page.setViewportSize({ width: 320, height: 576 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/responsive-landing-320.png",
    fullPage: true,
  });
});

test("keeps funded wallet tabs readable while resizing, including long balances and pending invoices", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 1_200_000_000);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("100000000");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("42");
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
  await page.getByRole("button", { name: "Close dialog" }).click();

  for (const tab of ["Wallet", "Activity", "Settings"]) {
    await page.getByRole("button", { name: tab, exact: true }).click();
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const colorScheme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme });
        await expectContained(
          page,
          "header, header a, header button, nav, .local-tag, .balance-label, .balance-number, .wallet-actions button, .backing, .backing-amount, .funding-row, .activity-row, .activity-name, .activity-value, .settings-card, .theme-option > span, footer",
        );
        await expectSeparate(page, "nav", ".local-tag");
        await expectSeparate(page, ".brand", ".theme-toggle");
        const amountLines = await page
          .locator(".balance-number strong, .activity-amount")
          .evaluateAll((elements) =>
            elements.map((element) => {
              const range = document.createRange();
              range.selectNodeContents(element);
              return range.getClientRects().length;
            }),
          );
        expect(amountLines.every((count) => count === 1)).toBe(true);
        if (tab === "Activity") {
          await expectSeparate(
            page,
            ".activity-row:first-child .activity-name",
            ".activity-row:first-child .activity-value",
          );
        }
      }
    }
  }
  await page.getByRole("button", { name: "Wallet", exact: true }).click();
  await expect(page.getByTestId("balance")).toHaveText("1,100,000,000sats");
  await expect(page.getByText("42 sats awaiting funding")).toBeVisible();
  await page.setViewportSize({ width: 320, height: 576 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/responsive-wallet-320.png",
    fullPage: true,
  });
});

async function expectScrollableDialog(page: Page) {
  const dialog = page.getByRole("dialog");
  for (const viewport of [
    { width: 320, height: 256 },
    { width: 320, height: 576 },
    { width: 576, height: 320 },
    { width: 844, height: 390 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    const bounds = (await dialog.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height);
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    await expectContained(
      page,
      ".modal input, .modal textarea, .modal select, .modal .button, .modal .qr",
    );
    const action = dialog.locator("button").last();
    await action.scrollIntoViewIfNeeded();
    await expect(action).toBeInViewport();
    const close = page.getByRole("button", { name: "Close dialog" });
    await close.scrollIntoViewIfNeeded();
    await expect(close).toBeInViewport();
  }
}

test("keeps funding, token, payment and backup dialogs usable in short and narrow viewports", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  for (const name of ["Add sats", "Receive", "Send", "Pay with Lightning"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expectScrollableDialog(page);
    if (name === "Add sats") {
      await page.getByLabel("Amount in sats").fill("10");
      await page
        .getByRole("button", { name: "Create funding invoice" })
        .click();
      await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
      await expect(page.locator(".qr")).toBeVisible();
      await expectScrollableDialog(page);
    }
    if (name === "Send") {
      await page.getByLabel("Amount in sats").fill("10");
      await page.getByRole("button", { name: "Create token" }).click();
      await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
      await expect(page.locator(".qr")).toBeVisible();
      await expectScrollableDialog(page);
    }
    if (name === "Pay with Lightning") {
      const response = await page.request.post(mintUrl + "/test/receiver", {
        data: { amount: 7 },
      });
      await page
        .getByLabel("Lightning invoice")
        .fill((await response.json()).request);
      await page.getByRole("button", { name: "Review payment" }).click();
      await expect(
        page.getByRole("button", { name: "Pay 7 sats", exact: true }),
      ).toBeVisible();
      await expectScrollableDialog(page);
    }
    await page.getByRole("button", { name: "Close dialog" }).click();
  }
  await page
    .getByRole("button", { name: /Deposit invoice history \(/ })
    .click();
  await expectScrollableDialog(page);
  await expectContained(page, ".invoice-history-row, .invoice-history-details");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "View token" }).click();
  await expectScrollableDialog(page);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  for (const name of ["Export backup", "Restore backup"]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expectScrollableDialog(page);
    await page.getByRole("button", { name: "Close dialog" }).click();
  }
});

test("keeps touch inputs large enough to avoid mobile focus zoom", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 320, height: 576 },
    isMobile: true,
    hasTouch: true,
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  try {
    await page.addInitScript(
      (url) => localStorage.setItem("layer3-mint", url),
      mintUrl,
    );
    await page.goto("/wallet/");
    await expect(
      page.getByText("Mint connected", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Receive", exact: true }).click();
    await expect(page.getByLabel("Ecash token")).toHaveCSS("font-size", "16px");
    await page.getByLabel("Ecash token").fill("cashuPS" + "a".repeat(1000));
    await expectScrollableDialog(page);
  } finally {
    await context.close();
  }
});
