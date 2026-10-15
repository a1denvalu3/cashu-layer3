import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import PREIMAGE, SEED

from tmp_cash.client import ClientError, fresh_secret, output
from tmp_cash.crypto.bls import PublicKey
from tmp_cash.crypto.ps import G1, present_private, prove_linear
from tmp_cash.db import MintError
from tmp_cash.keyset import common_h_statements, denominations
from tmp_cash.mint import COMMON_H_DST, Mint
from tmp_cash.models import (
    BlindOutput,
    InvoiceState,
    SwapInput,
    SwapRequest,
    Token,
    TokenProof,
    swap_binding,
)


def state(service, token):
    return asyncio.run(service.backend.get_invoice(token.payment_hash)).state


def test_denominations_share_one_keyset_but_have_independent_keys(service):
    token, _ = service.fund(12345)
    assert [p.amount for p in token.proofs] == denominations(12345)
    credentials = service.client.credentials(token)
    assert len({c.keyset_id for c in credentials}) == 1
    keys = service.client.keyset()
    assert len({key.to_bytes() for key in keys.values()}) == len(keys)
    assert len({c.h for c in credentials}) == 1
    assert state(service, token) == InvoiceState.accepted


def test_split_combine_and_private_transfer(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token, [8, 4, 2, 2])
    private = operation.request.model_dump_json()
    assert token.payment_hash not in private
    assert PREIMAGE.hex() not in private
    assert "backing_amount" not in private
    split = service.client.finish_swap(operation)
    assert [p.amount for p in split.proofs] == [8, 4, 2, 2]
    combined = service.client.finish_swap(service.client.prepare_swap(split))
    assert [p.amount for p in combined.proofs] == [16]
    assert state(service, token) == InvoiceState.accepted
    assert (
        service.client.check([service.client.nullifier(p) for p in split.proofs]).states
        == ["SPENT"] * 4
    )


@pytest.mark.parametrize("change", ["amount", "backing_amount", "payment_hash"])
def test_token_metadata_cannot_forge_value_or_backing(service, change):
    token, _ = service.fund(16)
    proof = token.proofs[0]
    updates = (
        {"amount": 8}
        if change == "amount"
        else {"backing_amount": 32}
        if change == "backing_amount"
        else {"payment_hash": "11" * 32, "preimage": None}
    )
    forged = TokenProof.model_validate(proof.model_dump() | updates)
    with pytest.raises(ClientError, match="invalid token"):
        service.client.credential(forged)


@pytest.mark.parametrize("funded", [False, True])
def test_unfunded_and_underfunded_holds_do_not_issue(service, funded):
    quote = service.client.quote(16, hashlib.sha256(PREIMAGE).hexdigest())
    if funded:
        asyncio.run(service.backend.accept(quote.payment_hash))
        with service.backend.db.transaction() as conn:
            conn.execute("UPDATE invoices SET amount_paid=8")
    operation = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    with pytest.raises(ClientError):
        service.client.finish_mint(operation)
    assert not service.client.get_quote(quote.quote).issued


