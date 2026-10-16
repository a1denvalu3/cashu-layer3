import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import replace

import pytest

from tmp_cash.api import create_app
from tmp_cash.client import ClientError
from tmp_cash.lightning import Payment, PaymentState
from tmp_cash.models import MeltQuoteResponse, preimage_hash


def record_payouts(monkeypatch, backend):
    """Accept the new keyword-only payout arguments and record them."""
    calls = []
    original = backend.pay_invoice

    async def recording(
        request, fee_limit_sat=None, *, idempotency_key=None, cltv_limit=None
    ):
        calls.append(
            {
                "request": request,
                "fee_limit_sat": fee_limit_sat,
                "idempotency_key": idempotency_key,
                "cltv_limit": cltv_limit,
            }
        )
        return await original(request, fee_limit_sat)

    monkeypatch.setattr(backend, "pay_invoice", recording)
    return calls


def pending_payouts(monkeypatch, backend):
    async def pending(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(backend, "pay_invoice", pending)


def test_first_submission_derives_stable_attempt_idempotency_key(service, monkeypatch):
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    calls = record_payouts(monkeypatch, service.backend)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    receipt = service.client.finish_melt(operation)
    assert receipt.amount == 10 and len(calls) == 1
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT attempt FROM melts").fetchone()
    expected = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{operation.quote.quote}:{row['attempt']}:{operation.quote.request}",
        )
    )
    assert calls[0]["idempotency_key"] == expected
    assert calls[0]["fee_limit_sat"] == 1
    # The time-based fake backend gets no block-based CLTV limit.
    assert calls[0]["cltv_limit"] is None

    # A new melt quote derives a fresh key for its own attempt.
    change = service.client.melt_change(operation, receipt)
    invoice = service.backend.receiver_invoice(4, os.urandom(32))
    second = service.client.prepare_melt(change, invoice, fee_reserve=0)
    service.client.finish_melt(second)
    assert len(calls) == 2
    assert calls[1]["idempotency_key"] != calls[0]["idempotency_key"]
    other = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{second.quote.quote}:{row['attempt']}:{second.quote.request}",
        )
    )
    assert calls[1]["idempotency_key"] != other  # attempts differ


def test_cltv_limit_tracks_earliest_backing_cutoff(service, monkeypatch):
    a, _ = service.fund(8)
    b, _ = service.fund(8, os.urandom(32))
    original = service.backend.get_invoice

    async def block_invoice(payment_hash):
        invoice = await original(payment_hash)
        expiry = 232 if payment_hash == a.payment_hash else 250
        return replace(
            invoice,
            htlc_expiry_height=expiry + 18,
            expiry_height=expiry,
            hold_expires_at=None,
        )

    async def height():
        return 200

    monkeypatch.setattr(service.backend, "get_invoice", block_invoice)
    monkeypatch.setattr(service.backend, "get_block_height", height)
    calls = record_payouts(monkeypatch, service.backend)
    from tmp_cash.models import Token

    mixed = Token(mint=a.mint, proofs=a.proofs + b.proofs)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    receipt = service.client.finish_melt(
        service.client.prepare_melt(mixed, invoice, fee_reserve=1)
    )
    assert receipt.amount == 10
    # min(expiry_height) - height - settle_margin_blocks, over all backings.
    assert calls[0]["cltv_limit"] == 232 - 200 - service.mint.settle_margin_blocks


def test_recovery_loop_survives_unexpected_exceptions(service, monkeypatch, caplog):
    calls = 0
    recovered = asyncio.Event()
    original = service.mint.resume_pending_melts

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("malformed provider data with secret detail")
        recovered.set()
        await original()

    async def exercise():
        polled = asyncio.Event()
        real_sleep = asyncio.sleep

        async def sleep(interval):
            polled.set()
            await real_sleep(0.01)

        monkeypatch.setattr("tmp_cash.api.asyncio.sleep", sleep)
        monkeypatch.setattr(service.mint, "resume_pending_melts", flaky)
        app = create_app(service.mint)
        with caplog.at_level(logging.WARNING, logger="tmp_cash.api"):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(recovered.wait(), 5)
        assert calls >= 2

    asyncio.run(exercise())
    # Only the exception type is logged, never its (possibly secret) payload.
    assert "ValueError" in caplog.text
    assert "secret detail" not in caplog.text


def test_resume_skips_malformed_outbox_and_recovers_later_rows(service, monkeypatch):
    a, _ = service.fund(16)
    b, _ = service.fund(16, os.urandom(32))
    pending_payouts(monkeypatch, service.backend)
    operation_a = service.client.prepare_melt(
        a, service.backend.receiver_invoice(16, os.urandom(32)), fee_reserve=0
    )
    payout_preimage = os.urandom(32)
    operation_b = service.client.prepare_melt(
        b, service.backend.receiver_invoice(16, payout_preimage), fee_reserve=0
    )
    for operation in (operation_a, operation_b):
        with pytest.raises(ClientError, match="pending"):
            service.client.finish_melt(operation)
    # Row A is unreadable; row B's payment completed externally.
    with service.mint.db.transaction() as conn:
        conn.execute(
            "UPDATE melts SET request='corrupted' WHERE quote=?",
            (operation_a.quote.quote,),
        )
    with service.backend.db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (
                hashlib.sha256(payout_preimage).hexdigest(),
                PaymentState.succeeded.value,
                payout_preimage,
            ),
        )
    asyncio.run(service.mint.resume_pending_melts())
    with service.mint.db.read() as conn:
        rows = {
            row["quote"]: row["state"]
            for row in conn.execute("SELECT quote,state FROM melts")
        }
    assert rows[operation_a.quote.quote] == "PENDING"
    assert rows[operation_b.quote.quote] == "PAID"


