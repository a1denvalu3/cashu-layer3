import asyncio
import copy
import hashlib
import os
import time
from uuid import uuid4

import pytest
from bolt11 import TagChar, decode, encode
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client, ClientError
from cashu.db import BackendError
from cashu.lightning import FakeBackend, PaymentState
from cashu.mint import Mint
from cashu.models import InvoiceState

breez = pytest.importorskip("breez_sdk_spark")
from cashu.sparkl2 import (  # noqa: E402 - optional test dependency
    SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA,
    SparkBackend,
    SparkConnection,
)

MNEMONIC = "abandon " * 11 + "about"


@pytest.mark.asyncio
async def test_spark_operator_balance_uses_synchronized_sdk_info(spark, monkeypatch):
    backend, sdk = spark

    async def get_info(request):
        assert request.ensure_synced is True
        return breez.GetInfoResponse(
            identity_pubkey="02" + "11" * 32,
            balance_sats=10000,
            token_balances={},
        )

    monkeypatch.setattr(sdk, "get_info", get_info, raising=False)
    assert await backend.get_balances() == {"Spark wallet": 10000}
    assert not sdk.claims and not sdk.sends


@pytest.mark.asyncio
async def test_spark_balance_failure_does_not_expose_sdk_secrets(spark, monkeypatch):
    backend, sdk = spark

    async def get_info(request):
        raise RuntimeError("secret SDK credentials")

    monkeypatch.setattr(sdk, "get_info", get_info, raising=False)
    with pytest.raises(BackendError, match="Spark balance lookup failed") as error:
        await backend.get_balances()
    assert "secret" not in str(error.value)


def sdk_payment(
    request,
    direction,
    status=breez.PaymentStatus.PENDING,
    preimage=None,
    fee=0,
    payment_id=None,
    deadline=None,
):
    invoice = decode(request)
    return breez.Payment(
        id=payment_id or str(uuid4()),
        payment_type=direction,
        status=status,
        amount=int(invoice.amount_msat) // 1000,
        fees=fee,
        timestamp=int(time.time()),
        method=breez.PaymentMethod.LIGHTNING,
        conversion_details=None,
        details=breez.PaymentDetails.LIGHTNING(
            description="test",
            invoice=request,
            destination_pubkey="02" + "11" * 32,
            htlc_details=breez.SparkHtlcDetails(
                payment_hash=invoice.payment_hash,
                preimage=preimage.hex() if preimage else None,
                expiry_time=deadline
                if deadline is not None
                else int(time.time()) + 900,
                status=(
                    breez.SparkHtlcStatus.PREIMAGE_SHARED
                    if preimage
                    else breez.SparkHtlcStatus.WAITING_FOR_PREIMAGE
                ),
            ),
            lnurl_pay_info=None,
            lnurl_withdraw_info=None,
            lnurl_receive_metadata=None,
            conversion_info=None,
        ),
    )


def sdk_prepared(request, fee=0):
    invoice = decode(request)
    return breez.PrepareSendPaymentResponse(
        payment_method=breez.SendPaymentMethod.BOLT11_INVOICE(
            invoice_details=breez.Bolt11InvoiceDetails(
                amount_msat=int(invoice.amount_msat),
                description="test",
                description_hash=None,
                expiry=invoice.expiry,
                invoice=breez.Bolt11Invoice(
                    bolt11=request,
                    source=breez.PaymentRequestSource(
                        bip_21_uri=None, bip_353_address=None
                    ),
                ),
                min_final_cltv_expiry_delta=18,
                network=breez.BitcoinNetwork.REGTEST,
                payee_pubkey="02" + "11" * 32,
                payment_hash=invoice.payment_hash,
                payment_secret="11" * 32,
                routing_hints=[],
                timestamp=invoice.date,
            ),
            spark_transfer_fee_sats=0,
            lightning_fee_sats=fee,
        ),
        amount=int(invoice.amount_msat) // 1000,
        token_identifier=None,
        conversion_estimate=None,
        fee_policy=breez.FeePolicy.FEES_EXCLUDED,
    )


