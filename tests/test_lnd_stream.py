import hashlib
import json

import httpx
import pytest
from conftest import PREIMAGE

from cashu.db import BackendError
from cashu.lightning import FakeBackend, LndRestBackend, PaymentState


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
async def test_payout_uses_router_stream_and_final_state(tmp_path, status):
    backend_db = FakeBackend(tmp_path / "fake.sqlite3")
    request = backend_db.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == "/v1/payments":
            return httpx.Response(200, json={"payments": [], "last_index_offset": "0"})
        assert http_request.method == "POST"
        assert http_request.url.path == "/v2/router/send"
        body = json.loads(http_request.content)
        assert body["payment_request"] == request
        assert body["fee_limit_sat"] == "10"
        assert body["no_inflight_updates"] is True
        # Intermediate updates remain safe even if LND disregards the flag.
        updates = [
            {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
            {
                "result": {
                    "payment_hash": payment_hash,
                    "status": status,
                    "payment_preimage": PREIMAGE.hex() if status == "SUCCEEDED" else "",
                }
            },
        ]
        return httpx.Response(200, content="\n".join(json.dumps(u) for u in updates))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(request)
    assert payment.state == (
        PaymentState.succeeded if status == "SUCCEEDED" else PaymentState.failed
    )
    assert payment.preimage == (PREIMAGE if status == "SUCCEEDED" else None)
    await backend.close()


@pytest.mark.asyncio
async def test_payout_stream_error_is_uncertain_until_tracking_resolves(tmp_path):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)

    def respond(http_request):
        if http_request.url.path == "/v1/payments":
            return httpx.Response(200, json={"payments": [], "last_index_offset": "0"})
        return httpx.Response(200, content=json.dumps({"error": {"code": 13}}))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(BackendError, match="retry to reconcile"):
        await backend.pay_invoice(request)
    await backend.close()
