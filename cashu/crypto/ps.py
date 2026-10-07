"""PS credentials and private swap proofs extracted from cashu-nft.

The two signed attributes are the HTLC claim scalar h (committing to the
full payment hash and invoice amount) and owner secret s. Initial issuance
signs C = s*G1 with a fresh secret k:
u = k*G1; v = k*((x + y_h*h)*G1 + y_s*C).
Private swaps use a public-key commitment to h so signing needs one response.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pyblst

from .bls import G2, PrivateKey, PublicKey, curve_order
from .keys import derive_keyset_id_ps

_G1_HEX = "97f1d3a73197d7942695638c4fa9ac0fc3688c4f9774b905a14e3a3f171bac586c55e83ff97a1aeffb3af00adb22c6bb"
G1 = PublicKey(compressed=bytes.fromhex(_G1_HEX), group="G1")
G2_PK = PublicKey(point=G2, group="G2")
PS_ISSUE_DST = b"Cashu_PS_Issue_v1"
PS_PRESENT_DST = b"Cashu_PS_Present_v1"
PS_GNULL_DST = b"CASHU_PS_GNULL_XMD:SHA-256_SSWU_RO_"
PS_BURN_BINDING = b"Cashu_PS_Burn_v1"
PS_KEY_DST = b"Cashu_PS_Key_v1"
PS_COMMIT_DST = b"Cashu_PS_CommitEq_v1"
PS_K2_DST = b"Cashu_PS_TransferK2_v1"
G_NULL = PublicKey(
    point=pyblst.BlstP1Element().hash_to_group(b"ps_nullifier_base", PS_GNULL_DST),
    group="G1",
)
LinearStatement = Tuple[PublicKey, List[Tuple[PublicKey, int]]]
KEYSET_ID_BYTES = 33


def _random_scalar() -> int:
    scalar = 0
    while not 0 < scalar < curve_order:
        scalar = int.from_bytes(os.urandom(32), "big")
    return scalar


def _scalar_bytes(scalar: int) -> bytes:
    return scalar.to_bytes(32, "big")


def _add_p1(a: PublicKey, b: PublicKey) -> PublicKey:
    return PublicKey(point=a.point + b.point, group="G1")


def _neg_p1(a: PublicKey) -> PublicKey:
    return PublicKey(point=-a.point, group="G1")


def _add_pk(a: PublicKey, b: PublicKey) -> PublicKey:
    return PublicKey(point=a.point + b.point, group=a.group)


def _neg_pk(a: PublicKey) -> PublicKey:
    return PublicKey(point=-a.point, group=a.group)


def _infinity(group: str) -> PublicKey:
    point = pyblst.BlstP1Element() if group == "G1" else pyblst.BlstP2Element()
    return PublicKey(point=point, group=group)


def _challenge(transcript: bytes) -> int:
    return int.from_bytes(hashlib.sha256(transcript).digest(), "big") % curve_order


def _g1_from_bytes(compressed: bytes) -> PublicKey:
    try:
        return PublicKey(compressed=compressed, group="G1")
    except ValueError:
        raise ValueError("invalid G1 point encoding")


def _g2_from_bytes(compressed: bytes) -> PublicKey:
    try:
        return PublicKey(compressed=compressed, group="G2")
    except ValueError:
        raise ValueError("invalid G2 point encoding")


def _scalar_from_bytes(raw: bytes) -> int:
    if len(raw) != 32:
        raise ValueError("scalars are 32 bytes")
    scalar = int.from_bytes(raw, "big")
    if scalar >= curve_order:
        raise ValueError("scalar out of range")
    return scalar


@dataclass
class DlogEqProof:
    """Chaum-Pedersen proof that one scalar is the discrete log of n points
    with respect to n bases."""

    challenge: int
    response: int

    def to_bytes(self) -> bytes:
        return _scalar_bytes(self.challenge) + _scalar_bytes(self.response)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DlogEqProof":
        if len(raw) != 64:
            raise ValueError("DlogEqProof is 64 bytes")
        return cls(
            challenge=_scalar_from_bytes(raw[:32]),
            response=_scalar_from_bytes(raw[32:]),
        )


def _dlog_eq_transcript(
    dst: bytes,
    bases: List[PublicKey],
    points: List[PublicKey],
    commitments: List[PublicKey],
    binding: bytes = b"",
) -> bytes:
    transcript = dst + len(binding).to_bytes(2, "big") + binding
    for group in (bases, points, commitments):
        for p in group:
            serialized = p.format()
            transcript += len(serialized).to_bytes(2, "big") + serialized
    return transcript


def prove_dlog_eq(
    bases: List[PublicKey],
    points: List[PublicKey],
    secret: int,
    dst: bytes,
    binding: bytes = b"",
) -> DlogEqProof:
    if not bases or len(bases) != len(points):
        raise ValueError("need an equal, nonzero number of bases and points")
    if not 0 < secret < curve_order:
        raise ValueError("secret must be in Fr*")
    nonce = _random_scalar()
    commitments = [base * nonce for base in bases]
    challenge = _challenge(
        _dlog_eq_transcript(dst, bases, points, commitments, binding)
    )
    response = (nonce + challenge * secret) % curve_order
    return DlogEqProof(challenge=challenge, response=response)


def verify_dlog_eq(
    bases: List[PublicKey],
    points: List[PublicKey],
    proof: DlogEqProof,
    dst: bytes,
    binding: bytes = b"",
) -> bool:
    if not bases or len(bases) != len(points):
        return False
    if not (0 <= proof.challenge < curve_order and 0 <= proof.response < curve_order):
        return False
    commitments = [
        _add_p1(base * proof.response, _neg_p1(point * proof.challenge))
        for base, point in zip(bases, points)
    ]
    expected = _challenge(_dlog_eq_transcript(dst, bases, points, commitments, binding))
    return expected == proof.challenge


@dataclass
class LinearProof:
    """Multi-witness sigma proof (Fiat-Shamir): knowledge of witnesses
    w_0..w_{n-1} satisfying every statement simultaneously, proving the
    SAME witness values across statements (e.g. one h in both a G2
    commitment and a G1 commitment)."""

    challenge: int
    responses: List[int]

    def to_bytes(self) -> bytes:
        return _scalar_bytes(self.challenge) + b"".join(
            _scalar_bytes(r) for r in self.responses
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "LinearProof":
        if len(raw) < 64 or len(raw) % 32 != 0:
            raise ValueError("LinearProof is 32 * (n + 1) bytes")
        return cls(
            challenge=_scalar_from_bytes(raw[:32]),
            responses=[
                _scalar_from_bytes(raw[i : i + 32]) for i in range(32, len(raw), 32)
            ],
        )


def _linear_num_witnesses(statements: List[LinearStatement]) -> int:
    n = 0
    for _, terms in statements:
        for _, witness_index in terms:
            n = max(n, witness_index + 1)
    return n


def _linear_combine(
    terms: List[Tuple[PublicKey, int]], scalars: List[int]
) -> PublicKey:
    acc = _infinity(terms[0][0].group)
    for base, i in terms:
        acc = _add_pk(acc, base * scalars[i])
    return acc


def _linear_transcript(
    dst: bytes,
    binding: bytes,
    statements: List[LinearStatement],
    commitments: List[PublicKey],
) -> bytes:
    transcript = dst + len(binding).to_bytes(2, "big") + binding
    for (point, terms), commitment in zip(statements, commitments):
        for p in (point, commitment):
            serialized = p.format()
            transcript += len(serialized).to_bytes(2, "big") + serialized
        for base, witness_index in terms:
            serialized = base.format()
            transcript += (
                len(serialized).to_bytes(2, "big")
                + serialized
                + witness_index.to_bytes(2, "big")
            )
    return transcript


def prove_linear(
    statements: List[LinearStatement],
    witnesses: List[int],
    dst: bytes,
    binding: bytes = b"",
) -> LinearProof:
    if not statements:
        raise ValueError("need at least one statement")
    if _linear_num_witnesses(statements) != len(witnesses):
        raise ValueError("witness count does not match the statements")
    if any(not 0 <= w < curve_order for w in witnesses):
        raise ValueError("witnesses must be scalars")
    nonces = [_random_scalar() for _ in witnesses]
    commitments = [_linear_combine(terms, nonces) for _, terms in statements]
    challenge = _challenge(_linear_transcript(dst, binding, statements, commitments))
    responses = [
        (nonce + challenge * w) % curve_order for nonce, w in zip(nonces, witnesses)
    ]
    return LinearProof(challenge=challenge, responses=responses)


def verify_linear(
    statements: List[LinearStatement],
    proof: LinearProof,
    dst: bytes,
    binding: bytes = b"",
) -> bool:
    if not statements:
        return False
    if len(proof.responses) != _linear_num_witnesses(statements):
        return False
    if not 0 <= proof.challenge < curve_order:
        return False
    if any(not 0 <= r < curve_order for r in proof.responses):
        return False
    commitments = [
        _add_pk(
            _linear_combine(terms, proof.responses),
            _neg_pk(point * proof.challenge),
        )
        for point, terms in statements
    ]
    expected = _challenge(_linear_transcript(dst, binding, statements, commitments))
    return expected == proof.challenge


def _derive_scalar(seed: bytes, label: bytes) -> int:
    scalar = 0
    counter = 0
    while not 0 < scalar < curve_order:
        scalar = (
            int.from_bytes(
                hashlib.sha256(
                    PS_KEY_DST + label + counter.to_bytes(4, "big") + seed
                ).digest(),
                "big",
            )
            % curve_order
        )
        counter += 1
    return scalar


class MintPrivateKeyPS:
    def __init__(
        self,
        x: Optional[PrivateKey] = None,
        y_h: Optional[PrivateKey] = None,
        y_s: Optional[PrivateKey] = None,
    ):
        self.x = x or PrivateKey()
        self.y_h = y_h or PrivateKey()
        self.y_s = y_s or PrivateKey()

    @classmethod
    def from_seed(cls, seed: bytes) -> "MintPrivateKeyPS":
        """Deterministically derive a mint key from at least 16 bytes of
        seed entropy, so mint keys can be backed up and restored."""
        if len(seed) < 16:
            raise ValueError("seed must be at least 16 bytes")
        return cls(
            x=PrivateKey(scalar=_derive_scalar(seed, b"x")),
            y_h=PrivateKey(scalar=_derive_scalar(seed, b"y_h")),
            y_s=PrivateKey(scalar=_derive_scalar(seed, b"y_s")),
        )

    @property
    def public_key(self) -> "MintPublicKeyPS":
        return MintPublicKeyPS(
            X2=self.x.get_g2_public_key(),
            Y_h2=self.y_h.get_g2_public_key(),
            Y_s2=self.y_s.get_g2_public_key(),
            Y_h1=G1 * self.y_h.scalar,
        )


class MintPublicKeyPS:
    """Mint public parameters.

    Y_h1 = y_h * g1 lives in G1 so the owner can commit to h before the
    mint chooses its signing randomness. Publishing it
    does not weaken forgery resistance: moving a credential between HTLC claims
    still requires u^{y_h}, and g1^{y_h} does not yield that without
    solving CDH in G1 (a type-3 pairing gives no G1<->G2 homomorphism).
    y_s remains G2-only. The other values exist in G2 only, by construction.
    """

    def __init__(
        self, X2: PublicKey, Y_h2: PublicKey, Y_s2: PublicKey, Y_h1: PublicKey
    ):
        self.X2 = X2
        self.Y_h2 = Y_h2
        self.Y_s2 = Y_s2
        self.Y_h1 = Y_h1

    def to_bytes(self) -> bytes:
        return (
            self.X2.format()
            + self.Y_h2.format()
            + self.Y_s2.format()
            + self.Y_h1.format()
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MintPublicKeyPS":
        if len(raw) != 336:
            raise ValueError("MintPublicKeyPS is 336 bytes")
        return cls(
            X2=_g2_from_bytes(raw[:96]),
            Y_h2=_g2_from_bytes(raw[96:192]),
            Y_s2=_g2_from_bytes(raw[192:288]),
            Y_h1=_g1_from_bytes(raw[288:]),
        )

    @property
    def keyset_id(self) -> str:
        """Version-03 identifier of this parameter set, derived the same
        way as v3 ecash keysets: a full 32-byte SHA-256 hash behind a
        version byte, committing to a length-framed preimage of the three
        G2 points and the G1 point Y_h1 (see keys.derive_keyset_id_ps).

        Credentials and presentations carry it so verifiers can select the
        right parameters across key rotations."""
        return derive_keyset_id_ps(
            self.X2.format(),
            self.Y_h2.format(),
            self.Y_s2.format(),
            self.Y_h1.format(),
        )


def _keyset_id_from_bytes(raw: bytes) -> str:
    if len(raw) != KEYSET_ID_BYTES:
        raise ValueError(f"keyset id is {KEYSET_ID_BYTES} bytes")
    return raw.hex()


def _keyset_id_to_bytes(keyset_id: str) -> bytes:
    try:
        raw = bytes.fromhex(keyset_id)
    except ValueError:
        raise ValueError("keyset id must be hex")
    if len(raw) != KEYSET_ID_BYTES:
        raise ValueError(f"keyset id must be {2 * KEYSET_ID_BYTES} hex chars")
    return raw


@dataclass
class Credential:
    """A PS credential as held by its owner."""

    u: PublicKey
    v: PublicKey
    h: int
    s: int
    keyset_id: str = ""

    def to_bytes(self) -> bytes:
        return (
            _keyset_id_to_bytes(self.keyset_id)
            + self.u.format()
            + self.v.format()
            + _scalar_bytes(self.h)
            + _scalar_bytes(self.s)
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Credential":
        if len(raw) != 193:
            raise ValueError("Credential is 193 bytes")
        return cls(
            u=_g1_from_bytes(raw[33:81]),
            v=_g1_from_bytes(raw[81:129]),
            h=_scalar_from_bytes(raw[129:161]),
            s=_scalar_from_bytes(raw[161:]),
            keyset_id=_keyset_id_from_bytes(raw[:33]),
        )


@dataclass
class Presentation:
    """A randomized, publicly verifiable credential presentation."""

    h: int
    u: PublicKey
    v: PublicKey
    u_s: PublicKey
    nullifier: PublicKey  # N = G_NULL^s
    proof: DlogEqProof
    keyset_id: str = ""

    def to_bytes(self) -> bytes:
        return (
            _keyset_id_to_bytes(self.keyset_id)
            + _scalar_bytes(self.h)
            + self.u.format()
            + self.v.format()
            + self.u_s.format()
            + self.nullifier.format()
            + self.proof.to_bytes()
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Presentation":
        if len(raw) != 321:
            raise ValueError("Presentation is 321 bytes")
        return cls(
            keyset_id=_keyset_id_from_bytes(raw[:33]),
            h=_scalar_from_bytes(raw[33:65]),
            u=_g1_from_bytes(raw[65:113]),
            v=_g1_from_bytes(raw[113:161]),
            u_s=_g1_from_bytes(raw[161:209]),
            nullifier=_g1_from_bytes(raw[209:257]),
            proof=DlogEqProof.from_bytes(raw[257:]),
        )


def prove_owner_secret(s: int) -> Tuple[PublicKey, DlogEqProof]:
    """User side of issuance: commit to the owner secret as S = g1^s and
    prove knowledge of it."""
    S = G1 * s
    proof = prove_dlog_eq([G1], [S], s, PS_ISSUE_DST)
    return S, proof


def verify_owner_secret(S: PublicKey, proof: DlogEqProof) -> bool:
    if S.is_infinity():
        return False
    return verify_dlog_eq([G1], [S], proof, PS_ISSUE_DST)


def issue(
    mint_key: MintPrivateKeyPS, h: int, S: PublicKey
) -> Tuple[PublicKey, PublicKey]:
    """Mint side of issuance. The caller must verify knowledge of s and consume a funded quote."""
    if S.is_infinity():
        raise ValueError("owner commitment must not be the point at infinity")
    if not 0 <= h < curve_order:
        raise ValueError("h must be a scalar")
    k = _random_scalar()
    u = G1 * k
    exponent = (mint_key.x.scalar + mint_key.y_h.scalar * h) % curve_order
    v = _add_p1(u * exponent, S * ((k * mint_key.y_s.scalar) % curve_order))
    return u, v


def present(
    cred: Credential, rho: Optional[int] = None, binding: bytes = b""
) -> Presentation:
    """Owner side: randomize the credential and build the presentation.
    The binding is mixed into the proof transcript, tying the presentation
    to one exact purpose (see the module docstring)."""
    if not 0 <= cred.h < curve_order:
        raise ValueError("h must be a scalar")
    if not 0 < cred.s < curve_order:
        raise ValueError("owner secret must be in Fr*")
    rho = _random_scalar() if rho is None else rho
    if not 0 < rho < curve_order:
        raise ValueError("rho must be in Fr*")
    u_r = cred.u * rho
    v_r = cred.v * rho
    u_s = u_r * cred.s
    N = G_NULL * cred.s
    proof = prove_dlog_eq([G_NULL, u_r], [N, u_s], cred.s, PS_PRESENT_DST, binding)
    return Presentation(
        h=cred.h,
        u=u_r,
        v=v_r,
        u_s=u_s,
        nullifier=N,
        proof=proof,
        keyset_id=cred.keyset_id,
    )


def verify_presentation(
    mint_public: MintPublicKeyPS, pres: Presentation, binding: bytes = b""
) -> bool:
    """Public, offline verification of a presentation.

    Checks authenticity (pairing equation against the mint's G2 parameters)
    and ownership (the same s in N and u_s). The caller must supply the
    purpose binding the presentation was created with, ask the mint whether
    pres.nullifier is spent (the only unspent nullifier belongs to the
    current holder) and, for transfers, claim the nullifier atomically with
    the re-issuance.
    """
    if pres.keyset_id != mint_public.keyset_id:
        return False
    for p in (pres.u, pres.v, pres.u_s, pres.nullifier):
        if p.is_infinity():
            return False
    if not 0 <= pres.h < curve_order:
        return False
    if not verify_dlog_eq(
        [G_NULL, pres.u],
        [pres.nullifier, pres.u_s],
        pres.proof,
        PS_PRESENT_DST,
        binding,
    ):
        return False
    return verify_signature(mint_public, pres.u, pres.v, pres.h, pres.u_s)


def verify_signature(
    mint_public: MintPublicKeyPS,
    u: PublicKey,
    v: PublicKey,
    h: int,
    u_s: PublicKey,
) -> bool:
    """Public pairing check; requires neither s nor a Lightning preimage.

    This verifies authenticity only. Spending also requires knowledge of s,
    a purpose-bound proof, and an unspent nullifier claimed atomically.
    """
    if not 0 <= h < curve_order or any(p.is_infinity() for p in (u, v, u_s)):
        return False
    base_h = _add_pk(mint_public.X2, mint_public.Y_h2 * h)
    miller = pyblst.miller_loop(-v.point, G2)
    miller = miller * pyblst.miller_loop(u.point, base_h.point)
    miller = miller * pyblst.miller_loop(u_s.point, mint_public.Y_s2.point)
    return pyblst.final_verify(miller, pyblst.BlstFP12Element())


def verify_credential(mint_public: MintPublicKeyPS, cred: Credential) -> bool:
    """The token holder verifies its credential locally, without a ZK proof."""
    if cred.keyset_id != mint_public.keyset_id or not 0 < cred.s < curve_order:
        return False
    return verify_signature(mint_public, cred.u, cred.v, cred.h, cred.u * cred.s)


def blind_base_for_nullifier(
    mint_key: MintPrivateKeyPS, nullifier: bytes
) -> Tuple[int, PublicKey]:
    """Deterministic fresh base u2 = g1^k2 for one nullifier, so the
    two-round blind transfer is stateless: begin and complete derive the
    same u2 and the user cannot substitute another base."""
    k2 = 0
    counter = 0
    while not 0 < k2 < curve_order:
        k2 = (
            int.from_bytes(
                hmac.new(
                    mint_key.x.private_key,
                    PS_K2_DST + counter.to_bytes(4, "big") + nullifier,
                    hashlib.sha256,
                ).digest(),
                "big",
            )
            % curve_order
        )
        counter += 1
    return k2, G1 * k2


@dataclass
class PrivatePresentation:
    """A randomized credential presentation that commits to h (kappa_h in
    G2) instead of revealing it."""

    u: PublicKey
    v: PublicKey
    kappa_h: PublicKey  # G2: h * Y_h2 + o * g2
    u_s: PublicKey  # s * u
    nullifier: PublicKey  # N = G_NULL^s
    proof_s: DlogEqProof
    keyset_id: str = ""

    def to_bytes(self) -> bytes:
        return (
            _keyset_id_to_bytes(self.keyset_id)
            + self.u.format()
            + self.v.format()
            + self.kappa_h.format()
            + self.u_s.format()
            + self.nullifier.format()
            + self.proof_s.to_bytes()
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PrivatePresentation":
        if len(raw) != 385:
            raise ValueError("PrivatePresentation is 385 bytes")
        return cls(
            keyset_id=_keyset_id_from_bytes(raw[:33]),
            u=_g1_from_bytes(raw[33:81]),
            v=_g1_from_bytes(raw[81:129]),
            kappa_h=_g2_from_bytes(raw[129:225]),
            u_s=_g1_from_bytes(raw[225:273]),
            nullifier=_g1_from_bytes(raw[273:321]),
            proof_s=DlogEqProof.from_bytes(raw[321:]),
        )


def present_private(
    mint_public: MintPublicKeyPS,
    cred: Credential,
    rho: Optional[int] = None,
    binding: bytes = b"",
) -> Tuple[PrivatePresentation, int]:
    """Randomize the credential into a hidden-h presentation. Returns
    (presentation, o): the fresh blinding o is needed again at re-issuance
    time (blind_transfer_commit), so the caller must keep it."""
    if not 0 <= cred.h < curve_order:
        raise ValueError("h must be in Fr for a private presentation")
    if not 0 < cred.s < curve_order:
        raise ValueError("owner secret must be in Fr*")
    rho = _random_scalar() if rho is None else rho
    if not 0 < rho < curve_order:
        raise ValueError("rho must be in Fr*")
    o = _random_scalar()
    u_r = cred.u * rho
    v_rr = _add_p1(cred.v * rho, u_r * o)
    kappa_h = _add_pk(mint_public.Y_h2 * cred.h, G2_PK * o)
    u_s = u_r * cred.s
    N = G_NULL * cred.s
    proof_s = prove_dlog_eq([G_NULL, u_r], [N, u_s], cred.s, PS_PRESENT_DST, binding)
    return (
        PrivatePresentation(
            u=u_r,
            v=v_rr,
            kappa_h=kappa_h,
            u_s=u_s,
            nullifier=N,
            proof_s=proof_s,
            keyset_id=cred.keyset_id,
        ),
        o,
    )


def verify_private_presentation(
    mint_public: MintPublicKeyPS, pres: PrivatePresentation, binding: bytes = b""
) -> bool:
    if pres.keyset_id != mint_public.keyset_id:
        return False
    for p in (
        pres.u,
        pres.v,
        pres.kappa_h,
        pres.u_s,
        pres.nullifier,
    ):
        if p.is_infinity():
            return False
    if not verify_dlog_eq(
        [G_NULL, pres.u],
        [pres.nullifier, pres.u_s],
        pres.proof_s,
        PS_PRESENT_DST,
        binding,
    ):
        return False
    miller = pyblst.miller_loop(-pres.v.point, G2)
    miller = miller * pyblst.miller_loop(pres.u.point, mint_public.X2.point)
    miller = miller * pyblst.miller_loop(pres.u.point, pres.kappa_h.point)
    miller = miller * pyblst.miller_loop(pres.u_s.point, mint_public.Y_s2.point)
    return pyblst.final_verify(miller, pyblst.BlstFP12Element())


def _commit_statements(
    mint_public: MintPublicKeyPS, kappa_h: PublicKey, B: PublicKey, u2: PublicKey
) -> List[LinearStatement]:
    return [
        (kappa_h, [(mint_public.Y_h2, 0), (G2_PK, 1)]),
        (B, [(u2, 0), (G1, 2)]),
    ]


def blind_transfer_commit(
    mint_public: MintPublicKeyPS,
    h: int,
    o: int,
    kappa_h: PublicKey,
    u2: Optional[PublicKey] = None,
    binding: bytes = b"",
) -> Tuple[PublicKey, int, LinearProof]:
    """Owner side: B = h * Y_h1 + t * g1 plus one
    multi-witness proof (pi_eq) that the same h opens both kappa_h and B.
    Returns (B, t, proof); the owner later subtracts t * u from the mint's
    response. Passing u2 explicitly supports the earlier two-round proof."""
    base = mint_public.Y_h1 if u2 is None else u2
    if base.is_infinity():
        raise ValueError("commitment base must not be the point at infinity")
    if not 0 <= h < curve_order:
        raise ValueError("h must be in Fr")
    if not 0 < o < curve_order:
        raise ValueError("o must be in Fr*")
    t = _random_scalar()
    B = _add_p1(base * h, G1 * t)
    proof = prove_linear(
        _commit_statements(mint_public, kappa_h, B, base),
        [h, o, t],
        PS_COMMIT_DST,
        binding,
    )
    return B, t, proof


def verify_blind_transfer(
    mint_public: MintPublicKeyPS,
    pres: PrivatePresentation,
    B: PublicKey,
    proof: LinearProof,
    u2: Optional[PublicKey] = None,
    binding: bytes = b"",
) -> bool:
    base = mint_public.Y_h1 if u2 is None else u2
    if base.is_infinity() or B.is_infinity():
        return False
    if not verify_private_presentation(mint_public, pres, binding=binding):
        return False
    return verify_linear(
        _commit_statements(mint_public, pres.kappa_h, B, base),
        proof,
        PS_COMMIT_DST,
        binding,
    )


def issue_blind(
    mint_key: MintPrivateKeyPS, B: PublicKey, S_new: PublicKey
) -> Tuple[PublicKey, PublicKey]:
    """Return u=k*G1 and v_raw=k*(x*G1+B+y_s*S_new), with fresh secret k.

    B=h*Y_h1+t*G1. Subtracting t*u yields the standard PS credential for
    the original hidden h and the new owner secret behind S_new.
    """
    for point in (B, S_new):
        if point.is_infinity():
            raise ValueError("points must not be the point at infinity")
    k = _random_scalar()
    u = G1 * k
    v_raw = _add_p1(_add_p1(G1 * mint_key.x.scalar, B), S_new * mint_key.y_s.scalar) * k
    return u, v_raw


def issue_blind_legacy(
    mint_key: MintPrivateKeyPS, k2: int, u2: PublicKey, B: PublicKey, S_new: PublicKey
) -> PublicKey:
    """v2_raw = x*u2 + y_h*B + (k2*y_s)*S_new, computed without learning h.
    B commits to h * u2, so y_h * B contributes y_h * h * u2 (the credential
    term) plus t * y_h * g1, which the owner strips with unblind_issued."""
    if not 0 < k2 < curve_order:
        raise ValueError("k2 must be in Fr*")
    for p in (u2, B, S_new):
        if p.is_infinity():
            raise ValueError("points must not be the point at infinity")
    return _add_p1(
        _add_p1(u2 * mint_key.x.scalar, B * mint_key.y_h.scalar),
        S_new * ((k2 * mint_key.y_s.scalar) % curve_order),
    )


def unblind_issued(v2_raw: PublicKey, t: int, base: PublicKey) -> PublicKey:
    """Strip t*base: the returned u for new swaps, Y_h1 for legacy swaps."""
    if not 0 < t < curve_order:
        raise ValueError("t must be in Fr*")
    if base.is_infinity():
        raise ValueError("unblinding base must not be the point at infinity")
    return _add_p1(v2_raw, _neg_p1(base * t))
