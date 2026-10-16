import asyncio
import hashlib
import os
import sqlite3
import time

import pytest
from conftest import PREIMAGE, SEED
from fastapi.testclient import TestClient

from tmp_cash.api import create_app
from tmp_cash.client import Client, ClientError
from tmp_cash.db import BackendError, MintError
from tmp_cash.lightning import FakeBackend, Payment, PaymentState
from tmp_cash.mint import Mint
from tmp_cash.models import InvoiceState, preimage_hash
from tmp_cash.wallet import Wallet


def fund(client, backend, amount=16):
    quote = client.quote(amount, preimage_hash(PREIMAGE.hex()))
    asyncio.run(backend.accept(quote.payment_hash))
    return client.finish_mint(client.prepare_mint(quote.quote, PREIMAGE.hex()))


class LosePayoutResponse(FakeBackend):
    calls = 0

    async def pay_invoice(self, request, fee_limit_sat=None, **kwargs):
        self.calls += 1
        await super().pay_invoice(request, fee_limit_sat)
        raise BackendError("response lost after Lightning paid")


class FailSettlement(FakeBackend):
    async def settle_invoice(self, preimage):
        raise BackendError("node unavailable during settlement")


class PendingPayout(FakeBackend):
    calls = 0

    async def pay_invoice(self, request, fee_limit_sat=None, **kwargs):
        self.calls += 1
        from tmp_cash.lightning import invoice_details

        payment_hash, _ = invoice_details(request)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO payments VALUES (?,?,NULL,0)",
                (payment_hash, PaymentState.pending.value),
            )
        return Payment(PaymentState.pending)


