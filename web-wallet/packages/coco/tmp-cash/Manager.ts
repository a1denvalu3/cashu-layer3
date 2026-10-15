/** Coco's durable prepare/reserve/execute/finalize lifecycle for TmpCash credentials. */
import {
  PSWallet,
  PSMintError,
  isPSPendingMeltError,
  decodePSToken,
  encodePSToken,
  psDenominations,
  psTokenAmount,
  psNullifier,
  psPreimageHash,
  psRandomHex,
  checkPSBacking,
  amount as amountSchema,
  type PSProof,
  type PSToken,
  type PSMeltQuote,
  type PSMeltOperation,
  type PSReceipt,
  type PSSnapshot,
} from "@cashu/cashu-ts/tmp-cash";
import { EventBus } from "../events/EventBus";
import type {
  TmpCashRepository,
  TmpCashView,
  TmpCashFunding,
  TmpCashOperation,
  TmpCashProofRecord,
  TmpCashActivity,
} from "./types";

export class TmpCashManager {
  readonly events = new EventBus<{ change: undefined }>();
  readonly wallet: PSWallet;
  private snapshot: PSSnapshot | null = null;
  private verifiedAt = 0;
  private verifiedProofs = new Set<string>();
  private queue: Promise<unknown> = Promise.resolve();
  private refreshTask: Promise<void> | null = null;
  constructor(
    readonly repo: TmpCashRepository,
    mintUrl: string,
    fetcher?: typeof fetch,
  ) {
    this.wallet = new PSWallet(mintUrl, fetcher);
  }
  async init(): Promise<void> {
    await this.wallet.loadMint();
    await this.refresh();
  }
  private exclusive<T>(fn: () => Promise<T>): Promise<T> {
    const run = async (): Promise<T> =>
      typeof navigator !== "undefined" && navigator.locks
        ? // Coordinate writes with tabs opened before the tmp.cash rebrand too.
          await navigator.locks.request("cashu-layer3-wallet", fn)
        : await fn();
    const task = this.queue.then(run, run);
    this.queue = task.catch(() => undefined);
    return task;
  }
  private async changed(): Promise<void> {
    await this.events.emit("change", undefined);
  }
  async view(): Promise<TmpCashView> {
    const state = await this.repo.read();
    const local = state.proofs.filter((p) => p.mint === this.wallet.mintUrl);
    const sum = (kind: TmpCashProofRecord["state"]) =>
      local
        .filter((p) => p.state === kind)
        .reduce((n, p) => n + p.proof.amount, 0);
    const verified =
      this.snapshot !== null && Date.now() - this.verifiedAt < 60000;
    const spendable = local.filter(
      (p) => p.state === "ready" && verified && this.isLive(p.proof),
    );
    return {
      ...state,
      mint: this.wallet.mintUrl,
      balance: spendable.reduce((n, p) => n + p.proof.amount, 0),
      redeemable: spendable
        .filter((p) => p.proof.preimage)
        .reduce((n, p) => n + p.proof.amount, 0),
      reserved: sum("reserved") + sum("pending"),
      sent: sum("sent"),
      expired: sum("expired"),
      snapshot: this.snapshot,
      verified,
    };
  }
  private isLive(proof: PSProof) {
    if (!this.snapshot) return false;
    try {
      checkPSBacking(proof, this.snapshot);
      return true;
    } catch {
      return false;
    }
  }
  refresh(): Promise<void> {
    if (this.refreshTask) return this.refreshTask;
    this.refreshTask = this.exclusive(() => this.refreshUnlocked()).finally(
      () => {
        this.refreshTask = null;
      },
    );
    return this.refreshTask;
  }
  private async refreshUnlocked(): Promise<void> {
    try {
      // Capture claims before the remote snapshot, while holding the same lock
      // as issuance and spending. An older snapshot must not expire new outputs.
      const state = await this.repo.read();
      this.snapshot = await this.wallet.pendingHTLCs();
      const changes: { id: string; state: TmpCashProofRecord["state"] }[] = [];
      const candidates = state.proofs.filter(
        (p) =>
          p.mint === this.wallet.mintUrl &&
          p.operationId === null &&
          ["ready", "sent", "pending", "expired"].includes(p.state),
      );
      for (const p of candidates)
        if (p.state !== "expired" && !this.isLive(p.proof))
          changes.push({ id: p.id, state: "expired" });
      const live = candidates.filter((p) => this.isLive(p.proof));
      // Old snapshots may have falsely expired live claims. Recovery must also
      // preserve sent status, because unclaimed outgoing tokens still belong
      // to their recipients even when the mint reports them as unspent.
      const sent = new Set<string>();
      if (live.some((p) => p.state === "expired"))
        for (const row of state.activity)
          if (
            row.mint === this.wallet.mintUrl &&
            row.purpose === "send" &&
            row.token
          )
            for (const proof of decodePSToken(row.token).proofs)
              sent.add(psNullifier(proof));
      // Closed backing already makes a claim unusable. Its old signing key
      // may be unavailable after a regtest reset or mint replacement.
      for (const p of live) {
        const identity = JSON.stringify(p.proof);
        if (!this.verifiedProofs.has(identity)) {
          this.wallet.verifyProof(p.proof);
          this.verifiedProofs.add(identity);
        }
      }
      for (let start = 0; start < live.length; start += 256) {
        const batch = live.slice(start, start + 256),
          states = await this.wallet.checkProofs(batch.map((p) => p.proof));
        states.forEach((s, i) => {
          if (s === "SPENT") changes.push({ id: batch[i].id, state: "spent" });
          if (s === "PENDING" && ["ready", "expired"].includes(batch[i].state))
            changes.push({
              id: batch[i].id,
              state: sent.has(batch[i].id) ? "sent" : "pending",
            });
          if (s === "UNSPENT" && batch[i].state === "pending")
            changes.push({ id: batch[i].id, state: "ready" });
          if (s === "UNSPENT" && batch[i].state === "expired")
            changes.push({
              id: batch[i].id,
              state: sent.has(batch[i].id) ? "sent" : "ready",
            });
        });
      }
      await this.repo.updateProofStates(changes);
      this.verifiedAt = Date.now();
      await this.changed();
    } catch (error) {
      this.verifiedAt = 0;
      await this.changed();
      throw error;
    }
  }
  private token(proofs: PSProof[]): PSToken {
    return { version: 3, mint: this.wallet.mintUrl, proofs };
  }
  private record(
    proof: PSProof,
    state: TmpCashProofRecord["state"] = "ready",
  ): TmpCashProofRecord {
    return {
      id: psNullifier(proof),
      mint: this.wallet.mintUrl,
      proof,
      state,
      operationId: null,
      createdAt: Date.now(),
    };
  }
  private async save(operation: TmpCashOperation): Promise<void> {
    const inputs =
      operation.payload.kind === "mint"
        ? []
        : operation.payload.token.proofs.map((p) => this.record(p));
    await this.repo.reserve(operation, inputs);
    await this.changed();
  }
  private operation(
    payload: TmpCashOperation["payload"],
    purpose: TmpCashOperation["purpose"],
    amount: number,
    sendCount = 0,
  ): TmpCashOperation {
    return {
      id: payload.id,
      mint: this.wallet.mintUrl,
      payload,
      purpose,
      amount,
      sendCount,
      createdAt: Date.now(),
      error: null,
    };
  }
  async createFunding(
    value: number,
    invoiceExpiry = 3600,
  ): Promise<TmpCashFunding> {
    return this.exclusive(async () => {
      amountSchema.parse(value);
      const preimage = psRandomHex(),
        funding: TmpCashFunding = {
          id: psPreimageHash(preimage),
          mint: this.wallet.mintUrl,
          amount: value,
          preimage,
          invoiceExpiry,
          quote: null,
          state: "waiting",
          createdAt: Date.now(),
        };
      // Preserve the preimage before the first remote POST, including a lost quote response.
      await this.repo.saveFunding(funding);
      await this.changed();
      funding.quote = await this.wallet.createMintQuote(
        value,
        funding.id,
        invoiceExpiry,
      );
      await this.repo.updateFunding(funding);
      await this.changed();
      return funding;
    });
  }
  async pollFunding(id: string): Promise<TmpCashFunding> {
    return this.exclusive(async () => {
      const state = await this.repo.read(),
        funding = state.fundings.find(
          (f) => f.id === id && f.mint === this.wallet.mintUrl,
        );
      if (!funding) throw new Error("Unknown funding request");
      if (funding.state !== "waiting") return funding;
      const pending = state.operations.find(
        (o) =>
          o.mint === this.wallet.mintUrl &&
          o.payload.kind === "mint" &&
          o.payload.quote.payment_hash === funding.id,
      );
      if (pending) {
        // The mint may already have issued this exact request before its HTLC
        // closed. Recover its receipt instead of discarding the issuance journal.
        await this.execute(pending);
        return (await this.repo.read()).fundings.find((f) => f.id === id)!;
      }
      funding.quote = funding.quote
        ? await this.wallet.getMintQuote(funding.quote.quote)
        : await this.wallet.createMintQuote(
            funding.amount,
            funding.id,
            funding.invoiceExpiry,
          );
      if (
        funding.quote.state === "SETTLED" ||
        funding.quote.state === "CANCELED"
      ) {
        funding.state = "closed";
        await this.repo.updateFunding(funding);
        await this.changed();
        return funding;
      }
      await this.repo.updateFunding(funding);
      if (funding.quote.state === "ACCEPTED") {
        const op = this.operation(
          this.wallet.prepareMint(funding.quote, funding.preimage),
          "mint",
          funding.amount,
        );
        await this.save(op);
        await this.execute(op);
      }
      return (await this.repo.read()).fundings.find((f) => f.id === id)!;
    });
  }
  async send(value: number, paymentHash?: string): Promise<string> {
    return this.exclusive(async () => {
      amountSchema.parse(value);
      await this.refreshUnlocked();
      const { proofs } = await this.repo.read(),
        groups = new Map<string, TmpCashProofRecord[]>();
      for (const p of proofs)
        if (
          p.mint === this.wallet.mintUrl &&
          p.state === "ready" &&
          p.proof.preimage &&
          this.isLive(p.proof)
        )
          groups.set(p.proof.payment_hash, [
            ...(groups.get(p.proof.payment_hash) ?? []),
            p,
          ]);
      const group = [...groups]
        .filter(
          ([hash, ps]) =>
            (!paymentHash || hash === paymentHash) &&
            ps.reduce((n, p) => n + p.proof.amount, 0) >= value,
        )
        .sort((a, b) => a[1][0].createdAt - b[1][0].createdAt)[0];
      if (!group)
        throw new Error(
          "One backing HTLC must cover the send amount and include its preimage",
        );
      const selected: PSProof[] = [];
      let total = 0;
      for (const p of group[1].sort(
        (a, b) => b.proof.amount - a.proof.amount,
      )) {
        selected.push(p.proof);
        total += p.proof.amount;
        if (total >= value) break;
      }
      const sendAmounts = psDenominations(value);
      const payload = await this.wallet.prepareSwap(this.token(selected), [
        ...sendAmounts,
        ...psDenominations(total - value),
      ]);
      const op = this.operation(payload, "send", value, sendAmounts.length);
      await this.save(op);
      return (await this.execute(op)).token!;
    });
  }
  async receive(encoded: string): Promise<number> {
    return this.exclusive(async () => {
      const token = this.wallet.verifyToken(decodePSToken(encoded));
      const incoming = new Set(token.proofs.map(psNullifier));
      if (
        (await this.repo.read()).operations.some(
          (op) =>
            op.mint === this.wallet.mintUrl &&
            op.purpose === "receive" &&
            op.payload.kind === "swap" &&
            op.payload.token.proofs.some((p) => incoming.has(psNullifier(p))),
        )
      )
        throw new Error(
          "This token has saved receive operations. Close this dialog and retry them under Pending operations.",
        );
      const groups = new Map<string, PSProof[]>();
      token.proofs.forEach((p) =>
        groups.set(p.payment_hash, [...(groups.get(p.payment_hash) ?? []), p]),
      );
      // Each private swap has one backing HTLC, even when importing a combined token.
      for (const proofs of groups.values()) {
        const states = await this.wallet.checkProofs(proofs);
        if (states.some((s) => s !== "UNSPENT"))
          throw new Error("Token is already spent or has a pending operation");
      }
      const operations: TmpCashOperation[] = [];
      for (const proofs of groups.values()) {
        const source = this.token(proofs),
          op = this.operation(
            await this.wallet.prepareSwap(source),
            "receive",
            psTokenAmount(source),
          );
        operations.push(op);
      }
      // Preserve every incoming backing before spending any of them. A lost
      // first response must leave later groups recoverable after a reload.
      await this.repo.reserveMany(
        operations.map((operation) => ({
          operation,
          inputs:
            operation.payload.kind === "swap"
              ? operation.payload.token.proofs.map((p) => this.record(p))
              : [],
        })),
      );
      await this.changed();
      for (const op of operations) await this.execute(op);
      await this.refreshUnlocked();
      return psTokenAmount(token);
    });
  }
  async meltQuote(invoice: string): Promise<PSMeltQuote> {
    return this.wallet.createMeltQuote(invoice);
  }
  async attachPreimage(paymentHash: string, preimage: string): Promise<void> {
    return this.exclusive(async () => {
      if (psPreimageHash(preimage) !== paymentHash)
        throw new Error("Preimage does not match this backing HTLC");
      await this.repo.attachPreimage(
        this.wallet.mintUrl,
        paymentHash,
        preimage,
      );
      await this.changed();
    });
  }
  async melt(quote: PSMeltQuote): Promise<PSReceipt> {
    return this.exclusive(async () => {
      const saved = (await this.repo.read()).operations.find(
        (op) =>
          op.mint === this.wallet.mintUrl &&
          op.payload.kind === "melt" &&
          (op.payload.quote.quote === quote.quote ||
            op.payload.quote.payment_hash === quote.payment_hash),
      );
      if (saved) return (await this.execute(saved)).receipt!;
      await this.refreshUnlocked();
      const state = await this.repo.read(),
        selected: PSProof[] = [];
      let total = 0;
      // Keep input grouping stable so the mint's ordered fee allocation is predictable.
      const candidates = state.proofs
        .filter(
          (p) =>
            p.mint === this.wallet.mintUrl &&
            p.state === "ready" &&
            p.proof.preimage &&
            this.isLive(p.proof),
        )
        .sort(
          (a, b) =>
            a.proof.payment_hash.localeCompare(b.proof.payment_hash) ||
            b.proof.amount - a.proof.amount,
        );
      for (const p of candidates) {
        selected.push(p.proof);
        total += p.proof.amount;
        if (total >= quote.amount + quote.fee_reserve) break;
      }
      if (total < quote.amount + quote.fee_reserve)
        throw new Error(
          "Not enough redeemable tokens for invoice and fee reserve",
        );
      const op = this.operation(
        await this.wallet.prepareMelt(this.token(selected), quote),
        "melt",
        quote.amount,
      );
      await this.save(op);
      return (await this.execute(op)).receipt!;
    });
  }
  async retry(id: string): Promise<TmpCashActivity> {
    return this.exclusive(async () => {
      const op = (await this.repo.read()).operations.find(
        (o) => o.id === id && o.mint === this.wallet.mintUrl,
      );
      if (!op) throw new Error("Unknown pending operation");
      return this.execute(op);
    });
  }
  private async finishMelt(op: PSMeltOperation) {
    for (let check = 0; ; check++) {
      try {
        return await this.wallet.finishMelt(op);
      } catch (error) {
        // Reconcile only an explicit pending response. Every check replays
        // the persisted request, with the same inputs and change secrets.
        if (!isPSPendingMeltError(error) || check >= 4) throw error;
        await this.repo.setError(
          op.id,
          "Payment is pending; checking for completion.",
        );
        await this.changed();
        await new Promise<void>((resolve) => setTimeout(resolve, 1000));
      }
    }
  }
  private async execute(op: TmpCashOperation): Promise<TmpCashActivity> {
    try {
      const activity: TmpCashActivity = {
        id: op.id,
        mint: op.mint,
        purpose: op.purpose,
        amount: op.amount,
        createdAt: op.createdAt,
        state: "complete",
        token: null,
        receipt: null,
      };
      let proofs: TmpCashProofRecord[];
      if (op.payload.kind === "mint") {
        const token = await this.wallet.finishMint(op.payload);
        proofs = token.proofs.map((p) => this.record(p));
      } else if (op.payload.kind === "swap") {
        const token = await this.wallet.finishSwap(op.payload);
        proofs = token.proofs.map((p, i) =>
          this.record(
            p,
            op.purpose === "send" && i < op.sendCount ? "sent" : "ready",
          ),
        );
        if (op.purpose === "send") {
          activity.state = "sent";
          activity.token = encodePSToken(
            this.token(token.proofs.slice(0, op.sendCount)),
          );
        }
      } else {
        const result = await this.finishMelt(op.payload);
        activity.receipt = result.receipt;
        proofs = result.token?.proofs.map((p) => this.record(p)) ?? [];
      }
      await this.repo.complete(op, proofs, activity);
      await this.changed();
      // A successful receipt remains successful even when the follow-up snapshot is offline.
      try {
        await this.refreshUnlocked();
      } catch {
        /* view reports unverified until refresh succeeds */
      }
      return activity;
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Operation failed";
      await this.repo.setError(op.id, message);
      if (
        op.payload.kind === "melt" &&
        error instanceof PSMintError &&
        error.status === 409 &&
        [
          "melt payment failed; tokens are unspent",
          "melt quote has expired",
          "backing HTLC is closed; its tokens have expired",
          "backing HTLC redemption deadline has passed",
          "melt quote already belongs to another request",
          "payout invoice is already paid",
        ].includes(error.message)
      ) {
        try {
          const states = await this.wallet.checkProofs(op.payload.token.proofs);
          if (states.every((state) => state === "UNSPENT")) {
            await this.repo.release(op);
            await this.refreshUnlocked();
          }
        } catch {
          // Failed proof checks keep reservations. A failed backing refresh
          // after release leaves the stored inputs unverified until reconnection.
        }
      }
      await this.changed();
      // Ambiguous failures remain reserved; only checked, definite rejections release.
      throw error;
    }
  }
}
