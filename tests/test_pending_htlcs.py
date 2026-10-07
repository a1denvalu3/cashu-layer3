import asyncio
import base64
import hashlib
from dataclasses import replace

import httpx
import pytest
from conftest import PREIMAGE, SEED
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client, ClientError
from cashu.db import BackendError
from cashu.lightning import FakeBackend, LndRestBackend, Payment, PaymentState
from cashu.mint import Mint
from cashu.models import PendingHTLCsResponse
from cashu.wallet import Wallet


def test_public_list_only_contains_funded_issued_pending_claims(service):
    http = service.client.http
    response = http.get("/v1/htlcs/pending")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["htlcs"] == []
    quote = service.client.quote(1000, hashlib.sha256(PREIMAGE).hexdigest())
    assert service.client.pending_htlcs().htlcs == []
    asyncio.run(service.backend.accept(quote.payment_hash))
    assert service.client.pending_htlcs().htlcs == []
    token = service.client.finish_mint(
        service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    )
    snapshot = service.client.pending_htlcs()
    assert snapshot.block_height is None
    assert len(snapshot.htlcs) == 1
    entry = snapshot.htlcs[0]
    assert entry.payment_hash == token.payment_hash
    assert entry.amount == token.amount
    assert entry.expires_at == quote.expiry
    assert entry.invoice_expires_at == quote.expiry
    assert entry.expiry_height is None
    assert entry.expires_at > snapshot.checked_at
    public = http.get("/v1/htlcs/pending").text
    assert PREIMAGE.hex() not in public
    assert token.credential not in public
    assert quote.quote not in public
    assert quote.request not in public
    assert service.client.nullifier(token) not in public
    assert http.get("/v1/info").json()["pending_htlcs"] == "/v1/htlcs/pending"
    received = service.client.finish_swap(service.client.prepare_swap(token))
    assert service.client.check_backing(received) == entry


@pytest.mark.parametrize("removed", ["canceled", "expired", "underfunded", "settled"])
def test_public_list_refreshes_and_receive_rejects_removed_backing(service, removed):
    token, payout = service.fund()
    service.client.check_backing(token)
    if removed == "canceled":
        asyncio.run(service.backend.cancel(token.payment_hash))
    elif removed == "settled":
        service.client.finish_burn(service.client.prepare_burn(token, payout))
    else:
        with service.backend.db.transaction() as conn:
            column, value = (
                ("expiry", 0) if removed == "expired" else ("amount_paid", 1)
            )
            conn.execute(
                f"UPDATE invoices SET {column}=? WHERE payment_hash=?",
                (value, token.payment_hash),
            )
    # The credential remains authentic; the live backing check is additional.
    service.client.credential(token)
    assert service.client.pending_htlcs().htlcs == []
    wallet = Wallet(service.path / "incoming.sqlite3")
    with pytest.raises(ClientError, match="pending backing HTLC"):
        operation = service.client.prepare_swap(token)
        wallet.save(operation)
    assert wallet.tokens() == []
    assert wallet.pending() == []
    if removed != "settled":
        assert service.client.check([service.client.nullifier(token)]).states == [
            "UNSPENT"
        ]


def test_pending_burn_is_excluded_until_definite_failure_releases_it(
    service, monkeypatch
):
    token, payout = service.fund()
    operation = service.client.prepare_burn(token, payout)

    async def pending(request):
        return Payment(PaymentState.pending)

    monkeypatch.setattr(service.backend, "pay_invoice", pending)
    with pytest.raises(ClientError, match="payment is pending"):
        service.client.finish_burn(operation)
    assert service.client.pending_htlcs().htlcs == []

    async def failed(request):
        return Payment(PaymentState.failed)

    monkeypatch.setattr(service.backend, "pay_invoice", failed)
    with pytest.raises(ClientError, match="token is unspent"):
        service.client.finish_burn(operation)
    assert service.client.check_backing(token).payment_hash == token.payment_hash