class SdkStub:
    """Provider/cache separation models losing a send before SDK persistence.

    Requests and responses use the actual pinned SDK's Python models.
    """

    def __init__(self):
        self.payments = []
        self.invoices = {}
        self.provider = {}
        self.receivers = {}
        self.claims = []
        self.sends = []
        self.fee = 0
        self.lose_response = False
        self.lose_cache = False
        self.fail = False
        self.sync_error = False
        self.pages = []
        self.cltv_delta = SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA

    async def sync_wallet(self, request):
        if self.sync_error:
            raise RuntimeError("sensitive SDK error")

    async def list_payments(self, request):
        self.pages.append(request.offset)
        payments = [p for p in self.payments if p.payment_type in request.type_filter]
        return breez.ListPaymentsResponse(
            payments=payments[request.offset : request.offset + request.limit]
        )

    async def receive_payment(self, request):
        method = request.payment_method
        assert isinstance(method, breez.ReceivePaymentMethod.BOLT11_INVOICE)
        assert method.payment_hash is not None
        invoice = FakeBackend._invoice(
            method.amount_sats,
            method.payment_hash,
            method.expiry_secs,
            method.description,
        )
        decoded = decode(invoice)
        decoded.tags.add(TagChar.min_final_cltv_expiry, self.cltv_delta)
        invoice = encode(decoded, private_key="11" * 32)
        self.invoices[method.payment_hash] = invoice
        return breez.ReceivePaymentResponse(payment_request=invoice, fee=0)

    def fund(self, payment_hash, deadline=None):
        payment = sdk_payment(
            self.invoices[payment_hash], breez.PaymentType.RECEIVE, deadline=deadline
        )
        self.payments.insert(0, payment)
        return payment

    async def claim_htlc_payment(self, request):
        self.claims.append(request.preimage)
        payment_hash = hashlib.sha256(bytes.fromhex(request.preimage)).hexdigest()
        payment = next(
            p
            for p in self.payments
            if p.details.htlc_details.payment_hash == payment_hash
        )
        payment.status = breez.PaymentStatus.COMPLETED
        payment.details.htlc_details.status = breez.SparkHtlcStatus.PREIMAGE_SHARED
        payment.details.htlc_details.preimage = request.preimage
        return breez.ClaimHtlcPaymentResponse(payment=payment)

    async def prepare_send_payment(self, request):
        return sdk_prepared(request.payment_request.input, self.fee)

    async def send_payment(self, request):
        assert request.options.prefer_spark is False
        self.sends.append(
            (
                request.idempotency_key,
                request.prepare_response.payment_method.lightning_fee_sats,
            )
        )
        payment = self.provider.get(request.idempotency_key)
        if payment is None:
            invoice = (
                request.prepare_response.payment_method.invoice_details.invoice.bolt11
            )
            payment = sdk_payment(
                invoice,
                breez.PaymentType.SEND,
                status=breez.PaymentStatus.FAILED
                if self.fail
                else breez.PaymentStatus.COMPLETED,
                preimage=None
                if self.fail
                else self.receivers[decode(invoice).payment_hash],
                fee=request.prepare_response.payment_method.lightning_fee_sats,
                payment_id=request.idempotency_key,
            )
            self.provider[request.idempotency_key] = payment
        if not self.lose_cache and payment not in self.payments:
            self.payments.insert(0, payment)
        if self.lose_response:
            raise RuntimeError("sensitive SDK error")
        return breez.SendPaymentResponse(payment=payment)

    async def disconnect(self):
        pass


@pytest.fixture
def spark(tmp_path):
    backend = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC)
    sdk = SdkStub()
    backend.sdk = sdk
    return backend, sdk


