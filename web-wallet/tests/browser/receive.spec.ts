import { test, expect, type Page } from "@playwright/test";
import {
  PSWallet,
  encodePSToken,
  psPreimageHash,
  psRandomHex,
  type PSSnapshot,
  type PSToken,
} from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "http://127.0.0.1:3339";
const warning =
  "Are you sure you want to receive this ecash that will expire in less than 20 minutes?";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    (url) => localStorage.setItem("layer3-mint", url),
    mintUrl,
  );
});

test.afterEach(async ({ page }) => {
  // Finish intercepted refreshes before Playwright closes their request context.
  await page.unrouteAll({ behavior: "wait" });
});

async function issueToken(page: Page, amount = 8): Promise<PSToken> {
  const wallet = new PSWallet(mintUrl);
  await wallet.loadMint();
  const preimage = psRandomHex();
  const quote = await wallet.createMintQuote(amount, psPreimageHash(preimage));
  const accepted = await page.request.post(mintUrl + "/test/accept", {
    data: { payment_hash: quote.payment_hash },
  });
  expect(accepted.ok()).toBe(true);
  return wallet.finishMint(
    wallet.prepareMint(await wallet.getMintQuote(quote.quote), preimage),
  );
}

async function setDeadline(
  page: Page,
  token: PSToken,
  deadline: { seconds: number } | { blocks: number },
) {
  const hashes = new Set(token.proofs.map((proof) => proof.payment_hash));
  await page.route(mintUrl + "/v1/htlcs/pending", async (route) => {
    const response = await route.fetch();
    const snapshot: PSSnapshot = await response.json();
    if ("blocks" in deadline) {
      snapshot.block_height = 1000;
      for (const backing of snapshot.htlcs) {
        const blocks = hashes.has(backing.payment_hash) ? deadline.blocks : 100;
        backing.melt_deadline_height = snapshot.block_height + blocks;
        backing.expiry_height = backing.melt_deadline_height + 6;
        backing.htlc_expiry_height = backing.expiry_height;
        backing.blocks_remaining = blocks + 6;
        backing.melt_deadline = null;
        backing.expires_at = null;
      }
    } else {
      for (const backing of snapshot.htlcs)
        if (hashes.has(backing.payment_hash))
          backing.melt_deadline = snapshot.checked_at + deadline.seconds;
    }
    await route.fulfill({ response, json: snapshot });
  });
}

async function openReceive(page: Page, token: PSToken) {
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Receive", exact: true }).click();
  await page.getByLabel("Ecash token").fill(encodePSToken(token));
}

test("checks a fresh deadline and leaves the token untouched until confirmed", async ({
  page,
}) => {
  const token = await issueToken(page);
  await openReceive(page, token);
  // The wallet's cached snapshot still has the original, distant deadline.
  await setDeadline(page, token, { seconds: 1199 });
  const messages: string[] = [];
  let accept = false;
  let swaps = 0;
  page.on("request", (request) => {
    if (request.url() === mintUrl + "/v1/swap") swaps++;
  });
  page.on("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    messages.push(dialog.message());
    if (accept) await dialog.accept();
    else await dialog.dismiss();
  });
  const receive = page.getByRole("button", {
    name: "Receive token",
    exact: true,
  });
  await receive.click();
  await expect.poll(() => messages).toEqual([warning]);
  await expect(receive).toBeEnabled();
  expect(swaps).toBe(0);
  await expect(page.getByLabel("Ecash token")).toHaveValue(
    encodePSToken(token),
  );
  await expect(page.getByTestId("balance")).toHaveText("0sats");
  await expect(
    page.getByRole("heading", { name: "Pending operations" }),
  ).toHaveCount(0);

  accept = true;
  await receive.click();
  await expect(
    page.getByText("Received 8 sats.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("8sats");
  expect(messages).toEqual([warning, warning]);
  expect(swaps).toBe(1);
});

for (const deadline of [{ seconds: 1200 }, { seconds: 1201 }, { blocks: 2 }]) {
  test(`receives without prompting at ${JSON.stringify(deadline)}`, async ({
    page,
  }) => {
    const token = await issueToken(page);
    await setDeadline(page, token, deadline);
    const messages: string[] = [];
    page.on("dialog", async (dialog) => {
      messages.push(dialog.message());
      await dialog.dismiss();
    });
    await openReceive(page, token);
    // Fix browser time behind the fresh mint snapshot for the exact boundary.
    const response = await page.request.get(mintUrl + "/v1/htlcs/pending");
    const snapshot: PSSnapshot = await response.json();
    await page.clock.setFixedTime(new Date(snapshot.checked_at * 1000));
    await page
      .getByRole("button", { name: "Receive token", exact: true })
      .click();
    await expect(page.getByTestId("balance")).toHaveText("8sats");
    expect(messages).toEqual([]);
  });
}

test("warns about an imminent block deadline and explains the estimate", async ({
  page,
}) => {
  const token = await issueToken(page);
  await setDeadline(page, token, { blocks: 1 });
  const messages: string[] = [];
  page.on("dialog", async (dialog) => {
    messages.push(dialog.message());
    await dialog.accept();
  });
  await openReceive(page, token);
  await page
    .getByRole("button", { name: "Receive token", exact: true })
    .click();
  await expect(page.getByTestId("balance")).toHaveText("8sats");
  expect(messages).toEqual([
    warning +
      "\n\nThis estimate assumes 10 minutes per block; actual block times vary.",
  ]);
});

test("warns when a later backing in a mixed token has a near deadline", async ({
  page,
}) => {
  const distant = await issueToken(page, 4);
  const urgent = await issueToken(page);
  await setDeadline(page, urgent, { seconds: 60 });
  const messages: string[] = [];
  page.on("dialog", async (dialog) => {
    messages.push(dialog.message());
    await dialog.accept();
  });
  await openReceive(page, {
    ...distant,
    proofs: [...distant.proofs, ...urgent.proofs],
  });
  await page
    .getByRole("button", { name: "Receive token", exact: true })
    .click();
  await expect(page.getByTestId("balance")).toHaveText("12sats");
  expect(messages).toEqual([warning]);
});

test("rejects an expired backing without offering to receive it", async ({
  page,
}) => {
  const token = await issueToken(page);
  await setDeadline(page, token, { seconds: 0 });
  const messages: string[] = [];
  page.on("dialog", async (dialog) => {
    messages.push(dialog.message());
    await dialog.accept();
  });
  await openReceive(page, token);
  await page
    .getByRole("button", { name: "Receive token", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("deadline has passed");
  await expect(page.getByTestId("balance")).toHaveText("0sats");
  expect(messages).toEqual([]);
});
