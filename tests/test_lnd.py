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


def tracked_stream(*updates):
    return "\n".join(
        update if isinstance(update, str) else json.dumps(update) for update in updates
    )


def track_path(payment_hash):
    encoded = base64.urlsafe_b64encode(bytes.fromhex(payment_hash)).decode()
    return f"/v2/router/track/{encoded}"


@pytest.mark.asyncio
async def test_lnd_payment_tracking_uses_trackv2_and_validates_preimage(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    wrong = False
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.url.path == track_path(payment_hash)
        return httpx.Response(
            200,
            content=tracked_stream(
                {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
                {
                    "result": {
                        "payment_hash": payment_hash,
                        "status": "SUCCEEDED",
                        "payment_preimage": "00" * 32 if wrong else PREIMAGE.hex(),
                        "fee_msat": "1501",
                    }
                },
            ),
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
    assert paths == [track_path(payment_hash)]
    wrong = True
    with pytest.raises(BackendError, match="invalid preimage"):
        await backend.get_payment(payment_hash)
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["INITIATED", "IN_FLIGHT"])
async def test_lnd_payment_tracking_maps_live_statuses_to_pending(tmp_path, status):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        # The track stream stays open for live payments; LND closes it only at
        # a terminal state. Ending the body simulates a dropped connection.
        return httpx.Response(
            200,
            content=tracked_stream(
                {"result": {"payment_hash": payment_hash, "status": status}}
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.get_payment(payment_hash)
    assert payment is not None
    assert payment.state == PaymentState.pending
    assert payment.preimage is None
    await backend.close()


@pytest.mark.asyncio
async def test_lnd_payment_tracking_maps_failed(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        return httpx.Response(
            200,
            content=tracked_stream(
                {"result": {"payment_hash": payment_hash, "status": "INITIATED"}},
                {"result": {"payment_hash": payment_hash, "status": "FAILED"}},
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.get_payment(payment_hash)
    assert payment is not None
    assert payment.state == PaymentState.failed
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("not_found", ["stream_error", "http_404"])
async def test_lnd_payment_tracking_not_found_is_unknown(tmp_path, not_found):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        if not_found == "http_404":
            return httpx.Response(404, json={"code": 5, "message": "not found"})
        return httpx.Response(
            200, content=tracked_stream({"error": {"code": 5, "message": "no payment"}})
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    assert await backend.get_payment(payment_hash) is None
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        "unknown_status",
        "invalid_json",
        "wrong_hash",
        "non_hex_preimage",
        "short_preimage",
        "negative_fee",
        "non_numeric_fee",
        "missing_status",
        "stream_error",
    ],
)
async def test_lnd_payment_tracking_wraps_malformed_data(tmp_path, malformed):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()
    succeeded = {
        "payment_hash": payment_hash,
        "status": "SUCCEEDED",
        "payment_preimage": PREIMAGE.hex(),
        "fee_msat": "1501",
    }
    if malformed == "unknown_status":
        body = tracked_stream(
            {"result": {"payment_hash": payment_hash, "status": "UNKNOWN"}}
        )
    elif malformed == "invalid_json":
        body = '{"result": {"payment_hash": '  # Not valid JSON.
    elif malformed == "wrong_hash":
        body = tracked_stream({"result": {**succeeded, "payment_hash": "11" * 32}})
    elif malformed == "non_hex_preimage":
        body = tracked_stream(
            {"result": {**succeeded, "payment_preimage": "not hex secret"}}
        )
    elif malformed == "short_preimage":
        body = tracked_stream({"result": {**succeeded, "payment_preimage": "00" * 31}})
    elif malformed == "negative_fee":
        body = tracked_stream({"result": {**succeeded, "fee_msat": "-1"}})
    elif malformed == "non_numeric_fee":
        body = tracked_stream({"result": {**succeeded, "fee_msat": "secret"}})
    elif malformed == "missing_status":
        body = tracked_stream({"result": {"payment_hash": payment_hash}})
    else:
        body = tracked_stream({"error": {"code": 13, "message": "secret detail"}})

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        return httpx.Response(200, content=body)

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(BackendError) as error:
        await backend.get_payment(payment_hash)
    assert "secret" not in str(error.value)
    await backend.close()


@pytest.mark.asyncio
async def test_lnd_payment_tracking_deadline_ignores_heartbeats(tmp_path):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        # Heartbeats keep every individual read inside the HTTP timeout while
        # the definitive SUCCEEDED update never arrives within the deadline.
        return httpx.Response(
            200,
            content=tracked_stream(
                "",
                {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
                "",
                {
                    "result": {
                        "payment_hash": payment_hash,
                        "status": "SUCCEEDED",
                        "payment_preimage": PREIMAGE.hex(),
                        "fee_msat": "0",
                    }
                },
            ),
        )

    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    backend.stream_timeout = 0  # Deadline already passed at the first update.
    assert await backend.get_payment(payment_hash) is None
    await backend.close()


@pytest.mark.asyncio
async def test_lnd_payment_tracking_deadline_returns_last_seen_state(
    tmp_path, monkeypatch
):
    payment_hash = hashlib.sha256(PREIMAGE).hexdigest()

    def respond(request):
        assert request.url.path == track_path(payment_hash)
        return httpx.Response(
            200,
            content=tracked_stream(
                {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
                {"result": {"payment_hash": payment_hash, "status": "IN_FLIGHT"}},
            ),
        )

    calls = {"count": 0}

    def fake_monotonic():
        calls["count"] += 1
        return 100.0 if calls["count"] <= 2 else 100.0 + 3600

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    backend = LndRestBackend(
        "https://lnd.test", macaroon, transport=httpx.MockTransport(respond)
    )
    payment = await backend.get_payment(payment_hash)
    assert payment is not None
    assert payment.state == PaymentState.pending
    await backend.close()