def test_saved_mint_and_multi_input_swap_survive_restart(service):
    quote = service.client.quote(123, preimage_hash(PREIMAGE.hex()))
    asyncio.run(service.backend.accept(quote.payment_hash))
    wallet = Wallet(service.path / "wallet.sqlite3")
    operation = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    wallet.save(operation)
    original = service.client.finish_mint(operation)  # HTTP reply lost to wallet.
    reopened = Wallet(wallet.db.path)
    recovered = reopened.retry(service.client, operation.id)
    assert recovered == original and recovered.amount == 123
    swap = service.client.prepare_swap(recovered, [64, 32, 16, 8, 2, 1])
    reopened.save(swap)
    first = service.client.finish_swap(swap)
    mint = Mint(service.mint.db.path, SEED, service.backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        again = Wallet(wallet.db.path).retry(client, swap.id)
        assert again == first
    assert reopened.pending() == []
    assert sum(r["amount"] for r in reopened.tokens() if r["state"] == "LIVE") == 123


def test_paid_melt_recovers_actual_fee_and_change_without_repaying(tmp_path):
    backend = LosePayoutResponse(tmp_path / "fake.sqlite3", fee_msat=1501)
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        token = fund(client, backend)
        invoice = backend.receiver_invoice(10, os.urandom(32))
        operation = client.prepare_melt(token, invoice, fee_reserve=3)
        wallet.save(operation)
        with pytest.raises(ClientError, match="503"):
            wallet.retry(client, operation.id)
        assert client.check([client.nullifier(p) for p in token.proofs]).states == [
            "PENDING"
        ]
    reopened = Mint(mint.db.path, SEED, backend)
    # A real restart clears orphaned executor locks on first acquisition;
    # same-process reopens deliberately do not, so simulate the restart.
    with reopened.db.transaction() as conn:
        conn.execute("UPDATE melts SET melt_lock='' WHERE melt_lock<>''")
    asyncio.run(reopened.resume_pending_melts())
    with TestClient(create_app(reopened, recovery=False)) as http:
        receipt = Wallet(wallet.db.path).retry(
            Client("http://testserver", http), operation.id
        )
    assert receipt.fee == 2
    assert sum(r["amount"] for r in wallet.tokens() if r["state"] == "LIVE") == 4
    assert backend.calls == 1


def test_settlement_failure_does_not_lose_paid_receipt_or_repay(tmp_path):
    backend = FailSettlement(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        token = fund(client, backend)
        invoice = backend.receiver_invoice(16, os.urandom(32))
        operation = client.prepare_melt(token, invoice, fee_reserve=0)
        receipt = client.finish_melt(operation)
        assert receipt.state == "PAID"
        assert client.finish_melt(operation) == receipt
        assert client.check_htlcs([token.payment_hash]).htlcs == []
    normal = FakeBackend(backend.db.path)
    reopened = Mint(mint.db.path, SEED, normal)
    asyncio.run(reopened.resume_pending_melts())
    assert (
        asyncio.run(normal.get_invoice(token.payment_hash)).state
        == InvoiceState.settled
    )
    with backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_pending_melt_cannot_be_abandoned_and_late_change_expires(tmp_path):
    backend = PendingPayout(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        token = fund(client, backend)
        payout_preimage = os.urandom(32)
        invoice = backend.receiver_invoice(10, payout_preimage)
        operation = client.prepare_melt(token, invoice, fee_reserve=1)
        wallet.save(operation)
        with pytest.raises(ClientError, match="pending"):
            wallet.retry(client, operation.id)
        with pytest.raises(ValueError, match="reserved"):
            wallet.abandon(client, operation.id)
        with backend.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET expiry=?",
                (int(time.time()) + mint.settle_margin_seconds,),
            )
        asyncio.run(mint.reconcile_holds())
        assert (
            asyncio.run(backend.get_invoice(token.payment_hash)).state
            == InvoiceState.settled
        )
        with backend.db.transaction() as conn:
            conn.execute(
                "UPDATE payments SET state='SUCCEEDED',preimage=?", (payout_preimage,)
            )
        receipt = wallet.retry(client, operation.id)
        assert receipt.expired_change == 6 and receipt.change == []
        assert backend.calls == 1
        assert not any(r["state"] == "LIVE" for r in wallet.tokens())


def test_definite_failure_releases_all_inputs_but_retains_known_preimage(service):
    token, _ = service.fund(16)
    # Well-formed invoice unregistered at the fake receiver causes definite failure.
    invoice = FakeBackend._invoice(10, "11" * 32, 3600, "unreachable")
    operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    wallet = Wallet(service.path / "wallet.sqlite3")
    wallet.save(operation)
    with pytest.raises(ClientError, match="failed"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == []
    assert all(r["state"] == "LIVE" for r in wallet.tokens())
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT preimage,redeemed,reserved FROM quotes").fetchone()
        assert row[:] == (PREIMAGE.hex(), 0, 0)
        assert conn.execute("SELECT expired FROM failed_melts").fetchone()[0] == 0


def test_v2_database_and_wrong_seed_cannot_be_reinterpreted(service, tmp_path):
    with pytest.raises(ValueError, match="different seed"):
        Mint(service.mint.db.path, os.urandom(32), service.backend)
    old = tmp_path / "old.sqlite3"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO metadata VALUES ('protocol','ps-htlc-claim-v2')")
    with pytest.raises(ValueError, match="previous protocol"):
        Mint(old, SEED, service.backend)


def test_corrupted_recovery_secret_counts_are_rejected_before_spending(service):
    from tmp_cash.client import MeltOperation, MintOperation, SwapOperation

    quote = service.client.quote(16, preimage_hash(PREIMAGE.hex()))
    asyncio.run(service.backend.accept(quote.payment_hash))
    mint_operation = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    token = service.client.finish_mint(mint_operation)
    swap_operation = service.client.prepare_swap(token, [8, 8])
    melt_operation = service.client.prepare_melt(
        token, service.backend.receiver_invoice(10, os.urandom(32)), fee_reserve=1
    )
    for cls, operation in [
        (MintOperation, mint_operation),
        (SwapOperation, swap_operation),
        (MeltOperation, melt_operation),
    ]:
        payload = operation.model_dump()
        payload["secrets"].pop()
        with pytest.raises(ValueError, match="incomplete"):
            cls.model_validate(payload)
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]


def test_poller_cannot_release_while_executor_owns_attempt(service, monkeypatch):
    """Nutshell PR #8 regression: a locked attempt survives a stale FAILED."""
    token, invoice = service.fund(64)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)

    async def exercise():
        send_started = asyncio.Event()
        finish_send = asyncio.Event()
        original_pay = service.backend.pay_invoice
        status_calls = 0

        async def hanging_pay(request, fee_limit_sat=None, **kwargs):
            send_started.set()
            await finish_send.wait()
            return await original_pay(request, fee_limit_sat)

        async def stale_status(payment_hash):
            nonlocal status_calls
            status_calls += 1
            return Payment(PaymentState.failed)

        monkeypatch.setattr(service.backend, "pay_invoice", hanging_pay)
        monkeypatch.setattr(service.backend, "get_payment", stale_status)
        task = asyncio.create_task(service.mint.melt(operation.request))
        await asyncio.wait_for(send_started.wait(), 2)
        # Constructing a second in-process mint (another worker) must not
        # steal the live executor lock; only a real restart clears orphans.
        other = Mint(service.mint.db.path, SEED, service.backend)
        # The other worker only sees the armed lock: no status lookup, no
        # release, no resend — just pending.
        with pytest.raises(MintError, match="pending"):
            await other.melt(operation.request)
        assert status_calls == 1  # Only the executor's own pre-send lookup.
        with service.mint.db.read() as conn:
            row = conn.execute("SELECT state,melt_lock,submitted FROM melts").fetchone()
            assert row["state"] == "PENDING" and row["submitted"] == 1
            assert row["melt_lock"]
            assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 64
        finish_send.set()
        return await task

    receipt = asyncio.run(exercise())
    assert receipt.amount == 64
    # The delayed success finalized exactly once and cleared the executor lock.
    assert service.client.finish_melt(operation) == receipt
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT state,melt_lock FROM melts").fetchone()[:] == (
            "PAID",
            "",
        )
        assert conn.execute("SELECT redeemed,reserved FROM quotes").fetchone()[:] == (
            64,
            0,
        )
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["SPENT"] * len(token.proofs)


