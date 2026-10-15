import asyncio
import os
import time
from dataclasses import replace

import pytest

from tmp_cash.api import create_app
from tmp_cash.client import ClientError
from tmp_cash.db import BackendError
from tmp_cash.models import InvoiceState


@pytest.fixture(params=["time", "blocks"])
def deadline_clock(service, monkeypatch, request):
    """Control the actual deadline inputs without sleeping or mining blocks."""
    now = 1_800_000_000
    height = 200
    monkeypatch.setattr(time, "time", lambda: now)
    if request.param == "blocks":
        original = service.backend.get_invoice

        async def get_invoice(payment_hash):
            return replace(
                await original(payment_hash),
                htlc_expiry_height=250,
                expiry_height=232,
                hold_expires_at=None,
            )

        async def get_height():
            return height

        monkeypatch.setattr(service.backend, "get_invoice", get_invoice)
        monkeypatch.setattr(service.backend, "get_block_height", get_height)

    def advance(offset):
        nonlocal now, height
        if request.param == "blocks":
            height = 232 - service.mint.settle_margin_blocks + offset
        else:
            now = 1_800_000_000 + 3600 - service.mint.settle_margin_seconds + offset

    return advance


def test_snapshot_exposes_deadline_and_backing_but_no_secrets(service):
    token, _ = service.fund(12345)
    response = service.client.http.get("/v1/htlcs/pending")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    entry = service.client.pending_htlcs().htlcs[0]
    assert entry.amount == 12345
    assert entry.melt_deadline == entry.expires_at - service.mint.settle_margin_seconds
    assert entry.payment_hash == token.payment_hash
    for proof in token.proofs:
        assert proof.preimage not in response.text
        assert proof.credential not in response.text
        assert service.client.nullifier(proof) not in response.text
    assert service.client.check_backing(token)[0] == entry


@pytest.mark.parametrize("incomplete", ["open", "underpaid"])
def test_issued_backing_reported_incomplete_fails_closed_instead_of_disappearing(
    service, monkeypatch, incomplete
):
    token, _ = service.fund(16)
    original = service.backend.get_invoice

    async def stale_invoice(payment_hash):
        invoice = await original(payment_hash)
        return (
            replace(invoice, state=InvoiceState.open)
            if incomplete == "open"
            else replace(invoice, amount_paid=invoice.amount - 1)
        )

    monkeypatch.setattr(service.backend, "get_invoice", stale_invoice)
    response = service.client.http.get("/v1/htlcs/pending")
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


@pytest.mark.parametrize("terminal", ["canceled", "expired", "settled"])
def test_terminal_backing_invalidates_remaining_fragments(
    service, terminal, monkeypatch
):
    token, _ = service.fund(16)
    swap = service.client.prepare_swap(token, [8, 8])
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    melt = service.client.prepare_melt(token, invoice, fee_reserve=0)
    if terminal == "canceled":
        asyncio.run(service.backend.cancel(token.payment_hash))
    elif terminal == "expired":
        with service.backend.db.transaction() as conn:
            conn.execute("UPDATE invoices SET expiry=0")
    else:
        asyncio.run(
            service.backend.settle_invoice(bytes.fromhex(token.proofs[0].preimage))
        )
    service.client.credentials(token)
    assert service.client.pending_htlcs().htlcs == []
    with pytest.raises(ClientError, match="pending backing"):
        service.client.prepare_swap(token)
    with pytest.raises(ClientError):
        service.client.prepare_melt(token, invoice, fee_reserve=0)

    # Bypass the wallet's backing check with a request prepared while live.
    response = service.client.http.post(
        "/v1/melt/bolt11", json=melt.request.model_dump()
    )
    assert response.status_code == 409
    assert "closed" in response.json()["detail"]
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]
    assert asyncio.run(service.backend.get_payment(melt.quote.payment_hash)) is None

    # Private swaps hide h from the mint. Even a stale swap cannot renew backing.
    swapped = service.client.finish_swap(swap)
    assert swapped.payment_hash == token.payment_hash
    assert swapped.proofs[0].backing_amount == token.proofs[0].backing_amount
    with pytest.raises(ClientError):
        service.client.prepare_melt(swapped, invoice, fee_reserve=0)

    # Construct a fresh redemption proof without the wallet's liveness guard.
    monkeypatch.setattr(service.client, "check_backing", lambda _: [])
    stale = service.client.prepare_melt(swapped, invoice, fee_reserve=0)
    response = service.client.http.post(
        "/v1/melt/bolt11", json=stale.request.model_dump()
    )
    assert response.status_code == 409
    assert asyncio.run(service.backend.get_payment(stale.quote.payment_hash)) is None


