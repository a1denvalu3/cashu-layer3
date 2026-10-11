/** Byte-compatible port of cashu/crypto/ps.py and cashu/keyset.py. */
import { bls12_381 as bls } from "@noble/curves/bls12-381.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex, hexToBytes, concatBytes } from "@noble/hashes/utils.js";
import { DENOMINATIONS, hex32, keysetId, type PSProof } from "./models";

export { bytesToHex, hexToBytes, concatBytes };
export const utf8 = (s: string) => new TextEncoder().encode(s);
export const ORDER = bls.fields.Fr.ORDER;
export const mod = (n: bigint) => ((n % ORDER) + ORDER) % ORDER;
export const scalarHex = (n: bigint) => mod(n).toString(16).padStart(64, "0");
export function scalar(raw: string, nonzero = false): bigint {
  hex32.parse(raw);
  const n = BigInt("0x" + raw);
  if (n >= ORDER || (nonzero && n === 0n)) throw new Error("Invalid scalar");
  return n;
}
export function randomHex(): string {
  return bytesToHex(crypto.getRandomValues(new Uint8Array(32)));
}
export function randomScalar(): bigint {
  for (;;) {
    const n = BigInt("0x" + randomHex());
    if (n > 0n && n < ORDER) return n;
  }
}
export function integer(n: number, length: number): Uint8Array {
  if (
    !Number.isSafeInteger(n) ||
    n < 0 ||
    BigInt(n) >= 1n << BigInt(length * 8)
  )
    throw new Error("Invalid integer");
  return hexToBytes(
    BigInt(n)
      .toString(16)
      .padStart(length * 2, "0"),
  );
}
const hashScalar = (bytes: Uint8Array) =>
  BigInt("0x" + bytesToHex(sha256(bytes))) % ORDER;
export const preimageHash = (preimage: string) =>
  bytesToHex(sha256(hexToBytes(hex32.parse(preimage))));
export const claimScalar = (hash: string, amount: number) =>
  hashScalar(
    concatBytes(
      utf8("Cashu_PS_HTLC_Claim_v3"),
      integer(amount, 8),
      hexToBytes(hex32.parse(hash)),
    ),
  );

export type G1Point = InstanceType<typeof bls.G1.Point>;
export type G2Point = InstanceType<typeof bls.G2.Point>;
export const G1 = bls.G1.Point.BASE;
export const G2 = bls.G2.Point.BASE;
export const GNULL = bls.G1.hashToCurve(utf8("ps_nullifier_base"), {
  DST: "CASHU_PS_GNULL_XMD:SHA-256_SSWU_RO_",
});
export function g1(raw: string): G1Point {
  if (!/^[0-9a-f]{96}$/.test(raw)) throw new Error("Invalid G1 encoding");
  const p = bls.G1.Point.fromHex(raw);
  p.assertValidity();
  if (p.equals(bls.G1.Point.ZERO) || p.toHex(true) !== raw)
    throw new Error("Invalid G1 point");
  return p;
}
export function g2(raw: string): G2Point {
  if (!/^[0-9a-f]{192}$/.test(raw)) throw new Error("Invalid G2 encoding");
  const p = bls.G2.Point.fromHex(raw);
  p.assertValidity();
  if (p.equals(bls.G2.Point.ZERO) || p.toHex(true) !== raw)
    throw new Error("Invalid G2 point");
  return p;
}
export function mul1(p: G1Point, n: bigint): G1Point {
  return n === 0n ? bls.G1.Point.ZERO : p.multiply(n);
}
export function mul2(p: G2Point, n: bigint): G2Point {
  return n === 0n ? bls.G2.Point.ZERO : p.multiply(n);
}
export type PSPublicKey = {
  X2: G2Point;
  Yh2: G2Point;
  Ys2: G2Point;
  Yh1: G1Point;
  id: string;
  raw: string;
};
export function publicKey(raw: string, id: string): PSPublicKey {
  if (!/^[0-9a-f]{672}$/.test(raw)) throw new Error("Invalid PS public key");
  return {
    X2: g2(raw.slice(0, 192)),
    Yh2: g2(raw.slice(192, 384)),
    Ys2: g2(raw.slice(384, 576)),
    Yh1: g1(raw.slice(576)),
    id: keysetId.parse(id),
    raw,
  };
}
export function denominationKeysetId(keys: Record<string, string>): string {
  if (
    Object.keys(keys).length !== DENOMINATIONS.length ||
    DENOMINATIONS.some((n) => !keys[n])
  )
    throw new Error("Incomplete denomination keyset");
  return (
    "03" +
    bytesToHex(
      sha256(
        concatBytes(
          utf8("Cashu_PS_Denominations_v3"),
          ...DENOMINATIONS.map((n) =>
            concatBytes(integer(n, 8), hexToBytes(keys[n])),
          ),
        ),
      ),
    )
  );
}
export type PSCredential = {
  id: string;
  u: G1Point;
  v: G1Point;
  h: bigint;
  s: bigint;
};
export function credential(raw: string): PSCredential {
  if (!/^[0-9a-f]{386}$/.test(raw)) throw new Error("Invalid credential");
  return {
    id: keysetId.parse(raw.slice(0, 66)),
    u: g1(raw.slice(66, 162)),
    v: g1(raw.slice(162, 258)),
    h: scalar(raw.slice(258, 322)),
    s: scalar(raw.slice(322), true),
  };
}
export const encodeCredential = (c: PSCredential) =>
  c.id + c.u.toHex(true) + c.v.toHex(true) + scalarHex(c.h) + scalarHex(c.s);
