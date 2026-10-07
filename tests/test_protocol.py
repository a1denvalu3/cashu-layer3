import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from conftest import PREIMAGE

from cashu.client import ClientError, SwapOperation
from cashu.crypto.bls import PublicKey, curve_order
from cashu.crypto.ps import (
    Credential,
    LinearProof,
    PrivatePresentation,
    blind_base_for_nullifier,
    blind_transfer_commit,
    issue_blind_legacy,
    present_private,
    prove_owner_secret,
)
from cashu.db import MintError
from cashu.models import (
    InvoiceState,
    SignatureResponse,
    SwapRequest,
    Token,
    htlc_claim_scalar,
    swap_binding,
)
from cashu.wallet import Wallet


def test_accepted_hold_mint_two_private_handoffs_and_redemption(service):
    token, payout = service.fund()
    payment_hash = token.payment_hash
    assert (
        asyncio.run(service.backend.get_invoice(payment_hash)).state
        == InvoiceState.accepted
    )
    original_nullifier = service.client.nullifier(token)
    for _ in range(2):
        operation = service.client.prepare_swap(token)
        # Only blinded group elements go to the mint during a swap.
        assert "payment_hash" not in operation.request.model_dump()
        assert "amount" not in operation.request.model_dump()
        assert "preimage" not in operation.request.model_dump()
        assert "h" not in operation.request.model_dump()
        old_nullifier = service.client.nullifier(token)
        token = service.client.finish_swap(operation)
        assert token.payment_hash == payment_hash
        assert token.amount == 1000
        assert token.preimage == PREIMAGE.hex()
        assert service.client.check([old_nullifier]).states == ["SPENT"]
        assert (
            asyncio.run(service.backend.get_invoice(payment_hash)).state
            == InvoiceState.accepted
        )
    burn = service.client.prepare_burn(token, payout)
    receipt = service.client.finish_burn(burn)
    assert receipt.preimage == PREIMAGE.hex()
    assert receipt.payout_hash != payment_hash
    assert (
        hashlib.sha256(bytes.fromhex(receipt.payout_preimage)).hexdigest()
        == receipt.payout_hash
    )
    assert service.client.finish_burn(burn) == receipt
    assert service.client.check(
        [original_nullifier, service.client.nullifier(token)]
    ).states == ["SPENT", "SPENT"]
    assert (
        asyncio.run(service.backend.get_invoice(payment_hash)).state
        == InvoiceState.settled
    )
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_unfunded_quotes_never_issue(service):
    h = hashlib.sha256(PREIMAGE).hexdigest()
    quote = service.client.quote(1000, h)
    operation = service.client.prepare_mint(quote.quote)
    with pytest.raises(ClientError, match="fully accepted"):
        service.client.finish_mint(operation)
    asyncio.run(service.backend.accept(h))
    token = service.client.finish_mint(operation)
    assert token.amount == 1000


def test_partially_funded_hold_never_issues_a_partial_token(service):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    quote = service.client.quote(1000, payment_hash)
    operation = service.client.prepare_mint(quote.quote)
    asyncio.run(service.backend.accept(payment_hash))
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET amount_paid=1 WHERE payment_hash=?", (payment_hash,)
        )
    with pytest.raises(ClientError, match="fully accepted"):
        service.client.finish_mint(operation)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET amount_paid=amount WHERE payment_hash=?",
            (payment_hash,),
        )
    assert service.client.finish_mint(operation).amount == 1000


def test_bad_owner_proof_does_not_consume_the_quote(service):
    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    operation = service.client.prepare_mint(quote.quote)
    altered = operation.model_copy(
        update={"request": operation.request.model_copy(update={"proof": "00" * 64})}
    )
    with pytest.raises(ClientError, match="commitment proof"):
        service.client.finish_mint(altered)
    assert service.client.finish_mint(operation).amount == 1000


