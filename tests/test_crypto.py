from dataclasses import replace

import pytest

from cashu.crypto.bls import PublicKey, curve_order
from cashu.crypto.ps import (
    G1,
    Credential,
    MintPrivateKeyPS,
    PrivatePresentation,
    blind_transfer_commit,
    issue,
    issue_blind,
    present,
    present_private,
    unblind_issued,
    verify_blind_transfer,
    verify_credential,
    verify_presentation,
    verify_private_presentation,
    verify_signature,
)


def credential(key, h, s):
    u, v = issue(key, h, G1 * s)
    return Credential(u, v, h, s, key.public_key.keyset_id)


@pytest.mark.parametrize("h", [0, 1, curve_order - 1])
def test_issuance_formula_and_rerandomization(h):
    key = MintPrivateKeyPS.from_seed(b"crypto test seed 0123456789")
    cred = credential(key, h, 1234567)
    expected = (
        key.x.scalar + key.y_h.scalar * h + key.y_s.scalar * cred.s
    ) % curve_order
    assert cred.v == cred.u * expected
    assert cred.u != G1
    assert verify_credential(key.public_key, cred)
    first = present(cred, binding=b"spend A")
    second = present(cred, binding=b"spend A")
    assert first.u != second.u
    assert first.nullifier == second.nullifier
    assert verify_presentation(key.public_key, first, b"spend A")
    assert not verify_presentation(key.public_key, first, b"spend B")
    private, _ = present_private(key.public_key, cred, binding=b"swap A")
    assert verify_private_presentation(key.public_key, private, b"swap A")
    assert not verify_private_presentation(key.public_key, private, b"swap B")


@pytest.mark.parametrize("h", [0, 1, curve_order - 1])
def test_one_round_blind_issuance_returns_u_with_the_signature(h):
    key = MintPrivateKeyPS.from_seed(b"single round test seed...........")
    old = credential(key, h, 1234)
    new_secret = 5678
    C = G1 * new_secret
    binding = C.format()
    pres, o = present_private(key.public_key, old, binding=binding)
    B, t, proof = blind_transfer_commit(
        key.public_key, h, o, pres.kappa_h, binding=binding
    )
    assert verify_blind_transfer(key.public_key, pres, B, proof, binding=binding)
    u, v_raw = issue_blind(key, B, C)
    v = unblind_issued(v_raw, t, u)
    expected = (
        key.x.scalar + key.y_h.scalar * h + key.y_s.scalar * new_secret
    ) % curve_order
    assert v == u * expected
    assert verify_credential(
        key.public_key, Credential(u, v, h, new_secret, key.public_key.keyset_id)
    )
    second_u, _ = issue_blind(key, B, C)
    assert second_u != u


def test_fixed_base_linear_forgery_is_not_available_with_fresh_k():
    key = MintPrivateKeyPS.from_seed(b"forgery test seed 0123456789")
    public = key.public_key
    h1, h2, s1, s2, target_h, target_s = 11, 22, 33, 44, 55, 66

    def add(a, b):
        return PublicKey(point=a.point + b.point, group="G1")

    def subtract(a, b):
        return PublicKey(point=a.point + (-b.point), group="G1")

    def forge(v1, v2):
        # Fixed-base signing exposes y_s*G1 from two chosen owner secrets,
        # because the public Y_h1 removes the h terms.
        a1 = subtract(v1, public.Y_h1 * h1)
        a2 = subtract(v2, public.Y_h1 * h2)
        y_s_g1 = subtract(a2, a1) * pow(s2 - s1, -1, curve_order)
        x_g1 = subtract(a1, y_s_g1 * s1)
        return Credential(
            G1,
            add(add(x_g1, public.Y_h1 * target_h), y_s_g1 * target_s),
            target_h,
            target_s,
            public.keyset_id,
        )

    fixed_v1 = G1 * (
        (key.x.scalar + key.y_h.scalar * h1 + key.y_s.scalar * s1) % curve_order
    )
    fixed_v2 = G1 * (
        (key.x.scalar + key.y_h.scalar * h2 + key.y_s.scalar * s2) % curve_order
    )
    assert verify_credential(public, forge(fixed_v1, fixed_v2))
    fresh1 = credential(key, h1, s1)
    fresh2 = credential(key, h2, s2)
    assert not verify_credential(public, forge(fresh1.v, fresh2.v))


def test_authenticity_is_separate_from_spending_knowledge():
    key = MintPrivateKeyPS.from_seed(b"pairing test seed 0123456789")
    cred = credential(key, 123, 456)
    pres = present(cred, binding=b"redeem")
    # A verifier sees only these points and h; neither secret is an input.
    assert verify_signature(key.public_key, pres.u, pres.v, pres.h, pres.u_s)
    broken = replace(
        pres,
        proof=replace(pres.proof, response=(pres.proof.response + 1) % curve_order),
    )
    assert verify_signature(key.public_key, broken.u, broken.v, broken.h, broken.u_s)
    assert not verify_presentation(key.public_key, broken, b"redeem")
    assert not verify_credential(key.public_key, replace(cred, s=457))
    assert not verify_credential(key.public_key, replace(cred, h=124))


def test_private_presentations_roundtrip_and_hide_attribute():
    key = MintPrivateKeyPS.from_seed(b"private test seed 0123456789")
    cred = credential(key, 123, 456)
    first, _ = present_private(key.public_key, cred)
    second, _ = present_private(key.public_key, cred)
    assert first.kappa_h != second.kappa_h
    restored = PrivatePresentation.from_bytes(first.to_bytes())
    assert verify_private_presentation(key.public_key, restored)
    assert Credential.from_bytes(cred.to_bytes()) == cred
    with pytest.raises(ValueError):
        PrivatePresentation.from_bytes(first.to_bytes()[:-1])
    with pytest.raises(ValueError):
        present(cred, rho=0)
