import { test, expect, type Page } from "@playwright/test";
import { psInvoiceDetails } from "@cashu/cashu-ts/layer3";
import { readFile } from "node:fs/promises";

const mintUrl = "http://127.0.0.1:3339";
const walletUrl = `http://127.0.0.1:${process.env.WALLET_E2E_PORT || "5173"}`;
test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    (url) => localStorage.setItem("layer3-mint", url),
    mintUrl,
  );
});
async function fund(page: Page, value: number) {
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill(String(value));
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
  const hash = psInvoiceDetails(
    await page.getByLabel("Funding invoice").inputValue(),
  ).paymentHash;
  const response = await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: hash },
  });
  expect(response.ok()).toBe(true);
  await page.getByRole("button", { name: "Check payment" }).click();
  await expect(
    page.getByText(`${value} sats received`, { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to wallet" }).click();
  await expect(page.getByTestId("balance")).toContainText(String(value));
  return hash;
}

test("shows the mint's two-week policy as a read-only setting", async ({
  page,
}) => {
  await page.route("**/v1/info", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        default_token_lifetime_blocks: 2016,
        funding_htlc_cltv_delta: 2040,
      },
    });
  });
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByText(/New tokens: 2,016 blocks \(about 14 days\)/),
  ).toBeVisible();
  await expect(page.getByText(/Set by the mint/)).toBeVisible();
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
});

test("fund, reload, send, receive in a second wallet, melt and expire residual change", async ({
  page,
  browser,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Cash, with a deadline." }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/wallet-empty.png",
    fullPage: true,
  });
  const backing = await fund(page, 100);
  await page.reload();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("10");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
  const token = await page.getByLabel("Outgoing token").inputValue();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await expect(page.getByTestId("balance")).toHaveText("90sats");
  await page.screenshot({
    path: "test-results/wallet-funded.png",
    fullPage: true,
  });
  const context = await browser.newContext(),
    bob = await context.newPage();
  await bob.addInitScript(
    (url) => localStorage.setItem("layer3-mint", url),
    mintUrl,
  );
  await bob.goto(walletUrl);
  await expect(bob.getByText("Mint connected", { exact: true })).toBeVisible();
  await bob.getByRole("button", { name: "Receive", exact: true }).click();
  await bob.getByLabel("Ecash token").fill(token);
  await bob.getByRole("button", { name: "Receive token", exact: true }).click();
  await expect(bob.getByTestId("balance")).toHaveText("10sats");
  const invoice = (
    await (
      await bob.request.post(mintUrl + "/test/receiver", {
        data: { amount: 7 },
      })
    ).json()
  ).request;
  await bob
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
  await bob.getByLabel("Lightning invoice").fill(invoice);
  await bob.getByLabel("Maximum routing fee (sats)").fill("3");
  await bob.getByRole("button", { name: "Review payment" }).click();
  await bob.getByRole("button", { name: "Pay 7 sats", exact: true }).click();
  await expect(bob.getByText("Paid 7 sats · 2 sat fee.")).toBeVisible();
  await expect(bob.getByTestId("balance")).toHaveText("1sats");
  await bob.request.post(mintUrl + "/test/settle", {
    data: { payment_hash: backing },
  });
  await bob.getByRole("button", { name: "Refresh backing status" }).click();
  await expect(bob.getByTestId("balance")).toHaveText("0sats");
  expect(errors).toEqual([]);
  await context.close();
});

test("retries the saved send after a lost response and browser reload", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  let dropped = false;
  await page.route(mintUrl + "/v1/swap", async (route) => {
    if (!dropped) {
      await route.fetch();
      dropped = true;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("10");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByRole("alert")).toContainText("fetch");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await expect(page.getByTestId("balance")).toHaveText("90sats");
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toHaveCount(0);
});

test("mobile navigation, invalid tokens, and offline balance visibility", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/wallet-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Receive", exact: true }).click();
  await page.getByLabel("Ecash token").fill("cashuAinvalid");
  await page
    .getByRole("button", { name: "Receive token", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("cashuPS");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Mint URL").fill("http://127.0.0.1:1");
  await page.getByRole("button", { name: "Save and connect" }).click();
  await expect(page.getByText("Mint offline", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Wallet", exact: true }).click();
  await expect(page.getByTestId("balance")).toHaveText("—sats");
});

test("exports a real encrypted wallet backup and restores it in an empty browser profile", async ({
  page,
  browser,
}) => {
  await page.goto("/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 64);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("10");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Export backup" }).click();
  await page.getByLabel("Backup password").fill("a practical test passphrase");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download encrypted backup" }).click();
  const download = await downloaded,
    buffer = await readFile((await download.path())!);
  expect(buffer.toString()).not.toContain("credential");
  const context = await browser.newContext(),
    restored = await context.newPage();
  await restored.addInitScript(
    (url) => localStorage.setItem("layer3-mint", url),
    mintUrl,
  );
  await restored.goto(walletUrl);
  await expect(
    restored.getByText("Mint connected", { exact: true }),
  ).toBeVisible();
  await restored.getByRole("button", { name: "Settings", exact: true }).click();
  await restored.getByRole("button", { name: "Restore backup" }).click();
  await restored.getByLabel("Backup file").setInputFiles({
    name: "backup.json",
    mimeType: "application/json",
    buffer,
  });
  await restored
    .getByLabel("Backup password")
    .fill("a practical test passphrase");
  await restored.getByRole("button", { name: "Restore wallet" }).click();
  await expect(restored.getByRole("dialog")).toHaveCount(0);
  await restored.getByRole("button", { name: "Wallet", exact: true }).click();
  await expect(restored.getByTestId("balance")).toHaveText("54sats");
  await restored.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    restored.getByRole("button", { name: "View token" }),
  ).toBeVisible();
  await context.close();
});