def test_mint_receipt_is_exact_and_quote_cannot_issue_twice(service):
    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    operation = service.client.prepare_mint(quote.quote)
    token = service.client.finish_mint(operation)
    assert service.client.finish_mint(operation) == token
    with pytest.raises(ClientError, match="already issued"):
        service.client.finish_mint(service.client.prepare_mint(quote.quote))


def test_swap_receipt_is_exact_and_conflicting_spend_is_rejected(service):
    token, _ = service.fund()
    first = service.client.prepare_swap(token)
    second = service.client.prepare_swap(token)
    new = service.client.finish_swap(first)
    assert service.client.finish_swap(first) == new
    with pytest.raises(ClientError, match="already spent"):
        service.client.finish_swap(second)


def test_private_swap_uses_one_spending_request(service, monkeypatch):
    token, _ = service.fund()
    post = service.client._post
    paths = []

    def record(path, request):
        paths.append(path)
        return post(path, request)

    monkeypatch.setattr(service.client, "_post", record)
    operation = service.client.prepare_swap(token)
    assert paths == []
    assert operation.u is None
    assert operation.request.version == 2
    new = service.client.finish_swap(operation)
    assert paths == ["/v1/swap"]
    assert new.amount == token.amount and new.payment_hash == token.payment_hash
    assert (
        service.client.http.get("/openapi.json").json()["paths"].get("/v1/swap/begin")
        is None
    )


def test_changing_swap_protocol_version_cannot_change_the_claim(service):
    token, _ = service.fund()
    operation = service.client.prepare_swap(token)
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(operation.request.model_copy(update={"version": 1}))
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]
    assert service.client.finish_swap(operation).amount == token.amount


@pytest.mark.parametrize("response_lost", [False, True])
def test_saved_legacy_swap_and_receipt_remain_recoverable(service, response_lost):
    token, _ = service.fund()
    public = service.client.keyset()
    cred = service.client.credential(token)
    secret = 1234567
    C, owner_proof = prove_owner_secret(secret)
    binding = swap_binding(C.format())
    pres, o = present_private(public, cred, binding=binding)
    k, u = blind_base_for_nullifier(service.mint.signing_key, pres.nullifier.format())
    B, t, equality = blind_transfer_commit(public, cred.h, o, pres.kappa_h, u, binding)
    request = SwapRequest(
        presentation=pres.to_bytes().hex(),
        commitment=C.format().hex(),
        owner_proof=owner_proof.to_bytes().hex(),
        b=B.format().hex(),
        equality_proof=equality.to_bytes().hex(),
    )
    operation = SwapOperation(
        id=os.urandom(32).hex(),
        token=token,
        secret=f"{secret:064x}",
        blinding=f"{t:064x}",
        u=u.format().hex(),
        request=request,
    )
    old_payload = operation.model_dump()
    del old_payload["request"]["version"]
    wallet = Wallet(service.path / "legacy-wallet.sqlite3")
    wallet.save(operation)
    with wallet.db.transaction() as conn:
        conn.execute(
            "UPDATE operations SET payload=? WHERE id=?",
            (json.dumps(old_payload), operation.id),
        )
    if response_lost:
        # Reproduce the receipt format and digest stored by the previous mint.
        v = issue_blind_legacy(service.mint.signing_key, k, u, B, C)
        response = SignatureResponse(
            keyset_id=public.keyset_id, u=u.format().hex(), v=v.format().hex()
        )
        digest = hashlib.sha256(
            request.model_dump_json(exclude={"version"}).encode()
        ).hexdigest()
        with service.mint.db.transaction() as conn:
            conn.execute(
                "INSERT INTO nullifiers VALUES (?,'SPENT','swap',?,?)",
                (pres.nullifier.format().hex(), digest, response.model_dump_json()),
            )
    recovered = wallet.retry(service.client, operation.id, "swap")
    assert isinstance(recovered, Token)
    assert service.client.credential(recovered).s == secret
    assert (
        recovered.amount == token.amount
        and recovered.payment_hash == token.payment_hash
    )
    assert wallet.pending() == []
    modern = service.client.finish_swap(service.client.prepare_swap(recovered))
    assert modern.amount == token.amount and modern.payment_hash == token.payment_hash


