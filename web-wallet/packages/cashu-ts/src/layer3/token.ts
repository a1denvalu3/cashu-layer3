import { base64url } from "@scure/base";
import { credential, nullifier, preimageHash, utf8 } from "./crypto";
import { tokenSchema, type PSToken } from "./models";

export function normalizePSMintUrl(input: string): string {
  const url = new URL(input.trim());
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "Use an HTTP or HTTPS mint URL without credentials, query or fragment",
    );
  return url.toString().replace(/\/+$/, "");
}
export function validatePSToken(value: unknown): PSToken {
  const token = tokenSchema.parse(value);
  token.mint = normalizePSMintUrl(token.mint);
  const seen = new Set<string>(),
    groups = new Map<string, string>();
  for (const proof of token.proofs) {
    const cred = credential(proof.credential),
      id = nullifier(proof);
    if (seen.has(id)) throw new Error("Duplicate token proof");
    seen.add(id);
    if (proof.preimage && preimageHash(proof.preimage) !== proof.payment_hash)
      throw new Error("Preimage does not match backing HTLC");
    const metadata = `${proof.backing_amount}:${proof.preimage}`;
    if (
      groups.has(proof.payment_hash) &&
      groups.get(proof.payment_hash) !== metadata
    )
      throw new Error("Inconsistent backing metadata");
    groups.set(proof.payment_hash, metadata);
    if (!cred.id.startsWith("03"))
      throw new Error("Unsupported credential version");
  }
  return token;
}
export function encodePSToken(token: PSToken): string {
  return (
    "cashuPS" +
    base64url
      .encode(utf8(JSON.stringify(validatePSToken(token))))
      .replace(/=+$/, "")
  );
}
export function decodePSToken(value: string): PSToken {
  let raw = value.trim();
  if (raw.startsWith("cashu:")) raw = raw.slice(6);
  if (!raw.startsWith("cashuPS"))
    throw new Error("This wallet accepts cashuPS tokens");
  if (raw.length > 262144) throw new Error("Token is too large");
  const payload = raw.slice(7).replace(/=+$/, "");
  if (!/^[A-Za-z0-9_-]+$/.test(payload))
    throw new Error("Invalid token encoding");
  return validatePSToken(
    JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(
        base64url.decode(payload + "=".repeat((4 - (payload.length % 4)) % 4)),
      ),
    ),
  );
}
export const psTokenAmount = (token: PSToken) =>
  token.proofs.reduce((n, p) => n + p.amount, 0);
