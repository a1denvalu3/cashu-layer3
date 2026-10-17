import { test, expect, type Page } from "@playwright/test";
import { psInvoiceDetails, type PSSnapshot } from "@cashu/cashu-ts/tmp-cash";
import { readFile } from "node:fs/promises";

const mintUrl = "http://127.0.0.1:3339";
const walletUrl = `http://127.0.0.1:${process.env.WALLET_E2E_PORT || "5173"}/wallet/`;
test.beforeEach(async ({ page }) => {
  await page.addInitScript((url) => {
    if (!localStorage.getItem("layer3-mint"))
      localStorage.setItem("layer3-mint", url);
  }, mintUrl);
});

test("keeps balances and backings visible when the saved mint URL has a trailing slash", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  await page.evaluate(
    (url) => localStorage.setItem("layer3-mint", url + "/"),
    mintUrl,
  );
  await page.reload();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await expect(
    page.getByRole("heading", { name: "Your backings 1" }),
  ).toBeVisible();
  await expect(page.getByText("Added ecash", { exact: true })).toBeVisible();
});

test("continues polling newer deposits when an older quote is unavailable", async ({
  page,
}) => {
  const quotes: string[] = [];
  await page.route(mintUrl + "/v1/mint/quote/bolt11", async (route) => {
    const response = await route.fetch();
    quotes.push((await response.json()).quote);
    await route.fulfill({ response });
  });
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  for (const value of [10, 100]) {
    await page.getByRole("button", { name: "Add sats", exact: true }).click();
    await page.getByLabel("Amount in sats").fill(String(value));
    await page.getByRole("button", { name: "Create funding invoice" }).click();
    await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
    const hash = psInvoiceDetails(
      await page.getByLabel("Funding invoice").inputValue(),
    ).paymentHash;
    await page.getByRole("button", { name: "Close dialog" }).click();
    if (value === 10)
      await page.route(
        mintUrl + "/v1/mint/quote/bolt11/" + quotes[0],
        (route) =>
          route.fulfill({
            status: 503,
            contentType: "application/json",
            json: { detail: "This older quote is unavailable" },
          }),
      );
    else
      await page.request.post(mintUrl + "/test/accept", {
        data: { payment_hash: hash },
      });
  }
  await expect(page.getByTestId("balance")).toHaveText("100sats", {
    timeout: 12000,
  });
  await expect(page.getByText("10 sats awaiting funding")).toBeVisible();
});