def test_busy_sync_admission_rejects_swap_without_consuming(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token, [8, 8])
    service.mint._sync_work = asyncio.Semaphore(0)
    response = service.client.http.post("/v1/swap", json=operation.request.model_dump())
    assert response.status_code == 503
    service.mint._sync_work = asyncio.Semaphore(4)
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


def test_busy_signing_admission_keeps_paid_outbox_for_recovery(service, monkeypatch):
    token, _ = service.fund(16)
    payout_preimage = os.urandom(32)
    invoice = service.backend.receiver_invoice(16, payout_preimage)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    sends = []
    original = service.backend.pay_invoice

    async def pending_once(request, fee_limit_sat=None, **kwargs):
        sends.append(request)
        if len(sends) == 1:
            return Payment(PaymentState.pending)
        return await original(request, fee_limit_sat)

    monkeypatch.setattr(service.backend, "pay_invoice", pending_once)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (
                hashlib.sha256(payout_preimage).hexdigest(),
                PaymentState.succeeded.value,
                payout_preimage,
            ),
        )
    # Admission is busy when the already-paid payout is finalized.
    service.mint._sync_work = asyncio.Semaphore(0)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT state,melt_lock FROM melts").fetchone()
        assert row[:] == ("PENDING", "")
        assert conn.execute("SELECT COUNT(*) FROM failed_melts").fetchone()[0] == 0
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 16
    # Recovery finalizes the same outbox without a second external send.
    service.mint._sync_work = asyncio.Semaphore(4)
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["PENDING"] * len(token.proofs)
    receipt = service.client.finish_melt(operation)
    assert receipt.amount == 16 and len(sends) == 1


def test_payout_invoice_expiry_capped_at_24_hours(service):
    service.fund(16)
    invoice = service.backend.receiver_invoice(10, os.urandom(32), expiry=25 * 3600)
    response = service.client.http.post(
        "/v1/melt/quote/bolt11", json={"request": invoice}
    )
    assert response.status_code == 400
    assert "24 hours" in response.json()["detail"]
    ok = service.backend.receiver_invoice(10, os.urandom(32), expiry=23 * 3600)
    assert service.client.melt_quote(ok).amount == 10


def test_expired_melt_quotes_pruned_only_without_recovery_records(service):
    now = int(time.time())
    quotes = {}

    def insert(payload_quote, expiry):
        payload = MeltQuoteResponse(
            quote=payload_quote,
            request="lnbcrt10n1pruning",
            payment_hash=preimage_hash(os.urandom(32).hex()),
            amount=1,
            fee_reserve=0,
            expiry=expiry,
        )
        with service.mint.db.transaction() as conn:
            conn.execute(
                "INSERT INTO melt_quotes VALUES (?,?)",
                (payload_quote, payload.model_dump_json()),
            )

    for name in ("bare", "with_melt", "with_failed", "live"):
        quotes[name] = os.urandom(32).hex()
        insert(quotes[name], now + 3600 if name == "live" else now - 10)
    with service.mint.db.transaction() as conn:
        conn.execute(
            "INSERT INTO melts (quote,payout_hash,request,digest,state,attempt) "
            "VALUES (?,?,?,?,'PENDING',?)",
            (quotes["with_melt"], os.urandom(32).hex(), "{}", "d", "a" * 32),
        )
        conn.execute(
            "INSERT INTO failed_melts (quote,digest,expired) VALUES (?,?,0)",
            (quotes["with_failed"], "d"),
        )
    service.mint._prune_melt_quotes()

    def remaining():
        with service.mint.db.read() as conn:
            return {row[0] for row in conn.execute("SELECT quote FROM melt_quotes")}

    assert quotes["bare"] not in remaining()
    assert {"with_melt", "with_failed", "live"} <= {
        name for name, quote in quotes.items() if quote in remaining()
    }
    # The five-minute gate shields freshly expired quotes until the next pass.
    gated = os.urandom(32).hex()
    insert(gated, now - 10)
    service.mint._prune_melt_quotes()
    assert gated in remaining()
    service.mint._last_prune = 0
    service.mint._prune_melt_quotes()
    assert gated not in remaining()


def test_keyed_lock_cancelled_waiter_does_not_leak_references(service):
    async def exercise():
        locks = service.mint._hold_locks
        holder = locks.hold("hash")
        await holder.__aenter__()
        waiter = asyncio.create_task(locks.hold("hash").__aenter__())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # Only the holder's reference remains; the waiter's is dropped.
        assert locks._entries["hash"][1] == 1
        await holder.__aexit__(None, None, None)
        assert locks._entries == {}

    asyncio.run(exercise())