def test_backend_failure_returns_503_instead_of_a_partial_or_stale_list(
    service, monkeypatch
):
    token, _ = service.fund()
    other, _ = service.fund(preimage=b"2" * 32)
    assert len(service.client.pending_htlcs().htlcs) == 2
    get_invoice = service.backend.get_invoice

    async def fail_one(payment_hash):
        if payment_hash == other.payment_hash:
            raise BackendError("simulated backend outage")
        return await get_invoice(payment_hash)

    monkeypatch.setattr(service.backend, "get_invoice", fail_one)
    response = service.client.http.get("/v1/htlcs/pending")
    assert response.status_code == 503
    assert "htlcs" not in response.json()
    with pytest.raises(ClientError, match="503"):
        service.client.prepare_swap(token)
    assert service.client.check([service.client.nullifier(token)]).states == ["UNSPENT"]


def test_missing_expiry_fails_closed(service, monkeypatch):
    token, _ = service.fund()
    get_invoice = service.backend.get_invoice

    async def no_deadline(payment_hash):
        return replace(await get_invoice(payment_hash), hold_expires_at=None)

    monkeypatch.setattr(service.backend, "get_invoice", no_deadline)
    assert service.client.http.get("/v1/htlcs/pending").status_code == 503
    with pytest.raises(ClientError, match="503"):
        service.client.prepare_swap(token)


def test_client_rejects_false_or_missing_deadlines_and_amounts(service):
    token, _ = service.fund()
    snapshot = service.client.pending_htlcs()
    entry = snapshot.htlcs[0]
    for altered in (
        entry.model_copy(update={"amount": 1}),
        entry.model_copy(update={"expires_at": None}),
        entry.model_copy(update={"expires_at": snapshot.checked_at}),
    ):
        with pytest.raises(ClientError):
            service.client.check_backing(
                snapshot=snapshot.model_copy(update={"htlcs": [altered]}), token=token
            )
    with pytest.raises(ClientError, match="unique pending"):
        service.client.check_backing(
            token, snapshot.model_copy(update={"htlcs": [entry, entry]})
        )


def test_backing_check_downloads_full_list_without_revealing_hash(service, monkeypatch):
    token, _ = service.fund()
    get = service.client._get
    post = service.client._post
    requests = []

    def record_get(path):
        requests.append(("GET", path, None))
        return get(path)

    def record_post(path, body):
        requests.append(("POST", path, body.model_dump_json()))
        return post(path, body)

    monkeypatch.setattr(service.client, "_get", record_get)
    monkeypatch.setattr(service.client, "_post", record_post)
    operation = service.client.prepare_swap(token)
    assert operation.backing.payment_hash == token.payment_hash
    service.client.finish_swap(operation)
    assert sum(path == "/v1/htlcs/pending" for _, path, _ in requests) == 1
    assert sum(method == "POST" for method, _, _ in requests) == 1
    for _, path, body in requests:
        assert token.payment_hash not in path
        if body is not None:
            assert token.payment_hash not in body
            assert token.preimage not in body
            assert '"backing"' not in body


def test_saved_swap_recovers_even_when_backing_status_is_unavailable(
    service, monkeypatch
):
    token, _ = service.fund()
    operation = service.client.prepare_swap(token)
    wallet = Wallet(service.path / "receiver.sqlite3")
    wallet.save(operation)
    issued = service.client.finish_swap(operation)  # Simulate a lost response.

    async def unavailable(payment_hash):
        raise BackendError("simulated node outage")

    monkeypatch.setattr(service.backend, "get_invoice", unavailable)
    with pytest.raises(ClientError, match="503"):
        service.client.prepare_swap(issued)
    assert wallet.retry(service.client, operation.id, "swap") == issued
    assert wallet.pending() == []
    assert wallet.token(service.client.nullifier(issued)) == issued


