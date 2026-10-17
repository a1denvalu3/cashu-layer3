import { test, expect, type Page } from "@playwright/test";
import { bech32 } from "@scure/base";
import QRCode from "qrcode";
import { psInvoiceDetails } from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "http://127.0.0.1:3339";
const endpoint = "https://pay.example/.well-known/lnurlp/alice";
const metadata = '[["text/plain","Coffee for Alice"]]';
const payRequest = {
  tag: "payRequest",
  callback: "https://pay.example/pay?recipient=alice",
  minSendable: 1000,
  maxSendable: 50000,
  metadata,
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    (url) => localStorage.setItem("layer3-mint", url),
    mintUrl,
  );
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("100");
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
  const paymentHash = psInvoiceDetails(
    await page.getByLabel("Funding invoice").inputValue(),
  ).paymentHash;
  await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: paymentHash },
  });
  await page.getByRole("button", { name: "Check payment" }).click();
  await page.getByRole("button", { name: "Back to wallet" }).click();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
});

async function receiver(page: Page, amount: number, description = metadata) {
  const response = await page.request.post(mintUrl + "/test/receiver", {
    data: { amount, metadata: description },
  });
  expect(response.ok()).toBe(true);
  return { pr: (await response.json()).request as string };
}

async function service(
  page: Page,
  options: {
    lookup?: (url: URL) => unknown | Promise<unknown>;
    invoice?: (amount: number) => Promise<unknown>;
  } = {},
) {
  const amounts: number[] = [];
  const lookups: string[] = [];
  const meltRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().startsWith(mintUrl + "/v1/melt/"))
      meltRequests.push(request.url());
  });
  await page.route("https://pay.example/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/.well-known/lnurlp/")) {
      lookups.push(url.href);
      await route.fulfill({
        json: options.lookup ? await options.lookup(url) : payRequest,
      });
    } else {
      const amount = Number(url.searchParams.get("amount")) / 1000;
      amounts.push(amount);
      await route.fulfill({
        json: options.invoice
          ? await options.invoice(amount)
          : await receiver(page, amount),
      });
    }
  });
  return { amounts, lookups, meltRequests };
}

test("detects a Lightning address, fetches its invoice, and pays only after confirmation on mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 576 });
  const requests = await service(page);
  await page
    .getByLabel("Lightning invoice or address")
    .fill("alice@pay.example");
  await expect(page.getByText("pay.example · Coffee for Alice")).toBeVisible();
  expect(requests.lookups).toEqual([endpoint]);
  expect(requests.amounts).toEqual([]);
  expect(requests.meltRequests).toEqual([]);
  const amount = page.getByLabel("Amount in sats");
  await expect(amount).toHaveAttribute("min", "1");
  await expect(amount).toHaveAttribute("max", "50");
  for (const invalid of ["0", "51", "10.5"]) {
    await amount.fill(invalid);
    await page.getByRole("button", { name: "Review payment" }).click();
    expect(requests.amounts).toEqual([]);
  }
  await amount.fill("10");
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Paying alice@pay.example")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Ready to pay?" }),
  ).toBeInViewport();
  await expect(page.getByText("up to 2 sats", { exact: true })).toBeVisible();
  expect(requests.amounts).toEqual([10]);
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("button", { name: "Pay 10 sats", exact: true }).click();
  await expect(page.getByText("Paid 10 sats · 2 sat fee.")).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("88sats");
});

test("scans an uppercase LNURL and automatically fills its fixed amount", async ({
  page,
}) => {
  const requests = await service(page, {
    lookup: () => ({ ...payRequest, minSendable: 10000, maxSendable: 10000 }),
  });
  const encoded = bech32
    .encode("lnurl", bech32.toWords(new TextEncoder().encode(endpoint)), 4096)
    .toUpperCase();
  await page.getByLabel("Invoice QR image").setInputFiles({
    name: "lnurl.png",
    mimeType: "image/png",
    buffer: await QRCode.toBuffer(`LIGHTNING:${encoded}`, { width: 600 }),
  });
  await expect(page.getByLabel("Amount in sats")).toHaveValue("10");
  expect(requests.amounts).toEqual([]);
  expect(requests.meltRequests).toEqual([]);
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
});