def test_known_preimage_settles_exactly_at_cutoff_and_expires_unspent_tokens(
    service, deadline_clock, monkeypatch
):
    settlements = []
    original = service.backend.settle_invoice

    async def settle(preimage):
        settlements.append((int(time.time()), await service.backend.get_block_height()))
        await original(preimage)

    monkeypatch.setattr(service.backend, "settle_invoice", settle)
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    receipt = service.client.finish_melt(operation)
    change = service.client.melt_change(operation, receipt)
    assert change.amount == 15
    assert settlements == []  # Revealing the preimage alone does not claim the hold.
    late_invoice = service.backend.receiver_invoice(1, os.urandom(32))
    late_melt = service.client.prepare_melt(change, late_invoice, fee_reserve=0)
    entry = service.client.pending_htlcs().htlcs[0]

    deadline_clock(-1)
    asyncio.run(service.mint.reconcile_holds())
    assert settlements == []
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.accepted
    )
    service.client.check_backing(change)

    deadline_clock(0)
    # Cutoff is enforced even before the recovery worker gets its next turn.
    response = service.client.http.post(
        "/v1/melt/bolt11", json=late_melt.request.model_dump()
    )
    assert response.status_code == 409
    assert "deadline" in response.json()["detail"]
    asyncio.run(service.mint.reconcile_holds())
    assert len(settlements) == 1
    timestamp, height = settlements[0]
    assert (height if height is not None else timestamp) == (
        entry.melt_deadline_height if height is not None else entry.melt_deadline
    )
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.settled
    )
    assert service.client.pending_htlcs().htlcs == []
    with pytest.raises(ClientError):
        service.client.prepare_swap(change)
    with pytest.raises(ClientError):
        service.client.prepare_melt(change, late_invoice, fee_reserve=0)
    response = service.client.http.post(
        "/v1/melt/bolt11", json=late_melt.request.model_dump()
    )
    assert response.status_code == 409
    assert "closed" in response.json()["detail"]
    assert (
        asyncio.run(service.backend.get_payment(late_melt.quote.payment_hash)) is None
    )

    deadline_clock(1)
    asyncio.run(service.mint.reconcile_holds())
    assert len(settlements) == 1
    # Recovery of an already-paid receipt is allowed; it cannot pay again.
    assert service.client.finish_melt(operation) == receipt


def test_cutoff_closes_tokens_before_settlement_and_retries_node_failure(
    service, deadline_clock, monkeypatch
):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    change = service.client.melt_change(
        operation, service.client.finish_melt(operation)
    )
    late_invoice = service.backend.receiver_invoice(1, os.urandom(32))
    late_melt = service.client.prepare_melt(change, late_invoice, fee_reserve=0)
    original = service.backend.settle_invoice
    attempts = 0

    async def settle(preimage):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BackendError("node unavailable during settlement")
        await original(preimage)

    monkeypatch.setattr(service.backend, "settle_invoice", settle)
    deadline_clock(0)
    asyncio.run(service.mint.reconcile_holds())
    assert attempts == 1
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.accepted
    )
    assert service.client.pending_htlcs().htlcs == []
    response = service.client.http.post(
        "/v1/melt/bolt11", json=late_melt.request.model_dump()
    )
    assert response.status_code == 409
    assert "closed" in response.json()["detail"]
    assert (
        asyncio.run(service.backend.get_payment(late_melt.quote.payment_hash)) is None
    )

    deadline_clock(1)
    asyncio.run(service.mint.reconcile_holds())
    assert attempts == 2
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.settled
    )
    asyncio.run(service.mint.reconcile_holds())
    assert attempts == 2


def test_without_preimage_mint_cannot_settle_and_hold_times_out(
    service, deadline_clock, monkeypatch
):
    settlements = []

    async def settle(preimage):
        settlements.append(preimage)
        raise AssertionError("mint has no preimage to claim this hold")

    monkeypatch.setattr(service.backend, "settle_invoice", settle)
    token, _ = service.fund(16)
    for offset in (-1, 0, 1):
        deadline_clock(offset)
        asyncio.run(service.mint.reconcile_holds())
    assert settlements == []
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.accepted
    )
    with pytest.raises(ClientError, match="deadline"):
        service.client.prepare_swap(token)
    with service.backend.db.transaction() as conn:
        conn.execute("UPDATE invoices SET expiry=0")
    assert service.client.pending_htlcs().htlcs == []
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.canceled
    )


@pytest.mark.parametrize("poll_offset", [0, 1])
def test_background_recovery_claims_on_first_poll_at_or_after_deadline(
    service, deadline_clock, monkeypatch, poll_offset
):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    service.client.finish_melt(operation)

    async def exercise():
        polled = asyncio.Event()
        next_poll = asyncio.Event()

        async def sleep(interval):
            assert interval == 5
            polled.set()
            await next_poll.wait()
            next_poll.clear()

        monkeypatch.setattr("tmp_cash.api.asyncio.sleep", sleep)
        deadline_clock(-1)
        app = create_app(service.mint)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(polled.wait(), timeout=2)
            assert (
                await service.backend.get_invoice(token.payment_hash)
            ).state == InvoiceState.accepted
            polled.clear()
            deadline_clock(poll_offset)
            next_poll.set()
            await asyncio.wait_for(polled.wait(), timeout=2)
            assert (
                await service.backend.get_invoice(token.payment_hash)
            ).state == InvoiceState.settled

    asyncio.run(exercise())


def test_block_cutoff_uses_lnd_cancellation_height_and_multipart_minimum(
    service, monkeypatch
):
    token, _ = service.fund(16)
    original = service.backend.get_invoice
    height = 200

    async def get_invoice(payment_hash):
        return replace(
            await original(payment_hash),
            htlc_expiry_height=250,
            expiry_height=232,
            hold_expires_at=None,
        )

    async def get_height():
        return height

    monkeypatch.setattr(service.backend, "get_invoice", get_invoice)
    monkeypatch.setattr(service.backend, "get_block_height", get_height)
    entry = service.client.pending_htlcs().htlcs[0]
    assert entry.melt_deadline_height == 226
    assert entry.blocks_remaining == 32 and entry.expires_at is None
    service.client.check_backing(token)
    height = 226
    with pytest.raises(ClientError, match="deadline"):
        service.client.check_backing(token)


def test_incomplete_snapshot_fails_closed(service, monkeypatch):
    service.fund(16)

    async def unavailable(payment_hash):
        raise BackendError("node unavailable")

    monkeypatch.setattr(service.backend, "get_invoice", unavailable)
    response = service.client.http.get("/v1/htlcs/pending")
    assert response.status_code == 503
    assert "htlcs" not in response.json()
