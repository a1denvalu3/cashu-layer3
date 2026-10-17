import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bech32 } from "@scure/base";
import {
  psLightningTarget,
  psLnurlPayRequest,
  psLnurlInvoice,
  type PSLnurlPayRequest,
} from "@cashu/cashu-ts/tmp-cash";

const metadata = '[["text/plain","Coffee for Alice"]]';
// Signed 10-sat regtest invoices, issued at 2024-01-01T00:00:00Z.
const hashed =
  "lnbcrt100n1pjeyqyqpp5yg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3qsp5xvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxveshp5x4zcrgpnfyt5hhw3k5cvsgqk37rpsxm326xxmhefas7emh9v20csxqrrss4ur5sdzdvpqvvk7zclxfv4wtwkv3u3wqqz5t37rtc3fsw9w4slqs80y0xdhjqcwuakk93gu8lknnz92upahx7xykn2peu49fdjfrgcsq8thppa";
const plain =
  "lnbcrt100n1pjeyqyqpp5yg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3qsp5xvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvesdq6gdhkven9v5sxvmmjypqkc6trv5xqrrss9taxjln3pdp73hdrjnku95z97r8yhknupr8z85s359vnw5ddkj44x9j4jp2jz4xera968jcx74w3kwrjs8rjkyljnygny5ryrfxk92sqldjayn";
const defaultExpiry =
  "lnbcrt100n1pjeyqyqpp5yg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3qsp5xvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxveshp5x4zcrgpnfyt5hhw3k5cvsgqk37rpsxm326xxmhefas7emh9v20csxzkqmus74vg23rsmz7a2fqtd2eu0e8wes6lgmcgq422mewwuy6hq59w687szn2qlkc45uzhdjauv7mka2h92arsaw9xw3cngglcfh3sp73fpev";