def test_submitted_outbox_reconciles_only_and_never_resends(service, monkeypatch):
    token, invoice = service.fund(64)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    sends = []

    async def pending_pay(request, fee_limit_sat=None, **kwargs):
        sends.append(request)
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending_pay)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    assert len(sends) == 1

    async def unknown(payment_hash):
        return None

    monkeypatch.setattr(service.backend, "get_payment", unknown)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    # Even after quote and backing deadlines pass, a submitted outbox whose
    # lookup sees nothing is never released and never sent again.
    monkeypatch.setattr(time, "time", lambda: operation.quote.expiry + 60)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    assert len(sends) == 1
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT state,melt_lock,submitted FROM melts").fetchone()[
            :
        ] == ("PENDING", "", 1)
        assert conn.execute("SELECT COUNT(*) FROM failed_melts").fetchone()[0] == 0
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 64
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["PENDING"] * len(token.proofs)


def test_late_failure_after_backing_close_is_terminal(tmp_path):
    backend = PendingPayout(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        token = fund(client, backend)
        invoice = backend.receiver_invoice(10, os.urandom(32))
        operation = client.prepare_melt(token, invoice, fee_reserve=1)
        wallet.save(operation)
        with pytest.raises(ClientError, match="pending"):
            wallet.retry(client, operation.id)
        # The backing reaches its cutoff and settles while the payout hangs.
        with backend.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET expiry=?",
                (int(time.time()) + mint.settle_margin_seconds,),
            )
        asyncio.run(mint.reconcile_holds())
        assert (
            asyncio.run(backend.get_invoice(token.payment_hash)).state
            == InvoiceState.settled
        )
        # Only now does the definite failure arrive.
        with backend.db.transaction() as conn:
            conn.execute("UPDATE payments SET state=?", (PaymentState.failed.value,))
        with pytest.raises(ClientError, match="backing has expired") as error:
            wallet.retry(client, operation.id)
        assert error.value.status_code == 410
        # Terminal tombstone replays as the same distinct error.
        with pytest.raises(ClientError, match="backing has expired"):
            client.finish_melt(operation)
        # The wallet cleared the operation and expired its inputs locally;
        # the wire nullifiers stay UNSPENT but can never be redeemed.
        assert wallet.pending() == []
        assert {r["state"] for r in wallet.tokens()} == {"EXPIRED"}
        wallet.refresh(client)
        assert {r["state"] for r in wallet.tokens()} == {"EXPIRED"}
        assert client.check([client.nullifier(p) for p in token.proofs]).states == [
            "UNSPENT"
        ] * len(token.proofs)
    with mint.db.read() as conn:
        assert conn.execute("SELECT expired FROM failed_melts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 0
        assert conn.execute("SELECT reserved,redeemed FROM quotes").fetchone()[:] == (
            0,
            0,
        )


def test_new_melt_lockout_near_cutoff(service, monkeypatch):
    margin = service.mint.settle_margin_seconds
    # Just outside the lockout (twice the settlement margin): melts proceed.
    quote = service.client.quote(
        16, preimage_hash(PREIMAGE.hex()), expiry=2 * margin + 5
    )
    asyncio.run(service.backend.accept(quote.payment_hash))
    token = service.client.finish_mint(
        service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    )
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    receipt = service.client.finish_melt(
        service.client.prepare_melt(token, invoice, fee_reserve=1)
    )
    assert receipt.amount == 10

    # Inside the lockout a brand-new melt is refused before reserving, and
    # the wallet safely releases its inputs.
    close_preimage = "aa" * 32
    close_quote = service.client.quote(
        16, preimage_hash(close_preimage), expiry=2 * margin
    )
    asyncio.run(service.backend.accept(close_quote.payment_hash))
    close_token = service.client.finish_mint(
        service.client.prepare_mint(close_quote.quote, close_preimage)
    )
    wallet = Wallet(service.path / "wallet.sqlite3")
    operation = service.client.prepare_melt(
        close_token,
        service.backend.receiver_invoice(10, os.urandom(32)),
        fee_reserve=1,
    )
    wallet.save(operation)
    with pytest.raises(ClientError, match="too close"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == []
    assert all(r["state"] == "LIVE" for r in wallet.tokens())
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 1
        reserved = conn.execute(
            "SELECT reserved FROM quotes WHERE payment_hash=?",
            (close_token.payment_hash,),
        ).fetchone()
        assert reserved[0] == 0
    assert service.client.check(
        [service.client.nullifier(p) for p in close_token.proofs]
    ).states == ["UNSPENT"] * len(close_token.proofs)

    # Once submitted, recovery is not blocked by the lockout: it finalizes
    # the external outcome and signs change while the backing is still live.
    live_preimage = "bb" * 32
    live_quote = service.client.quote(16, preimage_hash(live_preimage))
    asyncio.run(service.backend.accept(live_quote.payment_hash))
    live_token = service.client.finish_mint(
        service.client.prepare_mint(live_quote.quote, live_preimage)
    )
    payout_preimage = os.urandom(32)
    payout = service.backend.receiver_invoice(10, payout_preimage)

    async def pending_pay(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending_pay)
    pending_operation = service.client.prepare_melt(live_token, payout, fee_reserve=1)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(pending_operation)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET expiry=? WHERE payment_hash=?",
            (int(time.time()) + margin + 10, live_token.payment_hash),
        )
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (
                hashlib.sha256(payout_preimage).hexdigest(),
                PaymentState.succeeded.value,
                payout_preimage,
            ),
        )
    receipt = service.client.finish_melt(pending_operation)
    assert receipt.amount == 10
    assert sum(s.amount for s in receipt.change) == 6
    assert (
        asyncio.run(service.backend.get_invoice(live_token.payment_hash)).state
        == InvoiceState.accepted
    )


