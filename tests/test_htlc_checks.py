import asyncio
import json
import os

import httpx
import pytest

from tmp_cash.client import Client, ClientError
from tmp_cash.db import BackendError
from tmp_cash.models import MAX_HTLC_CHECK_HASHES

ENDPOINT = "/v1/htlcs/check"


def test_no_public_full_listing(service):
    service.fund(16)
    assert service.client.http.get("/v1/htlcs/pending").status_code == 404
    assert service.client.http.post("/v1/htlcs/pending").status_code == 404
    assert service.client.http.get(ENDPOINT).status_code == 405
    assert service.client.http.get("/v1/info").json()["pending_htlcs"] == ENDPOINT


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"payment_hashes": []},
        {"payment_hashes": ["a" * 64] * (MAX_HTLC_CHECK_HASHES + 1)},
        {"payment_hashes": ["A" * 64]},
        {"payment_hashes": ["z" * 64]},
        {"payment_hashes": ["a" * 63]},
        {"payment_hashes": "a" * 64},
        {"payment_hashes": ["a" * 64], "preimage": "b" * 64},
    ],
)
def test_invalid_checks_rejected_before_backend_access(service, monkeypatch, body):
    async def unexpected(*args):
        pytest.fail("invalid check reached the backend")

    monkeypatch.setattr(service.backend, "get_invoice", unexpected)
    monkeypatch.setattr(service.backend, "get_block_height", unexpected)
    assert service.client.http.post(ENDPOINT, json=body).status_code == 422


def test_check_scopes_backend_work_and_deduplicates_hashes(service, monkeypatch):
    selected, _ = service.fund(16)
    unrelated, _ = service.fund(32, os.urandom(32))
    original = service.backend.get_invoice
    calls = []

    async def lookup(payment_hash):
        calls.append(payment_hash)
        if payment_hash == unrelated.payment_hash:
            raise BackendError("unrelated hold unavailable")
        return await original(payment_hash)

    monkeypatch.setattr(service.backend, "get_invoice", lookup)
    response = service.client.http.post(
        ENDPOINT,
        json={"payment_hashes": [selected.payment_hash, selected.payment_hash]},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [h["payment_hash"] for h in response.json()["htlcs"]] == [
        selected.payment_hash
    ]
    assert calls == [selected.payment_hash]
    assert unrelated.payment_hash not in response.text
    assert selected.proofs[0].preimage not in response.text

    # Failure of a requested backing must not publish a partial successful result.
    response = service.client.http.post(
        ENDPOINT,
        json={"payment_hashes": [selected.payment_hash, unrelated.payment_hash]},
    )
    assert response.status_code == 503
    assert "htlcs" not in response.json()


def test_unknown_unissued_and_closed_hashes_have_no_pending_entry(service):
    unknown = os.urandom(32).hex()
    unissued = service.client.quote(16, os.urandom(32).hex()).payment_hash
    asyncio.run(service.backend.accept(unissued))
    closed, _ = service.fund(16)
    asyncio.run(service.backend.cancel(closed.payment_hash))
    assert (
        service.client.check_htlcs([unknown, unissued, closed.payment_hash]).htlcs == []
    )


def test_maximum_batch_and_unknown_hashes_do_not_lookup_unrelated_holds(
    service, monkeypatch
):
    service.fund(16)

    async def unexpected(*args):
        pytest.fail("unknown hashes caused an unrelated invoice lookup")

    monkeypatch.setattr(service.backend, "get_invoice", unexpected)
    response = service.client.http.post(
        ENDPOINT,
        json={"payment_hashes": [f"{i:064x}" for i in range(MAX_HTLC_CHECK_HASHES)]},
    )
    assert response.status_code == 200
    assert response.json()["htlcs"] == []


def test_python_client_batches_deduplicates_and_skips_empty_checks():
    hashes = [f"{i:064x}" for i in range(201)]
    batches = []

    def handle(request):
        assert request.method == "POST" and request.url.path == ENDPOINT
        body = json.loads(request.content)
        assert set(body) == {"payment_hashes"}
        batches.append(body["payment_hashes"])
        return httpx.Response(
            200,
            json={"checked_at": 100 + len(batches), "block_height": None, "htlcs": []},
        )

    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        client = Client("http://test", http)
        assert client.check_htlcs([]).htlcs == []
        assert batches == []
        assert client.check_htlcs(hashes + hashes).checked_at == 103
    assert [len(batch) for batch in batches] == [100, 100, 1]
    assert [h for batch in batches for h in batch] == hashes


@pytest.mark.parametrize("heights", [(200, 201), (201, 200)])
def test_python_client_merges_batches_using_latest_height(heights):
    hashes = [f"{i:064x}" for i in range(101)]
    calls = 0

    def handle(request):
        nonlocal calls
        height = heights[calls]
        calls += 1
        return httpx.Response(
            200,
            json={
                "checked_at": 100 + calls,
                "block_height": height,
                "htlcs": [
                    {
                        "payment_hash": h,
                        "amount": 1,
                        "invoice_expires_at": 100,
                        "htlc_expiry_height": 1018,
                        "expiry_height": 1000,
                        "blocks_remaining": 1000 - height,
                        "melt_deadline_height": 994,
                    }
                    for h in json.loads(request.content)["payment_hashes"]
                ],
            },
        )

    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        snapshot = Client("http://test", http).check_htlcs(hashes)
    assert snapshot.block_height == 201
    assert snapshot.checked_at == 102
    assert len(snapshot.htlcs) == 101
    assert {h.blocks_remaining for h in snapshot.htlcs} == {799}
    assert {h.melt_deadline_height for h in snapshot.htlcs} == {994}


@pytest.mark.parametrize("duplicate", [False, True])
def test_python_client_rejects_unrequested_or_duplicate_backings(service, duplicate):
    token, _ = service.fund(16)
    snapshot = service.client.check_htlcs([token.payment_hash]).model_dump(mode="json")
    if duplicate:
        snapshot["htlcs"] *= 2
    requested = token.payment_hash if duplicate else os.urandom(32).hex()
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
    ) as http:
        with pytest.raises(ClientError, match="unexpected backing"):
            Client("http://test", http).check_htlcs([requested])
