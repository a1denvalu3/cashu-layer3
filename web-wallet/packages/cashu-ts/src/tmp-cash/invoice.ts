import { decode } from "light-bolt11-decoder";
import { bech32 } from "@scure/base";
import { secp256k1 } from "@noble/curves/secp256k1.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { concatBytes, bytesToHex, utf8 } from "./crypto";
import { amount, hex32 } from "./models";

function wordsToBytes(words: number[]): Uint8Array {
  let acc = 0,
    bits = 0;
  const out: number[] = [];
  for (const word of words) {
    acc = (acc << 5) | word;
    bits += 5;
    while (bits >= 8) {
      bits -= 8;
      out.push((acc >>> bits) & 255);
    }
  }
  if (bits) out.push((acc << (8 - bits)) & 255);
  return Uint8Array.from(out);
}
/** Decode and verify the recoverable BOLT11 signature, including an explicit payee. */
export function psInvoiceDetails(request: string): {
  paymentHash: string;
  amount: number;
  expiry: number;
  descriptionHash?: string;
} {
  if (!request || request.length > 4096 || !/^ln(bc|tb|bcrt)/i.test(request))
    throw new Error("Enter a BOLT11 Lightning invoice");
  const normalized = request.toLowerCase();
  const decoded = decode(request),
    { prefix, words } = bech32.decode(
      normalized as `${string}1${string}`,
      4096,
    );
  const body = words.slice(0, -104),
    signature = wordsToBytes(words.slice(-104));
  if (body.length < 7 || signature.length !== 65 || signature[64] > 3)
    throw new Error("Invalid invoice signature");
  const digest = sha256(concatBytes(utf8(prefix), wordsToBytes(body)));
  const compact = signature.slice(0, 64);
  const pub = secp256k1.Signature.fromBytes(compact)
    .addRecoveryBit(signature[64])
    .recoverPublicKey(digest)
    .toBytes(true);
  if (!secp256k1.verify(compact, digest, pub, { prehash: false, lowS: false }))
    throw new Error("Invalid invoice signature");
  let descriptionHash: string | undefined;
  for (let i = 7; i < body.length; ) {
    const tag = body[i],
      length = body[i + 1] * 32 + body[i + 2];
    if (i + 3 + length > body.length) throw new Error("Invalid invoice tags");
    if (tag === 23) {
      if (length !== 52) throw new Error("Invalid invoice description hash");
      descriptionHash = bytesToHex(
        wordsToBytes(body.slice(i + 3, i + 3 + length)).slice(0, 32),
      );
    }
    if (
      tag === 19 &&
      bytesToHex(
        wordsToBytes(body.slice(i + 3, i + 3 + length)).slice(0, 33),
      ) !== bytesToHex(pub)
    )
      throw new Error("Invoice signature does not match payee");
    i += 3 + length;
  }
  const hash = decoded.sections.find((s) => s.name === "payment_hash");
  const value = decoded.sections.find((s) => s.name === "amount");
  const timestamp = decoded.sections.find((s) => s.name === "timestamp");
  const expiry = decoded.sections.find((s) => s.name === "expiry");
  if (
    !hash ||
    hash.name !== "payment_hash" ||
    !value ||
    value.name !== "amount"
  )
    throw new Error("Invoice must contain an amount and payment hash");
  const msat = BigInt(value.value);
  if (msat <= 0n || msat % 1000n !== 0n)
    throw new Error("Invoice must use whole satoshis");
  return {
    paymentHash: hex32.parse(hash.value),
    amount: amount.parse(Number(msat / 1000n)),
    expiry:
      (timestamp?.name === "timestamp" ? timestamp.value : 0) +
      (expiry?.name === "expiry" ? expiry.value : 3600),
    ...(descriptionHash ? { descriptionHash } : {}),
  };
}