def receiver(sdk, amount=10):
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    request = FakeBackend._invoice(amount, payment_hash, 3600, "receiver")
    sdk.receivers[payment_hash] = preimage
    return request, payment_hash, preimage


async def held(spark, amount=1000, expiry=3600):
    backend, sdk = spark
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    invoice = await backend.create_hold_invoice(amount, payment_hash, expiry)
    assert invoice.state == InvoiceState.open
    payment = sdk.fund(payment_hash)
    return preimage, payment_hash, payment


async def test_funding_is_accepted_without_revealing_preimage(spark):
    backend, sdk = spark
    preimage, payment_hash, payment = await held(spark)
    invoice = await backend.get_invoice(payment_hash)
    assert invoice.state == InvoiceState.accepted
    assert invoice.amount_paid == 1000
    assert invoice.hold_expires_at == payment.details.htlc_details.expiry_time
    assert invoice.hold_expires_at != invoice.expiry
    assert await backend.get_block_height() is None
    assert sdk.claims == []
    await backend.settle_invoice(preimage)
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.settled
    await backend.settle_invoice(preimage)
    assert sdk.claims == [preimage.hex()]


def test_mainnet_cltv_policy_is_published_without_inventing_token_lifetime(
    spark, tmp_path
):
    backend, _ = spark
    mint = Mint(tmp_path / "mint.sqlite3", os.urandom(32), backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        info = http.get("/v1/info").json()
    assert info["name"] == "cashu3"
    assert info["funding_htlc_cltv_delta"] == 60
    assert info["default_token_lifetime_blocks"] is None


@pytest.mark.parametrize("delta", [18, 59, 61, 120])
async def test_mainnet_invoice_policy_change_is_rejected_before_funding(spark, delta):
    backend, sdk = spark
    sdk.cltv_delta = delta
    with pytest.raises(BackendError, match="CLTV policy changed"):
        await held(spark)
    with backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    assert sdk.claims == [] and sdk.payments == []


@pytest.mark.parametrize("location", ["database", "sdk"])
async def test_existing_mainnet_invoice_cannot_bypass_cltv_policy(spark, location):
    backend, _ = spark
    _, payment_hash, payment = await held(spark)
    with backend.db.transaction() as conn:
        invoice = decode(conn.execute("SELECT request FROM invoices").fetchone()[0])
        invoice.tags.get(TagChar.min_final_cltv_expiry).data = 59
        changed = encode(invoice, private_key="11" * 32)
        if location == "database":
            conn.execute("UPDATE invoices SET request=?", (changed,))
        else:
            payment.details.invoice = changed
    with pytest.raises(BackendError, match="CLTV policy changed"):
        await backend.get_invoice(payment_hash)
    with pytest.raises(BackendError, match="CLTV policy changed"):
        await backend.create_hold_invoice(1000, payment_hash, 3600)


async def test_private_regtest_provider_has_its_own_cltv_policy(tmp_path):
    connection = SparkConnection(
        coordinator_identifier="operator",
        threshold=1,
        signing_operators=[
            {
                "id": 0,
                "identifier": "operator",
                "address": "https://operator.test",
                "identity_public_key": "02" + "11" * 32,
            }
        ],
        ssp_url="http://ssp.test",
        ssp_identity_public_key="02" + "11" * 32,
        esplora_url="http://esplora.test",
    )
    backend = SparkBackend(
        tmp_path / "spark.sqlite3", MNEMONIC, network="regtest", connection=connection
    )
    sdk = SdkStub()
    sdk.cltv_delta = 18
    backend.sdk = sdk
    _, payment_hash, _ = await held((backend, sdk))
    invoice = await backend.get_invoice(payment_hash)
    assert decode(invoice.request).min_final_cltv_expiry == 18
    assert backend.funding_htlc_cltv_delta is None
    assert invoice.state == InvoiceState.accepted


async def test_invoice_creation_is_persistent_and_amount_bound(spark, tmp_path):
    backend, sdk = spark
    _, payment_hash, _ = await held(spark)
    original = await backend.get_invoice(payment_hash)
    reopened = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC)
    reopened.sdk = sdk
    assert await reopened.create_hold_invoice(1000, payment_hash, 20) == original
    with pytest.raises(BackendError, match="another invoice amount"):
        await reopened.create_hold_invoice(10, payment_hash, 20)
    with pytest.raises(ValueError, match="another wallet"):
        SparkBackend(tmp_path / "spark.sqlite3", "legal " * 12)


