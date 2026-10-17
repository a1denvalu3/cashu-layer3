import { test, expect, type Page } from "@playwright/test";
import QRCode from "qrcode";
import { psInvoiceDetails } from "@cashu/cashu-ts/tmp-cash";

const mintUrl = "http://127.0.0.1:3339";

test.beforeEach(async ({ page }) => {
  await page.addInitScript((url) => {
    localStorage.setItem("layer3-mint", url);
    // Exercise the bundled decoder, including its worker and production CSP.
    Reflect.deleteProperty(window, "BarcodeDetector");
  }, mintUrl);
  await page.goto("/wallet/");
  await expect(page.getByText("Mint connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add sats", exact: true }).click();
  await page.getByLabel("Amount in sats").fill("100");
  await page.getByRole("button", { name: "Create funding invoice" }).click();
  const fundingInvoice = page.getByLabel("Funding invoice");
  await expect(fundingInvoice).toHaveValue(/^lnbcrt/);
  await page.request.post(mintUrl + "/test/accept", {
    data: {
      payment_hash: psInvoiceDetails(await fundingInvoice.inputValue())
        .paymentHash,
    },
  });
  await page.getByRole("button", { name: "Check payment" }).click();
  await page.getByRole("button", { name: "Back to wallet" }).click();
});

async function receiverInvoice(page: Page, amount = 10): Promise<string> {
  const response = await page.request.post(mintUrl + "/test/receiver", {
    data: { amount },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).request;
}

async function openPayment(page: Page) {
  await page
    .getByRole("button", { name: "Pay with Lightning", exact: true })
    .click();
}

async function uploadQR(page: Page, value: string) {
  await page.getByLabel("Invoice QR image").setInputFiles({
    name: "invoice.png",
    mimeType: "image/png",
    buffer: await QRCode.toBuffer(value, { width: 600 }),
  });
}

type CameraState = {
  tracks: MediaStreamTrack[];
  requests: number;
  release?: () => void;
};

async function mockCamera(page: Page, value = "", pending = false) {
  const imageUrl = value ? await QRCode.toDataURL(value, { width: 440 }) : "";
  await page.evaluate(
    ({ imageUrl, pending }) => {
      const state: CameraState = { tracks: [], requests: 0 };
      Object.assign(window, { invoiceTestCamera: state });
      Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
        value: async () => {
          state.requests++;
          if (pending)
            await new Promise<void>((resolve) => {
              state.release = resolve;
            });
          const canvas = document.createElement("canvas");
          canvas.width = canvas.height = 800;
          const context = canvas.getContext("2d")!;
          context.fillStyle = "white";
          context.fillRect(0, 0, 800, 800);
          const image = new Image();
          if (imageUrl) {
            image.src = imageUrl;
            await image.decode();
          }
          const draw = () => {
            if (imageUrl) context.drawImage(image, 180, 180, 440, 440);
            else context.fillRect(0, 0, 800, 800);
          };
          draw();
          const stream = canvas.captureStream(10);
          const timer = setInterval(draw, 100);
          for (const track of stream.getTracks()) {
            state.tracks.push(track);
            const stop = track.stop.bind(track);
            track.stop = () => {
              clearInterval(timer);
              stop();
            };
          }
          return stream;
        },
      });
    },
    { imageUrl, pending },
  );
}

async function cameraState(page: Page) {
  return page.evaluate(() => {
    const state = (window as unknown as { invoiceTestCamera: CameraState })
      .invoiceTestCamera;
    return {
      requests: state.requests,
      tracks: state.tracks.map((track) => track.readyState),
    };
  });
}

test("reads invoice QR images and pays only after review and confirmation", async ({
  page,
}) => {
  const invoice = await receiverInvoice(page);
  const replacement = await receiverInvoice(page, 20);
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/v1/melt/")) requests.push(request.url());
  });
  await openPayment(page);
  await uploadQR(page, invoice);
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(invoice);
  await uploadQR(page, `LIGHTNING:${replacement.toUpperCase()}`);
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(replacement);
  expect(requests).toEqual([]);
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(page.getByText("up to 2 sats", { exact: true })).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("100sats");
  await page.getByRole("button", { name: "Pay 20 sats", exact: true }).click();
  await expect(page.getByText("Paid 20 sats · 2 sat fee.")).toBeVisible();
  await expect(page.getByTestId("balance")).toHaveText("78sats");
});