@pytest.fixture
def lnd_backing(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    request = FakeBackend._invoice(1000, payment_hash, 60, "pending backing test")
    node = {
        "height": 300,
        "synced": True,
        "state": "OPEN",
        "htlcs": [],
    }
    calls = []

    def respond(r):
        calls.append(r)
        if r.url.path == "/v2/invoices/hodl":
            return httpx.Response(200, json={"payment_request": request})
        if r.url.path == f"/v1/invoice/{payment_hash}":
            return httpx.Response(
                200,
                json={
                    "r_hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
                    "value": "1000",
                    "payment_request": request,
                    "creation_date": "1",
                    "expiry": "60",  # Funding window expired; active HTLCs have not.
                    "state": node["state"],
                    "amt_paid_sat": "1000" if node["state"] == "ACCEPTED" else "0",
                    "htlcs": node["htlcs"],
                },
            )
        if r.url.path == "/v1/getinfo":
            return httpx.Response(
                200,
                json={
                    "block_height": node["height"],
                    "synced_to_chain": node["synced"],
                },
            )
        raise AssertionError(r.url)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test",
        macaroon,
        transport=httpx.MockTransport(respond),
        hold_expiry_delta=18,
    )
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        quote = client.quote(1000, payment_hash)
        node["state"] = "ACCEPTED"
        node["htlcs"] = [
            {"state": "CANCELED", "amt_msat": "1000000", "expiry_height": 200},
            {"state": "ACCEPTED", "amt_msat": "400000", "expiry_height": 350},
            {"state": "ACCEPTED", "amt_msat": "600000", "expiry_height": 400},
        ]
        token = client.finish_mint(client.prepare_mint(quote.quote, PREIMAGE.hex()))
        yield client, token, node, backend


def test_lnd_uses_earliest_accepted_mpp_expiry_and_early_cancel_delta(lnd_backing):
    client, token, node, backend = lnd_backing
    snapshot = client.pending_htlcs()
    assert snapshot.block_height == 300
    entry = client.check_backing(token, snapshot)
    assert entry.htlc_expiry_height == 350
    assert entry.expiry_height == 332
    assert entry.blocks_remaining == 32
    assert entry.invoice_expires_at == 61
    assert entry.expires_at is None  # Block-based expiry is not a wall-clock promise.
    backend.hold_expiry_delta = 0
    assert client.check_backing(token).expiry_height == 350
    backend.hold_expiry_delta = 18
    node["height"] = 331
    assert client.check_backing(token).blocks_remaining == 1
    node["height"] = 332
    assert client.pending_htlcs().htlcs == []
    with pytest.raises(ClientError, match="pending backing"):
        client.prepare_swap(token)


@pytest.mark.parametrize(
    "failure", ["unsynced", "missing-htlcs", "underfunded-htlcs", "zero-expiry"]
)
def test_lnd_incomplete_or_unsynced_backing_fails_closed(lnd_backing, failure):
    client, token, node, _ = lnd_backing
    if failure == "unsynced":
        node["synced"] = False
    elif failure == "missing-htlcs":
        node["htlcs"] = []
    elif failure == "underfunded-htlcs":
        node["htlcs"] = [{"state": "ACCEPTED", "amt_msat": "1", "expiry_height": 350}]
    else:
        node["htlcs"] = [
            {"state": "ACCEPTED", "amt_msat": "1000000", "expiry_height": 0}
        ]
    with pytest.raises(ClientError, match="503"):
        client.prepare_swap(token)


def test_client_checks_block_deadline_consistency(lnd_backing):
    client, token, _, _ = lnd_backing
    snapshot = client.pending_htlcs()
    entry = snapshot.htlcs[0]
    for altered in (
        entry.model_copy(update={"expiry_height": None}),
        entry.model_copy(update={"htlc_expiry_height": None}),
        entry.model_copy(update={"blocks_remaining": 9999}),
        entry.model_copy(update={"expiry_height": 9999, "blocks_remaining": 9699}),
        entry.model_copy(update={"expiry_height": 300, "blocks_remaining": 0}),
    ):
        forged = PendingHTLCsResponse(
            checked_at=snapshot.checked_at, block_height=300, htlcs=[altered]
        )
        with pytest.raises(ClientError, match="expired or has no deadline"):
            client.check_backing(token, forged)
