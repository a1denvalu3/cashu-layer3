"""Breez Spark hold invoices, adapted from Nutshell's SparkL2Wallet.

The optional SDK is imported only when this backend is selected. Incoming
preimages are supplied to the SDK only by settle_invoice, never by a melt.
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import breez_sdk_spark as breez
from bolt11 import decode
from pydantic import BaseModel, ConfigDict, Field

from .db import BackendError, Database
from .lightning import (
    HoldInvoiceBackend,
    Invoice,
    Payment,
    PaymentState,
    invoice_details,
)
from .models import InvoiceState, OperatorFundingRequest, OperatorFundingResponse

# Verified against unpaid HODL invoices from the SDK's default mainnet SSP on
# 2026-10-12. This is the invoice's minimum final-hop delta, not the funded
# Lightning expiry height or the Spark backing transfer's expiry timestamp.
SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA = 60
SPARK_SEND_PAYMENT_COMPLETION_TIMEOUT_SECONDS = 30


class SigningOperator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0)
    identifier: str
    address: str
    identity_public_key: str
    ca_cert_pem: str | None = None


class SparkConnection(BaseModel):
    """Optional operator/SSP configuration for a private regtest network."""

    model_config = ConfigDict(extra="forbid")
    coordinator_identifier: str
    threshold: int = Field(ge=1)
    signing_operators: list[SigningOperator] = Field(min_length=1)
    ssp_url: str
    ssp_identity_public_key: str
    schema_endpoint: str = "graphql/spark/rc"
    esplora_url: str


class SparkBackend(HoldInvoiceBackend):
    name = "spark"

    def __init__(
        self,
        path: Path,
        mnemonic: str,
        network: str = "mainnet",
        api_key: str | None = None,
        fee_limit_sat: int = 10,
        connection: SparkConnection | None = None,
    ):
        if network not in {"mainnet", "regtest"}:
            raise ValueError("Spark network must be mainnet or regtest")
        if not mnemonic.strip():
            raise ValueError("Spark needs a wallet mnemonic")
        if fee_limit_sat < 0:
            raise ValueError("Spark fee limit must not be negative")
        if network == "regtest" and connection is None:
            raise ValueError(
                "Spark regtest needs an explicit operator/SSP configuration"
            )
        if network != "regtest" and connection is not None:
            raise ValueError("custom Spark endpoints are supported only on regtest")
        self.mnemonic = " ".join(mnemonic.split())
        self.network = network
        self.api_key = api_key
        self.fee_limit_sat = fee_limit_sat
        self.connection = connection
        self.funding_htlc_cltv_delta = (
            SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA if network == "mainnet" else None
        )
        self.sdk: breez.BreezSdk | None = None
        self._connect_lock = asyncio.Lock()
        self._invoice_lock = asyncio.Lock()
        self._payment_lock = asyncio.Lock()
        self.storage_dir = path.parent / "spark-sdk"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = Database(path)
        identity = hashlib.sha256((network + "\0" + self.mnemonic).encode()).hexdigest()
        with self.db.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            previous = conn.execute(
                "SELECT value FROM metadata WHERE key='wallet'"
            ).fetchone()
            if previous is not None and previous["value"] != identity:
                raise ValueError("Spark database belongs to another wallet or network")
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('wallet',?)", (identity,)
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS invoices (payment_hash TEXT PRIMARY KEY, "
                "amount INTEGER NOT NULL, request TEXT NOT NULL, expiry INTEGER NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'OPEN')"
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(invoices)")
            }
            if "hold_expires_at" not in columns:
                conn.execute("ALTER TABLE invoices ADD COLUMN hold_expires_at INTEGER")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS payments (payment_hash TEXT PRIMARY KEY, "
                "request TEXT NOT NULL, fee_limit_sat INTEGER NOT NULL, "
                "idempotency_key TEXT NOT NULL, state TEXT NOT NULL, "
                "quoted_fee_sat INTEGER NOT NULL, preimage BLOB, "
                "fee_msat INTEGER NOT NULL DEFAULT 0)"
            )

    async def _ensure_sdk(self) -> breez.BreezSdk:
        async with self._connect_lock:
            if self.sdk is None:
                try:
                    config = breez.default_config(
                        breez.Network.MAINNET
                        if self.network == "mainnet"
                        else breez.Network.REGTEST
                    )
                    config.api_key = self.api_key
                    config.prefer_spark_over_lightning = False
                    if self.connection is not None:
                        spec = self.connection
                        config.lnurl_domain = None
                        config.real_time_sync_server_url = None
                        config.use_default_external_input_parsers = False
                        if config.spark_config is None:
                            raise BackendError("SDK has no Spark network configuration")
                        config.spark_config.coordinator_identifier = (
                            spec.coordinator_identifier
                        )
                        config.spark_config.threshold = spec.threshold
                        config.spark_config.signing_operators = [
                            breez.SparkSigningOperator(**operator.model_dump())
                            for operator in spec.signing_operators
                        ]
                        config.spark_config.ssp_config = breez.SparkSspConfig(
                            base_url=spec.ssp_url,
                            identity_public_key=spec.ssp_identity_public_key,
                            schema_endpoint=spec.schema_endpoint,
                        )
                    seed = breez.Seed.MNEMONIC(mnemonic=self.mnemonic, passphrase=None)
                    self.storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if self.connection is None:
                        self.sdk = await breez.connect(
                            breez.ConnectRequest(
                                config=config,
                                seed=seed,
                                storage_dir=str(self.storage_dir),
                            )
                        )
                    else:
                        breez.uniffi_set_event_loop(asyncio.get_running_loop())
                        builder = breez.SdkBuilder(config=config, seed=seed)
                        await builder.with_default_storage(str(self.storage_dir))
                        await builder.with_rest_chain_service(
                            self.connection.esplora_url,
                            breez.ChainApiType.ESPLORA,
                            None,
                        )
                        self.sdk = await builder.build()
                except Exception:
                    # SDK errors may contain seed material, invoices, or credentials.
                    raise BackendError("Spark SDK connection failed") from None
            return self.sdk

    async def _find_payments(
        self,
        payment_hash: str,
        direction: breez.PaymentType,
    ) -> list[breez.Payment]:
        """Collect every matching SDK record for the hash across all pages.

        A payment hash is not unique across send attempts: retrying an invoice
        with a fresh idempotency key leaves one record per attempt, so the
        newest record may shadow a completed or live older one.
        """
        sdk = await self._ensure_sdk()
        try:
            await sdk.sync_wallet(breez.SyncWalletRequest())
            matches = []
            offset = 0
            while True:
                response = await sdk.list_payments(
                    breez.ListPaymentsRequest(
                        type_filter=[direction],
                        asset_filter=breez.AssetFilter.BITCOIN(),
                        payment_details_filter=[
                            breez.PaymentDetailsFilter.LIGHTNING(None)
                        ],
                        sort_ascending=False,
                        limit=100,
                        offset=offset,
                    )
                )
                for payment in response.payments:
                    details = payment.details
                    if (
                        isinstance(details, breez.PaymentDetails.LIGHTNING)
                        and details.htlc_details.payment_hash == payment_hash
                    ):
                        if payment.payment_type != direction:
                            raise BackendError(
                                "Spark returned the wrong payment direction"
                            )
                        if invoice_details(details.invoice)[0] != payment_hash:
                            raise BackendError("Spark payment invoice/hash mismatch")
                        matches.append(payment)
                if len(response.payments) < 100:
                    return matches
                offset += len(response.payments)
        except BackendError:
            raise
        except Exception:
            raise BackendError(
                "Spark payment lookup failed; retry to reconcile"
            ) from None

    async def _find_payment(
        self,
        payment_hash: str,
        direction: breez.PaymentType,
    ) -> breez.Payment | None:
        matches = await self._find_payments(payment_hash, direction)
        return matches[0] if matches else None

    def _send_outcome(
        self, attempts: list[breez.Payment], payment_hash: str
    ) -> Payment:
        """Reconcile all send attempts for one hash.

        A valid completed attempt with a matching preimage wins; otherwise any
        live attempt means pending; failed is reported only when every matching
        attempt is definitively failed. Each candidate is validated before its
        status is accepted.
        """
        pending: Payment | None = None
        for attempt in attempts:
            result = self._payment(attempt, payment_hash)
            if result.state == PaymentState.succeeded:
                return result
            if result.state == PaymentState.pending:
                pending = result
        return pending if pending is not None else Payment(PaymentState.failed)

    async def create_funding_invoice(
        self, amount: int, expiry: int = 3600
    ) -> OperatorFundingResponse:
        parameters = OperatorFundingRequest(amount=amount, expiry=expiry)
        async with self._invoice_lock:
            sdk = await self._ensure_sdk()
            try:
                response = await sdk.receive_payment(
                    breez.ReceivePaymentRequest(
                        payment_method=breez.ReceivePaymentMethod.BOLT11_INVOICE(
                            description="tmp.cash operator liquidity",
                            amount_sats=parameters.amount,
                            expiry_secs=parameters.expiry,
                            # No supplied hash: the SDK owns the preimage and claims
                            # this payment normally, independently of ecash holds.
                            payment_hash=None,
                        )
                    )
                )
                request = response.payment_request
                payment_hash, invoice_amount = invoice_details(request)
                decoded = decode(request)
                if invoice_amount != amount:
                    raise BackendError("Spark funding invoice amount mismatch")
                currency = "bc" if self.network == "mainnet" else "bcrt"
                if decoded.currency != currency:
                    raise BackendError("Spark funding invoice network mismatch")
                expires_at = decoded.date + decoded.expiry
                if expires_at <= int(time.time()):
                    raise BackendError("Spark funding invoice has already expired")
                if type(response.fee) is not int or not 0 <= response.fee < amount:
                    raise BackendError("Spark funding invoice has invalid receive fees")
                return OperatorFundingResponse(
                    amount=amount,
                    request=request,
                    payment_hash=payment_hash,
                    expiry=expires_at,
                    fee=response.fee,
                )
            except BackendError:
                raise
            except Exception:
                raise BackendError("Spark funding invoice creation failed") from None

    async def create_hold_invoice(
        self, amount: int, payment_hash: str, expiry: int
    ) -> Invoice:
        async with self._invoice_lock:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT * FROM invoices WHERE payment_hash=?", (payment_hash,)
                ).fetchone()
            if row is not None:
                if row["amount"] != amount:
                    raise BackendError(
                        "payment hash already has another invoice amount"
                    )
            else:
                sdk = await self._ensure_sdk()
                try:
                    response = await sdk.receive_payment(
                        breez.ReceivePaymentRequest(
                            payment_method=breez.ReceivePaymentMethod.BOLT11_INVOICE(
                                description="tmp.cash hold",
                                amount_sats=amount,
                                expiry_secs=expiry,
                                payment_hash=payment_hash,
                            )
                        )
                    )
                    if response.fee != 0:
                        raise BackendError("Spark funding fees are not supported")
                    request = response.payment_request
                    if invoice_details(request) != (payment_hash, amount):
                        raise BackendError("Spark backing invoice/hash/amount mismatch")
                    decoded = decode(request)
                    self._validate_cltv(decoded.min_final_cltv_expiry)
                    with self.db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO invoices (payment_hash,amount,request,expiry) VALUES (?,?,?,?)",
                            (
                                payment_hash,
                                amount,
                                request,
                                decoded.date + decoded.expiry,
                            ),
                        )
                except BackendError:
                    raise
                except Exception:
                    raise BackendError("Spark hold invoice creation failed") from None
            return await self.get_invoice(payment_hash)

    def _validate_cltv(self, delta: int) -> None:
        if (
            self.funding_htlc_cltv_delta is not None
            and delta != self.funding_htlc_cltv_delta
        ):
            raise BackendError(
                "Spark mainnet invoice CLTV policy changed: "
                f"expected {self.funding_htlc_cltv_delta} blocks, received {delta}"
            )

    async def get_invoice(self, payment_hash: str) -> Invoice:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
        if row is None:
            raise BackendError("unknown Spark hold invoice")
        self._validate_cltv(decode(row["request"]).min_final_cltv_expiry)
        state = InvoiceState(row["state"])
        amount_paid = 0
        deadline = row["hold_expires_at"]
        if state not in {InvoiceState.settled, InvoiceState.canceled}:
            payment = await self._find_payment(payment_hash, breez.PaymentType.RECEIVE)
            if payment is None:
                if state == InvoiceState.accepted:
                    if deadline is None or deadline > int(time.time()):
                        raise BackendError(
                            "Spark accepted backing lookup unavailable; retry to reconcile"
                        )
                    state = InvoiceState.canceled
                elif row["expiry"] <= int(time.time()):
                    # The unpaid invoice's expiry cannot prove that a funded
                    # HTLC expired. The SDK may temporarily omit that receive.
                    raise BackendError(
                        "Spark funding state unavailable; retry to reconcile"
                    )
                else:
                    state = InvoiceState.open
            else:
                details = payment.details
                if not isinstance(details, breez.PaymentDetails.LIGHTNING):
                    raise BackendError("Spark backing is not a Lightning HTLC")
                if payment.amount != row["amount"] or invoice_details(
                    details.invoice
                ) != (payment_hash, row["amount"]):
                    raise BackendError("Spark backing amount does not match the hold")
                self._validate_cltv(decode(details.invoice).min_final_cltv_expiry)
                htlc = details.htlc_details
                deadline = htlc.expiry_time
                amount_paid = payment.amount
                if htlc.status == breez.SparkHtlcStatus.RETURNED:
                    state = InvoiceState.canceled
                elif (
                    htlc.status == breez.SparkHtlcStatus.PREIMAGE_SHARED
                    or payment.status == breez.PaymentStatus.COMPLETED
                ):
                    state = InvoiceState.settled
                elif payment.status == breez.PaymentStatus.FAILED:
                    state = InvoiceState.canceled
                elif (
                    payment.status == breez.PaymentStatus.PENDING
                    and htlc.status == breez.SparkHtlcStatus.WAITING_FOR_PREIMAGE
                    and not htlc.preimage
                ):
                    if deadline <= 0:
                        raise BackendError("accepted Spark HTLC expiry is unavailable")
                    state = (
                        InvoiceState.canceled
                        if deadline <= int(time.time())
                        else InvoiceState.accepted
                    )
                else:
                    raise BackendError("Spark returned an unknown backing state")
            with self.db.transaction() as conn:
                current = conn.execute(
                    "SELECT state,hold_expires_at FROM invoices WHERE payment_hash=?",
                    (payment_hash,),
                ).fetchone()
                current_state = InvoiceState(current["state"])
                if current_state in {InvoiceState.settled, InvoiceState.canceled}:
                    # A concurrent lookup may have confirmed closure while this
                    # lookup was awaiting an older SDK snapshot.
                    state = current_state
                    deadline = current["hold_expires_at"]
                elif (
                    state == InvoiceState.open
                    and current_state == InvoiceState.accepted
                ):
                    raise BackendError(
                        "Spark accepted backing lookup unavailable; retry to reconcile"
                    )
                elif state != InvoiceState.open:
                    conn.execute(
                        "UPDATE invoices SET state=?,hold_expires_at=? WHERE payment_hash=?",
                        (state.value, deadline, payment_hash),
                    )
        return Invoice(
            payment_hash=payment_hash,
            amount=row["amount"],
            request=row["request"],
            expiry=row["expiry"],
            state=state,
            amount_paid=amount_paid,
            hold_expires_at=deadline,
        )

    async def get_block_height(self) -> int | None:
        # Spark supplies the funded contract's expiry as a Unix timestamp.
        await self._ensure_sdk()
        return None

    async def settle_invoice(self, preimage: bytes) -> None:
        if len(preimage) != 32:
            raise ValueError("backing preimage must be 32 bytes")
        payment_hash = hashlib.sha256(preimage).hexdigest()
        invoice = await self.get_invoice(payment_hash)
        if invoice.state == InvoiceState.settled:
            return
        if invoice.state != InvoiceState.accepted:
            raise BackendError("Spark hold invoice is not accepted")
        sdk = await self._ensure_sdk()
        try:
            await sdk.claim_htlc_payment(
                breez.ClaimHtlcPaymentRequest(preimage=preimage.hex())
            )
        except Exception:
            raise BackendError(
                "Spark settlement interrupted; retry to reconcile"
            ) from None

    @staticmethod
    def _payment(payment: breez.Payment, payment_hash: str) -> Payment:
        details = payment.details
        if not isinstance(details, breez.PaymentDetails.LIGHTNING):
            raise BackendError("Spark payout is not a Lightning payment")
        if details.htlc_details.payment_hash != payment_hash:
            raise BackendError("Spark returned the wrong payout")
        if payment.payment_type != breez.PaymentType.SEND or invoice_details(
            details.invoice
        ) != (payment_hash, payment.amount):
            raise BackendError(
                "Spark payout amount/direction does not match the invoice"
            )
        fee_msat = payment.fees * 1000
        if fee_msat < 0:
            raise BackendError("Spark returned an invalid payment fee")
        if payment.status == breez.PaymentStatus.FAILED:
            if details.htlc_details.preimage:
                raise BackendError(
                    "Spark failed payout has a preimage; retry to reconcile"
                )
            return Payment(PaymentState.failed)
        if payment.status != breez.PaymentStatus.COMPLETED:
            return Payment(PaymentState.pending)
        preimage_hex = details.htlc_details.preimage
        if not preimage_hex:
            return Payment(PaymentState.pending)
        try:
            preimage = bytes.fromhex(preimage_hex)
        except ValueError:
            raise BackendError("Spark returned an invalid payout preimage") from None
        if len(preimage) != 32 or hashlib.sha256(preimage).hexdigest() != payment_hash:
            raise BackendError("Spark returned an invalid payout preimage")
        return Payment(PaymentState.succeeded, preimage, fee_msat)

    def _save_payment(self, payment_hash: str, payment: Payment) -> Payment:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE payments SET state=?,preimage=?,fee_msat=? WHERE payment_hash=?",
                (payment.state.value, payment.preimage, payment.fee_msat, payment_hash),
            )
        return payment

    @staticmethod
    def _validate_prepared(
        prepared: breez.PrepareSendPaymentResponse, request: str
    ) -> int:
        method = prepared.payment_method
        if not isinstance(method, breez.SendPaymentMethod.BOLT11_INVOICE):
            raise BackendError("Spark payout must use BOLT11")
        if (
            invoice_details(method.invoice_details.invoice.bolt11)
            != invoice_details(request)
            or prepared.amount != invoice_details(request)[1]
            or prepared.token_identifier is not None
            or prepared.conversion_estimate is not None
            or prepared.fee_policy != breez.FeePolicy.FEES_EXCLUDED
            or method.lightning_fee_sats < 0
        ):
            raise BackendError("Spark payout preparation does not match the invoice")
        return method.lightning_fee_sats

    async def _send(
        self, row, prepared: breez.PrepareSendPaymentResponse | None = None
    ) -> Payment:
        sdk = await self._ensure_sdk()
        try:
            if prepared is None:
                prepared = await sdk.prepare_send_payment(
                    breez.PrepareSendPaymentRequest(
                        payment_request=breez.PaymentRequest.INPUT(row["request"]),
                        fee_policy=breez.FeePolicy.FEES_EXCLUDED,
                    )
                )
            self._validate_prepared(prepared, row["request"])
            method = prepared.payment_method
            if not isinstance(method, breez.SendPaymentMethod.BOLT11_INVOICE):
                raise BackendError("Spark payout must use BOLT11")
            # Reuse both the transfer UUID and the original Lightning fee cap.
            # A new quote must not change an already authorized provider request.
            method.lightning_fee_sats = row["quoted_fee_sat"]
            response = await sdk.send_payment(
                breez.SendPaymentRequest(
                    prepare_response=prepared,
                    options=breez.SendPaymentOptions.BOLT11_INVOICE(
                        prefer_spark=False,
                        completion_timeout_secs=SPARK_SEND_PAYMENT_COMPLETION_TIMEOUT_SECONDS,
                    ),
                    idempotency_key=row["idempotency_key"],
                )
            )
            return self._save_payment(
                row["payment_hash"],
                self._payment(response.payment, row["payment_hash"]),
            )
        except BackendError:
            raise
        except Exception:
            raise BackendError("Spark payout interrupted; retry to reconcile") from None

    async def get_payment(self, payment_hash: str) -> Payment | None:
        async with self._payment_lock:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
                ).fetchone()
            if row is not None and row["state"] == PaymentState.succeeded.value:
                return Payment(PaymentState.succeeded, row["preimage"], row["fee_msat"])
            attempts = await self._find_payments(payment_hash, breez.PaymentType.SEND)
            if attempts:
                outcome = self._send_outcome(attempts, payment_hash)
                if outcome.state != PaymentState.failed:
                    return self._save_payment(payment_hash, outcome)
            if row is None:
                return Payment(PaymentState.failed) if attempts else None
            if row["state"] == PaymentState.failed.value:
                return Payment(PaymentState.failed)
            if attempts and row["idempotency_key"] in {
                attempt.id for attempt in attempts
            }:
                # Every matching attempt, including the stored one, failed.
                return self._save_payment(payment_hash, Payment(PaymentState.failed))
            # The stored attempt is missing from the SDK cache, so the cached
            # records cannot prove its outcome. Reuse the durable SDK transfer
            # UUID even if the original call died before the SDK wrote its
            # payment cache. Provider retries are idempotent.
            return await self._send(row)

    async def pay_invoice(
        self,
        request: str,
        fee_limit_sat: int | None = None,
        *,
        cltv_limit: int | None = None,
        idempotency_key: str | None = None,
    ) -> Payment:
        # Spark backing is timestamp-based; a route CLTV limit is validated
        # for interface compatibility but does not constrain the send.
        if cltv_limit is not None and (type(cltv_limit) is not int or cltv_limit < 0):
            raise ValueError("CLTV limit must be nonnegative blocks")
        payment_hash, _ = invoice_details(request)
        async with self._payment_lock:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
                ).fetchone()
            if row is not None:
                if row["request"] != request:
                    raise BackendError("Spark payout hash already has another invoice")
                if row["state"] == PaymentState.succeeded.value:
                    return Payment(
                        PaymentState.succeeded, row["preimage"], row["fee_msat"]
                    )
            attempts = await self._find_payments(payment_hash, breez.PaymentType.SEND)
            if attempts:
                result = self._send_outcome(attempts, payment_hash)
                if result.state != PaymentState.failed:
                    return self._save_payment(payment_hash, result)
            if (
                row is not None
                and row["state"] != PaymentState.failed.value
                and row["idempotency_key"] not in {attempt.id for attempt in attempts}
            ):
                # The stored attempt is not cached; only it may resume its send.
                return await self._send(row)
            decoded = decode(request)
            if decoded.date + decoded.expiry <= int(time.time()):
                return Payment(PaymentState.failed)
            limit = (
                self.fee_limit_sat
                if fee_limit_sat is None
                else min(fee_limit_sat, self.fee_limit_sat)
            )
            if limit < 0:
                raise ValueError("Spark fee limit must not be negative")
            # A supplied key is validated and used only for a new payments row;
            # existing rows always keep their stored transfer UUID.
            if idempotency_key is None:
                idempotency_key = str(uuid4())
            else:
                try:
                    idempotency_key = str(UUID(idempotency_key))
                except ValueError:
                    raise ValueError("Spark idempotency key must be a UUID") from None
            sdk = await self._ensure_sdk()
            try:
                prepared = await sdk.prepare_send_payment(
                    breez.PrepareSendPaymentRequest(
                        payment_request=breez.PaymentRequest.INPUT(request),
                        fee_policy=breez.FeePolicy.FEES_EXCLUDED,
                    )
                )
                quoted_fee = self._validate_prepared(prepared, request)
                # Spark is disabled for payouts: only the Lightning quote is charged.
                if quoted_fee > limit:
                    return Payment(PaymentState.failed)
            except BackendError:
                raise
            except Exception:
                raise BackendError("Spark payout preparation failed") from None
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO payments (payment_hash,request,fee_limit_sat,"
                    "idempotency_key,state,quoted_fee_sat) VALUES (?,?,?,?,?,?)",
                    (
                        payment_hash,
                        request,
                        limit,
                        idempotency_key,
                        PaymentState.pending.value,
                        quoted_fee,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
                ).fetchone()
            return await self._send(row, prepared)

    async def get_balances(self) -> dict[str, int]:
        sdk = await self._ensure_sdk()
        try:
            info = await sdk.get_info(breez.GetInfoRequest(ensure_synced=True))
            if type(info.balance_sats) is not int or info.balance_sats < 0:
                raise ValueError("invalid balance")
            return {"Spark wallet": info.balance_sats}
        except Exception:
            raise BackendError("Spark balance lookup failed") from None

    async def close(self) -> None:
        if self.sdk is not None:
            sdk, self.sdk = self.sdk, None
            try:
                await sdk.disconnect()
            except Exception:
                raise BackendError("Spark SDK shutdown failed") from None


def load_connection(path: Path) -> SparkConnection:
    return SparkConnection.model_validate(json.loads(path.read_text()))
