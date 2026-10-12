import type {
  PSOperation,
  PSProof,
  PSQuote,
  PSReceipt,
  PSSnapshot,
} from "@cashu/cashu-ts/layer3";

export type Layer3ProofState =
  | "ready"
  | "pending"
  | "reserved"
  | "sent"
  | "spent"
  | "expired";
export interface Layer3ProofRecord {
  id: string;
  mint: string;
  proof: PSProof;
  state: Layer3ProofState;
  operationId: string | null;
  createdAt: number;
}
export interface Layer3Funding {
  id: string;
  mint: string;
  amount: number;
  preimage: string;
  invoiceExpiry: number;
  quote: PSQuote | null;
  state: "waiting" | "minted" | "closed";
  createdAt: number;
}
export interface Layer3Operation {
  id: string;
  mint: string;
  payload: PSOperation;
  purpose: "mint" | "send" | "receive" | "melt";
  sendCount: number;
  amount: number;
  createdAt: number;
  error: string | null;
}
export interface Layer3Activity {
  id: string;
  mint: string;
  purpose: Layer3Operation["purpose"];
  amount: number;
  createdAt: number;
  state: "complete" | "sent";
  token: string | null;
  receipt: PSReceipt | null;
}
export interface Layer3State {
  proofs: Layer3ProofRecord[];
  operations: Layer3Operation[];
  fundings: Layer3Funding[];
  activity: Layer3Activity[];
}
export interface Layer3Repository {
  read(): Promise<Layer3State>;
  saveFunding(funding: Layer3Funding): Promise<void>;
  updateFunding(funding: Layer3Funding): Promise<void>;
  reserve(
    operation: Layer3Operation,
    inputs: Layer3ProofRecord[],
  ): Promise<void>;
  complete(
    operation: Layer3Operation,
    proofs: Layer3ProofRecord[],
    activity: Layer3Activity,
  ): Promise<void>;
  release(operation: Layer3Operation): Promise<void>;
  setError(id: string, message: string): Promise<void>;
  updateProofStates(
    changes: { id: string; state: Layer3ProofState }[],
  ): Promise<void>;
  attachPreimage(
    mint: string,
    paymentHash: string,
    preimage: string,
  ): Promise<void>;
  importState(state: Layer3State): Promise<void>;
}
export interface Layer3View extends Layer3State {
  mint: string;
  balance: number;
  redeemable: number;
  reserved: number;
  sent: number;
  expired: number;
  snapshot: PSSnapshot | null;
  verified: boolean;
}
