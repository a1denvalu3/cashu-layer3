import asyncio

import pytest
from conftest import PAYOUT_PREIMAGE, PREIMAGE, SEED, Service
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.cli import wait_and_mint
from cashu.client import Client, ClientError
from cashu.db import BackendError
from cashu.lightning import FakeBackend, Payment, PaymentState
from cashu.mint import Mint
from cashu.models import InvoiceState
from cashu.wallet import Wallet


def reopen(service):
    backend = FakeBackend(service.path / "fake.sqlite3")
    mint = Mint(service.path / "mint.sqlite3", SEED, backend)
    client = Client("http://testserver", TestClient(create_app(mint, recovery=False)))
    return backend, mint, client


def test_mint_and_swap_recovery_material_survives_restart(service):
    import hashlib

    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    wallet_path = service.path / "wallet.sqlite3"
    wallet = Wallet(wallet_path)
    mint_op = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    wallet.save(mint_op)
    original = service.client.finish_mint(mint_op)  # response lost before local commit
    _, _, client = reopen(service)
    wallet = Wallet(wallet_path)
    token = wallet.retry(client, mint_op.id, "mint")
    assert token == original
    swap_op = client.prepare_swap(token)
    wallet.save(swap_op)
    swapped = client.finish_swap(swap_op)  # response lost again
    _, _, client = reopen(service)
    wallet = Wallet(wallet_path)
    recovered = wallet.retry(client, swap_op.id, "swap")
    assert recovered == swapped
    assert recovered.preimage == PREIMAGE.hex()
    assert wallet.pending() == []
    assert sorted(t["state"] for t in wallet.tokens()) == ["LIVE", "SPENT"]


def test_resuming_mint_after_lost_response_reuses_its_saved_secret(service):
    import hashlib

    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.accept(quote.payment_hash))
    wallet = Wallet(service.path / "wallet.sqlite3")
    operation = service.client.prepare_mint(quote.quote)
    wallet.save(operation)
    issued = service.client.finish_mint(operation)
    recovered = wait_and_mint(
        service.client, wallet, service.client.get_quote(quote.quote)
    )
    assert recovered == issued
    assert wallet.pending() == []
    assert wallet.token(service.client.nullifier(issued)) == issued


def test_waiting_stops_when_the_hold_is_canceled(service):
    import hashlib

    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    asyncio.run(service.backend.cancel(quote.payment_hash))
    wallet = Wallet(service.path / "wallet.sqlite3")
    with pytest.raises(ClientError, match="CANCELED"):
        wait_and_mint(service.client, wallet, quote)
    assert wallet.tokens() == []


def test_resuming_a_saved_mint_can_add_the_original_preimage(service):
    import hashlib

    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    wallet = Wallet(service.path / "wallet.sqlite3")
    original = service.client.prepare_mint(quote.quote)
    wallet.save(original)
    asyncio.run(service.backend.accept(quote.payment_hash))
    token = wait_and_mint(service.client, wallet, quote, PREIMAGE.hex())
    assert token.preimage == PREIMAGE.hex()
    from cashu.crypto.ps import Credential

    assert Credential.from_bytes(bytes.fromhex(token.credential)).s == int(
        original.secret, 16
    )


class FailSettlement(FakeBackend):
    async def settle_invoice(self, preimage):
        raise BackendError("simulated upstream outage")


class LosePayoutResponse(FakeBackend):
    async def pay_invoice(self, request):
        await super().pay_invoice(request)
        raise BackendError("simulated payout response loss")


@pytest.mark.parametrize("backend_type", [FailSettlement, LosePayoutResponse])
def test_outbox_resumes_after_payout_without_paying_twice(tmp_path, backend_type):
    backend = backend_type(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    client = Client("http://testserver", TestClient(create_app(mint, recovery=False)))
    service = Service(backend, mint, client, tmp_path)
    token, payout = service.fund()
    op = client.prepare_burn(token, payout)
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    wallet.save(op)
    with pytest.raises(ClientError, match="503"):
        client.finish_burn(op)
    assert client.check([client.nullifier(token)]).states == ["PENDING"]
    with pytest.raises(ClientError, match="already spent"):
        client.finish_swap(client.prepare_swap(token))
    backend, mint, client = reopen(service)
    asyncio.run(mint.resume_pending_burns())
    assert client.check([client.nullifier(token)]).states == ["SPENT"]
    assert (
        asyncio.run(backend.get_invoice(token.payment_hash)).state
        == InvoiceState.settled
    )
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    receipt = wallet.retry(client, op.id, "burn")
    assert receipt.preimage == PREIMAGE.hex()
    assert receipt.payout_hash != token.payment_hash
    assert receipt.payout_preimage == PAYOUT_PREIMAGE.hex()
    with backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


class PendingPayout(FakeBackend):
    async def pay_invoice(self, request):
        from cashu.lightning import invoice_details

        payment_hash, _ = invoice_details(request)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO payments VALUES (?,'IN_FLIGHT',NULL)", (payment_hash,)
            )
        return Payment(PaymentState.pending)


