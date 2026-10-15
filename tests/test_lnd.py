import base64
import hashlib
import json
import time

import httpx
import pytest
from bolt11 import decode, encode
from conftest import PREIMAGE
from fastapi.testclient import TestClient

from tmp_cash.api import create_app
from tmp_cash.db import BackendError
from tmp_cash.lightning import FakeBackend, LndRestBackend, PaymentState
from tmp_cash.mint import Mint
from tmp_cash.models import InvoiceState


def test_lnd_operator_funding_uses_standard_invoices_not_holds(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    invoice = FakeBackend._invoice(10000, payment_hash, 600, "operator funding")
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.method == "POST"
        assert request.url.path == "/v1/invoices"
        assert json.loads(request.content) == {
            "value": "10000",
            "expiry": "600",
            "memo": "tmp.cash operator liquidity",
        }
        return httpx.Response(
            200,
            json={
                "r_hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
                "payment_request": invoice,
                "add_index": "1",
            },
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    mint = Mint(tmp_path / "mint.sqlite3", b"m" * 32, backend)
    with TestClient(
        create_app(mint, recovery=False, admin_token="test-operator-secret")
    ) as http:
        response = http.post(
            "/v1/admin/fund",
            json={"amount": 10000, "expiry": 600},
            headers={"Authorization": "Bearer test-operator-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "amount": 10000,
            "unit": "sat",
            "request": invoice,
            "payment_hash": payment_hash,
            "expiry": decode(invoice).date + 600,
            "fee": 0,
        }
    assert calls == [("POST", "/v1/invoices")]
    with mint.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


@pytest.mark.parametrize(
    "invalid", ["missing", "hash", "amount", "expiry", "encoding", "provider_error"]
)
@pytest.mark.asyncio
async def test_lnd_operator_funding_rejects_bad_provider_data_without_leaking_it(
    tmp_path, invalid
):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    invoice = FakeBackend._invoice(
        10001 if invalid == "amount" else 10000,
        payment_hash,
        600,
        "operator funding",
    )
    if invalid == "expiry":
        decoded = decode(invoice)
        decoded.date -= 7200
        invoice = encode(decoded, private_key="11" * 32)
    body = {
        "r_hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
        "payment_request": invoice,
    }
    if invalid == "missing":
        del body["r_hash"]
    elif invalid == "hash":
        body["r_hash"] = base64.b64encode(bytes.fromhex("11" * 32)).decode()
    elif invalid == "encoding":
        body["payment_request"] = "secret invalid provider invoice"

    def respond(request):
        if invalid == "provider_error":
            return httpx.Response(503, json={"error": "secret backend credentials"})
        return httpx.Response(200, json=body)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(BackendError) as error:
            await backend.create_funding_invoice(10000)
        assert "secret" not in str(error.value)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_lnd_hold_uses_supplied_hash_and_accepted_is_not_settled(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    invoice = await fake.create_hold_invoice(1000, payment_hash, 3600)
    state = "OPEN"
    calls = []

    def respond(request):
        nonlocal state
        calls.append(request)
        if request.url.path == "/v2/invoices/hodl":
            import json

            body = json.loads(request.content)
            assert base64.b64decode(body["hash"]).hex() == payment_hash
            assert body["value"] == "1000"
            assert body["expiry"] == "3600"  # Invoice payment window, in seconds.
            assert body["cltv_expiry"] == "2040"  # Funding HTLC lifetime, in blocks.
            assert "preimage" not in body
            return httpx.Response(200, json={"payment_request": invoice.request})
        if request.url.path == f"/v1/invoice/{payment_hash}":
            return httpx.Response(
                200,
                json={
                    "r_hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
                    "value": "1000",
                    "payment_request": invoice.request,
                    "creation_date": str(int(time.time())),
                    "expiry": "3600",
                    "state": state,
                    "amt_paid_sat": "1000" if state != "OPEN" else "0",
                },
            )
        if request.url.path == "/v2/invoices/settle":
            import json

            assert base64.b64decode(json.loads(request.content)["preimage"]) == PREIMAGE
            state = "SETTLED"
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test",
        macaroon,
        transport=httpx.MockTransport(respond),
        cltv_expiry=2040,
    )
    created = await backend.create_hold_invoice(1000, payment_hash, 3600)
    assert created.state == InvoiceState.open
    state = "ACCEPTED"
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.accepted
    assert not any(c.url.path == "/v2/invoices/settle" for c in calls)
    await backend.settle_invoice(PREIMAGE)
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.settled
    await backend.close()


@pytest.mark.asyncio
async def test_lnd_payment_lookup_paginates_and_validates_preimage(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    offsets = []
    wrong = False

    def respond(request):
        offsets.append(request.url.params["index_offset"])
        if request.url.params["index_offset"] == "0":
            return httpx.Response(
                200,
                json={
                    "payments": [{"payment_hash": "00" * 32, "status": "FAILED"}],
                    "last_index_offset": "1000",
                },
            )
        return httpx.Response(
            200,
            json={
                "payments": [
                    {
                        "payment_hash": payment_hash,
                        "status": "SUCCEEDED",
                        "payment_preimage": "00" * 32 if wrong else PREIMAGE.hex(),
                        "fee_msat": "1501",
                    }
                ],
                "last_index_offset": "1001",
            },
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.get_payment(payment_hash)
    assert payment.state == PaymentState.succeeded
    assert payment.preimage == PREIMAGE
    assert payment.fee_msat == 1501
    assert offsets == ["0", "1000"]
    wrong = True
    with pytest.raises(BackendError, match="invalid preimage"):
        await backend.get_payment(payment_hash)
    await backend.close()
