import asyncio
import os
import sqlite3
import time

import pytest
from conftest import PREIMAGE, SEED
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client, ClientError
from cashu.db import BackendError
from cashu.lightning import FakeBackend, Payment, PaymentState
from cashu.mint import Mint
from cashu.models import InvoiceState, preimage_hash
from cashu.wallet import Wallet


def fund(client, backend, amount=16):
    quote = client.quote(amount, preimage_hash(PREIMAGE.hex()))
    asyncio.run(backend.accept(quote.payment_hash))
    return client.finish_mint(client.prepare_mint(quote.quote, PREIMAGE.hex()))


class LosePayoutResponse(FakeBackend):
    calls = 0

    async def pay_invoice(self, request, fee_limit_sat=None):
        self.calls += 1
        await super().pay_invoice(request, fee_limit_sat)
        raise BackendError("response lost after Lightning paid")


class FailSettlement(FakeBackend):
    async def settle_invoice(self, preimage):
        raise BackendError("node unavailable during settlement")


class PendingPayout(FakeBackend):
    calls = 0

    async def pay_invoice(self, request, fee_limit_sat=None):
        self.calls += 1
        from cashu.lightning import invoice_details

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
        assert client.pending_htlcs().htlcs == []
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
    wallet.abandon(service.client, operation.id)
    assert all(r["state"] == "LIVE" for r in wallet.tokens())
    with service.mint.db.read() as conn:
        row = conn.execute("SELECT preimage,redeemed,reserved FROM quotes").fetchone()
        assert row[:] == (PREIMAGE.hex(), 0, 0)


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
    from cashu.client import MeltOperation, MintOperation, SwapOperation

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