def test_pending_payout_is_never_refunded_early(tmp_path):
    backend = PendingPayout(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    client = Client("http://testserver", TestClient(create_app(mint, recovery=False)))
    service = Service(backend, mint, client, tmp_path)
    token, payout = service.fund()
    op = client.prepare_burn(token, payout)
    for _ in range(2):
        with pytest.raises(ClientError, match="payment is pending"):
            client.finish_burn(op)
        assert client.check([client.nullifier(token)]).states == ["PENDING"]
    with backend.db.transaction() as conn:
        conn.execute(
            "UPDATE payments SET state='SUCCEEDED',preimage=?", (PAYOUT_PREIMAGE,)
        )
    asyncio.run(mint.resume_pending_burns())
    assert client.finish_burn(op).preimage == PREIMAGE.hex()


def test_definite_payout_failure_releases_token(service):
    token, _ = service.fund()
    other = FakeBackend(service.path / "receiver.sqlite3")
    unknown_invoice = other.receiver_invoice(token.amount, PREIMAGE)
    op = service.client.prepare_burn(token, unknown_invoice)
    wallet = Wallet(service.path / "wallet.sqlite3")
    wallet.save(op)
    with pytest.raises(ClientError, match="payment failed; token is unspent"):
        service.client.finish_burn(op)
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]
    wallet.abandon(service.client, op.id)
    assert wallet.token(service.client.nullifier(token)) == token
    replacement = service.client.finish_swap(service.client.prepare_swap(token))
    assert replacement.amount == token.amount


def test_previous_burn_outbox_migrates_and_recovers_without_repaying(tmp_path):
    import json

    backend = FailSettlement(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    client = Client("http://testserver", TestClient(create_app(mint, recovery=False)))
    service = Service(backend, mint, client, tmp_path)
    token, _ = service.fund()
    payout = backend.receiver_invoice(token.amount, PREIMAGE)
    operation = client.prepare_burn(token, payout)
    operation = operation.model_copy(
        update={
            "token": token.model_copy(update={"preimage": None}),
            "request": operation.request.model_copy(update={"preimage": None}),
        }
    )
    wallet = Wallet(tmp_path / "wallet.sqlite3")
    wallet.save(operation)
    with pytest.raises(ClientError, match="503"):
        client.finish_burn(operation)
    with mint.db.transaction() as conn:
        conn.execute("UPDATE burns SET response=NULL")
        conn.execute("DROP INDEX burns_payout_hash")
        conn.execute("ALTER TABLE burns DROP COLUMN payout_hash")
        raw = json.loads(conn.execute("SELECT request FROM burns").fetchone()[0])
        raw.pop("preimage")
        conn.execute("UPDATE burns SET request=?", (json.dumps(raw),))
    with wallet.db.transaction() as conn:
        raw = json.loads(conn.execute("SELECT payload FROM operations").fetchone()[0])
        raw["token"].pop("preimage")
        raw["request"].pop("preimage")
        conn.execute("UPDATE operations SET payload=?", (json.dumps(raw),))
    backend, mint, client = reopen(service)
    asyncio.run(mint.resume_pending_burns())
    receipt = wallet.retry(client, operation.id, "burn")
    assert receipt.preimage == PREIMAGE.hex()
    assert receipt.payout_hash == token.payment_hash
    assert client.check([client.nullifier(token)]).states == ["SPENT"]
    with backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    with mint.db.read() as conn:
        assert (
            conn.execute("SELECT payout_hash FROM burns").fetchone()[0]
            == token.payment_hash
        )


def test_mint_seed_mismatch_cannot_silently_replace_the_key(service):
    with pytest.raises(ValueError, match="different seed"):
        Mint(
            service.path / "mint.sqlite3",
            b"different mint seed..............",
            service.backend,
        )


def test_previous_protocol_database_is_not_reinterpreted(service):
    service.fund()
    with service.mint.db.transaction() as conn:
        conn.execute("DELETE FROM metadata WHERE key='protocol'")
    with pytest.raises(ValueError, match="previous payment-hash protocol"):
        Mint(service.path / "mint.sqlite3", SEED, service.backend)
    with service.mint.db.read() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM quotes WHERE issued=1").fetchone()[0]
            == 1
        )
