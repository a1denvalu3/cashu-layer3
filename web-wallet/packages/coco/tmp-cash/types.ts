import type {
  PSOperation,
  PSProof,
  PSQuote,
  PSReceipt,
  PSSnapshot,
} from "@cashu/cashu-ts/tmp-cash";

export type TmpCashProofState =
  | "ready"
  | "pending"
  | "reserved"
  | "sent"
  | "spent"
  | "expired";
export interface TmpCashProofRecord {
  id: string;
  mint: string;
  proof: PSProof;
  state: TmpCashProofState;
  operationId: string | null;
  createdAt: number;
  /** When this wallet first observed expiry, not the backing's exact close time. */
  expiredAt?: number;
}
export interface TmpCashFunding {
  id: string;
  mint: string;
  amount: number;
  preimage: string;
  invoiceExpiry: number;
  quote: PSQuote | null;
  state: "waiting" | "minted" | "closed";
  createdAt: number;
}
export interface TmpCashOperation {
  id: string;
  mint: string;
  payload: PSOperation;
  purpose: "mint" | "send" | "receive" | "melt";
  sendCount: number;
  amount: number;
  createdAt: number;
  error: string | null;
}
export interface TmpCashActivity {
  id: string;
  mint: string;
  purpose: TmpCashOperation["purpose"];
  amount: number;
  createdAt: number;
  state: "complete" | "sent";
  token: string | null;
  receipt: PSReceipt | null;
}
export interface TmpCashState {
  proofs: TmpCashProofRecord[];
  operations: TmpCashOperation[];
  fundings: TmpCashFunding[];
  activity: TmpCashActivity[];
}
export interface TmpCashRepository {
  read(): Promise<TmpCashState>;
  saveFunding(funding: TmpCashFunding): Promise<void>;
  updateFunding(funding: TmpCashFunding): Promise<void>;
  reserve(
    operation: TmpCashOperation,
    inputs: TmpCashProofRecord[],
  ): Promise<void>;
  reserveMany(
    entries: {
      operation: TmpCashOperation;
      inputs: TmpCashProofRecord[];
    }[],
  ): Promise<void>;
  complete(
    operation: TmpCashOperation,
    proofs: TmpCashProofRecord[],
    activity: TmpCashActivity,
  ): Promise<void>;
  release(operation: TmpCashOperation): Promise<void>;
  expire(operation: TmpCashOperation): Promise<void>;
  setError(id: string, message: string): Promise<void>;
  updateProofStates(
    changes: { id: string; state: TmpCashProofState }[],
  ): Promise<void>;
  attachPreimage(
    mint: string,
    paymentHash: string,
    preimage: string,
  ): Promise<void>;
  importState(state: TmpCashState): Promise<void>;
}
export interface TmpCashView extends TmpCashState {
  mint: string;
  balance: number;
  redeemable: number;
  reserved: number;
  sent: number;
  expired: number;
  snapshot: PSSnapshot | null;
  verified: boolean;
}
