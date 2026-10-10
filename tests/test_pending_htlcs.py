import asyncio
import os
import time
from dataclasses import replace

import pytest

from cashu.client import ClientError
from cashu.db import BackendError
from cashu.models import InvoiceState


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


@pytest.mark.parametrize("terminal", ["canceled", "expired", "settled"])
def test_terminal_backing_invalidates_remaining_fragments(service, terminal):
    token, _ = service.fund(16)
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
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    with pytest.raises(ClientError):
        service.client.prepare_melt(token, invoice, fee_reserve=0)


def test_known_preimage_settles_at_cutoff_and_expires_unspent_tokens(service):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    receipt = service.client.finish_melt(operation)
    change = service.client.melt_change(operation, receipt)
    assert change.amount == 15
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET expiry=?",
            (int(time.time()) + service.mint.settle_margin_seconds,),
        )
    asyncio.run(service.mint.reconcile_holds())
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.settled
    )
    assert service.client.pending_htlcs().htlcs == []
    with pytest.raises(ClientError):
        service.client.prepare_swap(change)


def test_without_preimage_mint_cannot_settle_and_hold_times_out(service):
    token, _ = service.fund(16)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET expiry=?",
            (int(time.time()) + service.mint.settle_margin_seconds,),
        )
    asyncio.run(service.mint.reconcile_holds())
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