def test_reconcile_holds_settles_unblocked_rows_independently(service, monkeypatch):
    a, _ = service.fund(8)
    b, _ = service.fund(8, os.urandom(32))
    # Reveal both preimages through partial melts so both holds are candidates.
    for token in (a, b):
        invoice = service.backend.receiver_invoice(1, os.urandom(32))
        service.client.finish_melt(
            service.client.prepare_melt(token, invoice, fee_reserve=0)
        )
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET expiry=?",
            (int(time.time()) + service.mint.settle_margin_seconds,),
        )

    async def exercise():
        original = service.backend.get_invoice
        started = asyncio.Event()
        release = asyncio.Event()

        async def slowed(payment_hash):
            if payment_hash == a.payment_hash:
                started.set()
                await release.wait()
            return await original(payment_hash)

        monkeypatch.setattr(service.backend, "get_invoice", slowed)
        task = asyncio.create_task(service.mint.reconcile_holds())
        await asyncio.wait_for(started.wait(), 2)
        # One blocked hold must not stop another hold's settlement.
        for _ in range(100):
            if (await original(b.payment_hash)).state == InvoiceState.settled:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("a blocked hold stopped another hold's settlement")
        release.set()
        await asyncio.wait_for(task, 5)
        assert (await original(a.payment_hash)).state == InvoiceState.settled

    asyncio.run(exercise())


