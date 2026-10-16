import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from conftest import PREIMAGE
from fastapi.testclient import TestClient

from tmp_cash.api import create_app
from tmp_cash.cache import SuccessfulResponseCache
from tmp_cash.db import BackendError, MintError
from tmp_cash.models import (
    InvoiceState,
    MeltQuoteRequest,
    SignaturesResponse,
    preimage_hash,
)


@pytest.fixture(params=["mint", "swap", "melt"])
def operation(service, request):
    kind = request.param
    if kind == "mint":
        quote = service.client.quote(16, preimage_hash(PREIMAGE.hex()))
        asyncio.run(service.backend.accept(quote.payment_hash))
        operation = service.client.prepare_mint(quote.quote, PREIMAGE.hex())
    else:
        token, _ = service.fund(16)
        if kind == "swap":
            operation = service.client.prepare_swap(token, [8, 8])
        else:
            invoice = service.backend.receiver_invoice(10, os.urandom(32))
            operation = service.client.prepare_melt(token, invoice, fee_reserve=1)
    path = "/v1/swap" if kind == "swap" else f"/v1/{kind}/bolt11"
    return kind, path, operation.request


def test_successful_posts_reuse_validated_requests(service, operation, monkeypatch):
    kind, path, request = operation
    original = getattr(service.mint, kind)
    spy = AsyncMock(wraps=original)
    monkeypatch.setattr(service.mint, kind, spy)
    http = service.client.http
    first = http.post(path, json=request.model_dump())
    assert first.status_code == 200
    # Formatting, key order, and omitted default fields share a validated key.
    body = dict(reversed(request.model_dump(exclude_defaults=True).items()))
    repeated = http.post(
        path,
        content=json.dumps(body, indent=2),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://wallet.example",
        },
    )
    assert repeated.content == first.content
    assert repeated.headers["access-control-allow-origin"] == "*"
    assert spy.call_count == 1
    assert http.post(path, json=body | {"unexpected": True}).status_code == 422
    assert spy.call_count == 1
    changed = request.model_dump()
    if kind == "swap":
        changed["equality_proof"] = "00"
    else:
        changed["quote"] = "ff" * 32
    assert http.post(path, json=changed).status_code in (400, 404)
    assert spy.call_count == 2


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (BackendError("backend temporarily unavailable"), 503),
        (MintError("pending; retry the same request", 409), 409),
        (ValueError("invalid encoding"), 400),
    ],
)
def test_failed_posts_can_succeed_on_retry(
    service, operation, monkeypatch, error, status
):
    kind, path, request = operation
    original = getattr(service.mint, kind)
    calls = 0

    def flaky(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return original(request)

    monkeypatch.setattr(service.mint, kind, flaky)
    http = service.client.http
    body = request.model_dump()
    assert http.post(path, json=body).status_code == status
    successful = http.post(path, json=body)
    assert successful.status_code == 200
    assert http.post(path, json=body).content == successful.content
    assert calls == 2


async def test_concurrent_identical_posts_share_success(
    service, operation, monkeypatch
):
    kind, path, request = operation
    original = getattr(service.mint, kind)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        started.set()
        await release.wait()
        result = original(request)
        return await result

    spy = AsyncMock(side_effect=slow)
    monkeypatch.setattr(service.mint, kind, spy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.client.http.app),
        base_url="http://testserver",
    ) as http:
        tasks = [
            asyncio.create_task(http.post(path, json=request.model_dump()))
            for _ in range(8)
        ]
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            release.set()
        responses = await asyncio.gather(*tasks)
    assert all(response.status_code == 200 for response in responses)
    assert len({response.content for response in responses}) == 1
    assert spy.call_count == 1


def test_quotes_and_state_queries_remain_uncached(service):
    payment_hash = preimage_hash(PREIMAGE.hex())
    first = service.client.quote(16, payment_hash)
    assert first.state == InvoiceState.open
    asyncio.run(service.backend.accept(payment_hash))
    assert service.client.quote(16, payment_hash).state == InvoiceState.accepted
    token = service.client.finish_mint(
        service.client.prepare_mint(first.quote, PREIMAGE.hex())
    )
    assert service.client.get_quote(first.quote).issued
    nullifiers = [service.client.nullifier(p) for p in token.proofs]
    assert service.client.check(nullifiers).states == ["UNSPENT"]
    service.client.finish_swap(service.client.prepare_swap(token))
    assert service.client.check(nullifiers).states == ["SPENT"]
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    assert (
        service.client.melt_quote(invoice).quote
        != service.client.melt_quote(invoice).quote
    )


def test_new_app_uses_durable_recovery_with_an_empty_cache(
    service, operation, monkeypatch
):
    kind, path, request = operation
    first = service.client.http.post(path, json=request.model_dump())
    assert first.status_code == 200
    original = getattr(service.mint, kind)
    spy = AsyncMock(wraps=original)
    monkeypatch.setattr(service.mint, kind, spy)
    with TestClient(create_app(service.mint, recovery=False)) as http:
        assert http.post(path, json=request.model_dump()).content == first.content
    assert spy.call_count == 1


async def test_cache_ttl_does_not_extend_on_hits(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("tmp_cash.cache.monotonic", lambda: clock[0])
    cache = SuccessfulResponseCache(ttl=300)
    request = MeltQuoteRequest(request="test")
    response = SignaturesResponse(signatures=[])
    operation = Mock(return_value=response)
    await cache.run("mint", request, operation)
    clock[0] = 299
    cached = await cache.run("mint", request, operation)
    assert cached == response and cached is not response
    assert operation.call_count == 1
    clock[0] = 300
    await cache.run("mint", request, operation)
    assert operation.call_count == 2


@pytest.mark.parametrize("limit", ["entries", "bytes"])
async def test_cache_evicts_least_recently_used_responses(limit):
    response = SignaturesResponse(signatures=[])
    size = len(response.model_dump_json().encode())
    cache = SuccessfulResponseCache(
        max_entries=2 if limit == "entries" else 10,
        max_bytes=size * 2 if limit == "bytes" else 1024,
    )
    request = MeltQuoteRequest(request="test")
    operation = Mock(return_value=response)
    for endpoint in ("mint", "swap", "mint", "melt", "mint"):
        await cache.run(endpoint, request, operation)
    assert operation.call_count == 3
    await cache.run("swap", request, operation)
    assert operation.call_count == 4


async def test_oversized_responses_are_not_cached():
    cache = SuccessfulResponseCache(max_bytes=1)
    request = MeltQuoteRequest(request="test")
    operation = Mock(return_value=SignaturesResponse(signatures=[]))
    for _ in range(2):
        await cache.run("mint", request, operation)
    assert operation.call_count == 2


async def test_canceled_request_releases_waiting_retry():
    cache = SuccessfulResponseCache()
    request = MeltQuoteRequest(request="test")
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    first = asyncio.create_task(cache.run("mint", request, blocked))
    await asyncio.wait_for(started.wait(), timeout=5)
    operation = Mock(return_value=SignaturesResponse(signatures=[]))
    retry = asyncio.create_task(cache.run("mint", request, operation))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.wait_for(retry, timeout=5) == operation.return_value
    await cache.run("mint", request, operation)
    assert operation.call_count == 1
    assert cache._pending == {}