export function verifyCredential(
  key: PSPublicKey,
  cred: PSCredential,
): boolean {
  if (key.id !== cred.id) return false;
  const result = bls.pairingBatch([
    { g1: cred.v.negate(), g2: G2 },
    { g1: cred.u, g2: key.X2.add(mul2(key.Yh2, cred.h)) },
    { g1: mul1(cred.u, cred.s), g2: key.Ys2 },
  ]);
  return bls.fields.Fp12.eql(result, bls.fields.Fp12.ONE);
}
const framed = (raw: Uint8Array) => concatBytes(integer(raw.length, 2), raw);
const bindingPrefix = (dst: string, binding: Uint8Array) =>
  concatBytes(utf8(dst), framed(binding));
export function proveDlog(
  bases: G1Point[],
  points: G1Point[],
  secret: bigint,
  dst: string,
  binding: Uint8Array = new Uint8Array(),
): string {
  const nonce = randomScalar();
  const commitments = bases.map((p) => mul1(p, nonce));
  const c = hashScalar(
    concatBytes(
      bindingPrefix(dst, binding),
      ...[bases, points, commitments].flatMap((ps) =>
        ps.map((p) => framed(p.toBytes(true))),
      ),
    ),
  );
  return scalarHex(c) + scalarHex(nonce + c * secret);
}
export function ownerOutput(amount: number): {
  output: { amount: number; commitment: string; proof: string };
  secret: string;
} {
  const secret = randomScalar(),
    commitment = mul1(G1, secret);
  return {
    output: {
      amount,
      commitment: commitment.toHex(true),
      proof: proveDlog([G1], [commitment], secret, "Cashu_PS_Issue_v1"),
    },
    secret: scalarHex(secret),
  };
}
export const nullifier = (proof: PSProof) =>
  mul1(GNULL, credential(proof.credential).s).toHex(true);
export function present(cred: PSCredential, binding: Uint8Array): string {
  const rho = randomScalar(),
    u = mul1(cred.u, rho),
    v = mul1(cred.v, rho),
    us = mul1(u, cred.s),
    n = mul1(GNULL, cred.s);
  return (
    cred.id +
    scalarHex(cred.h) +
    [u, v, us, n].map((p) => p.toHex(true)).join("") +
    proveDlog([GNULL, u], [n, us], cred.s, "Cashu_PS_Present_v1", binding)
  );
}
export function presentPrivate(
  key: PSPublicKey,
  cred: PSCredential,
  binding: Uint8Array,
): { raw: string; o: bigint; kappa: G2Point } {
  const rho = randomScalar(),
    o = randomScalar(),
    u = mul1(cred.u, rho);
  const v = mul1(cred.v, rho).add(mul1(u, o)),
    kappa = mul2(key.Yh2, cred.h).add(mul2(G2, o));
  const us = mul1(u, cred.s),
    n = mul1(GNULL, cred.s);
  return {
    raw:
      cred.id +
      u.toHex(true) +
      v.toHex(true) +
      kappa.toHex(true) +
      us.toHex(true) +
      n.toHex(true) +
      proveDlog([GNULL, u], [n, us], cred.s, "Cashu_PS_Present_v1", binding),
    o,
    kappa,
  };
}
type Point = { group: 1; p: G1Point } | { group: 2; p: G2Point };
export const p1 = (p: G1Point): Point => ({ group: 1, p });
export const p2 = (p: G2Point): Point => ({ group: 2, p });
export type Statement = {
  point: Point;
  terms: { base: Point; index: number }[];
};
function multiply(p: Point, n: bigint): Point {
  return p.group === 1 ? p1(mul1(p.p, n)) : p2(mul2(p.p, n));
}
function add(a: Point, b: Point): Point {
  if (a.group === 1 && b.group === 1) return p1(a.p.add(b.p));
  if (a.group === 2 && b.group === 2) return p2(a.p.add(b.p));
  throw new Error("Mixed curve groups");
}
export function proveLinear(
  statements: Statement[],
  witnesses: bigint[],
  binding: Uint8Array,
): string {
  const nonces = witnesses.map(randomScalar);
  const chunks = [bindingPrefix("Cashu_PS_Common_HTLC_v3", binding)];
  for (const { point, terms } of statements) {
    const parts = terms.map(({ base, index }) => multiply(base, nonces[index]));
    const commitment = parts.slice(1).reduce(add, parts[0]);
    chunks.push(
      framed(point.p.toBytes(true)),
      framed(commitment.p.toBytes(true)),
    );
    for (const { base, index } of terms)
      chunks.push(framed(base.p.toBytes(true)), integer(index, 2));
  }
  const c = hashScalar(concatBytes(...chunks));
  return (
    scalarHex(c) +
    witnesses.map((w, i) => scalarHex(nonces[i] + c * w)).join("")
  );
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object")
    return (
      "{" +
      Object.entries(value)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export const transcript = (domain: string, value: unknown) =>
  sha256(utf8(domain + canonical(value)));
