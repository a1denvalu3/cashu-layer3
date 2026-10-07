"""Hold-invoice backends. ACCEPTED is distinct from SETTLED throughout."""

import base64
import hashlib
import os
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx
from bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, decode, encode
from pydantic import BaseModel, ConfigDict, Field

from .db import BackendError, Database
from .models import InvoiceState


@dataclass(frozen=True)
class Invoice:
    payment_hash: str
    amount: int
    request: str
    expiry: int
    state: InvoiceState
    amount_paid: int = 0
    htlc_expiry_height: int | None = None
    expiry_height: int | None = None
    hold_expires_at: int | None = None


class PaymentState(str, Enum):
    pending = "IN_FLIGHT"
    succeeded = "SUCCEEDED"
    failed = "FAILED"


@dataclass(frozen=True)
class Payment:
    state: PaymentState
    preimage: bytes | None = None


def invoice_details(request: str) -> tuple[str, int]:
    """Validate the signed BOLT11 invoice, including its exact sats amount."""
    try:
        invoice = decode(request)
    except Exception as exc:
        raise ValueError("invalid BOLT11 invoice") from exc
    if invoice.amount_msat is None or invoice.amount_msat <= 0:
        raise ValueError("redemption invoice must have an amount")
    if invoice.amount_msat % 1000:
        raise ValueError("redemption invoice must use whole satoshis")
    return invoice.payment_hash, int(invoice.amount_msat) // 1000


class HoldInvoiceBackend(ABC):
    @abstractmethod
    async def create_hold_invoice(
        self, amount: int, payment_hash: str, expiry: int
    ) -> Invoice: ...

    @abstractmethod
    async def get_invoice(self, payment_hash: str) -> Invoice: ...

    @abstractmethod
    async def settle_invoice(self, preimage: bytes) -> None: ...

    @abstractmethod
    async def pay_invoice(self, request: str) -> Payment: ...

    @abstractmethod
    async def get_payment(self, payment_hash: str) -> Payment | None: ...

    async def get_block_height(self) -> int | None:
        raise BackendError("backend cannot report a live backing snapshot")

    async def close(self) -> None:
        pass


