import { bech32 } from "@scure/base";
import { sha256 } from "@noble/hashes/sha2.js";
import { z } from "zod";
import { bytesToHex, utf8 } from "./crypto";
import { psInvoiceDetails } from "./invoice";

export type PSLightningTarget =
  | { kind: "invoice"; invoice: string }
  | { kind: "lnurl"; url: string; label: string };

export interface PSLnurlPayRequest {
  url: string;
  domain: string;
  callback: string;
  metadata: string;
  description: string;
  minSats: number;
  maxSats: number;
}

function paymentUrl(value: string): URL {
  const url = new URL(value);
  if (
    value.length > 4096 ||
    (url.protocol !== "https:" &&
      !(url.protocol === "http:" && url.hostname.endsWith(".onion"))) ||
    url.username ||
    url.password ||
    url.hash
  )
    throw new Error(
      "LNURL payment endpoints must use HTTPS or an onion address",
    );
  return url;
}

/** Recognize BOLT11, LUD-16 addresses, and encoded or plain LNURL-pay links. */
export function psLightningTarget(value: string): PSLightningTarget {
  const input = value.trim().replace(/^lightning:/i, "");
  if (!input || input.length > 4096)
    throw new Error("Enter a Lightning invoice, Lightning address, or LNURL");
  if (/^ln(bc|tb|bcrt)/i.test(input)) {
    psInvoiceDetails(input);
    return { kind: "invoice", invoice: input.toLowerCase() };
  }
  const address = /^([a-z0-9_.+-]+)@([^\s/@?#:\\]+)$/i.exec(input);
  if (address) {
    const domain = new URL(`https://${address[2]}`).hostname;
    const user = address[1].toLowerCase();
    const protocol = domain.endsWith(".onion") ? "http:" : "https:";
    const url = paymentUrl(
      `${protocol}//${domain}/.well-known/lnurlp/${encodeURIComponent(user)}`,
    );
    return { kind: "lnurl", url: url.href, label: `${user}@${domain}` };
  }
  let endpoint = input;
  if (/^lnurl1/i.test(input)) {
    try {
      const { prefix, words } = bech32.decode(
        input as `${string}1${string}`,
        4096,
      );
      if (prefix !== "lnurl") throw new Error();
      endpoint = new TextDecoder("utf-8", { fatal: true }).decode(
        bech32.fromWords(words),
      );
    } catch {
      throw new Error("Invalid LNURL encoding");
    }
  } else if (/^lnurlp:\/\//i.test(input)) {
    const raw = new URL(input.replace(/^lnurlp:/i, "https:"));
    if (raw.hostname.endsWith(".onion")) raw.protocol = "http:";
    endpoint = raw.href;
  } else if (!/^https?:\/\//i.test(input)) {
    throw new Error("Enter a Lightning invoice, Lightning address, or LNURL");
  }
  const url = paymentUrl(endpoint);
  return { kind: "lnurl", url: url.href, label: url.hostname };
}

async function requestJson(
  url: string,
  signal?: AbortSignal,
): Promise<unknown> {
  let response: Response;
  try {
    const timeout = AbortSignal.timeout(15000);
    response = await fetch(paymentUrl(url), {
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new Error("Could not reach the LNURL service. Please try again.");
  }
  if (response.url) paymentUrl(response.url);
  // LUD-01 defines errors in the JSON body, regardless of HTTP status.
  let result: unknown;
  try {
    result = await response.json();
  } catch {
    throw new Error("The LNURL service returned an invalid response");
  }
  if (
    result &&
    typeof result === "object" &&
    "status" in result &&
    result.status === "ERROR"
  )
    throw new Error(
      "reason" in result && typeof result.reason === "string"
        ? `LNURL service: ${result.reason.slice(0, 500)}`
        : "The LNURL service rejected this payment",
    );
  return result;
}

const payRequestSchema = z.object({
  tag: z.literal("payRequest"),
  callback: z.string(),
  minSendable: z.number().int().positive().safe(),
  maxSendable: z.number().int().positive().safe(),
  metadata: z.string().max(200000),
  payerData: z
    .record(z.object({ mandatory: z.boolean().optional() }))
    .optional(),
});

export async function psLnurlPayRequest(
  url: string,
  signal?: AbortSignal,
): Promise<PSLnurlPayRequest> {
  const parsed = payRequestSchema.safeParse(await requestJson(url, signal));
  if (!parsed.success)
    throw new Error(
      "This LNURL does not provide a valid Lightning payment request",
    );
  const data = parsed.data;
  const minSats = Math.ceil(data.minSendable / 1000);
  const maxSats = Math.floor(data.maxSendable / 1000);
  if (data.minSendable > data.maxSendable || minSats > maxSats)
    throw new Error(
      "This LNURL does not accept a whole-satoshi payment amount",
    );
  if (Object.values(data.payerData ?? {}).some((field) => field.mandatory))
    throw new Error(
      "This LNURL requires payer information that this wallet does not support",
    );
  let metadata: unknown;
  try {
    metadata = JSON.parse(data.metadata);
  } catch {
    throw new Error("The LNURL service returned invalid payment metadata");
  }
  if (
    !Array.isArray(metadata) ||
    !metadata.every(
      (entry) => Array.isArray(entry) && typeof entry[0] === "string",
    )
  )
    throw new Error("The LNURL service returned invalid payment metadata");
  const descriptions = metadata.filter((entry) => entry[0] === "text/plain");
  if (descriptions.length !== 1 || typeof descriptions[0][1] !== "string")
    throw new Error("The LNURL service did not provide a payment description");
  return {
    url,
    domain: paymentUrl(url).hostname,
    callback: paymentUrl(data.callback).href,
    metadata: data.metadata,
    description: descriptions[0][1],
    minSats,
    maxSats,
  };
}

export async function psLnurlInvoice(
  pay: PSLnurlPayRequest,
  amount: number,
  signal?: AbortSignal,
): Promise<string> {
  if (
    !Number.isSafeInteger(amount) ||
    !Number.isSafeInteger(amount * 1000) ||
    amount < pay.minSats ||
    amount > pay.maxSats
  )
    throw new Error(
      `Enter an amount between ${pay.minSats} and ${pay.maxSats} sats`,
    );
  const callback = paymentUrl(pay.callback);
  callback.searchParams.set("amount", String(amount * 1000));
  const response = z
    .object({ pr: z.string().max(4096) })
    .safeParse(await requestJson(callback.href, signal));
  if (!response.success)
    throw new Error("The LNURL service did not return a Lightning invoice");
  const invoice = response.data.pr;
  const details = psInvoiceDetails(invoice);
  if (details.amount !== amount)
    throw new Error(
      "The LNURL invoice amount does not match your requested amount",
    );
  if (
    !Number.isSafeInteger(details.expiry) ||
    details.expiry <= Date.now() / 1000
  )
    throw new Error(
      "The LNURL service returned an expired invoice. Please try again.",
    );
  if (
    details.descriptionHash &&
    details.descriptionHash !== bytesToHex(sha256(utf8(pay.metadata)))
  )
    throw new Error("The LNURL invoice does not match the payment metadata");
  return invoice.toLowerCase();
}
