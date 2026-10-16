import { decodePSToken, psNullifier } from "@cashu/cashu-ts/tmp-cash";
import type {
  TmpCashActivity,
  TmpCashProofRecord,
} from "coco-cashu-core/tmp-cash";

export interface ExpiredBacking {
  hash: string;
  amount: number;
  sharedAmount: number;
  observedAt: number | null;
}

/** Derive history from durable proofs so recovery also removes false expiries. */
export function expiredBackings(
  proofs: TmpCashProofRecord[],
  activity: TmpCashActivity[],
  mint: string,
): ExpiredBacking[] {
  const expired = proofs.filter(
    (p) => p.mint === mint && p.state === "expired",
  );
  if (!expired.length) return [];
  const shared = new Set(
    activity
      .filter((row) => row.mint === mint && row.purpose === "send" && row.token)
      .flatMap((row) => decodePSToken(row.token!).proofs.map(psNullifier)),
  );
  const groups = new Map<string, ExpiredBacking>();
  for (const p of expired) {
    const hash = p.proof.payment_hash;
    const group = groups.get(hash) ?? {
      hash,
      amount: 0,
      sharedAmount: 0,
      observedAt: null,
    };
    if (shared.has(p.id)) group.sharedAmount += p.proof.amount;
    else group.amount += p.proof.amount;
    if (p.expiredAt !== undefined)
      group.observedAt = Math.min(group.observedAt ?? Infinity, p.expiredAt);
    groups.set(hash, group);
  }
  return [...groups.values()].sort(
    (a, b) => (b.observedAt ?? 0) - (a.observedAt ?? 0),
  );
}
