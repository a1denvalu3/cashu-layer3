import { z } from "zod";
import {
  proofSchema,
  quoteSchema,
  operationSchema,
  receiptSchema,
  hex32,
  g1hex,
  sats,
  normalizePSMintUrl,
  psNullifier,
  psPreimageHash,
  decodePSToken,
} from "@cashu/cashu-ts/tmp-cash";
import type { TmpCashState } from "coco-cashu-core/tmp-cash";

const mint = z.string().max(2048).transform(normalizePSMintUrl),
  date = z.number().int().nonnegative().safe();
const purpose = z.enum(["mint", "send", "receive", "melt"]);
const funding = z
  .object({
    id: hex32,
    mint,
    amount: sats,
    preimage: hex32,
    invoiceExpiry: z.number().int().min(60).max(86400),
    quote: quoteSchema.nullable(),
    state: z.enum(["waiting", "minted", "closed"]),
    createdAt: date,
  })
  .strict();
const proof = z
  .object({
    id: g1hex,
    mint,
    proof: proofSchema,
    state: z.enum(["ready", "pending", "reserved", "sent", "spent", "expired"]),
    operationId: hex32.nullable(),
    createdAt: date,
  })
  .strict();
const operation = z
  .object({
    id: hex32,
    mint,
    payload: operationSchema,
    purpose,
    sendCount: z.number().int().min(0).max(256),
    amount: sats,
    createdAt: date,
    error: z.string().max(500).nullable(),
  })
  .strict();
const activity = z
  .object({
    id: hex32,
    mint,
    purpose,
    amount: sats,
    createdAt: date,
    state: z.enum(["complete", "sent"]),
    token: z.string().max(262144).nullable(),
    receipt: receiptSchema.nullable(),
  })
  .strict();
const stateSchema = z
  .object({
    proofs: z.array(proof).max(100000),
    operations: z.array(operation).max(10000),
    fundings: z.array(funding).max(10000),
    activity: z.array(activity).max(100000),
  })
  .strict();
export function validateTmpCashState(input: unknown): TmpCashState {
  const state = stateSchema.parse(input);
  const proofById = new Map(state.proofs.map((p) => [p.id, p]));
  const operationById = new Map(state.operations.map((op) => [op.id, op]));
  for (const table of [
    state.proofs,
    state.operations,
    state.fundings,
    state.activity,
  ])
    if (new Set(table.map((r) => r.id)).size !== table.length)
      throw new Error("Backup contains duplicate records");
  for (const f of state.fundings)
    if (
      psPreimageHash(f.preimage) !== f.id ||
      (f.quote &&
        (f.quote.payment_hash !== f.id || f.quote.amount !== f.amount))
    )
      throw new Error("Backup funding preimage or amount is inconsistent");
  for (const p of state.proofs)
    if (
      p.id !== psNullifier(p.proof) ||
      (p.proof.preimage &&
        psPreimageHash(p.proof.preimage) !== p.proof.payment_hash)
    )
      throw new Error("Backup proof metadata is inconsistent");
  for (const op of state.operations) {
    const payload = op.payload;
    if (
      op.id !== payload.id ||
      (payload.kind !== "mint" &&
        normalizePSMintUrl(payload.token.mint) !== op.mint)
    )
      throw new Error("Backup operation identity is inconsistent");
    const count =
      payload.kind === "mint"
        ? payload.request.outputs.length
        : payload.kind === "swap"
          ? payload.request.outputs.length
          : payload.request.change.length;
    if (
      payload.secrets.length !== count ||
      (payload.kind === "swap" && payload.blindings.length !== count)
    )
      throw new Error("Backup omits operation secrets");
    if (payload.kind !== "mint")
      for (const proof of payload.token.proofs) {
        const saved = proofById.get(psNullifier(proof));
        if (!saved || saved.operationId !== op.id || saved.state !== "reserved")
          throw new Error("Backup operation reservations are inconsistent");
      }
  }
  for (const p of state.proofs)
    if (p.state === "reserved" && !operationById.has(p.operationId ?? ""))
      throw new Error("Backup has an orphaned reservation");
  for (const p of state.proofs)
    if (p.state !== "reserved" && p.operationId !== null)
      throw new Error("Backup reservation state is inconsistent");
  for (const row of state.activity) if (row.token) decodePSToken(row.token);
  return state;
}
