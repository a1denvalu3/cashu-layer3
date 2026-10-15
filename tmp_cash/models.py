"""Version 3: denomination credentials bound to expiring HTLC claims."""

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .crypto.bls import curve_order
from .keyset import DENOMINATIONS, MAX_AMOUNT

Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
G1Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{96}$")]
KeysetId = Annotated[str, StringConstraints(pattern=r"^03[0-9a-f]{64}$")]
ProofHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
PresentationHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{642}$")]
PrivatePresentationHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{770}$")]
Amount = Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT)]
Sats = Annotated[int, Field(strict=True, ge=0, le=MAX_AMOUNT)]
MAX_PROOFS = 256


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


class OperatorFundingRequest(WireModel):
    amount: Amount
    expiry: Annotated[int, Field(strict=True, ge=60, le=86400)] = 3600


class OperatorFundingResponse(WireModel):
    amount: Amount
    unit: Literal["sat"] = "sat"
    request: str
    payment_hash: Hex32
    expiry: Annotated[int, Field(ge=0)]
    fee: Sats


class PendingHTLC(WireModel):
    payment_hash: Hex32
    amount: Amount
    invoice_expires_at: Annotated[int, Field(ge=0)]
    htlc_expiry_height: Annotated[int, Field(gt=0)] | None = None
    expiry_height: Annotated[int, Field(ge=0)] | None = None
    blocks_remaining: Annotated[int, Field(ge=0)] | None = None
    expires_at: Annotated[int, Field(ge=0)] | None = None
    melt_deadline_height: Annotated[int, Field(ge=0)] | None = None
    melt_deadline: Annotated[int, Field(ge=0)] | None = None


class PendingHTLCsResponse(WireModel):
    checked_at: Annotated[int, Field(ge=0)]
    block_height: Annotated[int, Field(ge=0)] | None
    htlcs: list[PendingHTLC]


class KeysetResponse(WireModel):
    id: KeysetId
    unit: Literal["sat"] = "sat"
    keys: dict[int, str]


class Denominated(WireModel):
    amount: Amount

    @model_validator(mode="after")
    def denomination(self):
        if self.amount not in DENOMINATIONS:
            raise ValueError("amount must be a supported binary denomination")
        return self


class Output(Denominated):
    commitment: G1Hex
    proof: ProofHex


class BlindOutput(Output):
    b: G1Hex


class MintRequest(WireModel):
    quote: Hex32
    outputs: Annotated[list[Output], Field(min_length=1, max_length=MAX_PROOFS)]


class SignatureResponse(Denominated):
    keyset_id: KeysetId
    u: G1Hex
    v: G1Hex
    payment_hash: Hex32 | None = None


class SignaturesResponse(WireModel):
    signatures: list[SignatureResponse]


class SwapInput(Denominated):
    presentation: PrivatePresentationHex


class SwapRequest(WireModel):
    version: Literal[3] = 3
    inputs: Annotated[list[SwapInput], Field(min_length=1, max_length=MAX_PROOFS)]
    outputs: Annotated[list[BlindOutput], Field(min_length=1, max_length=MAX_PROOFS)]
    equality_proof: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]+$", max_length=33024)
    ]


class MeltQuoteRequest(WireModel):
    request: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    fee_reserve: Sats | None = None


class MeltQuoteResponse(WireModel):
    quote: Hex32
    request: str
    payment_hash: Hex32
    amount: Amount
    fee_reserve: Sats
    expiry: int


class MeltInput(Denominated):
    backing_amount: Amount
    preimage: Hex32
    presentation: PresentationHex


class ChangeOutput(Output):
    payment_hash: Hex32


class MeltRequest(WireModel):
    quote: Hex32
    inputs: Annotated[list[MeltInput], Field(min_length=1, max_length=MAX_PROOFS)]
    change: Annotated[list[ChangeOutput], Field(max_length=MAX_PROOFS)]


