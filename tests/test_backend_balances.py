import asyncio
import logging

import httpx
import pytest
from conftest import SEED
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.db import BackendError
from cashu.lightning import FakeBackend, LndRestBackend
from cashu.mint import Mint


@pytest.mark.asyncio
async def test_lnd_balances_keep_channel_holds_and_onchain_funds_separate(tmp_path):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/balance/channels":
            return httpx.Response(
                200,
                json={
                    "balance": "12000",
                    "local_balance": {"sat": "12000", "msat": "12000000"},
                    "remote_balance": {"sat": "8000", "msat": "8000000"},
                    "unsettled_local_balance": {"sat": "0", "msat": "0"},
                    "unsettled_remote_balance": {"sat": "100", "msat": "100000"},
                },
            )
        if request.url.path == "/v1/balance/blockchain":
            return httpx.Response(
                200,
                json={
                    "total_balance": "31000",
                    "confirmed_balance": "30000",
                    "unconfirmed_balance": "1000",
                },
            )
        raise AssertionError(request.url.path)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    try:
        balances = await backend.get_balances()
    finally:
        await backend.close()
    assert balances == {
        "Local channels": 12000,
        "Remote channels": 8000,
        "Unsettled local channels": 0,
        "Unsettled remote channels": 100,
        "On-chain confirmed": 30000,
        "On-chain unconfirmed": 1000,
    }
    assert sorted(calls) == [
        ("GET", "/v1/balance/blockchain"),
        ("GET", "/v1/balance/channels"),
    ]


@pytest.mark.asyncio
async def test_lnd_invalid_balances_do_not_expose_provider_data(tmp_path):
    def respond(request):
        if request.url.path == "/v1/balance/channels":
            return httpx.Response(200, json={"local_balance": {"sat": "secret"}})
        return httpx.Response(
            200, json={"confirmed_balance": "0", "unconfirmed_balance": "0"}
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(BackendError, match="invalid balance metadata") as error:
            await backend.get_balances()
        assert "secret" not in str(error.value)
    finally:
        await backend.close()


def test_startup_logs_operator_balances_once_without_publishing_them(tmp_path, caplog):
    class Backend(FakeBackend):
        name = "spark"
        network = "mainnet"
        calls = 0

        async def get_balances(self):
            self.calls += 1
            return {"Spark wallet": 10000}

    backend = Backend(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(create_app(mint, recovery=False, report_balances=True)) as http:
            for _ in range(2):
                info = http.get("/v1/info")
                assert info.status_code == 200
                assert "balance" not in info.text
    assert backend.calls == 1
    assert "Backend balances (spark, mainnet):" in caplog.text
    assert "Spark wallet: 10,000 sats" in caplog.text


def test_balance_lookup_failure_does_not_stop_startup_or_log_secrets(tmp_path, caplog):
    class Backend(FakeBackend):
        async def get_balances(self):
            raise RuntimeError("secret wallet credentials")

    backend = Backend(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(create_app(mint, recovery=False, report_balances=True)) as http:
            assert http.get("/v1/info").status_code == 200
    assert "Balance unavailable; continuing mint startup" in caplog.text
    assert "secret" not in caplog.text


def test_balance_lookup_timeout_is_bounded_and_startup_continues(
    tmp_path, monkeypatch, caplog
):
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout):
        assert timeout == 15
        return await original_wait_for(awaitable, timeout=0.01)

    class Backend(FakeBackend):
        cancelled = False

        async def get_balances(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    monkeypatch.setattr("cashu.api.asyncio.wait_for", fast_wait_for)
    backend = Backend(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(create_app(mint, recovery=False, report_balances=True)) as http:
            assert http.get("/v1/info").status_code == 200
    assert backend.cancelled
    assert "Balance unavailable; continuing mint startup" in caplog.text


def test_fake_startup_does_not_present_backing_as_real_wallet_funds(service, caplog):
    service.fund(100)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(
            create_app(service.mint, recovery=False, report_balances=True)
        ) as http:
            assert http.get("/v1/info").status_code == 200
    assert "Backend balances (fake):" in caplog.text
    assert "No real wallet balance (simulated backend)" in caplog.text
    assert "100 sats" not in caplog.text