test("opens with a malformed saved preference and preserves funds when reconnecting", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  await page.route("http://127.0.0.1:3338/**", (route) =>
    route.abort("failed"),
  );
  await page.evaluate(() =>
    localStorage.setItem("layer3-mint", "javascript:invalid-preference"),
  );
  await page.reload();
  await expect(page.getByText("Mint offline", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Mint URL").fill(mintUrl);
  await page.getByRole("button", { name: "Save and connect" }).click();
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Wallet", exact: true }).click();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
});
async function fund(page: Page, value: number) {
  const balance = Number(
    (await page.getByTestId("balance").innerText()).replace(/\D/g, ""),
  );
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
  await expect(page.getByTestId("balance")).toHaveText(
    `${(balance + value).toLocaleString("en-US")}sats`,
  );
  return hash;
}

for (const deadline of ["time", "block"] as const) {
  test(`keeps ${deadline}-expired backings and activity visible across reload and offline, then clears a false expiry`, async ({
    page,
  }) => {
    await page.goto("/wallet/");
    await expect(
      page.getByText("Mint connected", { exact: true }),
    ).toBeVisible();
    const hash = await fund(page, 100);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await page.getByLabel("Amount in sats").fill("10");
    await page.getByRole("button", { name: "Create token" }).click();
    await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
    await page.getByRole("button", { name: "Close dialog" }).click();
    await fund(page, 32);

    await page.route(mintUrl + "/v1/htlcs/check", async (route) => {
      const response = await route.fetch();
      const snapshot: PSSnapshot = await response.json();
      if (deadline === "block") snapshot.block_height = 1000;
      for (const backing of snapshot.htlcs) {
        if (deadline === "time") {
          if (backing.payment_hash === hash)
            backing.melt_deadline = snapshot.checked_at - 1;
        } else {
          backing.melt_deadline_height =
            backing.payment_hash === hash ? 1000 : 1100;
          backing.expiry_height = backing.melt_deadline_height + 6;
          backing.htlc_expiry_height = backing.expiry_height;
          backing.blocks_remaining =
            backing.expiry_height - snapshot.block_height!;
        }
      }
      await route.fulfill({ response, json: snapshot });
    });
    await page.getByRole("button", { name: "Refresh backing status" }).click();
    await expect(page.getByTestId("balance")).toHaveText("32sats");
    await expect(
      page.getByRole("heading", { name: "Your backings 1" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Expired backings 1" }),
    ).toBeVisible();
    const card = page.locator(".backing.expired");
    await expect(card).toHaveCount(1);
    await expect(card.locator(".backing-amount")).toHaveText("90 sats");
    await expect(card).toContainText("Also 10 sats in shared tokens.");
    await expect(card.getByRole("button")).toHaveCount(0);
    const observed = await card.getByText(/^Observed /).textContent();
    await page.getByRole("button", { name: "Refresh backing status" }).click();
    await page.reload();
    await expect(
      page.getByText("Mint connected", { exact: true }),
    ).toBeVisible();
    await expect(card.getByText(/^Observed /)).toHaveText(observed!);
    await page.getByRole("button", { name: "Activity", exact: true }).click();
    const expiry = page
      .locator(".activity-row")
      .filter({ hasText: "Backing expired" });
    await expect(expiry).toHaveCount(1);
    await expect(expiry.locator(".activity-amount")).toHaveText("−90");
    await expect(expiry).toContainText("10 sats in shared tokens.");

    if (deadline === "time") {
      for (const width of [320, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        for (const colorScheme of ["light", "dark"] as const) {
          await page.emulateMedia({ colorScheme });
          for (const tab of ["Wallet", "Activity"]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            const contained = await page
              .locator(
                ".backing.expired, .activity-row, .activity-name, .activity-value",
              )
              .evaluateAll((elements) =>
                elements.every((element) => {
                  const bounds = element.getBoundingClientRect();
                  return (
                    bounds.left >= 0 &&
                    bounds.right <= document.documentElement.clientWidth + 1 &&
                    element.scrollWidth <= element.clientWidth + 1
                  );
                }),
              );
            expect(contained).toBe(true);
          }
        }
      }
      await page.getByRole("button", { name: "Wallet", exact: true }).click();
      await page.screenshot({
        path: "test-results/wallet-expired-backings.png",
        fullPage: true,
        animations: "disabled",
      });
    }

    await page.route(mintUrl + "/**", (route) => route.abort("failed"));
    await page.reload();
    await expect(page.getByText("Mint offline", { exact: true })).toBeVisible();
    await expect(card.getByText(/^Observed /)).toHaveText(observed!);
    await expect(
      page.getByText("A little cash. A little freedom."),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Activity", exact: true }).click();
    await expect(expiry).toHaveCount(1);

    // The actual backing is live: a successful recheck must remove false history.
    await page.unrouteAll({ behavior: "wait" });
    await page.reload();
    await expect(page.getByTestId("balance")).toHaveText("122sats");
    await expect(card).toHaveCount(0);
    await page.getByRole("button", { name: "Activity", exact: true }).click();
    await expect(expiry).toHaveCount(0);
  });
}

test("records expiry of fully shared tokens without counting another balance loss", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  const hash = await fund(page, 32);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("32");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByLabel("Outgoing token")).toHaveValue(/^cashuPS/);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.request.post(mintUrl + "/test/settle", {
    data: { payment_hash: hash },
  });
  await page.getByRole("button", { name: "Refresh backing status" }).click();
  await expect(page.getByTestId("balance")).toHaveText("0sats");
  await expect(page.locator(".backing.expired .backing-amount")).toHaveText(
    "32 sats shared",
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  const expiry = page
    .locator(".activity-row")
    .filter({ hasText: "Backing expired" });
  await expect(expiry).toHaveCount(1);
  await expect(expiry.locator(".activity-amount")).toHaveText("32");
  await expect(expiry.locator(".activity-value")).toContainText(
    "Shared tokens",
  );
});

test("discards a quote when the invoice changes during its request", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  const invoices: string[] = [];
  for (let i = 0; i < 2; i++) {
    const response = await page.request.post(mintUrl + "/test/receiver", {
      data: { amount: 10 },
    });
    invoices.push((await response.json()).request);
  }
  const [invoiceA, invoiceB] = invoices;
  const quotes = new Map<string, string>();
  let releaseQuote!: () => void;
  const quoteGate = new Promise<void>((resolve) => {
    releaseQuote = resolve;
  });
  await page.route(mintUrl + "/v1/melt/quote/bolt11", async (route) => {
    const request = route.request().postDataJSON().request as string;
    const response = await route.fetch();
    quotes.set(request, (await response.json()).quote);
    if (request === invoiceA) await quoteGate;
    await route.fulfill({ response });
  });

  await page
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
  await page.getByLabel("Lightning invoice").fill(invoiceA);
  await page.getByRole("button", { name: "Review payment" }).click();
  await page.getByLabel("Lightning invoice").fill(invoiceB);
  releaseQuote();
  await expect(page.getByLabel("Lightning invoice")).toHaveValue(invoiceB);
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toBeVisible();
  let paidQuote = "";
  await page.route(mintUrl + "/v1/melt/bolt11", async (route) => {
    paidQuote = route.request().postDataJSON().quote;
    await route.continue();
  });
  await page.getByRole("button", { name: "Pay 10 sats", exact: true }).click();
  await expect(page.getByText("Paid 10 sats · 2 sat fee.")).toBeVisible();
  expect(paidQuote).toBe(quotes.get(invoiceB));
  expect(paidQuote).not.toBe(quotes.get(invoiceA));
});

test("completes a pending payment without a manual retry or a second payment", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  const requests: string[] = [];
  await page.route(mintUrl + "/v1/melt/bolt11", async (route) => {
    requests.push(route.request().postData()!);
    if (requests.length === 1) {
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "melt payment is pending; retry the same request",
        }),
      });
    } else await route.continue();
  });
  const invoice = (
    await (
      await page.request.post(mintUrl + "/test/receiver", {
        data: { amount: 10 },
      })
    ).json()
  ).request;
  await page
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
  await page.getByLabel("Lightning invoice").fill(invoice);
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(page.getByText("up to 2 sats", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Pay 10 sats", exact: true }).click();
  await expect(page.getByText("Paid 10 sats · 2 sat fee.")).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("88sats");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toHaveCount(0);
  expect(requests).toHaveLength(2);
  expect(requests[0]).toBe(requests[1]);
});

