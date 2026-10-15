import asyncio
import hashlib
import os
import time

import pytest
from conftest import SEED
from fastapi.testclient import TestClient

from tmp_cash.api import create_app
from tmp_cash.lightning import FakeBackend, PaymentState
from tmp_cash.mint import Mint
from tmp_cash.models import OperatorFundingResponse

ADMIN_TOKEN = "test-only-operator-token-" + "a" * 32
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
ENDPOINT = "/v1/admin/fund"


@pytest.fixture
def funding_mint(tmp_path):
    class Backend(FakeBackend):
        name = "future-test-backend"
        calls = []

        async def create_funding_invoice(self, amount, expiry=3600):
            self.calls.append((amount, expiry))
            payment_hash = os.urandom(32).hex()
            return OperatorFundingResponse(
                amount=amount,
                request=self._invoice(amount, payment_hash, expiry, "operator funding"),
                payment_hash=payment_hash,
                expiry=int(time.time()) + expiry,
                fee=2,
            )

    return Mint(tmp_path / "mint.sqlite3", SEED, Backend(tmp_path / "fake.sqlite3"))


@pytest.mark.parametrize("token", [None, ""])
def test_operator_funding_is_disabled_without_a_configured_secret(funding_mint, token):
    with TestClient(
        create_app(funding_mint, recovery=False, admin_token=token)
    ) as http:
        for headers in ({}, AUTH):
            response = http.post(ENDPOINT, json={"amount": 10000}, headers=headers)
            assert response.status_code == 404
    assert funding_mint.backend.calls == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic credentials"}],
)
def test_operator_funding_authenticates_before_processing_the_request(
    funding_mint, headers
):
    with TestClient(
        create_app(funding_mint, recovery=False, admin_token=ADMIN_TOKEN)
    ) as http:
        # Even invalid request bodies must not bypass authentication.
        response = http.post(ENDPOINT, json={"amount": -1}, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert ADMIN_TOKEN not in response.text
    assert funding_mint.backend.calls == []


@pytest.mark.parametrize("expiry", [None, 600])
def test_operator_funding_creates_only_a_backend_invoice(funding_mint, expiry):
    payload = {"amount": 10000}
    if expiry is not None:
        payload["expiry"] = expiry
    with TestClient(
        create_app(funding_mint, recovery=False, admin_token=ADMIN_TOKEN)
    ) as http:
        response = http.post(ENDPOINT, json=payload, headers=AUTH)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        invoice = response.json()
        assert invoice["amount"] == 10000
        assert invoice["unit"] == "sat"
        assert invoice["request"].startswith("lnbcrt")
        assert invoice["fee"] == 2
        assert invoice["expiry"] > time.time()
        assert len(invoice["payment_hash"]) == 64
        assert funding_mint.backend.calls == [(10000, expiry or 3600)]
        assert http.get("/v1/htlcs/pending").json()["htlcs"] == []
    with funding_mint.db.read() as conn:
        for table in ("quotes", "nullifiers", "melts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"amount": 0},
        {"amount": -1},
        {"amount": True},
        {"amount": 1.5},
        {"amount": "10000"},
        {"amount": 2100000000000001},
        {"amount": 10000, "expiry": 59},
        {"amount": 10000, "expiry": 86401},
        {"amount": 10000, "expiry": True},
        {"amount": 10000, "payment_hash": "11" * 32},
    ],
)
def test_operator_funding_rejects_invalid_amounts_expiries_and_hold_parameters(
    funding_mint, payload
):
    with TestClient(
        create_app(funding_mint, recovery=False, admin_token=ADMIN_TOKEN)
    ) as http:
        assert http.post(ENDPOINT, json=payload, headers=AUTH).status_code == 422
    assert funding_mint.backend.calls == []


def test_operator_funding_is_not_advertised_in_public_api_or_schema(funding_mint):
    with TestClient(
        create_app(funding_mint, recovery=False, admin_token=ADMIN_TOKEN)
    ) as http:
        schema = http.get("/openapi.json").json()
        assert ENDPOINT not in schema["paths"]
        assert "OperatorFundingRequest" not in schema["components"]["schemas"]
        assert ENDPOINT not in http.get("/v1/info").text
        assert http.get(ENDPOINT, headers=AUTH).status_code == 405


def test_operator_funding_fake_invoice_can_be_paid_without_creating_a_hold(service):
    with TestClient(
        create_app(service.mint, recovery=False, admin_token=ADMIN_TOKEN)
    ) as http:
        response = http.post(ENDPOINT, json={"amount": 10000}, headers=AUTH)
    assert response.status_code == 200
    invoice = response.json()
    assert invoice["fee"] == 0
    payment = asyncio.run(service.backend.pay_invoice(invoice["request"]))
    assert payment.state == PaymentState.succeeded
    assert hashlib.sha256(payment.preimage).hexdigest() == invoice["payment_hash"]
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