def test_issuance_conserves_funded_amount_and_retries_exactly(service):
    quote = service.client.quote(16, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    operation = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    body = operation.request.model_dump()
    body["outputs"][0]["amount"] = 32
    assert service.client.http.post("/v1/mint/bolt11", json=body).status_code == 400
    token = service.client.finish_mint(operation)
    assert service.client.finish_mint(operation) == token
    conflicting = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    with pytest.raises(ClientError, match="409"):
        service.client.finish_mint(conflicting)


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_mint_instances_issue_one_durable_receipt(
    service, monkeypatch, same_request
):
    quote = service.client.quote(16, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    first = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    second = (
        first
        if same_request
        else service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    )
    other = Mint(service.path / "mint.sqlite3", SEED, service.backend)

    async def race():
        arrived = 0
        barrier = asyncio.Event()
        original = service.mint._live_invoice

        async def live(row):
            nonlocal arrived
            result = await original(row)
            arrived += 1
            if arrived == 2:
                barrier.set()
            await barrier.wait()
            return result

        monkeypatch.setattr(service.mint, "_live_invoice", live)
        monkeypatch.setattr(other, "_live_invoice", live)
        return await asyncio.gather(
            service.mint.mint(first.request),
            other.mint(second.request),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    if same_request:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, MintError) for result in results) == 1
        failed = next(result for result in results if isinstance(result, MintError))
        assert failed.status_code == 409
    winner = first if not isinstance(results[0], MintError) else second
    token = service.client.finish_mint(winner)
    assert token.amount == 16
    assert service.client.finish_mint(winner) == token


def test_swap_rejects_unbalanced_and_duplicate_inputs_without_spending(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token, [8, 8])
    body = operation.request.model_dump()
    body["outputs"].pop()
    assert service.client.http.post("/v1/swap", json=body).status_code == 400
    body = operation.request.model_dump()
    body["inputs"] *= 2
    body["outputs"] *= 2
    assert service.client.http.post("/v1/swap", json=body).status_code == 400
    assert service.client.check([service.client.nullifier(token.proofs[0])]).states == [
        "UNSPENT"
    ]


def test_common_h_proof_rejects_mixed_htlcs_even_with_valid_spending_proofs(service):
    a, _ = service.fund(8)
    b, _ = service.fund(8, os.urandom(32))
    mixed = Token(mint=a.mint, proofs=a.proofs + b.proofs)
    with pytest.raises(ValueError, match="multiple backing"):
        service.client.prepare_swap(mixed)
    keys = service.client.keyset()
    credentials = service.client.credentials(mixed, keys)
    new, _ = output(16)
    t = fresh_secret()
    commitment = PublicKey(
        point=(keys[16].Y_h1 * credentials[0].h).point + (G1 * t).point, group="G1"
    )
    outputs = [BlindOutput(**new.model_dump(), b=commitment.format().hex())]
    binding = swap_binding(
        [(p.amount, service.client.nullifier(p)) for p in mixed.proofs], outputs
    )
    presentations = [
        present_private(keys[p.amount], c, binding=binding)
        for p, c in zip(mixed.proofs, credentials)
    ]
    statements = common_h_statements(
        [(keys[p.amount], pres) for p, (pres, _) in zip(mixed.proofs, presentations)],
        [(keys[16], commitment)],
    )
    proof = prove_linear(
        statements,
        [credentials[0].h] + [o for _, o in presentations] + [t],
        COMMON_H_DST,
        binding,
    )
    request = SwapRequest(
        inputs=[
            SwapInput(amount=p.amount, presentation=pres.to_bytes().hex())
            for p, (pres, _) in zip(mixed.proofs, presentations)
        ],
        outputs=outputs,
        equality_proof=proof.to_bytes().hex(),
    )
    response = service.client.http.post("/v1/swap", json=request.model_dump())
    assert response.status_code == 400
    assert "equality" in response.text
    assert service.client.check(
        [service.client.nullifier(p) for p in mixed.proofs]
    ).states == ["UNSPENT", "UNSPENT"]


def test_swap_outputs_are_bound_and_receipt_survives_expiry(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token, [8, 8])
    body = operation.request.model_dump()
    body["outputs"].reverse()
    assert service.client.http.post("/v1/swap", json=body).status_code == 400
    received = service.client.finish_swap(operation)
    asyncio.run(service.backend.cancel(token.payment_hash))
    assert service.client.finish_swap(operation) == received
    with pytest.raises(ClientError):
        service.client.prepare_swap(received)


def test_concurrent_double_spend_has_exactly_one_winner(service):
    token, _ = service.fund(16)
    operations = [service.client.prepare_swap(token, [8, 8]) for _ in range(2)]

    def spend(operation):
        return service.client.http.post(
            "/v1/swap", json=operation.request.model_dump()
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(spend, operations)) == [200, 409]


def test_partial_melt_returns_fee_change_and_keeps_hold_pending(service):
    token, _ = service.fund(16)
    service.backend.fee_msat = 1501
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=3)
    receipt = service.client.finish_melt(operation)
    assert receipt.amount == 10 and receipt.fee == 2 and receipt.fee_msat == 1501
    change = service.client.melt_change(operation, receipt)
    assert change.amount == 4
    assert change.payment_hash == token.payment_hash
    assert state(service, token) == InvoiceState.accepted
    assert len(service.client.pending_htlcs().htlcs) == 1
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT redeemed,reserved FROM quotes").fetchone()[:] == (
            12,
            0,
        )
    assert service.client.finish_melt(operation) == receipt
    service.backend.fee_msat = 0
    final_invoice = service.backend.receiver_invoice(4, os.urandom(32))
    final = service.client.finish_melt(
        service.client.prepare_melt(change, final_invoice, fee_reserve=0)
    )
    assert final.change == []
    assert state(service, token) == InvoiceState.settled
    assert service.client.pending_htlcs().htlcs == []


def test_routing_fees_count_toward_full_redemption(service):
    token, _ = service.fund(16)
    service.backend.fee_msat = 1000
    invoice = service.backend.receiver_invoice(15, os.urandom(32))
    receipt = service.client.finish_melt(
        service.client.prepare_melt(token, invoice, fee_reserve=1)
    )
    assert receipt.fee == 1 and receipt.change == []
    assert state(service, token) == InvoiceState.settled


