import type { Table } from "dexie";
import { IdbDb } from "../lib/db";
import type {
  Layer3Repository,
  Layer3State,
  Layer3Funding,
  Layer3ProofRecord,
  Layer3Operation,
  Layer3Activity,
  Layer3ProofState,
} from "coco-cashu-core/layer3";
import { validateLayer3State } from "./validation";
import { psPreimageHash } from "@cashu/cashu-ts/layer3";

/** Separate PS schema: standard Coco secp256k1 proof tables are never reinterpreted. */
export class Layer3IndexedDbRepository
  extends IdbDb
  implements Layer3Repository
{
  proofs!: Table<Layer3ProofRecord, string>;
  operations!: Table<Layer3Operation, string>;
  fundings!: Table<Layer3Funding, string>;
  activity!: Table<Layer3Activity, string>;
  constructor(name = "cashu_layer3_wallet_v3") {
    super({ name });
    this.version(1).stores({
      proofs: "id,mint,state,operationId",
      operations: "id,mint",
      fundings: "id,mint,state",
      activity: "id,mint,createdAt",
    });
  }
  async read(): Promise<Layer3State> {
    return this.runTransaction(
      "r",
      ["proofs", "operations", "fundings", "activity"],
      async () => ({
        proofs: await this.proofs.toArray(),
        operations: await this.operations.toArray(),
        fundings: await this.fundings.toArray(),
        activity: await this.activity.toArray(),
      }),
    );
  }
  async saveFunding(f: Layer3Funding): Promise<void> {
    await this.fundings.add(f);
  }
  async updateFunding(f: Layer3Funding): Promise<void> {
    await this.fundings.put(f);
  }
  async reserve(
    op: Layer3Operation,
    inputs: Layer3ProofRecord[],
  ): Promise<void> {
    await this.runTransaction("rw", ["operations", "proofs"], async () => {
      if (await this.operations.get(op.id))
        throw new Error("Operation already exists");
      const mintQuoteId =
        op.payload.kind === "mint" ? op.payload.quote.quote : null;
      if (
        mintQuoteId &&
        (await this.operations.toArray()).some(
          (o) =>
            o.payload.kind === "mint" && o.payload.quote.quote === mintQuoteId,
        )
      )
        throw new Error("Quote already has a saved mint request");
      const meltQuoteId =
        op.payload.kind === "melt" ? op.payload.quote.quote : null;
      const payoutHash =
        op.payload.kind === "melt" ? op.payload.quote.payment_hash : null;
      if (
        meltQuoteId &&
        (await this.operations.toArray()).some(
          (existing) =>
            existing.mint === op.mint &&
            existing.payload.kind === "melt" &&
            (existing.payload.quote.quote === meltQuoteId ||
              existing.payload.quote.payment_hash === payoutHash),
        )
      )
        throw new Error("Quote already has a saved melt request; retry it");
      for (const p of inputs) {
        const existing = await this.proofs.get(p.id);
        if (
          existing &&
          (existing.mint !== p.mint ||
            (existing.state !== "ready" &&
              !(op.purpose === "receive" && existing.state === "sent")))
        )
          throw new Error("Proof is already reserved, sent or spent");
        await this.proofs.put({ ...p, state: "reserved", operationId: op.id });
      }
      await this.operations.add(op);
    });
  }
  async complete(
    op: Layer3Operation,
    proofs: Layer3ProofRecord[],
    activity: Layer3Activity,
  ): Promise<void> {
    await this.runTransaction(
      "rw",
      ["operations", "proofs", "activity", "fundings"],
      async () => {
        if (!(await this.operations.get(op.id)))
          throw new Error("Unknown saved operation");
        const inputs = await this.proofs
          .where("operationId")
          .equals(op.id)
          .toArray();
        for (const p of inputs)
          await this.proofs.put({ ...p, state: "spent", operationId: null });
        for (const p of proofs) {
          if (await this.proofs.get(p.id))
            throw new Error("Mint reused an existing proof");
          await this.proofs.add(p);
        }
        if (op.payload.kind === "mint") {
          const f = await this.fundings.get(op.payload.quote.payment_hash);
          if (f) await this.fundings.put({ ...f, state: "minted" });
        }
        await this.activity.add(activity);
        await this.operations.delete(op.id);
      },
    );
  }
  async release(op: Layer3Operation): Promise<void> {
    await this.runTransaction("rw", ["operations", "proofs"], async () => {
      const saved = await this.operations.get(op.id);
      if (!saved) return;
      if (
        saved.mint !== op.mint ||
        JSON.stringify(saved.payload) !== JSON.stringify(op.payload)
      )
        throw new Error("Saved operation changed");
      const inputs = await this.proofs
        .where("operationId")
        .equals(op.id)
        .toArray();
      for (const proof of inputs) {
        if (proof.state !== "reserved")
          throw new Error("Input reservation changed");
        await this.proofs.update(proof.id, {
          state: "ready",
          operationId: null,
        });
      }
      await this.operations.delete(op.id);
    });
  }
  async setError(id: string, error: string): Promise<void> {
    await this.operations.update(id, { error: error.slice(0, 500) });
  }
  async updateProofStates(
    changes: { id: string; state: Layer3ProofState }[],
  ): Promise<void> {
    await this.runTransaction("rw", ["proofs"], async () => {
      for (const { id, state } of changes) {
        const p = await this.proofs.get(id);
        if (p && ["ready", "sent", "pending"].includes(p.state))
          await this.proofs.update(id, { state });
      }
    });
  }
  async attachPreimage(
    mint: string,
    paymentHash: string,
    preimage: string,
  ): Promise<void> {
    if (psPreimageHash(preimage) !== paymentHash)
      throw new Error("Preimage does not match backing HTLC");
    await this.runTransaction("rw", ["proofs"], async () => {
      const proofs = (
        await this.proofs.where("mint").equals(mint).toArray()
      ).filter((p) => p.proof.payment_hash === paymentHash);
      if (!proofs.length || proofs.some((p) => p.state === "reserved"))
        throw new Error("Backing is unknown or has a pending operation");
      for (const p of proofs)
        await this.proofs.put({ ...p, proof: { ...p.proof, preimage } });
    });
  }
  async importState(input: Layer3State): Promise<void> {
    const state = validateLayer3State(input);
    await this.runTransaction(
      "rw",
      ["proofs", "operations", "fundings", "activity"],
      async () => {
        // Restore into an empty wallet to avoid resurrecting locally spent or reserved proofs.
        if (
          (await this.proofs.count()) ||
          (await this.operations.count()) ||
          (await this.fundings.count()) ||
          (await this.activity.count())
        )
          throw new Error(
            "Restore requires an empty wallet in a new browser profile",
          );
        await this.proofs.bulkAdd(state.proofs);
        await this.operations.bulkAdd(state.operations);
        await this.fundings.bulkAdd(state.fundings);
        await this.activity.bulkAdd(state.activity);
      },
    );
  }
}
