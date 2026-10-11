"""Breez Spark hold invoices, adapted from Nutshell's SparkL2Wallet.

The optional SDK is imported only when this backend is selected. Incoming
preimages are supplied to the SDK only by settle_invoice, never by a melt.
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

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
from .models import InvoiceState


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

    async def _find_payment(
        self,
        payment_hash: str,
        direction: breez.PaymentType,
        payment_id: str | None = None,
    ) -> breez.Payment | None:
        sdk = await self._ensure_sdk()
        try:
            await sdk.sync_wallet(breez.SyncWalletRequest())
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
                        and (payment_id is None or payment.id == payment_id)
                    ):
                        if payment.payment_type != direction:
                            raise BackendError(
                                "Spark returned the wrong payment direction"
                            )
                        if invoice_details(details.invoice)[0] != payment_hash:
                            raise BackendError("Spark payment invoice/hash mismatch")
                        return payment
                if len(response.payments) < 100:
                    return None
                offset += len(response.payments)
        except BackendError:
            raise
        except Exception:
            raise BackendError(
                "Spark payment lookup failed; retry to reconcile"
            ) from None

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
                                description="Layer 3 hold",
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

    async def get_invoice(self, payment_hash: str) -> Invoice:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
        if row is None:
            raise BackendError("unknown Spark hold invoice")
        state = InvoiceState(row["state"])
        amount_paid = 0
        deadline = None
        if state not in {InvoiceState.settled, InvoiceState.canceled}:
            payment = await self._find_payment(payment_hash, breez.PaymentType.RECEIVE)
            if payment is None:
                state = (
                    InvoiceState.canceled
                    if row["expiry"] <= int(time.time())
                    else InvoiceState.open
                )
            else:
                details = payment.details
                if not isinstance(details, breez.PaymentDetails.LIGHTNING):
                    raise BackendError("Spark backing is not a Lightning HTLC")
                if payment.amount != row["amount"] or invoice_details(
                    details.invoice
                ) != (payment_hash, row["amount"]):
                    raise BackendError("Spark backing amount does not match the hold")
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
            if state in {InvoiceState.settled, InvoiceState.canceled}:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE invoices SET state=? WHERE payment_hash=?",
                        (state.value, payment_hash),
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
                        prefer_spark=False, completion_timeout_secs=0
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
            payment = await self._find_payment(
                payment_hash,
                breez.PaymentType.SEND,
                row["idempotency_key"] if row is not None else None,
            )
            if payment is not None:
                return self._save_payment(
                    payment_hash, self._payment(payment, payment_hash)
                )
            if row is None:
                return None
            if row["state"] == PaymentState.failed.value:
                return Payment(PaymentState.failed)
            # Reuse the durable SDK transfer UUID even if the original call died
            # before the SDK wrote its payment cache. Provider retries are idempotent.
            return await self._send(row)

    async def pay_invoice(
        self, request: str, fee_limit_sat: int | None = None
    ) -> Payment:
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
            previous = await self._find_payment(
                payment_hash,
                breez.PaymentType.SEND,
                row["idempotency_key"] if row is not None else None,
            )
            if previous is not None:
                result = self._payment(previous, payment_hash)
                if result.state != PaymentState.failed:
                    return self._save_payment(payment_hash, result)
            if (
                row is not None
                and row["state"] != PaymentState.failed.value
                and previous is None
            ):
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
                        str(uuid4()),
                        PaymentState.pending.value,
                        quoted_fee,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
                ).fetchone()
            return await self._send(row, prepared)

    async def close(self) -> None:
        if self.sdk is not None:
            sdk, self.sdk = self.sdk, None
            try:
                await sdk.disconnect()
            except Exception:
                raise BackendError("Spark SDK shutdown failed") from None


def load_connection(path: Path) -> SparkConnection:
    return SparkConnection.model_validate(json.loads(path.read_text()))