def test_paid_melt_reconciles_only_its_own_backing(service, monkeypatch):
    a, _ = service.fund(8)
    b, _ = service.fund(8, os.urandom(32))
    # Reveal both preimages so an unscoped scan would visit both holds.
    change = None
    for token in (a, b):
        invoice = service.backend.receiver_invoice(1, os.urandom(32))
        operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
        receipt = service.client.finish_melt(operation)
        if token is a:
            change = service.client.melt_change(operation, receipt)
    seen = set()
    original = service.backend.get_invoice

    async def spy(payment_hash):
        seen.add(payment_hash)
        return await original(payment_hash)

    monkeypatch.setattr(service.backend, "get_invoice", spy)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    service.client.finish_melt(
        service.client.prepare_melt(change, invoice, fee_reserve=0)
    )
    assert b.payment_hash not in seen


def test_stale_attempt_cannot_release_a_pending_outbox(service, monkeypatch):
    token, invoice = service.fund(16)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)

    async def pending_pay(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending_pay)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT * FROM melts").fetchone()
        assert row["submitted"] == 1 and row["melt_lock"] == ""
    # Foreign attempts never decrement reservations or delete the outbox.
    with pytest.raises(BackendError, match="another attempt"):
        service.mint._release_melt(row["quote"], row["digest"], "0" * 32)
    assert service.mint._claim_melt(row) is True
    assert service.mint._claim_melt(row) is False  # Already armed.
    with pytest.raises(BackendError, match="another attempt"):
        service.mint._release_melt(row["quote"], row["digest"], "0" * 32)
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 1
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 16
        assert conn.execute("SELECT COUNT(*) FROM failed_melts").fetchone()[0] == 0
    # The owning attempt can still release, restoring the reservation.
    service.mint._release_melt(row["quote"], row["digest"], row["attempt"])
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 0
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 0
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"] * len(token.proofs)


def test_paid_outbox_is_terminal_and_never_released(service):
    token, invoice = service.fund(16)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    receipt = service.client.finish_melt(operation)
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT * FROM melts").fetchone()
        assert row["state"] == "PAID" and row["melt_lock"] == ""
    with pytest.raises(BackendError, match="terminal"):
        service.mint._release_melt(row["quote"], row["digest"], row["attempt"])
    assert service.client.finish_melt(operation) == receipt
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT state FROM melts").fetchone()[0] == "PAID"
        assert (
            conn.execute("SELECT redeemed,reserved FROM quotes").fetchone()["reserved"]
            == 0
        )


def test_competing_executors_finalize_a_paid_outbox_exactly_once(service, monkeypatch):
    token, invoice = service.fund(16)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)

    async def pending_pay(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending_pay)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    # The payment completes externally while the outbox is unlocked.
    from conftest import PAYOUT_PREIMAGE

    with service.backend.db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (
                operation.quote.payment_hash,
                PaymentState.succeeded.value,
                PAYOUT_PREIMAGE,
            ),
        )

    async def exercise():
        original = service.backend.get_payment

        async def slow(payment_hash):
            await asyncio.sleep(0.05)
            return await original(payment_hash)

        monkeypatch.setattr(service.backend, "get_payment", slow)
        other = Mint(service.mint.db.path, SEED, service.backend)
        return await asyncio.gather(
            service.mint.melt(operation.request),
            other.melt(operation.request),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())
    receipts = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, MintError)]
    assert len(receipts) == 1
    assert len(errors) == 1 and errors[0].status_code == 409
    # Retrying the loser replays the same paid receipt; value moved once.
    assert service.client.finish_melt(operation) == receipts[0]
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT state FROM melts").fetchone()[0] == "PAID"
        assert conn.execute("SELECT redeemed,reserved FROM quotes").fetchone()[:] == (
            16,
            0,
        )