@pytest.mark.parametrize(
    "change", ["amount", "missing_expiry", "unknown_state", "wrong_invoice"]
)
async def test_invalid_backing_fails_closed(spark, change):
    backend, _ = spark
    _, payment_hash, payment = await held(spark)
    if change == "amount":
        payment.amount = 10
    elif change == "missing_expiry":
        payment.details.htlc_details.expiry_time = 0
    elif change == "unknown_state":
        payment.status = "unknown"
    else:
        payment.details.invoice = FakeBackend._invoice(1000, "11" * 32, 3600, "wrong")
    with pytest.raises(BackendError):
        await backend.get_invoice(payment_hash)


@pytest.mark.parametrize("closure", ["expired", "returned", "preimage_shared"])
async def test_closed_holds_cannot_be_revived_by_stale_sdk_cache(spark, closure):
    backend, _ = spark
    _, payment_hash, payment = await held(spark)
    htlc = payment.details.htlc_details
    if closure == "expired":
        htlc.expiry_time = int(time.time()) - 1
    elif closure == "returned":
        htlc.status = breez.SparkHtlcStatus.RETURNED
    else:
        htlc.status = breez.SparkHtlcStatus.PREIMAGE_SHARED
    closed = await backend.get_invoice(payment_hash)
    assert closed.state in {InvoiceState.canceled, InvoiceState.settled}
    htlc.expiry_time = int(time.time()) + 900
    htlc.status = breez.SparkHtlcStatus.WAITING_FOR_PREIMAGE
    assert (await backend.get_invoice(payment_hash)).state == closed.state


@pytest.mark.parametrize("elapsed", [0, 61])
async def test_missing_accepted_hold_is_uncertain_across_restart(
    spark, tmp_path, monkeypatch, elapsed
):
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    backend, sdk = spark
    _, payment_hash, payment = await held(spark, expiry=60)
    accepted = await backend.get_invoice(payment_hash)
    assert accepted.state == InvoiceState.accepted
    sdk.payments.clear()
    now += elapsed  # The unpaid invoice can expire before the funded HTLC does.

    reopened = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC)
    reopened.sdk = sdk
    with pytest.raises(BackendError, match="backing lookup unavailable"):
        await reopened.get_invoice(payment_hash)

    sdk.payments.append(payment)
    assert await reopened.get_invoice(payment_hash) == accepted


async def test_unseen_funded_hold_is_not_canceled_at_unpaid_invoice_expiry(
    spark, monkeypatch
):
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    backend, sdk = spark
    _, payment_hash, payment = await held(spark, expiry=60)
    # Funding succeeded, but synchronization has not exposed the receive yet.
    sdk.payments.clear()
    now += 61
    with pytest.raises(BackendError, match="funding state unavailable"):
        await backend.get_invoice(payment_hash)
    sdk.payments.append(payment)
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.accepted


