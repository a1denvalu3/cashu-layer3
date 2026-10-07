import hashlib
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .crypto.bls import curve_order

Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
G1Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{96}$")]
KeysetId = Annotated[str, StringConstraints(pattern=r"^03[0-9a-f]{64}$")]
ProofHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
PresentationHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{642}$")]
PrivatePresentationHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{770}$")]
LinearProofHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{256}$")]
Amount = Annotated[int, Field(strict=True, gt=0, le=21_000_000 * 100_000_000)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvoiceState(str, Enum):
    open = "OPEN"
    accepted = "ACCEPTED"
    settled = "SETTLED"
    canceled = "CANCELED"


class QuoteRequest(WireModel):
    amount: Amount
    unit: Literal["sat"] = "sat"
    payment_hash: Hex32
    expiry: Annotated[int, Field(strict=True, ge=60, le=86400)] = 3600


class QuoteResponse(WireModel):
    quote: Hex32
    amount: Amount
    unit: Literal["sat"] = "sat"
    payment_hash: Hex32
    request: str
    expiry: int
    state: InvoiceState
    issued: bool
    keyset_id: KeysetId


class PendingHTLC(WireModel):
    """Public backing metadata; expiry_height includes LND's early cancellation."""

    payment_hash: Hex32
    amount: Amount
    invoice_expires_at: Annotated[int, Field(ge=0)]
    htlc_expiry_height: Annotated[int, Field(gt=0)] | None = None
    expiry_height: Annotated[int, Field(ge=0)] | None = None
    blocks_remaining: Annotated[int, Field(ge=0)] | None = None
    expires_at: Annotated[int, Field(ge=0)] | None = None


class PendingHTLCsResponse(WireModel):
    checked_at: Annotated[int, Field(ge=0)]
    block_height: Annotated[int, Field(ge=0)] | None
    htlcs: list[PendingHTLC]


class KeysetResponse(WireModel):
    id: KeysetId
    unit: Literal["sat"] = "sat"
    public_key: str


class MintRequest(WireModel):
    quote: Hex32
    commitment: G1Hex
    proof: ProofHex


class SignatureResponse(WireModel):
    keyset_id: KeysetId
    u: G1Hex
    v: G1Hex


class SwapRequest(WireModel):
    """Preserve the signed HTLC claim without revealing h, hash, or amount."""

    presentation: PrivatePresentationHex
    commitment: G1Hex
    owner_proof: ProofHex
    b: G1Hex
    equality_proof: LinearProofHex
    # Missing version identifies saved requests from the earlier two-round swap.
    version: Literal[1, 2] = 1


class BurnRequest(WireModel):
    amount: Amount
    presentation: PresentationHex
    request: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    # Absent only in saved requests from the earlier same-hash payout flow.
    preimage: Hex32 | None = None


class BurnResponse(WireModel):
    state: Literal["SETTLED"] = "SETTLED"
    amount: Amount
    payment_hash: Hex32
    preimage: Hex32
    payout_hash: Hex32 | None = None
    payout_preimage: Hex32 | None = None


class CheckRequest(WireModel):
    nullifiers: Annotated[list[G1Hex], Field(min_length=1, max_length=100)]


class CheckResponse(WireModel):
    states: list[Literal["UNSPENT", "PENDING", "SPENT"]]


class Token(WireModel):
    version: Literal[2] = 2
    mint: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    amount: Amount
    payment_hash: Hex32
    credential: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{386}$")]
    preimage: Hex32 | None = None

    @model_validator(mode="after")
    def validate_preimage(self) -> "Token":
        if (
            self.preimage is not None
            and preimage_hash(self.preimage) != self.payment_hash
        ):
            raise ValueError("HTLC preimage does not match the token payment hash")
        return self

    def with_preimage(self, preimage: str) -> "Token":
        return Token.model_validate(self.model_dump() | {"preimage": preimage})


def preimage_hash(preimage: str) -> str:
    if len(preimage) != 64 or any(c not in "0123456789abcdef" for c in preimage):
        raise ValueError("HTLC preimage must be 32-byte lowercase hex")
    return hashlib.sha256(bytes.fromhex(preimage)).hexdigest()


def htlc_claim_scalar(payment_hash: str, amount: int) -> int:
    """Commit to the full Lightning hash and whole invoice amount in sats.

    This is a PS attribute, separate from Lightning's SHA256(preimage).
    Hash before reducing into Fr so raw payment hashes separated by the
    curve order cannot be substituted for one another.
    """
    if type(amount) is not int or not 0 < amount <= 21_000_000 * 100_000_000:
        raise ValueError("invalid HTLC amount")
    if len(payment_hash) != 64 or any(
        c not in "0123456789abcdef" for c in payment_hash
    ):
        raise ValueError("payment hash must be 32-byte lowercase hex")
    encoded = (
        b"Cashu_PS_HTLC_Claim_v2"
        + amount.to_bytes(8, "big")
        + bytes.fromhex(payment_hash)
    )
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big") % curve_order


def burn_binding(amount: int, request: str) -> bytes:
    """Bind a spend to its exact payout invoice and amount."""
    return (
        b"Cashu_PS_HTLC_Redeem_v2"
        + amount.to_bytes(8, "big")
        + hashlib.sha256(request.encode()).digest()
    )


def swap_binding(commitment: bytes) -> bytes:
    return b"Cashu_PS_HTLC_Swap_v2" + commitment