def test_concurrent_double_spend_has_one_winner(service):
    token, _ = service.fund()
    requests = [service.client.prepare_swap(token).request for _ in range(2)]

    def spend(request):
        try:
            return service.mint.swap(request)
        except MintError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(spend, requests))
    assert sum(r == "token is already spent or reserved" for r in results) == 1


@pytest.mark.parametrize("change", ["payment_hash", "amount"])
def test_swap_cannot_change_either_part_of_the_hidden_htlc_claim(service, change):
    token, _ = service.fund()
    cred = service.client.credential(token)
    public = service.client.keyset()
    operation = service.client.prepare_swap(token)
    C = PublicKey(compressed=bytes.fromhex(operation.request.commitment), group="G1")
    binding = swap_binding(C.format())
    pres, o = present_private(public, cred, binding=binding)
    payment_hash = "11" * 32 if change == "payment_hash" else token.payment_hash
    amount = token.amount + 1 if change == "amount" else token.amount
    changed_h = htlc_claim_scalar(payment_hash, amount)
    # An attacker commits to a different HTLC claim and generates a fresh proof.
    # The same witness then fails to open the old kappa_h commitment.
    B, _, proof = blind_transfer_commit(
        public, changed_h, o, pres.kappa_h, binding=binding
    )
    request = operation.request.model_copy(
        update={
            "presentation": pres.to_bytes().hex(),
            "b": B.format().hex(),
            "equality_proof": proof.to_bytes().hex(),
        }
    )
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(request)
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]
    assert service.client.finish_swap(operation).payment_hash == token.payment_hash


def test_swap_owner_substitution_and_invalid_equality_proof_fail(service):
    token, _ = service.fund()
    operation = service.client.prepare_swap(token)
    C, proof = prove_owner_secret(12345)
    request = operation.request.model_copy(
        update={
            "commitment": C.format().hex(),
            "owner_proof": proof.to_bytes().hex(),
        }
    )
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(request)
    wrong_proof = LinearProof(challenge=0, responses=[0, 0, 0])
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(
            operation.request.model_copy(
                update={"equality_proof": wrong_proof.to_bytes().hex()}
            )
        )


@pytest.mark.parametrize("change", ["payment_hash", "amount"])
def test_htlc_claim_is_signed_under_one_common_mint_key(service, change):
    token, _ = service.fund()
    update = (
        {"amount": token.amount + 1}
        if change == "amount"
        else {"payment_hash": "11" * 32}
    )
    altered = token.model_copy(update=update)
    with pytest.raises(ClientError, match="HTLC hash or amount"):
        service.client.credential(altered)
    # Also rewrite the serialized h to match the changed public claim.
    # Verification must then reject the actual signature under the same key.
    cred = service.client.credential(token)
    changed = replace(cred, h=htlc_claim_scalar(altered.payment_hash, altered.amount))
    with pytest.raises(ClientError, match="credential or amount"):
        service.client.credential(
            altered.model_copy(update={"credential": changed.to_bytes().hex()})
        )