async def test_missing_hold_expires_at_observed_funded_deadline(spark, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    backend, sdk = spark
    _, payment_hash, payment = await held(spark, expiry=60)
    accepted = await backend.get_invoice(payment_hash)
    sdk.payments.clear()
    now = accepted.hold_expires_at
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.canceled

    # Reintroducing a stale, apparently live SDK entry cannot revive the hold.
    payment.details.htlc_details.expiry_time = now + 900
    sdk.payments.append(payment)
    assert (await backend.get_invoice(payment_hash)).state == InvoiceState.canceled


async def test_delayed_sdk_snapshot_cannot_override_confirmed_hold_closure(
    spark, monkeypatch
):
    backend, sdk = spark
    _, payment_hash, payment = await held(spark)
    await backend.get_invoice(payment_hash)
    stale = copy.deepcopy(payment)
    snapshot_waiting = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def list_payments(request):
        if not snapshot_waiting.is_set():
            snapshot_waiting.set()
            await release_snapshot.wait()
            return breez.ListPaymentsResponse(payments=[stale])
        return breez.ListPaymentsResponse(payments=[payment])

    monkeypatch.setattr(sdk, "list_payments", list_payments)
    delayed_lookup = asyncio.create_task(backend.get_invoice(payment_hash))
    await snapshot_waiting.wait()
    payment.details.htlc_details.status = breez.SparkHtlcStatus.RETURNED
    try:
        closed = await backend.get_invoice(payment_hash)
        assert closed.state == InvoiceState.canceled
    finally:
        release_snapshot.set()
    assert (await delayed_lookup).state == InvoiceState.canceled


@pytest.mark.parametrize("elapsed", [0, 61])
def test_missing_sdk_hold_does_not_publish_an_empty_live_backing_list(
    spark, tmp_path, monkeypatch, elapsed
):
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    backend, sdk = spark
    mint = Mint(tmp_path / "mint.sqlite3", os.urandom(32), backend)
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        quote = client.quote(100, payment_hash, expiry=60)
        payment = sdk.fund(payment_hash)
        token = client.finish_mint(client.prepare_mint(quote.quote, preimage.hex()))
        assert len(client.pending_htlcs().htlcs) == 1
        sdk.payments.clear()
        now += elapsed
        assert http.get("/v1/htlcs/pending").status_code == 503

        sdk.payments.append(payment)
        client.check_backing(token)
        assert len(client.pending_htlcs().htlcs) == 1


async def test_sync_errors_do_not_masquerade_as_missing_payment(spark):
    backend, sdk = spark
    sdk.sync_error = True
    with pytest.raises(BackendError, match="lookup failed") as exc:
        await backend.get_payment("11" * 32)
    assert "sensitive" not in str(exc.value)


async def test_payment_lookup_checks_all_pages(spark):
    backend, sdk = spark
    request, payment_hash, preimage = receiver(sdk)
    payment = sdk_payment(
        request, breez.PaymentType.SEND, breez.PaymentStatus.COMPLETED, preimage
    )
    sdk.payments = [replace_dummy(payment) for _ in range(100)] + [payment]
    assert (await backend.get_payment(payment_hash)).preimage == preimage
    assert sdk.pages == [0, 100]


def replace_dummy(payment):
    return sdk_payment(
        FakeBackend._invoice(10, os.urandom(32).hex(), 3600, "other"),
        payment.payment_type,
    )


async def test_fee_cap_is_enforced_before_send(spark):
    backend, sdk = spark
    request, _, preimage = receiver(sdk)
    sdk.fee = 2
    assert (await backend.pay_invoice(request, 1)).state == PaymentState.failed
    assert sdk.sends == []
    payment = await backend.pay_invoice(request, 2)
    assert payment.state == PaymentState.succeeded
    assert payment.fee_msat == 2000
    assert payment.preimage == preimage


@pytest.mark.parametrize("cache_saved", [True, False])
async def test_lost_send_response_recovers_after_restart(spark, tmp_path, cache_saved):
    backend, sdk = spark
    request, payment_hash, preimage = receiver(sdk)
    sdk.fee = 1
    sdk.lose_response = True
    sdk.lose_cache = not cache_saved
    with pytest.raises(BackendError, match="interrupted"):
        await backend.pay_invoice(request, 2)
    reopened = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC)
    reopened.sdk = sdk
    sdk.lose_response = False
    sdk.lose_cache = False
    sdk.fee = 9  # Recovery must preserve the original quote and transfer UUID.
    payment = await reopened.get_payment(payment_hash)
    assert payment.state == PaymentState.succeeded
    assert payment.preimage == preimage
    assert payment.fee_msat == 1000
    assert len(sdk.provider) == 1
    assert len(set(sdk.sends)) == 1
    sdk.sync_error = True
    assert await reopened.pay_invoice(request, 0) == payment


