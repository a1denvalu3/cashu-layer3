import { z } from "zod";

export const MAX_AMOUNT = 2_100_000_000_000_000;
export const MAX_PROOFS = 256;
export const DENOMINATIONS = Array.from({ length: 51 }, (_, i) => 2 ** i);
export const sats = z.number().int().min(0).max(MAX_AMOUNT).safe();
export const amount = sats.refine((n) => n > 0, "Amount must be positive");
export const denomination = amount.refine(
  (n) => DENOMINATIONS.includes(n),
  "Unsupported denomination",
);
export const hex32 = z.string().regex(/^[0-9a-f]{64}$/);
export const g1hex = z.string().regex(/^[0-9a-f]{96}$/);
export const keysetId = z.string().regex(/^03[0-9a-f]{64}$/);
const timestamp = z.number().int().nonnegative().safe();

export const quoteSchema = z
  .object({
    quote: hex32,
    amount,
    unit: z.literal("sat"),
    payment_hash: hex32,
    request: z.string().max(4096),
    expiry: timestamp,
    state: z.enum(["OPEN", "ACCEPTED", "SETTLED", "CANCELED"]),
    issued: z.boolean(),
    keyset_id: keysetId,
  })
  .strict();
export type PSQuote = z.infer<typeof quoteSchema>;

export const backingSchema = z
  .object({
    payment_hash: hex32,
    amount,
    invoice_expires_at: timestamp,
    htlc_expiry_height: timestamp.nullable().default(null),
    expiry_height: timestamp.nullable().default(null),
    blocks_remaining: timestamp.nullable().default(null),
    expires_at: timestamp.nullable().default(null),
    melt_deadline_height: timestamp.nullable().default(null),
    melt_deadline: timestamp.nullable().default(null),
  })
  .strict();
export const snapshotSchema = z
  .object({
    checked_at: timestamp,
    block_height: timestamp.nullable(),
    htlcs: z.array(backingSchema).max(100000),
  })
  .strict();
export type PSBacking = z.infer<typeof backingSchema>;
export type PSSnapshot = z.infer<typeof snapshotSchema>;

export const proofSchema = z
  .object({
    amount: denomination,
    payment_hash: hex32,
    backing_amount: amount,
    credential: z.string().regex(/^[0-9a-f]{386}$/),
    preimage: hex32.nullable().default(null),
  })
  .strict()
  .refine((p) => p.amount <= p.backing_amount, "Proof exceeds backing amount");
export type PSProof = z.infer<typeof proofSchema>;
export const tokenSchema = z
  .object({
    version: z.literal(3),
    mint: z.string().min(1).max(2048),
    proofs: z.array(proofSchema).min(1).max(MAX_PROOFS),
  })
  .strict()
  .refine(
    (t) => t.proofs.reduce((n, p) => n + p.amount, 0) <= MAX_AMOUNT,
    "Token too large",
  );
export type PSToken = z.infer<typeof tokenSchema>;
export const signatureSchema = z
  .object({
    amount: denomination,
    keyset_id: keysetId,
    u: g1hex,
    v: g1hex,
    payment_hash: hex32.nullable().default(null),
  })
  .strict();
export const signaturesSchema = z
  .object({ signatures: z.array(signatureSchema).max(MAX_PROOFS) })
  .strict();
export type PSSignature = z.infer<typeof signatureSchema>;
export const outputSchema = z
  .object({
    amount: denomination,
    commitment: g1hex,
    proof: z.string().regex(/^[0-9a-f]{128}$/),
  })
  .strict();
export type PSOutput = z.infer<typeof outputSchema>;
export const blindOutputSchema = outputSchema.extend({ b: g1hex });
export const changeOutputSchema = outputSchema.extend({ payment_hash: hex32 });
export const mintRequestSchema = z
  .object({
    quote: hex32,
    outputs: z.array(outputSchema).min(1).max(MAX_PROOFS),
  })
  .strict();
export const swapRequestSchema = z
  .object({
    version: z.literal(3),
    inputs: z
      .array(
        z
          .object({
            amount: denomination,
            presentation: z.string().regex(/^[0-9a-f]{770}$/),
          })
          .strict(),
      )
      .min(1)
      .max(MAX_PROOFS),
    outputs: z.array(blindOutputSchema).min(1).max(MAX_PROOFS),
    equality_proof: z
      .string()
      .regex(/^[0-9a-f]+$/)
      .max(33024),
  })
  .strict();
export const meltQuoteSchema = z
  .object({
    quote: hex32,
    request: z.string().min(1).max(4096),
    payment_hash: hex32,
    amount,
    fee_reserve: sats,
    expiry: timestamp,
  })
  .strict();
export type PSMeltQuote = z.infer<typeof meltQuoteSchema>;
export const meltRequestSchema = z
  .object({
    quote: hex32,
    inputs: z
      .array(
        z
          .object({
            amount: denomination,
            backing_amount: amount,
            preimage: hex32,
            presentation: z.string().regex(/^[0-9a-f]{642}$/),
          })
          .strict(),
      )
      .min(1)
      .max(MAX_PROOFS),
    change: z.array(changeOutputSchema).max(MAX_PROOFS),
  })
  .strict();
export const receiptSchema = z
  .object({
    state: z.literal("PAID"),
    amount,
    fee: sats,
    fee_msat: timestamp,
    payout_hash: hex32,
    payout_preimage: hex32,
    change: z.array(signatureSchema).max(MAX_PROOFS),
    expired_change: sats,
  })
  .strict();
export type PSReceipt = z.infer<typeof receiptSchema>;
const operationBase = { id: hex32 };
export const mintOperationSchema = z
  .object({
    ...operationBase,
    kind: z.literal("mint"),
    quote: quoteSchema,
    secrets: z.array(hex32).min(1).max(MAX_PROOFS),
    request: mintRequestSchema,
    preimage: hex32.nullable(),
  })
  .strict();
export const swapOperationSchema = z
  .object({
    ...operationBase,
    kind: z.literal("swap"),
    token: tokenSchema,
    secrets: z.array(hex32).min(1).max(MAX_PROOFS),
    blindings: z.array(hex32).min(1).max(MAX_PROOFS),
    request: swapRequestSchema,
    backing: backingSchema,
  })
  .strict();
export const meltOperationSchema = z
  .object({
    ...operationBase,
    kind: z.literal("melt"),
    token: tokenSchema,
    quote: meltQuoteSchema,
    secrets: z.array(hex32).max(MAX_PROOFS),
    request: meltRequestSchema,
  })
  .strict();
export const operationSchema = z.discriminatedUnion("kind", [
  mintOperationSchema,
  swapOperationSchema,
  meltOperationSchema,
]);
export type PSMintOperation = z.infer<typeof mintOperationSchema>;
export type PSSwapOperation = z.infer<typeof swapOperationSchema>;
export type PSMeltOperation = z.infer<typeof meltOperationSchema>;
export type PSOperation = z.infer<typeof operationSchema>;