class MeltResponse(WireModel):
    state: Literal["PAID"] = "PAID"
    amount: Amount
    fee: Sats
    fee_msat: Annotated[int, Field(strict=True, ge=0)]
    payout_hash: Hex32
    payout_preimage: Hex32
    change: list[SignatureResponse]
    expired_change: Sats = 0


class CheckRequest(WireModel):
    nullifiers: Annotated[list[G1Hex], Field(min_length=1, max_length=MAX_PROOFS)]


class CheckResponse(WireModel):
    states: list[Literal["UNSPENT", "PENDING", "SPENT"]]


class TokenProof(Denominated):
    payment_hash: Hex32
    backing_amount: Amount
    credential: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{386}$")]
    preimage: Hex32 | None = None

    @model_validator(mode="after")
    def validate_backing(self):
        if self.amount > self.backing_amount:
            raise ValueError("denomination exceeds backing HTLC amount")
        if (
            self.preimage is not None
            and preimage_hash(self.preimage) != self.payment_hash
        ):
            raise ValueError("HTLC preimage does not match the token payment hash")
        return self


class Token(WireModel):
    version: Literal[3] = 3
    mint: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    proofs: Annotated[list[TokenProof], Field(min_length=1, max_length=MAX_PROOFS)]

    @model_validator(mode="after")
    def validate_proofs(self):
        from .crypto.ps import Credential

        identities = [
            Credential.from_bytes(bytes.fromhex(p.credential)).s for p in self.proofs
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate token proof")
        if self.amount > MAX_AMOUNT:
            raise ValueError("token amount exceeds maximum")
        backing: dict[str, tuple[int, str | None]] = {}
        for proof in self.proofs:
            previous = backing.setdefault(
                proof.payment_hash, (proof.backing_amount, proof.preimage)
            )
            if previous != (proof.backing_amount, proof.preimage):
                raise ValueError("inconsistent backing metadata")
        return self

    @property
    def amount(self) -> int:
        return sum(p.amount for p in self.proofs)

    @property
    def payment_hash(self) -> str:
        if len({p.payment_hash for p in self.proofs}) != 1:
            raise ValueError("token contains multiple backing HTLCs")
        return self.proofs[0].payment_hash

    def with_preimage(self, preimage: str) -> "Token":
        return Token(
            mint=self.mint,
            proofs=[
                TokenProof.model_validate(p.model_dump() | {"preimage": preimage})
                for p in self.proofs
            ],
        )


def preimage_hash(preimage: str) -> str:
    if len(preimage) != 64 or any(c not in "0123456789abcdef" for c in preimage):
        raise ValueError("HTLC preimage must be 32-byte lowercase hex")
    return hashlib.sha256(bytes.fromhex(preimage)).hexdigest()


def htlc_claim_scalar(payment_hash: str, amount: int) -> int:
    if type(amount) is not int or not 0 < amount <= MAX_AMOUNT:
        raise ValueError("invalid HTLC amount")
    if len(payment_hash) != 64 or any(
        c not in "0123456789abcdef" for c in payment_hash
    ):
        raise ValueError("payment hash must be 32-byte lowercase hex")
    encoded = (
        b"Cashu_PS_HTLC_Claim_v3"
        + amount.to_bytes(8, "big")
        + bytes.fromhex(payment_hash)
    )
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big") % curve_order


def transcript(domain: str, payload: object) -> bytes:
    return hashlib.sha256(
        domain.encode()
        + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def swap_binding(inputs: list[tuple[int, str]], outputs: list[BlindOutput]) -> bytes:
    return transcript(
        "Cashu_PS_HTLC_Swap_v3",
        {"inputs": inputs, "outputs": [p.model_dump() for p in outputs]},
    )


def melt_binding(
    quote: MeltQuoteResponse,
    inputs: list[tuple[int, int, str, str]],
    change: list[ChangeOutput],
) -> bytes:
    return transcript(
        "Cashu_PS_HTLC_Melt_v3",
        {
            "quote": quote.model_dump(),
            "inputs": inputs,
            "change": [p.model_dump() for p in change],
        },
    )
