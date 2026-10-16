import base64
import hashlib
import json

import httpx
import pytest
from conftest import PREIMAGE

from tmp_cash.db import BackendError
from tmp_cash.lightning import FakeBackend, LndRestBackend, PaymentState


def tracked_stream(*updates):
    return "\n".join(
        update if isinstance(update, str) else json.dumps(update) for update in updates
    )


def track_path(payment_hash):
    encoded = base64.urlsafe_b64encode(bytes.fromhex(payment_hash)).decode()
    return f"/v2/router/track/{encoded}"


NOT_FOUND = tracked_stream({"error": {"code": 5, "message": "payment not initiated"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
@pytest.mark.parametrize("fee_reserve", [None, 2, 0])
async def test_payout_uses_router_stream_and_final_state(tmp_path, status, fee_reserve):
    backend_db = FakeBackend(tmp_path / "fake.sqlite3")
    request = backend_db.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        assert http_request.method == "POST"
        assert http_request.url.path == "/v2/router/send"
        body = json.loads(http_request.content)
        assert body["payment_request"] == request
        assert body["fee_limit_sat"] == str(10 if fee_reserve is None else fee_reserve)
        assert body["no_inflight_updates"] is True
        assert "cltv_limit" not in body
        # Intermediate updates remain safe even if LND disregards the flag.
        updates = [
            {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
            {
                "result": {
                    "payment_hash": payment_hash,
                    "status": status,
                    "payment_preimage": PREIMAGE.hex() if status == "SUCCEEDED" else "",
                    "fee_msat": "1501" if fee_reserve != 0 else "0",
                }
            },
        ]
        return httpx.Response(200, content="\n".join(json.dumps(u) for u in updates))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(request, fee_reserve)
    assert payment.state == (
        PaymentState.succeeded if status == "SUCCEEDED" else PaymentState.failed
    )
    assert payment.preimage == (PREIMAGE if status == "SUCCEEDED" else None)
    assert payment.fee_msat == (
        1501 if status == "SUCCEEDED" and fee_reserve != 0 else 0
    )
    await backend.close()


@pytest.mark.asyncio
async def test_payout_passes_cltv_limit_to_sendpaymentv2(tmp_path):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        assert http_request.url.path == "/v2/router/send"
        body = json.loads(http_request.content)
        assert body["cltv_limit"] == "80"
        return httpx.Response(
            200,
            content=tracked_stream(
                {
                    "result": {
                        "payment_hash": payment_hash,
                        "status": "SUCCEEDED",
                        "payment_preimage": PREIMAGE.hex(),
                        "fee_msat": "0",
                    }
                }
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(
        request, cltv_limit=80, idempotency_key="melt-attempt-1"
    )
    assert payment.state == PaymentState.succeeded
    await backend.close()


@pytest.mark.asyncio
async def test_payout_accepts_and_ignores_idempotency_key(tmp_path):
    """LND dedupes by payment hash; an arbitrary key must not be sent or rejected."""
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        assert http_request.url.path == "/v2/router/send"
        body = json.loads(http_request.content)
        assert "idempotency_key" not in body
        return httpx.Response(
            200,
            content=tracked_stream(
                {
                    "result": {
                        "payment_hash": payment_hash,
                        "status": "SUCCEEDED",
                        "payment_preimage": PREIMAGE.hex(),
                        "fee_msat": "0",
                    }
                }
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(request, idempotency_key="not-a-uuid")
    assert payment.state == PaymentState.succeeded
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cltv_limit", [-1, True, "80"])
async def test_payout_rejects_invalid_cltv_limit_before_any_request(
    tmp_path, cltv_limit
):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    calls = []

    def respond(http_request):
        calls.append(http_request.url.path)
        return httpx.Response(500, json={"error": "unexpected request"})

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(ValueError, match="CLTV limit"):
        await backend.pay_invoice(request, cltv_limit=cltv_limit)
    assert calls == []
    await backend.close()


@pytest.mark.asyncio
async def test_fake_backend_accepts_and_ignores_cltv_limit(tmp_path):
    backend_db = FakeBackend(tmp_path / "fake.sqlite3")
    request = backend_db.receiver_invoice(1000, PREIMAGE)
    payment = await backend_db.pay_invoice(
        request, cltv_limit=80, idempotency_key="melt-attempt-1"
    )
    assert payment.state == PaymentState.succeeded
    assert payment.preimage == PREIMAGE


@pytest.mark.asyncio
async def test_payout_returns_tracked_payment_without_resending(tmp_path):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    sends = []

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(
                200,
                content=tracked_stream(
                    {
                        "result": {
                            "payment_hash": payment_hash,
                            "status": "SUCCEEDED",
                            "payment_preimage": PREIMAGE.hex(),
                            "fee_msat": "1501",
                        }
                    }
                ),
            )
        sends.append(http_request.url.path)
        return httpx.Response(500, json={"error": "unexpected send"})

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(request)
    assert payment.state == PaymentState.succeeded
    assert payment.preimage == PREIMAGE
    assert payment.fee_msat == 1501
    assert sends == []
    await backend.close()


@pytest.mark.asyncio
async def test_payout_stream_error_is_uncertain_until_tracking_resolves(tmp_path):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        return httpx.Response(200, content=json.dumps({"error": {"code": 13}}))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(BackendError, match="retry to reconcile"):
        await backend.pay_invoice(request)
    await backend.close()


class QuietStream(httpx.AsyncByteStream):
    """Yields the given lines, then hangs the read like a quiet LND stream."""

    def __init__(self, *lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line.encode()
        raise httpx.ReadTimeout("stream went quiet")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_quiet_track_read_timeout_after_pending_update_returns_pending(
    tmp_path,
):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    update = json.dumps(
        {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}}
    )

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, http_request):
            assert http_request.url.path == track_path(payment_hash)
            return httpx.Response(
                200, stream=QuietStream(update + "\n"), request=http_request
            )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend("https://lnd.test", macaroon, transport=Transport())
    payment = await backend.get_payment(payment_hash)
    assert payment is not None
    assert payment.state == PaymentState.pending
    await backend.close()


@pytest.mark.asyncio
async def test_quiet_track_read_timeout_without_updates_is_unknown(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, http_request):
            assert http_request.url.path == track_path(payment_hash)
            return httpx.Response(200, stream=QuietStream(), request=http_request)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend("https://lnd.test", macaroon, transport=Transport())
    assert await backend.get_payment(payment_hash) is None
    await backend.close()


@pytest.mark.asyncio
async def test_quiet_send_read_timeout_remains_uncertain(tmp_path):
    """A send-stream read timeout must never report a state, even a seen one."""
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    update = json.dumps(
        {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}}
    )

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, http_request):
            if http_request.url.path == track_path(payment_hash):
                return httpx.Response(200, content=NOT_FOUND, request=http_request)
            assert http_request.url.path == "/v2/router/send"
            return httpx.Response(
                200, stream=QuietStream(update + "\n"), request=http_request
            )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend("https://lnd.test", macaroon, transport=Transport())
    with pytest.raises(BackendError, match="retry to reconcile"):
        await backend.pay_invoice(request)
    await backend.close()


@pytest.mark.asyncio
async def test_payout_stream_error_with_tracked_failure_is_ambiguous(tmp_path):
    """A stale tracked FAILED after a send error must not signal a refund."""
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(
                200,
                content=tracked_stream(
                    {"result": {"payment_hash": payment_hash, "status": "FAILED"}}
                ),
            )
        return httpx.Response(200, content=json.dumps({"error": {"code": 13}}))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(BackendError, match="retry to reconcile"):
        await backend.pay_invoice(request)
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCEEDED", "IN_FLIGHT"])
async def test_payout_stream_error_returns_tracked_success_or_pending(tmp_path, status):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    result = {
        "payment_hash": payment_hash,
        "status": status,
        "payment_preimage": PREIMAGE.hex() if status == "SUCCEEDED" else "",
        "fee_msat": "1501",
    }

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=tracked_stream({"result": result}))
        return httpx.Response(200, content=json.dumps({"error": {"code": 13}}))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.pay_invoice(request)
    if status == "SUCCEEDED":
        assert payment.state == PaymentState.succeeded
        assert payment.preimage == PREIMAGE
        assert payment.fee_msat == 1501
    else:
        assert payment.state == PaymentState.pending
        assert payment.preimage is None
    await backend.close()


@pytest.mark.asyncio
async def test_payout_stream_deadline_is_uncertain(tmp_path):
    """Heartbeats must not extend the send stream beyond the total deadline."""
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        assert http_request.url.path == "/v2/router/send"
        return httpx.Response(
            200,
            content=tracked_stream(
                "",
                {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    backend.stream_timeout = 0  # Deadline already passed at the first update.
    with pytest.raises(BackendError, match="retry to reconcile"):
        await backend.pay_invoice(request)
    await backend.close()


@pytest.mark.asyncio
async def test_payout_stream_rejects_wrong_payment_and_bad_preimage(tmp_path):
    fake = FakeBackend(tmp_path / "fake.sqlite3")
    request = fake.receiver_invoice(1000, PREIMAGE)
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    updates = [
        {
            "result": {
                "payment_hash": "11" * 32,
                "status": "SUCCEEDED",
                "payment_preimage": PREIMAGE.hex(),
            }
        },
        {
            "result": {
                "payment_hash": payment_hash,
                "status": "SUCCEEDED",
                "payment_preimage": "00" * 32,
            }
        },
    ]

    def respond(http_request):
        if http_request.url.path == track_path(payment_hash):
            return httpx.Response(200, content=NOT_FOUND)
        assert http_request.url.path == "/v2/router/send"
        return httpx.Response(200, content=tracked_stream(updates.pop(0)))

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(BackendError, match="wrong payment"):
        await backend.pay_invoice(request)
    with pytest.raises(BackendError, match="invalid preimage"):
        await backend.pay_invoice(request)
    await backend.close()