def test_one_sat_hold_cannot_be_advertised_as_one_million_sats(service):
    from cashu.crypto.ps import verify_signature

    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    quote = service.client.quote(1, payment_hash)
    operation = service.client.prepare_mint(quote.quote)
    asyncio.run(service.backend.accept(payment_hash))
    inflated_h = htlc_claim_scalar(payment_hash, 1_000_000)
    body = operation.request.model_dump()
    # The client cannot select either the claim scalar or the issuance amount.
    for extra in ({"h": f"{inflated_h:064x}"}, {"amount": 1_000_000}):
        assert (
            service.client.http.post("/v1/mint/bolt11", json=body | extra).status_code
            == 422
        )
    token = service.client.finish_mint(operation)
    cred = service.client.credential(token)
    assert cred.h == htlc_claim_scalar(payment_hash, 1)
    assert not verify_signature(
        service.client.keyset(), cred.u, cred.v, inflated_h, cred.u * cred.s
    )
    with pytest.raises(ClientError, match="HTLC hash or amount"):
        service.client.credential(token.model_copy(update={"amount": 1_000_000}))
    forged = token.model_copy(
        update={
            "amount": 1_000_000,
            "credential": replace(cred, h=inflated_h).to_bytes().hex(),
        }
    )
    with pytest.raises(ClientError, match="credential or amount"):
        service.client.credential(forged)


def test_arbitrary_whole_invoice_amounts_share_one_keyset(service):
    first, _ = service.fund(1000)
    other_preimage = hashlib.sha256(b"another whole HTLC").digest()
    second, _ = service.fund(12345, other_preimage)
    first_cred = service.client.credential(first)
    second_cred = service.client.credential(second)
    assert (
        first_cred.keyset_id
        == second_cred.keyset_id
        == service.client.keyset().keyset_id
    )
    assert first_cred.h != second_cred.h
    keys = service.client.http.get("/v1/keys").json()
    assert "amount" not in keys
    assert service.client.http.get("/v1/keys/1000").status_code == 404
    swapped = service.client.finish_swap(service.client.prepare_swap(second))
    assert swapped.amount == 12345
    assert swapped.payment_hash == second.payment_hash


def test_swap_has_exactly_one_input_and_one_output(service):
    token, _ = service.fund()
    operation = service.client.prepare_swap(token)
    body = operation.request.model_dump()
    # A caller cannot introduce amounts, splitting, or combining in the wire API.
    for extra in ({"amount": 1}, {"inputs": [body, body]}, {"outputs": [1, 999]}):
        assert (
            service.client.http.post("/v1/swap", json=body | extra).status_code == 422
        )
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]


def test_redemption_binds_backing_preimage_payout_amount_and_invoice(service):
    token, payout = service.fund()
    burn = service.client.prepare_burn(token, payout)
    other_invoice = service.backend.receiver_invoice(
        token.amount, b"different receiver preimage......"[:32]
    )
    wrong_amount = service.backend.receiver_invoice(token.amount + 1, PREIMAGE)
    with pytest.raises(ClientError, match="preimage or payout amount"):
        service.client.finish_burn(
            burn.model_copy(
                update={
                    "request": burn.request.model_copy(update={"request": wrong_amount})
                }
            )
        )
    with pytest.raises(ClientError, match="burn proof"):
        service.client.finish_burn(
            burn.model_copy(
                update={
                    "request": burn.request.model_copy(
                        update={"request": other_invoice}
                    )
                }
            )
        )
    with pytest.raises(ClientError, match="preimage or payout amount"):
        service.client.finish_burn(
            burn.model_copy(
                update={
                    "request": burn.request.model_copy(update={"preimage": "11" * 32})
                }
            )
        )
    assert service.client.finish_burn(burn).preimage == PREIMAGE.hex()