def test_backend_error_before_submission_unlocks_for_retry(service, monkeypatch):
    """A lookup/health failure before submission is retryable, not fail-closed."""
    token, invoice = service.fund(16)
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    failing = True
    original = service.backend.get_payment

    async def maybe_unavailable(payment_hash):
        if failing:
            raise BackendError("node unreachable")
        return await original(payment_hash)

    monkeypatch.setattr(service.backend, "get_payment", maybe_unavailable)
    with pytest.raises(ClientError, match="503"):
        service.client.finish_melt(operation)
    # Nothing was submitted: the outbox stays reserved but unlocked.
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT state,melt_lock,submitted FROM melts").fetchone()
        assert row[:] == ("PENDING", "", 0)
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 16
        assert conn.execute("SELECT COUNT(*) FROM failed_melts").fetchone()[0] == 0
    failing = False
    receipt = service.client.finish_melt(operation)
    assert receipt.amount == 16
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT state FROM melts").fetchone()[0] == "PAID"


def test_fail_melt_liveness_read_is_atomic_with_reconciliation(service, monkeypatch):
    """No settlement can land between a failed melt's reconcile and release."""
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(16, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)

    async def pending(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (operation.quote.payment_hash, PaymentState.failed.value, None),
        )

    async def exercise():
        reconciled = asyncio.Event()
        proceed = asyncio.Event()
        original = service.mint._reconcile_hold_locked

        async def barriered(payment_hash):
            await original(payment_hash)
            reconciled.set()
            await proceed.wait()

        monkeypatch.setattr(service.mint, "_reconcile_hold_locked", barriered)
        task = asyncio.create_task(service.mint.melt(operation.request))
        await asyncio.wait_for(reconciled.wait(), 2)
        # The backing passes its cutoff only now; an external reconcile must
        # wait for the melt's hold lock instead of closing it mid-decision.
        with service.backend.db.transaction() as conn:
            conn.execute("UPDATE invoices SET expiry=?", (int(time.time()) - 1,))
        external = asyncio.create_task(service.mint.reconcile_holds())
        await asyncio.sleep(0.1)
        assert not external.done()
        proceed.set()
        # The melt's atomic decision point saw a live backing: ordinary
        # unspent failure, never a false terminal expiry.
        with pytest.raises(MintError, match="unspent"):
            await task
        await asyncio.wait_for(external, 2)

    asyncio.run(exercise())
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT expired FROM failed_melts").fetchone()[0] == 0
        assert conn.execute("SELECT closed FROM quotes").fetchone()[0] == 2
    assert (
        asyncio.run(service.backend.get_invoice(token.payment_hash)).state
        == InvoiceState.canceled
    )


def test_fail_melt_with_unknown_backing_liveness_keeps_outbox(service, monkeypatch):
    """Past cutoff with an unreachable backend: no false ordinary failure."""
    token, _ = service.fund(16)
    invoice = service.backend.receiver_invoice(16, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)

    async def pending(request, fee_limit_sat=None, **kwargs):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending)
    with pytest.raises(ClientError, match="pending"):
        service.client.finish_melt(operation)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments VALUES (?,?,?,0)",
            (operation.quote.payment_hash, PaymentState.failed.value, None),
        )
        # Past the backing cutoff, but the backend cannot report its state.
        conn.execute(
            "UPDATE invoices SET expiry=?",
            (int(time.time()) + service.mint.settle_margin_seconds - 1,),
        )
    original = service.backend.get_invoice

    async def unavailable(payment_hash):
        raise BackendError("node unavailable")

    monkeypatch.setattr(service.backend, "get_invoice", unavailable)
    with pytest.raises(ClientError, match="503"):
        service.client.finish_melt(operation)
    # The outbox is kept reserved and unlocked for a later definite-failure
    # release; no tombstone is written and no inputs are freed.
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT state,melt_lock,submitted FROM melts").fetchone()
        assert row[:] == ("PENDING", "", 1)
        assert conn.execute("SELECT COUNT(*) FROM failed_melts").fetchone()[0] == 0
        assert conn.execute("SELECT reserved FROM quotes").fetchone()[0] == 16
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["PENDING"] * len(token.proofs)
    # Once liveness is known (past cutoff), the failure becomes terminal.
    monkeypatch.setattr(service.backend, "get_invoice", original)
    with pytest.raises(ClientError, match="backing has expired") as error:
        service.client.finish_melt(operation)
    assert error.value.status_code == 410
    with service.mint.db.read() as conn:
        assert conn.execute("SELECT expired FROM failed_melts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM melts").fetchone()[0] == 0