class FakeBackend(HoldInvoiceBackend):
    """Persistent fake funding and settlement for local tests, with no money.

    A receiver registers its own invoice and preimage here. The mint API
    only creates hold invoices and learns the secret from the payout result.
    Fake invoice signatures use a fixed test-only key.
    """

    def __init__(self, path: Path):
        self.db = Database(path)
        with self.db.read() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    payment_hash TEXT PRIMARY KEY, amount INTEGER NOT NULL,
                    request TEXT NOT NULL, expiry INTEGER NOT NULL,
                    state TEXT NOT NULL, amount_paid INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS receivers (
                    request TEXT PRIMARY KEY, payment_hash TEXT NOT NULL,
                    amount INTEGER NOT NULL, preimage BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    payment_hash TEXT PRIMARY KEY, state TEXT NOT NULL,
                    preimage BLOB
                );
                """
            )

    @staticmethod
    def _invoice(amount: int, payment_hash: str, expiry: int, memo: str) -> str:
        return encode(
            Bolt11(
                currency="bcrt",
                date=int(time.time()),
                amount_msat=MilliSatoshi(amount * 1000),
                tags=Tags(
                    [
                        Tag(TagChar.payment_hash, payment_hash),
                        Tag(TagChar.payment_secret, os.urandom(32).hex()),
                        Tag(TagChar.description, memo),
                        Tag(TagChar.expire_time, expiry),
                    ]
                ),
            ),
            private_key="11" * 32,
        )

    async def create_hold_invoice(
        self, amount: int, payment_hash: str, expiry: int
    ) -> Invoice:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT amount FROM invoices WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
            if row is not None and row["amount"] != amount:
                raise BackendError("payment hash already has another invoice amount")
            if row is None:
                request = self._invoice(amount, payment_hash, expiry, "Layer 3 hold")
                conn.execute(
                    "INSERT INTO invoices VALUES (?,?,?,?,?,0)",
                    (
                        payment_hash,
                        amount,
                        request,
                        int(time.time()) + expiry,
                        InvoiceState.open.value,
                    ),
                )
        return await self.get_invoice(payment_hash)

    async def get_invoice(self, payment_hash: str) -> Invoice:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET state=? WHERE payment_hash=? AND expiry<=? "
                "AND state IN (?,?)",
                (
                    InvoiceState.canceled.value,
                    payment_hash,
                    int(time.time()),
                    InvoiceState.open.value,
                    InvoiceState.accepted.value,
                ),
            )
            row = conn.execute(
                "SELECT * FROM invoices WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
        if row is None:
            raise BackendError("unknown hold invoice")
        return Invoice(
            payment_hash=payment_hash,
            amount=row["amount"],
            request=row["request"],
            expiry=row["expiry"],
            state=InvoiceState(row["state"]),
            amount_paid=row["amount_paid"],
            hold_expires_at=row["expiry"],
        )

    async def get_block_height(self) -> int | None:
        # The fake backend expires holds by wall clock, without a blockchain.
        return None

    async def accept(self, payment_hash: str) -> None:
        """Simulate a payer whose HTLCs are held, without disclosing a preimage."""
        invoice = await self.get_invoice(payment_hash)
        if invoice.state != InvoiceState.open:
            raise BackendError("invoice is not open")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET state=?,amount_paid=amount WHERE payment_hash=?",
                (InvoiceState.accepted.value, payment_hash),
            )

    async def cancel(self, payment_hash: str) -> None:
        invoice = await self.get_invoice(payment_hash)
        if invoice.state == InvoiceState.settled:
            raise BackendError("invoice already settled")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET state=? WHERE payment_hash=?",
                (InvoiceState.canceled.value, payment_hash),
            )

    def receiver_invoice(self, amount: int, preimage: bytes) -> str:
        if len(preimage) != 32 or amount <= 0:
            raise ValueError("need a positive amount and a 32-byte preimage")
        payment_hash = hashlib.sha256(preimage).hexdigest()
        request = self._invoice(amount, payment_hash, 3600, "Layer 3 receiver")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO receivers VALUES (?,?,?,?)",
                (request, payment_hash, amount, preimage),
            )
        return request

    async def settle_invoice(self, preimage: bytes) -> None:
        payment_hash = hashlib.sha256(preimage).hexdigest()
        invoice = await self.get_invoice(payment_hash)
        if invoice.state == InvoiceState.settled:
            return
        if invoice.state != InvoiceState.accepted:
            raise BackendError("hold invoice is not accepted")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE invoices SET state=? WHERE payment_hash=?",
                (InvoiceState.settled.value, payment_hash),
            )

    async def pay_invoice(self, request: str) -> Payment:
        payment_hash, _ = invoice_details(request)
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
            if previous is not None:
                return Payment(PaymentState(previous["state"]), previous["preimage"])
            receiver = conn.execute(
                "SELECT preimage FROM receivers WHERE request=?", (request,)
            ).fetchone()
            if receiver is None:
                return Payment(PaymentState.failed)
            preimage = bytes(receiver["preimage"])
            conn.execute(
                "INSERT INTO payments VALUES (?,?,?)",
                (payment_hash, PaymentState.succeeded.value, preimage),
            )
        return Payment(PaymentState.succeeded, preimage)

    async def get_payment(self, payment_hash: str) -> Payment | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
        if row is None:
            return None
        return Payment(PaymentState(row["state"]), row["preimage"])


class LndHTLC(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    amt_msat: int = Field(ge=0)
    expiry_height: int = Field(ge=0)


class LndInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    block_height: int = Field(ge=0)
    synced_to_chain: bool


class LndInvoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    r_hash: str
    value: str
    payment_request: str
    creation_date: str
    expiry: str
    state: InvoiceState
    amt_paid_sat: str = "0"
    htlcs: list[LndHTLC] = []


class LndHoldResponse(BaseModel):
    payment_request: str


class LndPayment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment_hash: str
    status: str
    payment_preimage: str = ""


class LndPayments(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payments: list[LndPayment] = []
    last_index_offset: str = "0"


class LndStreamError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: int


class LndPaymentUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    result: LndPayment | None = None
    error: LndStreamError | None = None


class LndRestBackend(HoldInvoiceBackend):
    """LND REST AddHoldInvoice, LookupInvoice, SendPaymentV2 and SettleInvoice.

    TLS verification stays enabled. Pass LND's certificate for a private CA.
    A payment hash is supplied to LND; no preimage is generated by this mint.
    """

    def __init__(
        self,
        endpoint: str,
        macaroon_path: Path,
        cert_path: Path | None = None,
        fee_limit_sat: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
        hold_expiry_delta: int = 18,
    ):
        if not endpoint.startswith("https://"):
            raise ValueError("LND REST endpoint must use https")
        if fee_limit_sat < 0:
            raise ValueError("fee limit must not be negative")
        if hold_expiry_delta < 0:
            raise ValueError("hold expiry delta must not be negative")
        tls = ssl.create_default_context(cafile=str(cert_path) if cert_path else None)
        self.fee_limit_sat = fee_limit_sat
        self.hold_expiry_delta = hold_expiry_delta
        self.client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            headers={"Grpc-Metadata-macaroon": macaroon_path.read_bytes().hex()},
            verify=tls,
            timeout=httpx.Timeout(70, connect=10),
            transport=transport,
        )

    async def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        try:
            response = await self.client.request(method, path, json=body, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never interpolate backend bodies: they can contain credentials or secrets.
            raise BackendError("LND request failed; operation can be retried") from exc

    async def create_hold_invoice(
        self, amount: int, payment_hash: str, expiry: int
    ) -> Invoice:
        try:
            body = await self._call(
                "POST",
                "/v2/invoices/hodl",
                {
                    "hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
                    "value": str(amount),
                    "expiry": str(expiry),
                    "memo": "Layer 3 hold",
                },
            )
            response = LndHoldResponse.model_validate(body)
            if invoice_details(response.payment_request) != (payment_hash, amount):
                raise BackendError(
                    "LND returned an invoice with the wrong hash or amount"
                )
        except BackendError:
            # Recovers a quote if AddHoldInvoice succeeded but its response was lost.
            existing = await self.get_invoice(payment_hash)
            if existing.amount != amount:
                raise BackendError("existing hold invoice has another amount")
            return existing
        return await self.get_invoice(payment_hash)

    async def get_invoice(self, payment_hash: str) -> Invoice:
        body = await self._call("GET", f"/v1/invoice/{payment_hash}")
        try:
            invoice = LndInvoice.model_validate(body)
            if base64.b64decode(invoice.r_hash, validate=True).hex() != payment_hash:
                raise BackendError("LND invoice hash mismatch")
            active = [htlc for htlc in invoice.htlcs if htlc.state == "ACCEPTED"]
            expiry_height = None
            if active and sum(h.amt_msat for h in active) >= int(invoice.value) * 1000:
                if any(h.expiry_height <= 0 for h in active):
                    raise BackendError("LND accepted HTLC has no expiry height")
                # Matches LND's invoice expiry watcher, including multipart holds.
                expiry_height = min(h.expiry_height for h in active)
            return Invoice(
                payment_hash=payment_hash,
                amount=int(invoice.value),
                request=invoice.payment_request,
                expiry=int(invoice.creation_date) + int(invoice.expiry),
                state=invoice.state,
                amount_paid=int(invoice.amt_paid_sat),
                htlc_expiry_height=expiry_height,
                expiry_height=(
                    max(0, expiry_height - self.hold_expiry_delta)
                    if expiry_height is not None
                    else None
                ),
            )
        except ValueError as exc:
            raise BackendError("LND returned invalid invoice metadata") from exc

    async def get_block_height(self) -> int | None:
        body = await self._call("GET", "/v1/getinfo")
        try:
            info = LndInfo.model_validate(body)
        except ValueError as exc:
            raise BackendError("LND returned invalid chain metadata") from exc
        if not info.synced_to_chain:
            raise BackendError("LND is not synced; backing snapshot is unavailable")
        return info.block_height

    async def settle_invoice(self, preimage: bytes) -> None:
        if len(preimage) != 32:
            raise ValueError("preimage must be 32 bytes")
        await self._call(
            "POST",
            "/v2/invoices/settle",
            {"preimage": base64.b64encode(preimage).decode()},
        )

    async def pay_invoice(self, request: str) -> Payment:
        payment_hash, _ = invoice_details(request)
        previous = await self.get_payment(payment_hash)
        if previous is not None and previous.state != PaymentState.failed:
            return previous
        # SendPaymentSync was removed in LND 0.21. SendPaymentV2 returns a
        # stream of JSON envelopes; request only the final state.
        try:
            async with self.client.stream(
                "POST",
                "/v2/router/send",
                json={
                    "payment_request": request,
                    "fee_limit_sat": str(self.fee_limit_sat),
                    "timeout_seconds": 60,
                    "no_inflight_updates": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    update = LndPaymentUpdate.model_validate_json(line)
                    if update.error is not None:
                        # Errors can accompany duplicate attempts. Reconcile the
                        # tracked payment instead of refunding an uncertain payout.
                        tracked = await self.get_payment(payment_hash)
                        if tracked is not None:
                            return tracked
                        raise BackendError("LND rejected payout; retry to reconcile")
                    if update.result is not None:
                        payment = update.result
                        if payment.payment_hash != payment_hash:
                            raise BackendError("LND streamed the wrong payment")
                        if payment.status == "FAILED":
                            return Payment(PaymentState.failed)
                        if payment.status == "SUCCEEDED":
                            preimage = bytes.fromhex(payment.payment_preimage)
                            if (
                                len(preimage) != 32
                                or hashlib.sha256(preimage).hexdigest() != payment_hash
                            ):
                                raise BackendError(
                                    "LND returned an invalid payout preimage"
                                )
                            return Payment(PaymentState.succeeded, preimage)
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError("LND payout interrupted; retry to reconcile") from exc
        return Payment(PaymentState.pending)

    async def get_payment(self, payment_hash: str) -> Payment | None:
        offset = "0"
        while True:
            body = await self._call(
                "GET",
                "/v1/payments",
                params={
                    "include_incomplete": "true",
                    "max_payments": "1000",
                    "index_offset": offset,
                },
            )
            page = LndPayments.model_validate(body)
            for payment in page.payments:
                if payment.payment_hash == payment_hash:
                    if payment.status == "SUCCEEDED":
                        preimage = bytes.fromhex(payment.payment_preimage)
                        if (
                            len(preimage) != 32
                            or hashlib.sha256(preimage).hexdigest() != payment_hash
                        ):
                            raise BackendError(
                                "LND payment tracking returned an invalid preimage"
                            )
                        return Payment(PaymentState.succeeded, preimage)
                    if payment.status == "FAILED":
                        return Payment(PaymentState.failed)
                    return Payment(PaymentState.pending)
            if not page.payments or page.last_index_offset == offset:
                return None
            offset = page.last_index_offset

    async def close(self) -> None:
        await self.client.aclose()