async def test_completed_send_without_preimage_remains_pending(spark):
    backend, sdk = spark
    request, payment_hash, preimage = receiver(sdk)
    result = await backend.pay_invoice(request)
    payment = sdk.provider[sdk.sends[0][0]]
    payment.details.htlc_details.preimage = None
    # Use a new backend receipt rather than the already confirmed one.
    with backend.db.transaction() as conn:
        conn.execute("UPDATE payments SET state='IN_FLIGHT',preimage=NULL")
    assert (await backend.get_payment(payment_hash)).state == PaymentState.pending
    payment.details.htlc_details.preimage = preimage.hex()
    assert await backend.get_payment(payment_hash) == result


@pytest.mark.parametrize("lost_failure", [False, True])
async def test_failed_attempt_retry_uses_new_uuid_and_ignores_old_failure(
    spark, lost_failure
):
    backend, sdk = spark
    request, payment_hash, _ = receiver(sdk)
    sdk.fail = True
    sdk.lose_response = lost_failure
    if lost_failure:
        with pytest.raises(BackendError):
            await backend.pay_invoice(request)
    else:
        assert (await backend.pay_invoice(request)).state == PaymentState.failed
    sdk.fail = False
    sdk.lose_response = True
    sdk.lose_cache = True
    with pytest.raises(BackendError):
        await backend.pay_invoice(request)
    sdk.lose_response = False
    sdk.lose_cache = False
    assert (await backend.get_payment(payment_hash)).state == PaymentState.succeeded
    assert len(sdk.provider) == 2


@pytest.mark.parametrize("mutation", ["bad_preimage", "wrong_hash", "negative_fee"])
async def test_malformed_payout_does_not_release_reserved_melt(spark, mutation):
    backend, sdk = spark
    request, payment_hash, preimage = receiver(sdk)
    payment = sdk_payment(
        request, breez.PaymentType.SEND, breez.PaymentStatus.COMPLETED, preimage
    )
    if mutation == "bad_preimage":
        payment.details.htlc_details.preimage = "invalid"
    elif mutation == "wrong_hash":
        payment.details.htlc_details.preimage = "11" * 32
    else:
        payment.fees = -1
    sdk.payments = [payment]
    with pytest.raises(BackendError):
        await backend.get_payment(payment_hash)


async def test_connection_reuses_one_sdk_and_disables_spark_fallback(
    tmp_path, monkeypatch
):
    backend = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC, api_key="test-key")
    requests = []
    sdk = SdkStub()

    async def connect(request):
        requests.append(request)
        await asyncio.sleep(0)
        return sdk

    monkeypatch.setattr(breez, "connect", connect)
    assert await asyncio.gather(backend._ensure_sdk(), backend._ensure_sdk()) == [
        sdk,
        sdk,
    ]
    assert len(requests) == 1
    assert requests[0].config.prefer_spark_over_lightning is False
    assert requests[0].config.api_key == "test-key"
    assert requests[0].seed.mnemonic == MNEMONIC
    await backend.close()
    assert backend.sdk is None


async def test_failed_connection_does_not_expose_sdk_secrets(tmp_path, monkeypatch):
    backend = SparkBackend(tmp_path / "spark.sqlite3", MNEMONIC)

    async def connect(request):
        raise RuntimeError("sensitive SDK error")

    monkeypatch.setattr(breez, "connect", connect)
    with pytest.raises(BackendError) as exc:
        await backend._ensure_sdk()
    assert str(exc.value) == "Spark SDK connection failed"