test("shows lookup and callback errors and retries without spending funds", async ({
  page,
}) => {
  let lookups = 0;
  let invoices = 0;
  const requests = await service(page, {
    lookup: () =>
      ++lookups === 1
        ? { status: "ERROR", reason: "Recipient temporarily unavailable" }
        : payRequest,
    invoice: async (amount) =>
      ++invoices === 1
        ? { status: "ERROR", reason: "Invoice creation failed" }
        : receiver(page, amount),
  });
  await page
    .getByLabel("Lightning invoice or address")
    .fill("alice@pay.example");
  await expect(page.getByRole("alert")).toContainText(
    "Recipient temporarily unavailable",
  );
  await page.getByRole("button", { name: "Retry lookup" }).click();
  await page.getByLabel("Amount in sats").fill("10");
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Invoice creation failed",
  );
  expect(requests.meltRequests).toEqual([]);
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toBeVisible();
  expect(lookups).toBe(2);
});

test("ignores an older address lookup after the recipient changes", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests = await service(page, {
    lookup: async (url) => {
      if (url.pathname.endsWith("alice")) {
        await gate;
        return payRequest;
      }
      return { ...payRequest, metadata: '[["text/plain","Payment for Bob"]]' };
    },
  });
  await page
    .getByLabel("Lightning invoice or address")
    .fill("alice@pay.example");
  await expect.poll(() => requests.lookups.length).toBe(1);
  await page.getByLabel("Lightning invoice or address").fill("bob@pay.example");
  await expect(page.getByText("pay.example · Payment for Bob")).toBeVisible();
  release();
  await expect(page.getByText("pay.example · Coffee for Alice")).toHaveCount(0);
  expect(requests.amounts).toEqual([]);
  expect(requests.meltRequests).toEqual([]);
});

test("discards an LNURL invoice if its amount changes during the callback", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests = await service(page, {
    invoice: async (amount) => {
      if (amount === 10) await gate;
      return receiver(page, amount);
    },
  });
  await page
    .getByLabel("Lightning invoice or address")
    .fill("alice@pay.example");
  await page.getByLabel("Amount in sats").fill("10");
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect.poll(() => requests.amounts).toEqual([10]);
  await page.getByLabel("Amount in sats").fill("20");
  release();
  await expect(
    page.getByRole("button", { name: "Review payment" }),
  ).toBeEnabled();
  expect(requests.meltRequests).toEqual([]);
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 20 sats", exact: true }),
  ).toBeVisible();
  expect(requests.amounts).toEqual([10, 20]);
  await page.getByRole("button", { name: "Pay 20 sats", exact: true }).click();
  await expect(page.getByText("Paid 20 sats · 2 sat fee.")).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("78sats");
});

for (const mismatch of ["amount", "metadata"] as const) {
  test(`rejects an LNURL invoice with mismatched ${mismatch} before requesting a mint quote`, async ({
    page,
  }) => {
    const requests = await service(page, {
      invoice: (amount) =>
        receiver(
          page,
          mismatch === "amount" ? amount + 1 : amount,
          mismatch === "metadata"
            ? '[["text/plain","Another recipient"]]'
            : metadata,
        ),
    });
    await page
      .getByLabel("Lightning invoice or address")
      .fill("alice@pay.example");
    await page.getByLabel("Amount in sats").fill("10");
    await page.getByRole("button", { name: "Review payment" }).click();
    await expect(page.getByRole("alert")).toContainText(
      mismatch === "amount"
        ? "amount does not match"
        : "does not match the payment metadata",
    );
    expect(requests.meltRequests).toEqual([]);
    await expect(page.getByTestId("balance")).toHaveText("100sats");
  });
}
