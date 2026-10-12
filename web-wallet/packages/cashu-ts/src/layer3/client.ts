import { z } from "zod";
import {
  MAX_AMOUNT,
  DENOMINATIONS,
  amount,
  denomination,
  keysetId,
  hex32,
  quoteSchema,
  snapshotSchema,
  signaturesSchema,
  meltQuoteSchema,
  receiptSchema,
  type PSQuote,
  type PSProof,
  type PSToken,
} from "./models";
import type {
  PSBacking,
  PSSnapshot,
  PSSignature,
  PSMintOperation,
  PSSwapOperation,
  PSMeltOperation,
  PSMeltQuote,
  PSReceipt,
} from "./models";
import {
  publicKey,
  denominationKeysetId,
  credential,
  verifyCredential,
  claimScalar,
  randomHex,
  randomScalar,
  scalarHex,
  scalar,
  ownerOutput,
  g1,
  mul1,
  G1,
  G2,
  nullifier,
  preimageHash,
  encodeCredential,
  present,
  presentPrivate,
  p1,
  p2,
  proveLinear,
  transcript,
  type PSPublicKey,
  type PSCredential,
  type Statement,
} from "./crypto";
import { normalizePSMintUrl, validatePSToken, psTokenAmount } from "./token";
import { psInvoiceDetails } from "./invoice";

export class PSMintError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "PSMintError";
  }
}
export function psDenominations(value: number): number[] {
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_AMOUNT)
    throw new Error("Invalid amount");
  let rest = value;
  return [...DENOMINATIONS].reverse().filter((n) => {
    if (rest >= n) {
      rest -= n;
      return true;
    }
    return false;
  });
}
export function checkPSBacking(
  proof: PSProof,
  snapshot: PSSnapshot,
  now = Math.floor(Date.now() / 1000),
): PSBacking {
  const matches = snapshot.htlcs.filter(
    (h) => h.payment_hash === proof.payment_hash,
  );
  const h = matches[0];
  if (matches.length !== 1 || h.amount !== proof.backing_amount)
    throw new Error("Backing HTLC is closed or unavailable");
  if (snapshot.block_height !== null) {
    if (
      h.expiry_height === null ||
      h.htlc_expiry_height === null ||
      h.expiry_height > h.htlc_expiry_height ||
      h.blocks_remaining !== h.expiry_height - snapshot.block_height ||
      h.melt_deadline_height === null ||
      h.melt_deadline_height > h.expiry_height ||
      snapshot.block_height >= h.melt_deadline_height
    )
      throw new Error(
        "Backing HTLC redemption deadline has passed or is inconsistent",
      );
  } else if (
    h.expires_at === null ||
    h.melt_deadline === null ||
    h.melt_deadline > h.expires_at ||
    h.melt_deadline <= Math.max(now, snapshot.checked_at)
  )
    throw new Error(
      "Backing HTLC redemption deadline has passed or is inconsistent",
    );
  return h;
}