def test_combined_melt_settles_only_fully_consumed_backing(spark, tmp_path):
    backend, sdk = spark
    sdk.fee = 1
    mint = Mint(tmp_path / "mint.sqlite3", os.urandom(32), backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        tokens = []
        preimages = []
        for amount in (9, 7):
            preimage = os.urandom(32).hex()
            preimages.append(preimage)
            payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
            quote = client.quote(amount, payment_hash)
            sdk.fund(payment_hash)
            tokens.append(
                client.finish_mint(client.prepare_mint(quote.quote, preimage))
            )
        token = tokens[0].model_copy(
            update={"proofs": tokens[0].proofs + tokens[1].proofs}
        )
        payout, _, _ = receiver(sdk, 10)
        operation = client.prepare_melt(token, payout, fee_reserve=1)
        receipt = client.finish_melt(operation)
        change = client.melt_change(operation, receipt)
        assert change.amount == 5
        assert {proof.payment_hash for proof in change.proofs} == {
            tokens[1].proofs[0].payment_hash
        }
        assert sdk.claims == [preimages[0]]


def test_partial_melt_claims_at_exact_spark_cutoff_and_expires_remaining_tokens(
    spark, tmp_path, monkeypatch
):
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    backend, sdk = spark
    mint = Mint(tmp_path / "mint.sqlite3", os.urandom(32), backend)
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        quote = client.quote(1000, payment_hash)
        payment = sdk.fund(payment_hash)
        token = client.finish_mint(client.prepare_mint(quote.quote, preimage.hex()))
        payout, _, _ = receiver(sdk, 9)
        operation = client.prepare_melt(token, payout, fee_reserve=1)
        receipt = client.finish_melt(operation)
        change = client.melt_change(operation, receipt)
        assert receipt.fee == 0
        assert sum(p.amount for p in change.proofs) == 991
        assert sdk.claims == []
        entry = client.pending_htlcs().htlcs[0]
        assert entry.payment_hash == payment_hash
        assert (
            entry.melt_deadline
            == payment.details.htlc_details.expiry_time - mint.settle_margin_seconds
        )
        late_payout, _, _ = receiver(sdk, 1)
        late_melt = client.prepare_melt(change, late_payout, fee_reserve=0)

        now = entry.melt_deadline - 1
        asyncio.run(mint.reconcile_holds())
        assert sdk.claims == []
        client.check_backing(change)
        now += 1
        response = http.post("/v1/melt/bolt11", json=late_melt.request.model_dump())
        assert response.status_code == 409
        assert "deadline" in response.json()["detail"]
        asyncio.run(mint.reconcile_holds())
        assert sdk.claims == [preimage.hex()]
        assert client.pending_htlcs().htlcs == []
        with pytest.raises(ClientError):
            client.prepare_swap(change)
        with pytest.raises(ClientError):
            client.prepare_melt(change, late_payout, fee_reserve=0)
        response = http.post("/v1/melt/bolt11", json=late_melt.request.model_dump())
        assert response.status_code == 409
        assert "closed" in response.json()["detail"]
        assert len(sdk.sends) == 1
        now += 1
        asyncio.run(mint.reconcile_holds())
        assert sdk.claims == [preimage.hex()]


def test_full_melt_including_fee_claims_backing(spark, tmp_path):
    backend, sdk = spark
    sdk.fee = 1
    mint = Mint(tmp_path / "mint.sqlite3", os.urandom(32), backend)
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    with TestClient(create_app(mint, recovery=False)) as http:
        client = Client("http://testserver", http)
        quote = client.quote(16, payment_hash)
        sdk.fund(payment_hash)
        token = client.finish_mint(client.prepare_mint(quote.quote, preimage.hex()))
        payout, _, _ = receiver(sdk, 15)
        receipt = client.finish_melt(client.prepare_melt(token, payout, fee_reserve=1))
        assert receipt.fee == 1
        assert sdk.claims == [preimage.hex()]