test("rejects unrelated QR codes and unreadable images while preserving manual entry", async ({
  page,
}) => {
  const invoice = await receiverInvoice(page);
  await openPayment(page);
  await page
    .getByLabel("Lightning invoice or address", { exact: true })
    .fill(invoice);
  await uploadQR(page, "bitcoin:bc1qexample");
  await expect(page.getByRole("alert")).toHaveText(
    "This QR code does not contain a valid Lightning invoice, Lightning address, or LNURL.",
  );
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(invoice);
  await page.getByLabel("Invoice QR image").setInputFiles({
    name: "broken.png",
    mimeType: "image/png",
    buffer: Buffer.from("not an image"),
  });
  await expect(page.getByRole("alert")).toHaveText(
    "Could not read a QR code. Choose a clear image of a Lightning invoice, address, or LNURL.",
  );
  await uploadQR(page, invoice.toUpperCase());
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(invoice);
  await page.getByRole("button", { name: "Review payment" }).click();
  await expect(
    page.getByRole("button", { name: "Pay 10 sats", exact: true }),
  ).toBeVisible();
});

test("scans real camera frames and releases the camera after a valid invoice", async ({
  page,
}) => {
  const invoice = await receiverInvoice(page);
  await mockCamera(page, `lightning:${invoice}`);
  await openPayment(page);
  expect((await cameraState(page)).requests).toBe(0);
  await page.getByRole("button", { name: "Scan QR code", exact: true }).click();
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(invoice);
  await expect(page.getByLabel("Invoice QR camera")).toHaveCount(0);
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 1,
      tracks: ["ended"],
    });
  await expect(
    page.getByRole("button", { name: "Review payment" }),
  ).toBeVisible();
});

test("releases the camera on stop and when the payment dialog closes", async ({
  page,
}) => {
  await mockCamera(page);
  await openPayment(page);
  await page.getByRole("button", { name: "Scan QR code", exact: true }).click();
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 1,
      tracks: ["live"],
    });
  await page
    .getByRole("button", { name: "Stop scanning", exact: true })
    .click();
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 1,
      tracks: ["ended"],
    });
  await page.getByRole("button", { name: "Scan QR code", exact: true }).click();
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 2,
      tracks: ["ended", "live"],
    });
  await page.getByRole("button", { name: "Close dialog" }).click();
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 2,
      tracks: ["ended", "ended"],
    });
  await openPayment(page);
  await expect(page.getByLabel("Invoice QR camera")).toHaveCount(0);
});

test("releases camera access granted after the payment dialog has closed", async ({
  page,
}) => {
  await mockCamera(page, "", true);
  await openPayment(page);
  await page.getByRole("button", { name: "Scan QR code", exact: true }).click();
  await expect.poll(async () => (await cameraState(page)).requests).toBe(1);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.evaluate(() => {
    (window as unknown as { invoiceTestCamera: CameraState }).invoiceTestCamera
      .release!();
  });
  await expect
    .poll(() => cameraState(page))
    .toEqual({
      requests: 1,
      tracks: ["ended"],
    });
});

test("keeps upload and paste available when camera permission is denied", async ({
  page,
}) => {
  await page.evaluate(() => {
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      value: async () => {
        throw new DOMException("Permission denied", "NotAllowedError");
      },
    });
  });
  const invoice = await receiverInvoice(page);
  await openPayment(page);
  await page.getByRole("button", { name: "Scan QR code", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Could not access the camera",
  );
  await expect(page.getByLabel("Invoice QR camera")).toHaveCount(0);
  await uploadQR(page, invoice);
  await expect(
    page.getByLabel("Lightning invoice or address", { exact: true }),
  ).toHaveValue(invoice);
  await expect(page.getByRole("alert")).toHaveCount(0);
});
