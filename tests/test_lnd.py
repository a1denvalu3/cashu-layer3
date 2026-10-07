import base64
import hashlib
import time

import httpx
import pytest
from conftest import PREIMAGE

from cashu.db import BackendError
from cashu.lightning import FakeBackend, LndRestBackend, PaymentState
from cashu.models import InvoiceState


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
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
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
    assert offsets == ["0", "1000"]
    wrong = True
    with pytest.raises(BackendError, match="invalid preimage"):
        await backend.get_payment(payment_hash)
    await backend.close()
