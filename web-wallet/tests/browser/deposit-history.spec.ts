import { test, expect } from "@playwright/test";
import { psInvoiceDetails } from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "http://127.0.0.1:3339";

test.beforeEach(async ({ page }) => {
  await page.addInitScript((url) => {
    localStorage.setItem("layer3-mint", url);
  }, mintUrl);
});

test("shows only the latest waiting invoice and keeps the full deposit history", async ({
  page,
}) => {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  const historyButton = page.getByRole("button", {
    name: /Deposit invoice history \(/,
  });
  const dialogHistoryButton = page.getByRole("dialog").getByRole("button", {
    name: "Deposit invoice history",
    exact: true,
  });
  await expect(historyButton).toHaveCount(0);

  const invoices = new Map<number, string>();
  for (const amount of [10, 20, 30]) {
    await page.getByRole("button", { name: "Add sats", exact: true }).click();
    await page.getByLabel("Amount in sats").fill(String(amount));
    await page.getByRole("button", { name: "Create funding invoice" }).click();
    await expect(page.getByLabel("Funding invoice")).toHaveValue(/^lnbcrt/);
    invoices.set(amount, await page.getByLabel("Funding invoice").inputValue());
    await page.getByRole("button", { name: "Close dialog" }).click();
    await expect(page.locator(".funding-row")).toHaveCount(1);
    await expect(page.locator(".funding-row")).toContainText(
      `${amount} sats awaiting funding`,
    );
  }

  await page.reload();
  await expect(page.locator(".funding-row")).toHaveCount(1);
  await expect(page.locator(".funding-row")).toContainText(
    "30 sats awaiting funding",
  );
  await page.locator(".funding-row").click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(
    invoices.get(30)!,
  );
  await dialogHistoryButton.click();
  const history = page.getByRole("list", { name: "Deposit invoices" });
  await expect(history.locator("strong")).toHaveText([
    "30 sats",
    "20 sats",
    "10 sats",
  ]);
  await expect(history.getByText("Awaiting payment")).toHaveCount(3);
  await expect(history.locator("time")).toHaveCount(3);

  await history.getByRole("button", { name: /^20 sats/ }).click();
  await expect(page.getByLabel("Funding invoice")).toHaveValue(
    invoices.get(20)!,
  );
  await dialogHistoryButton.click();

  // Hidden older invoices must still be claimed while history is open.
  const accepted = await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: psInvoiceDetails(invoices.get(10)!).paymentHash },
  });
  expect(accepted.ok()).toBe(true);
  await expect(history.getByRole("button", { name: /^10 sats/ })).toContainText(
    "Funded",
    { timeout: 12000 },
  );
  await expect(page.getByTestId("balance")).toHaveText("10sats");

  const closed = await page.request.post(mintUrl + "/test/settle", {
    data: { payment_hash: psInvoiceDetails(invoices.get(30)!).paymentHash },
  });
  expect(closed.ok()).toBe(true);
  await expect(history.getByRole("button", { name: /^30 sats/ })).toContainText(
    "Closed",
    { timeout: 12000 },
  );
  await page.getByRole("button", { name: "Close dialog" }).click();
  await expect(page.locator(".funding-row")).toHaveCount(1);
  await expect(page.locator(".funding-row")).toContainText(
    "20 sats awaiting funding",
  );

  await historyButton.click();
  await history.getByRole("button", { name: /^20 sats/ }).click();
  const paid = await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: psInvoiceDetails(invoices.get(20)!).paymentHash },
  });
  expect(paid.ok()).toBe(true);
  await page.getByRole("button", { name: "Check payment" }).click();
  await expect(
    page.getByRole("heading", { name: "20 sats received" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to wallet" }).click();
  await expect(page.locator(".funding-row")).toHaveCount(0);
  await expect(historyButton).toHaveText("Deposit invoice history (3)");

  // History remains available without pending invoices or a mint connection.
  await page.route(mintUrl + "/**", (route) => route.abort("failed"));
  await page.reload();
  await expect(page.getByText("Mint offline", { exact: true })).toBeVisible();
  await expect(page.locator(".funding-row")).toHaveCount(0);
  await historyButton.click();
  await expect(history.locator("strong")).toHaveText([
    "30 sats",
    "20 sats",
    "10 sats",
  ]);
  await expect(history.getByText("Funded", { exact: true })).toHaveCount(2);
  await expect(history.getByText("Closed", { exact: true })).toHaveCount(1);
  await history.getByRole("button", { name: /^30 sats/ }).click();
  await expect(
    page.getByRole("heading", { name: "This invoice has closed." }),
  ).toBeVisible();
  await dialogHistoryButton.click();
  await history.getByRole("button", { name: /^10 sats/ }).click();
  await expect(
    page.getByRole("heading", { name: "10 sats received" }),
  ).toBeVisible();
});