test("shows a slow payment as pending while preserving its request", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await fund(page, 100);
  await page.route(mintUrl + "/v1/melt/bolt11", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "melt payment is pending; retry the same request",
      }),
    }),
  );
  const invoice = (
    await (
      await page.request.post(mintUrl + "/test/receiver", {
        data: { amount: 10 },
      })
    ).json()
  ).request;
  await page
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
  await page.getByLabel("Lightning invoice").fill(invoice);
  await page.getByRole("button", { name: "Review payment" }).click();
  await page.getByRole("button", { name: "Pay 10 sats", exact: true }).click();
  await expect(
    page.getByText(
      "Payment pending. Your request is saved; use Retry to check it safely.",
    ),
  ).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("36sats");
});

test("mints after closing the invoice and recovers falsely expired proofs on reload", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("100");
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
  const hash = psInvoiceDetails(
    await page.getByLabel("Funding invoice").inputValue(),
  ).paymentHash;
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: hash },
  });
  await expect(page.getByTestId("balance")).toHaveText("100sats", {
    timeout: 15000,
  });
  // Reproduce the old wallet's persisted state after a stale snapshot arrived.
  await page.evaluate(async () => {
    await new Promise<void>((resolve, reject) => {
      const opening = indexedDB.open("cashu_layer3_wallet_v3");
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const db = opening.result,
          transaction = db.transaction("proofs", "readwrite"),
          cursor = transaction.objectStore("proofs").openCursor();
        cursor.onsuccess = () => {
          const row = cursor.result;
          if (!row) return;
          row.update({ ...row.value, state: "expired" });
          row.continue();
        };
        transaction.oncomplete = () => {
          db.close();
          resolve();
        };
        transaction.onabort = () => {
          db.close();
          reject(transaction.error);
        };
      };
    });
  });
  await page.reload();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await expect(
    page.getByRole("heading", { name: "Your backings 1" }),
  ).toBeVisible();
  await expect(page.getByText("Added ecash", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toHaveCount(0);
});

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
  await page.goto("/wallet/");
  await expect(page).toHaveTitle("tmp.cash wallet — Cash with a deadline");
  await expect(page.getByRole("link", { name: "tmp.cash home" })).toBeVisible();
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByText(/New tokens: 2,016 blocks \(about 14 days\)/),
  ).toBeVisible();
  await expect(page.getByText(/Set by the mint/)).toBeVisible();
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
});

test("shows Spark's minimum CLTV separately from token lifetime", async ({
  page,
}) => {
  await page.route("**/v1/info", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        default_token_lifetime_blocks: null,
        funding_htlc_cltv_delta: 60,
      },
    });
  });
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByText(/Minimum Lightning funding timelock: 60 blocks/),
  ).toBeVisible();
  await expect(page.getByText(/New tokens:/)).toHaveCount(0);
});

test("fund, reload, send, receive in a second wallet, melt and expire residual change", async ({
  page,
  browser,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/wallet/");
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
  await expect(bob.getByRole("spinbutton")).toHaveCount(0);
  await bob.getByRole("button", { name: "Review payment" }).click();
  await expect(
    bob.getByText("Mint fee reserve", { exact: true }),
  ).toBeVisible();
  await expect(bob.getByText("up to 2 sats", { exact: true })).toBeVisible();
  await bob.getByRole("button", { name: "Pay 7 sats", exact: true }).click();
  await expect(bob.getByText("Paid 7 sats · 2 sat fee.")).toBeVisible();
  await expect(bob.getByTestId("balance")).toHaveText("1sats");
  await bob.request.post(mintUrl + "/test/settle", {
    data: { payment_hash: backing },
  });
  await bob.getByRole("button", { name: "Refresh backing status" }).click();
  await expect(bob.getByTestId("balance")).toHaveText("0sats");
  await expect(bob.getByTestId("expired-balance")).toContainText(
    "Expired token records: 1 sats",
  );
  expect(errors).toEqual([]);
  await context.close();
});

test("retries the saved send after a lost response and browser reload", async ({
  page,
}) => {
  await page.goto("/wallet/");
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
  await page.goto("/wallet/");
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
  await expect(
    page.getByRole("button", { name: "Receive token", exact: true }),
  ).toBeDisabled();
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
  await page.goto("/wallet/");
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