export class PSWallet {
  readonly mintUrl: string;
  keys: Record<number, PSPublicKey> = {};
  keysetId = "";
  defaultTokenLifetimeBlocks: number | null = null;
  fundingHtlcCltvDelta: number | null = null;
  constructor(
    mintUrl: string,
    private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {
    this.mintUrl = normalizePSMintUrl(mintUrl);
  }
  private async request(path: string, body?: unknown): Promise<unknown> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (
      /\.(ngrok-free\.app|ngrok-free\.dev|ngrok\.app|ngrok\.dev|ngrok\.io)$/.test(
        new URL(this.mintUrl).hostname,
      )
    )
      headers["ngrok-skip-browser-warning"] = "1";
    const response = await this.fetcher(this.mintUrl + path, {
      method: body === undefined ? "GET" : "POST",
      cache: "no-store",
      credentials: "omit",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(90000),
    });
    const raw = await response.text();
    if (raw.length > 4_000_000)
      throw new PSMintError("Mint response is too large");
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new PSMintError(
        "Mint returned an invalid response",
        response.status,
      );
    }
    if (!response.ok) {
      const detail = z.object({ detail: z.string() }).safeParse(parsed);
      throw new PSMintError(
        detail.success
          ? detail.data.detail
          : `Mint request failed (${response.status})`,
        response.status,
      );
    }
    return parsed;
  }
  async loadMint(): Promise<void> {
    const info = z
      .object({
        scheme: z.literal("PS/BLS12-381"),
        token_version: z.literal(3),
        swap_request_version: z.literal(3),
        swap: z.literal("same-htlc-many-to-many"),
        default_token_lifetime_blocks: z
          .number()
          .int()
          .positive()
          .max(65535)
          .nullable()
          .optional(),
        funding_htlc_cltv_delta: z
          .number()
          .int()
          .positive()
          .max(65535)
          .nullable()
          .optional(),
      })
      .passthrough()
      .parse(await this.request("/v1/info"));
    if (!info) throw new Error("Unsupported mint");
    const response = z
      .object({
        id: keysetId,
        unit: z.literal("sat"),
        keys: z.record(z.string().regex(/^[0-9a-f]{672}$/)),
      })
      .strict()
      .parse(await this.request("/v1/keys"));
    if (denominationKeysetId(response.keys) !== response.id)
      throw new Error("Mint returned inconsistent keyset parameters");
    this.keysetId = response.id;
    this.defaultTokenLifetimeBlocks =
      info.default_token_lifetime_blocks ?? null;
    this.fundingHtlcCltvDelta = info.funding_htlc_cltv_delta ?? null;
    this.keys = Object.fromEntries(
      Object.entries(response.keys).map(([n, raw]) => [
        Number(n),
        publicKey(raw, response.id),
      ]),
    );
  }
  private ready() {
    if (!this.keysetId) throw new Error("Load the mint first");
  }
  async createMintQuote(
    value: number,
    hash: string,
    expiry = 3600,
  ): Promise<PSQuote> {
    this.ready();
    amount.parse(value);
    hex32.parse(hash);
    if (!Number.isInteger(expiry) || expiry < 60 || expiry > 86400)
      throw new Error("Invalid invoice expiry");
    const quote = quoteSchema.parse(
      await this.request("/v1/mint/quote/bolt11", {
        amount: value,
        unit: "sat",
        payment_hash: hash,
        expiry,
      }),
    );
    this.validateQuote(quote);
    if (quote.amount !== value || quote.payment_hash !== hash)
      throw new Error("Mint changed quote amount or hash");
    return quote;
  }
  private validateQuote(quote: PSQuote) {
    const invoice = psInvoiceDetails(quote.request);
    if (
      quote.keyset_id !== this.keysetId ||
      quote.amount !== invoice.amount ||
      quote.payment_hash !== invoice.paymentHash
    )
      throw new Error("Inconsistent mint quote");
  }
  async getMintQuote(id: string): Promise<PSQuote> {
    const quote = quoteSchema.parse(
      await this.request("/v1/mint/quote/bolt11/" + hex32.parse(id)),
    );
    this.validateQuote(quote);
    return quote;
  }
  async pendingHTLCs(): Promise<PSSnapshot> {
    return snapshotSchema.parse(await this.request("/v1/htlcs/pending"));
  }
  verifyProof(proof: PSProof): PSCredential {
    this.ready();
    const cred = credential(proof.credential),
      key = this.keys[proof.amount];
    if (
      !key ||
      cred.h !== claimScalar(proof.payment_hash, proof.backing_amount) ||
      !verifyCredential(key, cred)
    )
      throw new Error("Invalid token credential, denomination or backing");
    return cred;
  }
  verifyToken(input: PSToken): PSToken {
    const token = validatePSToken(input);
    if (token.mint !== this.mintUrl)
      throw new Error("Token belongs to a different mint");
    token.proofs.forEach((p) => this.verifyProof(p));
    return token;
  }
  async checkProofs(
    proofs: PSProof[],
  ): Promise<("UNSPENT" | "PENDING" | "SPENT")[]> {
    if (!proofs.length || proofs.length > 256)
      throw new Error("Invalid proof count");
    const response = z
      .object({
        states: z.array(z.enum(["UNSPENT", "PENDING", "SPENT"])).max(256),
      })
      .strict()
      .parse(
        await this.request("/v1/checkstate", {
          nullifiers: proofs.map(nullifier),
        }),
      );
    if (response.states.length !== proofs.length)
      throw new Error("Mint omitted proof states");
    return response.states;
  }
  prepareMint(quote: PSQuote, preimage: string | null): PSMintOperation {
    this.validateQuote(quote);
    if (preimage && preimageHash(preimage) !== quote.payment_hash)
      throw new Error("Preimage does not match quote");
    const outputs = psDenominations(quote.amount).map(ownerOutput);
    return {
      kind: "mint",
      id: randomHex(),
      quote,
      preimage,
      secrets: outputs.map((p) => p.secret),
      request: { quote: quote.quote, outputs: outputs.map((p) => p.output) },
    };
  }
  private proof(
    sig: PSSignature,
    secret: string,
    source: {
      payment_hash: string;
      backing_amount: number;
      preimage: string | null;
    },
    blinding?: string,
  ): PSProof {
    if (sig.keyset_id !== this.keysetId)
      throw new Error("Mint changed signing keyset");
    const u = g1(sig.u),
      rawV = g1(sig.v);
    const cred = {
      id: sig.keyset_id,
      u,
      v: blinding ? rawV.subtract(mul1(u, scalar(blinding, true))) : rawV,
      h: claimScalar(source.payment_hash, source.backing_amount),
      s: scalar(secret, true),
    };
    const proof: PSProof = {
      amount: sig.amount,
      ...source,
      credential: encodeCredential(cred),
    };
    this.verifyProof(proof);
    return proof;
  }
  private matchOutputs(signatures: PSSignature[], amounts: number[]) {
    if (
      signatures.length !== amounts.length ||
      signatures.some((s, i) => s.amount !== amounts[i])
    )
      throw new Error("Mint changed output denominations");
  }
  async finishMint(op: PSMintOperation): Promise<PSToken> {
    const response = signaturesSchema.parse(
      await this.request("/v1/mint/bolt11", op.request),
    );
    this.matchOutputs(
      response.signatures,
      op.request.outputs.map((p) => p.amount),
    );
    return {
      version: 3,
      mint: this.mintUrl,
      proofs: response.signatures.map((sig, i) =>
        this.proof(sig, op.secrets[i], {
          payment_hash: op.quote.payment_hash,
          backing_amount: op.quote.amount,
          preimage: op.preimage,
        }),
      ),
    };
  }
  async prepareSwap(
    input: PSToken,
    amounts = psDenominations(psTokenAmount(input)),
  ): Promise<PSSwapOperation> {
    const token = this.verifyToken(input);
    if (new Set(token.proofs.map((p) => p.payment_hash)).size !== 1)
      throw new Error("Each swap must use exactly one backing HTLC");
    if (
      amounts.length < 1 ||
      amounts.length > 256 ||
      amounts.reduce((n, a) => n + denomination.parse(a), 0) !==
        psTokenAmount(token)
    )
      throw new Error("Swap outputs must preserve value");
    const snapshot = await this.pendingHTLCs(),
      backing = checkPSBacking(token.proofs[0], snapshot);
    const credentials = token.proofs.map((p) => this.verifyProof(p));
    const prepared = amounts.map(ownerOutput),
      blindings = amounts.map(randomScalar);
    const commitments = amounts.map((n, i) =>
      mul1(this.keys[n].Yh1, credentials[0].h).add(mul1(G1, blindings[i])),
    );
    const outputs = prepared.map((p, i) => ({
      ...p.output,
      b: commitments[i].toHex(true),
    }));
    const binding = transcript("Cashu_PS_HTLC_Swap_v3", {
      inputs: token.proofs.map((p) => [p.amount, nullifier(p)]),
      outputs,
    });
    const presentations = credentials.map((c, i) =>
      presentPrivate(this.keys[token.proofs[i].amount], c, binding),
    );
    const statements: Statement[] = [
      ...presentations.map((p, i) => ({
        point: p2(p.kappa),
        terms: [
          { base: p2(this.keys[token.proofs[i].amount].Yh2), index: 0 },
          { base: p2(G2), index: i + 1 },
        ],
      })),
      ...outputs.map((p, i) => ({
        point: p1(commitments[i]),
        terms: [
          { base: p1(this.keys[p.amount].Yh1), index: 0 },
          { base: p1(G1), index: presentations.length + i + 1 },
        ],
      })),
    ];
    return {
      kind: "swap",
      id: randomHex(),
      token,
      backing,
      secrets: prepared.map((p) => p.secret),
      blindings: blindings.map(scalarHex),
      request: {
        version: 3,
        inputs: token.proofs.map((p, i) => ({
          amount: p.amount,
          presentation: presentations[i].raw,
        })),
        outputs,
        equality_proof: proveLinear(
          statements,
          [credentials[0].h, ...presentations.map((p) => p.o), ...blindings],
          binding,
        ),
      },
    };
  }
  async finishSwap(op: PSSwapOperation): Promise<PSToken> {
    const response = signaturesSchema.parse(
      await this.request("/v1/swap", op.request),
    );
    this.matchOutputs(
      response.signatures,
      op.request.outputs.map((p) => p.amount),
    );
    const { payment_hash, backing_amount, preimage } = op.token.proofs[0];
    return {
      version: 3,
      mint: this.mintUrl,
      proofs: response.signatures.map((sig, i) =>
        this.proof(
          sig,
          op.secrets[i],
          { payment_hash, backing_amount, preimage },
          op.blindings[i],
        ),
      ),
    };
  }
  async createMeltQuote(
    request: string,
    feeReserve?: number,
  ): Promise<PSMeltQuote> {
    const invoice = psInvoiceDetails(request);
    const body = { request, fee_reserve: feeReserve ?? null };
    const quote = meltQuoteSchema.parse(
      await this.request("/v1/melt/quote/bolt11", body),
    );
    if (
      quote.request !== request ||
      quote.payment_hash !== invoice.paymentHash ||
      quote.amount !== invoice.amount ||
      (feeReserve !== undefined && quote.fee_reserve !== feeReserve)
    )
      throw new Error("Mint returned an inconsistent melt quote");
    return quote;
  }
  async prepareMelt(
    input: PSToken,
    quote: PSMeltQuote,
  ): Promise<PSMeltOperation> {
    const token = this.verifyToken(input),
      snapshot = await this.pendingHTLCs();
    if (psTokenAmount(token) < quote.amount + quote.fee_reserve)
      throw new Error("Not enough tokens to cover invoice and fee reserve");
    const groups = new Map<string, number>();
    for (const p of token.proofs) {
      checkPSBacking(p, snapshot);
      if (!p.preimage) throw new Error("Token is missing its backing preimage");
      groups.set(p.payment_hash, (groups.get(p.payment_hash) ?? 0) + p.amount);
    }
    const prepared = [...groups].flatMap(([hash, value]) =>
      Array.from({ length: Math.floor(Math.log2(value)) + 1 }, (_, i) => ({
        hash,
        ...ownerOutput(2 ** i),
      })),
    );
    if (prepared.length > 256)
      throw new Error("Too many change outputs; use fewer backing HTLCs");
    const change = prepared.map((p) => ({ ...p.output, payment_hash: p.hash }));
    const binding = transcript("Cashu_PS_HTLC_Melt_v3", {
      quote,
      inputs: token.proofs.map((p) => [
        p.amount,
        p.backing_amount,
        p.preimage,
        nullifier(p),
      ]),
      change,
    });
    return {
      kind: "melt",
      id: randomHex(),
      token,
      quote,
      secrets: prepared.map((p) => p.secret),
      request: {
        quote: quote.quote,
        change,
        inputs: token.proofs.map((p) => ({
          amount: p.amount,
          backing_amount: p.backing_amount,
          preimage: p.preimage!,
          presentation: present(this.verifyProof(p), binding),
        })),
      },
    };
  }
  async finishMelt(
    op: PSMeltOperation,
  ): Promise<{ receipt: PSReceipt; token: PSToken | null }> {
    const receipt = receiptSchema.parse(
      await this.request("/v1/melt/bolt11", op.request),
    );
    if (
      receipt.amount !== op.quote.amount ||
      receipt.payout_hash !== op.quote.payment_hash ||
      preimageHash(receipt.payout_preimage) !== receipt.payout_hash ||
      receipt.fee !== Math.ceil(receipt.fee_msat / 1000) ||
      receipt.fee > op.quote.fee_reserve ||
      receipt.change.reduce((n, p) => n + p.amount, 0) +
        receipt.expired_change !==
        psTokenAmount(op.token) - receipt.amount - receipt.fee
    )
      throw new Error("Mint returned an invalid melt receipt");
    const slots = new Map(
      op.request.change.map((p, i) => [
        `${p.payment_hash}:${p.amount}`,
        op.secrets[i],
      ]),
    );
    const sources = new Map(op.token.proofs.map((p) => [p.payment_hash, p]));
    const used = new Set<string>();
    const proofs = receipt.change.map((sig) => {
      const slot = `${sig.payment_hash}:${sig.amount}`,
        secret = slots.get(slot),
        source = sources.get(sig.payment_hash ?? "");
      if (!source || !secret || used.has(slot))
        throw new Error("Unexpected or duplicate change");
      used.add(slot);
      return this.proof(sig, secret, {
        payment_hash: source.payment_hash,
        backing_amount: source.backing_amount,
        preimage: source.preimage,
      });
    });
    let cost = receipt.amount + receipt.fee;
    for (const [hash] of sources) {
      const total = op.token.proofs
        .filter((p) => p.payment_hash === hash)
        .reduce((n, p) => n + p.amount, 0);
      const charged = Math.min(cost, total);
      cost -= charged;
      const returned = proofs
        .filter((p) => p.payment_hash === hash)
        .reduce((n, p) => n + p.amount, 0);
      if (returned !== 0 && returned !== total - charged)
        throw new Error("Mint changed backing allocation of change");
    }
    return {
      receipt,
      token: proofs.length ? { version: 3, mint: this.mintUrl, proofs } : null,
    };
  }
}