const endpoint = "https://pay.example/.well-known/lnurlp/alice";
const response = {
  tag: "payRequest",
  callback: "https://api.example/pay?recipient=alice&amount=1",
  minSendable: 1001,
  maxSendable: 50999,
  metadata,
};
const pay: PSLnurlPayRequest = {
  url: endpoint,
  domain: "pay.example",
  callback: response.callback,
  metadata,
  description: "Coffee for Alice",
  minSats: 2,
  maxSats: 50,
};
const fetchMock = vi.fn<typeof fetch>();
function reply(body: unknown, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), { status }),
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2024-01-01T00:01:00Z"));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Lightning input detection", () => {
  it.each([
    "alice@pay.example",
    "LIGHTNING:ALICE@PAY.EXAMPLE",
    " alice@pay.example ",
  ])("recognizes address %s without making a request", (input) => {
    expect(psLightningTarget(input)).toEqual({
      kind: "lnurl",
      url: endpoint,
      label: "alice@pay.example",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it("encodes address tags and preserves case-sensitive URL paths", () => {
    expect(psLightningTarget("alice+coffee@pay.example")).toMatchObject({
      url: "https://pay.example/.well-known/lnurlp/alice%2Bcoffee",
    });
    expect(
      psLightningTarget("LNURLP://pay.example/Pay/Alice?secret=AbC"),
    ).toMatchObject({ url: "https://pay.example/Pay/Alice?secret=AbC" });
  });
  it("decodes uppercase LNURL QR payloads", () => {
    const encoded = bech32.encode(
      "lnurl",
      bech32.toWords(new TextEncoder().encode(endpoint)),
      4096,
    );
    expect(
      psLightningTarget(`LIGHTNING:${encoded.toUpperCase()}`),
    ).toMatchObject({ kind: "lnurl", url: endpoint });
    expect(() => psLightningTarget(encoded.slice(0, -1) + "q")).toThrow(
      "Invalid LNURL encoding",
    );
    expect(() =>
      psLightningTarget(encoded.slice(0, 10).toUpperCase() + encoded.slice(10)),
    ).toThrow("Invalid LNURL encoding");
  });
  it("retains valid invoice support", () => {
    expect(psLightningTarget(`lightning:${hashed.toUpperCase()}`)).toEqual({
      kind: "invoice",
      invoice: hashed,
    });
  });
  it.each([
    "alice@pay.example/path",
    "alice@@pay.example",
    "hello",
    "",
    "javascript:alert(1)",
    "http://pay.example/pay",
    "https://user:password@pay.example/pay",
    "https://pay.example/pay#fragment",
  ])("rejects malformed or insecure input %s", (input) => {
    expect(() => psLightningTarget(input)).toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("LNURL-pay lookup", () => {
  it("reads metadata, rounds limits to whole sats, and omits browser credentials", async () => {
    reply(response);
    await expect(psLnurlPayRequest(endpoint)).resolves.toEqual(pay);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    });
  });
  it.each([
    [
      { ...response, tag: "withdrawRequest" },
      "valid Lightning payment request",
    ],
    [{ ...response, minSendable: 60000 }, "whole-satoshi"],
    [{ ...response, minSendable: 1001, maxSendable: 1999 }, "whole-satoshi"],
    [
      { ...response, maxSendable: Number.MAX_SAFE_INTEGER + 1 },
      "valid Lightning payment request",
    ],
    [{ ...response, metadata: "not json" }, "invalid payment metadata"],
    [
      { ...response, metadata: '[["image/png;base64","image"]]' },
      "payment description",
    ],
    [{ ...response, callback: "http://pay.example/pay" }, "HTTPS"],
    [
      { ...response, payerData: { email: { mandatory: true } } },
      "payer information",
    ],
  ])("rejects invalid payment requests", async (body, message) => {
    reply(body);
    await expect(psLnurlPayRequest(endpoint)).rejects.toThrow(message);
  });
  it("surfaces service errors even on an HTTP error response", async () => {
    reply({ status: "ERROR", reason: "Unknown recipient" }, 404);
    await expect(psLnurlPayRequest(endpoint)).rejects.toThrow(
      "Unknown recipient",
    );
  });
  it("reports network and invalid JSON failures", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(psLnurlPayRequest(endpoint)).rejects.toThrow(
      "Could not reach",
    );
    fetchMock.mockResolvedValueOnce(new Response("<html>Bad gateway</html>"));
    await expect(psLnurlPayRequest(endpoint)).rejects.toThrow(
      "invalid response",
    );
  });
});

describe("LNURL invoice validation", () => {
  it.each([hashed, plain, defaultExpiry])(
    "validates signed invoices and requests millisatoshis",
    async (invoice) => {
      reply({ pr: invoice.toUpperCase() });
      await expect(psLnurlInvoice(pay, 10)).resolves.toBe(invoice);
      const url = new URL(String(fetchMock.mock.calls[0][0]));
      expect(url.searchParams.getAll("amount")).toEqual(["10000"]);
      expect(url.searchParams.get("recipient")).toBe("alice");
    },
  );
  it.each([0, 1, 51, 10.5, NaN, Infinity])(
    "rejects invalid amounts before requesting an invoice",
    async (amount) => {
      await expect(psLnurlInvoice(pay, amount)).rejects.toThrow(
        "Enter an amount",
      );
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
  it("rejects wrong amounts, metadata commitments, and expired invoices", async () => {
    reply({ pr: hashed });
    await expect(psLnurlInvoice(pay, 20)).rejects.toThrow(
      "amount does not match",
    );
    reply({ pr: hashed });
    await expect(
      psLnurlInvoice(
        { ...pay, metadata: '[["text/plain","Someone else"]]' },
        10,
      ),
    ).rejects.toThrow("payment metadata");
    vi.setSystemTime(new Date("2024-01-01T01:00:00Z"));
    reply({ pr: hashed });
    await expect(psLnurlInvoice(pay, 10)).rejects.toThrow("expired invoice");
  });
  it("rejects missing and corrupted invoices and callback errors", async () => {
    reply({ pr: hashed.slice(0, -1) + "q" });
    await expect(psLnurlInvoice(pay, 10)).rejects.toThrow();
    reply({});
    await expect(psLnurlInvoice(pay, 10)).rejects.toThrow(
      "did not return a Lightning invoice",
    );
    reply({ status: "ERROR", reason: "Recipient is offline" });
    await expect(psLnurlInvoice(pay, 10)).rejects.toThrow(
      "Recipient is offline",
    );
  });
});