def test_payout_invoice_cannot_redeem_two_backing_claims(service):
    first, payout = service.fund()
    second, _ = service.fund(preimage=hashlib.sha256(b"second backing claim").digest())
    operation = service.client.prepare_burn(first, payout)
    receipt = service.client.finish_burn(operation)
    with pytest.raises(ClientError, match="payout invoice already used"):
        service.client.finish_burn(service.client.prepare_burn(second, payout))
    assert service.client.check([service.client.nullifier(second)]).states == [
        "UNSPENT"
    ]
    assert service.client.finish_burn(operation) == receipt
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_concurrent_claims_cannot_pay_the_same_invoice_twice(service):
    first, payout = service.fund()
    second, _ = service.fund(
        preimage=hashlib.sha256(b"concurrent backing claim").digest()
    )
    requests = [
        service.client.prepare_burn(token, payout).request for token in (first, second)
    ]

    def redeem(request):
        try:
            return asyncio.run(service.mint.burn(request))
        except MintError as exc:
            assert exc.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, requests))
    assert sum(result is not None for result in results) == 1
    assert sorted(
        service.client.check(
            [service.client.nullifier(first), service.client.nullifier(second)]
        ).states
    ) == ["SPENT", "UNSPENT"]
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["payout_hash", "payout_preimage"])
def test_client_verifies_the_independent_payout_receipt(service, monkeypatch, field):
    token, payout = service.fund()
    operation = service.client.prepare_burn(token, payout)
    receipt = service.client.finish_burn(operation)
    altered = receipt.model_copy(update={field: "11" * 32}).model_dump_json()
    monkeypatch.setattr(service.client, "_post", lambda path, request: altered)
    with pytest.raises(ClientError, match="invalid payout receipt"):
        service.client.finish_burn(operation)


def test_canceled_htlc_cannot_issue_or_redeem(service):
    token, payout = service.fund()
    asyncio.run(service.backend.cancel(token.payment_hash))
    with pytest.raises(ClientError, match="canceled"):
        service.client.finish_burn(service.client.prepare_burn(token, payout))
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]


def test_quote_retries_and_full_hash_binding_despite_old_reduction_aliases(service):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    quote = service.client.quote(1000, payment_hash)
    assert service.client.quote(1000, payment_hash).quote == quote.quote
    with pytest.raises(ClientError, match="different amount"):
        service.client.quote(1001, payment_hash)
    original = int(payment_hash, 16)
    alias = (
        original + curve_order
        if original + curve_order < 2**256
        else original - curve_order
    )
    alias_hash = f"{alias:064x}"
    assert alias % curve_order == original % curve_order
    assert htlc_claim_scalar(alias_hash, 1000) != htlc_claim_scalar(payment_hash, 1000)
    other = service.client.quote(1000, alias_hash)
    assert other.quote != quote.quote


@pytest.mark.parametrize(
    "body",
    [
        {"amount": True, "payment_hash": "11" * 32},
        {"amount": -1, "payment_hash": "11" * 32},
        {"amount": 1.2, "payment_hash": "11" * 32},
        {"amount": 1, "payment_hash": "AA" * 32},
        {"amount": 1, "payment_hash": "11" * 31},
        {"amount": 1, "payment_hash": "11" * 32, "preimage": "22" * 32},
    ],
)
def test_quote_boundary_rejects_invalid_fields(service, body):
    assert (
        service.client.http.post("/v1/mint/quote/bolt11", json=body).status_code == 422
    )


def test_infinity_encoding_and_unknown_keyset_are_rejected(service):
    token, _ = service.fund()
    operation = service.client.prepare_swap(token)
    from cashu.crypto.ps import G_NULL

    infinity = (G_NULL * 0).format().hex()
    request = operation.request.model_copy(update={"commitment": infinity})
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(request)
    pres = PrivatePresentation.from_bytes(bytes.fromhex(operation.request.presentation))
    altered = replace(pres, keyset_id="03" + "00" * 32)
    with pytest.raises(MintError, match="private swap proof"):
        service.mint.swap(
            operation.request.model_copy(
                update={"presentation": altered.to_bytes().hex()}
            )
        )


def test_pairing_verification_needs_no_preimage_and_api_has_no_show(service):
    token, _ = service.fund()
    from cashu.crypto.ps import verify_signature

    cred = Credential.from_bytes(bytes.fromhex(token.credential))
    assert verify_signature(
        service.client.keyset(), cred.u, cred.v, cred.h, cred.u * cred.s
    )
    assert service.client.http.get("/v1/show").status_code == 404


def test_previous_token_version_is_rejected(service):
    token, _ = service.fund()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Token.model_validate(token.model_dump() | {"version": 1})
