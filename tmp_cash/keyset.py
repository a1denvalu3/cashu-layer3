"""One keyset with independent PS keys for each binary denomination."""

import hashlib
import hmac

from .crypto.bls import PublicKey
from .crypto.ps import (
    G1,
    G2_PK,
    LinearStatement,
    MintPrivateKeyPS,
    MintPublicKeyPS,
    PrivatePresentation,
)

MAX_AMOUNT = 21_000_000 * 100_000_000
DENOMINATIONS = tuple(1 << i for i in range(MAX_AMOUNT.bit_length()))
COMMON_H_DST = b"Cashu_PS_Common_HTLC_v3"


def denominations(amount: int) -> list[int]:
    if type(amount) is not int or not 0 <= amount <= MAX_AMOUNT:
        raise ValueError("invalid amount")
    return [value for value in reversed(DENOMINATIONS) if amount & value]


def keyset_id(keys: dict[int, MintPublicKeyPS]) -> str:
    if set(keys) != set(DENOMINATIONS):
        raise ValueError("incomplete denomination keyset")
    payload = b"Cashu_PS_Denominations_v3" + b"".join(
        amount.to_bytes(8, "big") + keys[amount].to_bytes() for amount in DENOMINATIONS
    )
    return "03" + hashlib.sha256(payload).hexdigest()


class Keyset:
    def __init__(self, seed: bytes):
        self.private = {
            amount: MintPrivateKeyPS.from_seed(
                hmac.new(
                    seed,
                    b"Cashu_PS_Denomination_v3" + amount.to_bytes(8, "big"),
                    hashlib.sha256,
                ).digest()
            )
            for amount in DENOMINATIONS
        }
        self.public = {amount: key.public_key for amount, key in self.private.items()}
        self.id = keyset_id(self.public)
        for key in self.public.values():
            key._keyset_id = self.id


def common_h_statements(
    inputs: list[tuple[MintPublicKeyPS, PrivatePresentation]],
    outputs: list[tuple[MintPublicKeyPS, PublicKey]],
) -> list[LinearStatement]:
    """One h witness; a distinct blinder for every input and output."""
    return [
        (presentation.kappa_h, [(key.Y_h2, 0), (G2_PK, index + 1)])
        for index, (key, presentation) in enumerate(inputs)
    ] + [
        (commitment, [(key.Y_h1, 0), (G1, len(inputs) + index + 1)])
        for index, (key, commitment) in enumerate(outputs)
    ]