def test_combined_melt_allocates_change_to_original_htlc(service):
    a, _ = service.fund(8)
    b, _ = service.fund(8, os.urandom(32))
    mixed = Token(mint=a.mint, proofs=a.proofs + b.proofs)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    operation = service.client.prepare_melt(mixed, invoice, fee_reserve=1)
    receipt = service.client.finish_melt(operation)
    change = service.client.melt_change(operation, receipt)
    assert change.amount == 6 and change.payment_hash == b.payment_hash
    assert state(service, a) == InvoiceState.settled
    assert state(service, b) == InvoiceState.accepted
    assert service.client.check(
        [service.client.nullifier(p) for p in mixed.proofs]
    ).states == ["SPENT", "SPENT"]


def test_melt_binds_invoice_inputs_and_change(service):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    body = operation.request.model_dump()
    body["change"].reverse()
    assert service.client.http.post("/v1/melt/bolt11", json=body).status_code == 400
    other = service.client.melt_quote(
        service.backend.receiver_invoice(10, os.urandom(32)), 1
    )
    body = operation.request.model_dump()
    body["quote"] = other.quote
    assert service.client.http.post("/v1/melt/bolt11", json=body).status_code == 400
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


def test_same_invoice_cannot_pay_twice_with_different_backing(service):
    a, invoice = service.fund(16)
    b, _ = service.fund(16, os.urandom(32))
    first = service.client.prepare_melt(a, invoice, fee_reserve=0)
    second = service.client.prepare_melt(b, invoice, fee_reserve=0)
    service.client.finish_melt(first)
    with pytest.raises(ClientError, match="already paid"):
        service.client.finish_melt(second)
    assert service.client.check(
        [service.client.nullifier(p) for p in b.proofs]
    ).states == ["UNSPENT"]


def test_definite_failed_pending_melt_releases_inputs_without_another_send(
    service, monkeypatch
):
    import asyncio

    from tmp_cash.lightning import Payment, PaymentState

    token, invoice = service.fund(64)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    sends = []

    async def pending(request, fee_limit_sat=None):
        sends.append(request)
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)

    async def failed(payment_hash):
        return Payment(PaymentState.failed)

    monkeypatch.setattr(service.backend, "get_payment", failed)
    with pytest.raises(ClientError, match="failed"):
        service.client.finish_melt(operation)
    assert len(sends) == 1
    assert service.client.check(
        [service.client.nullifier(proof) for proof in token.proofs]
    ).states == ["UNSPENT"]
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 0
        row = conn.execute("SELECT reserved,preimage FROM quotes").fetchone()
    assert row["reserved"] == 0
    assert row["preimage"] == token.proofs[0].preimage
    asyncio.run(service.mint.resume_pending_melts())
    with pytest.raises(ClientError, match="failed"):
        service.client.finish_melt(operation)
    assert len(sends) == 1


def test_melt_fee_reserve_is_enforced_before_payment(service):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    with pytest.raises(ClientError, match="fee reserve"):
        service.client.melt_quote(invoice, 11)
    service.backend.fee_msat = 1001
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    with pytest.raises(ClientError, match="failed"):
        service.client.finish_melt(operation)
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_bad_common_proof_and_owner_proof_do_not_consume_inputs(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token, [8, 8])
    for field in ("equality_proof", "owner"):
        body = operation.request.model_dump()
        if field == "owner":
            body["outputs"][0]["proof"] = "00" * 64
        else:
            raw = body["equality_proof"]
            body["equality_proof"] = raw[:-1] + ("1" if raw[-1] != "1" else "2")
        assert service.client.http.post("/v1/swap", json=body).status_code == 400
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


def test_incomplete_change_commitments_rejected_before_paying(service):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    # The proof binds the full change list; even stripping one commitment fails.
    body = operation.request.model_dump()
    body["change"].pop()
    assert service.client.http.post("/v1/melt/bolt11", json=body).status_code == 400
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_wire_amount_relabeling_cannot_inflate_a_swap(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token)
    body = operation.request.model_dump()
    body["inputs"][0]["amount"] = 32
    body["outputs"][0]["amount"] = 32
    assert service.client.http.post("/v1/swap", json=body).status_code == 400
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


def test_melt_rejects_exhausted_backing_even_for_conflicting_requests(service):
    token, _ = service.fund(16)
    invoices = [service.backend.receiver_invoice(15, os.urandom(32)) for _ in range(2)]
    operations = [
        service.client.prepare_melt(token, request, fee_reserve=1)
        for request in invoices
    ]
    first = service.client.finish_melt(operations[0])
    assert sum(s.amount for s in first.change) == 1
    with pytest.raises(ClientError, match="spent or reserved"):
        service.client.finish_melt(operations[1])
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
