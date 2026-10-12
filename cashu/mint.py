"""Denominated, expiring HTLC credentials and a durable Lightning melt outbox."""

import asyncio
import hashlib
import hmac
import os
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

from bolt11 import decode

from .crypto.bls import PublicKey
from .crypto.ps import (
    DlogEqProof,
    LinearProof,
    Presentation,
    PrivatePresentation,
    issue,
    issue_blind,
    verify_linear,
    verify_owner_secret,
    verify_presentation,
    verify_private_presentation,
)
from .db import BackendError, Database, IncompatibleMintDatabase, MintError
from .keyset import COMMON_H_DST, Keyset, common_h_statements, denominations
from .lightning import HoldInvoiceBackend, Invoice, PaymentState, invoice_details
from .models import (
    CheckResponse,
    InvoiceState,
    KeysetResponse,
    MeltQuoteRequest,
    MeltQuoteResponse,
    MeltRequest,
    MeltResponse,
    MintRequest,
    Output,
    PendingHTLC,
    PendingHTLCsResponse,
    QuoteRequest,
    QuoteResponse,
    SignatureResponse,
    SignaturesResponse,
    SwapRequest,
    htlc_claim_scalar,
    melt_binding,
    preimage_hash,
    swap_binding,
)


def request_digest(request) -> str:
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


class Mint:
    def __init__(
        self,
        path: Path,
        seed: bytes,
        backend: HoldInvoiceBackend,
        settle_margin_blocks: int = 6,
        settle_margin_seconds: int = 120,
    ):
        if len(seed) < 32:
            raise ValueError("mint seed must contain at least 32 bytes")
        if settle_margin_blocks < 1 or settle_margin_seconds < 1:
            raise ValueError("settlement margins must be positive")
        self.db = Database(path)
        self.backend = backend
        self.settle_margin_blocks = settle_margin_blocks
        self.settle_margin_seconds = settle_margin_seconds
        self.fee_limit_sat = getattr(backend, "fee_limit_sat", 10)
        self.keys = Keyset(seed)
        self._state_lock = asyncio.Lock()
        self._melt_locks: dict[str, asyncio.Lock] = {}
        fingerprint = hmac.new(
            seed, b"Cashu_PS_Denominations_DB_v3", hashlib.sha256
        ).hexdigest()
        with self.db.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            metadata = {
                row["key"]: row["value"]
                for row in conn.execute("SELECT * FROM metadata")
            }
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if metadata.get("protocol") not in (None, "ps-htlc-denominations-v3") or (
                "quotes" in tables and "protocol" not in metadata
            ):
                raise IncompatibleMintDatabase(
                    "mint database uses a previous protocol; use a new v3 database"
                )
            if metadata.get("seed_fingerprint", fingerprint) != fingerprint:
                raise ValueError("mint database belongs to a different seed")
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('protocol','ps-htlc-denominations-v3')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('seed_fingerprint',?)",
                (fingerprint,),
            )
            for statement in (
                "CREATE TABLE IF NOT EXISTS quotes (quote TEXT PRIMARY KEY, payment_hash TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL, h TEXT UNIQUE NOT NULL, request TEXT NOT NULL, expiry INTEGER NOT NULL, issued INTEGER NOT NULL DEFAULT 0, digest TEXT, response TEXT, preimage TEXT, redeemed INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS nullifiers (nullifier TEXT PRIMARY KEY, state TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, response TEXT)",
                "CREATE TABLE IF NOT EXISTS melt_quotes (quote TEXT PRIMARY KEY, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), payout_hash TEXT UNIQUE NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT)",
                "CREATE TABLE IF NOT EXISTS failed_melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), digest TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS melt_inputs (melt TEXT NOT NULL REFERENCES melts(quote) ON DELETE CASCADE, payment_hash TEXT NOT NULL REFERENCES quotes(payment_hash), amount INTEGER NOT NULL, PRIMARY KEY(melt,payment_hash))",
            ):
                conn.execute(statement)

    def keyset(self) -> KeysetResponse:
        return KeysetResponse(
            id=self.keys.id,
            keys={
                amount: key.to_bytes().hex() for amount, key in self.keys.public.items()
            },
        )

    def _key(self, amount: int):
        if amount not in self.keys.private:
            raise MintError("unsupported denomination")
        return self.keys.private[amount]

    def _owner(self, output: Output) -> PublicKey:
        self._key(output.amount)
        point = PublicKey(compressed=bytes.fromhex(output.commitment), group="G1")
        if not verify_owner_secret(
            point, DlogEqProof.from_bytes(bytes.fromhex(output.proof))
        ):
            raise MintError("invalid owner commitment proof")
        return point

    def _sign(
        self, output: Output, h: int, payment_hash: str | None = None
    ) -> SignatureResponse:
        u, v = issue(self._key(output.amount), h, self._owner(output))
        return SignatureResponse(
            amount=output.amount,
            keyset_id=self.keys.id,
            u=u.format().hex(),
            v=v.format().hex(),
            payment_hash=payment_hash,
        )

    @staticmethod
    def _unique_outputs(outputs: Sequence[Output]):
        if len({p.commitment for p in outputs}) != len(outputs):
            raise MintError("duplicate output commitment")

    def _validate_invoice(self, invoice: Invoice, payment_hash: str, amount: int):
        try:
            details = invoice_details(invoice.request)
        except ValueError as exc:
            raise BackendError("backend returned an invalid backing invoice") from exc
        if (
            invoice.payment_hash != payment_hash
            or invoice.amount != amount
            or details != (payment_hash, amount)
        ):
            raise BackendError("backend invoice does not match the quote")

    def _deadline(
        self, invoice: Invoice, height: int | None
    ) -> tuple[int | None, int | None]:
        if height is not None:
            if (
                invoice.expiry_height is None
                or invoice.htlc_expiry_height is None
                or not 0 <= invoice.expiry_height <= invoice.htlc_expiry_height
            ):
                raise BackendError("accepted backing HTLC expiry is unavailable")
            return max(0, invoice.expiry_height - self.settle_margin_blocks), None
        if invoice.hold_expires_at is None:
            raise BackendError("accepted backing HTLC expiry is unavailable")
        return None, max(0, invoice.hold_expires_at - self.settle_margin_seconds)

    def _before_deadline(self, invoice: Invoice, height: int | None) -> bool:
        block, timestamp = self._deadline(invoice, height)
        if height is not None and block is not None:
            return height < block
        if timestamp is None:
            raise BackendError("accepted backing HTLC deadline is unavailable")
        return int(time.time()) < timestamp

    async def _live_invoice(self, row) -> Invoice:
        if row["closed"]:
            raise MintError("backing HTLC is closed; its tokens have expired", 409)
        invoice = await self.backend.get_invoice(row["payment_hash"])
        self._validate_invoice(invoice, row["payment_hash"], row["amount"])
        if (
            invoice.state != InvoiceState.accepted
            or invoice.amount_paid < row["amount"]
        ):
            raise MintError(
                "backing HTLC is not fully accepted; its tokens are unavailable", 409
            )
        height = await self.backend.get_block_height()
        if not self._before_deadline(invoice, height):
            raise MintError("backing HTLC redemption deadline has passed", 409)
        return invoice

    async def pending_htlcs(self) -> PendingHTLCsResponse:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE issued=1 AND closed=0 AND redeemed<amount ORDER BY payment_hash"
            ).fetchall()
        height = await self.backend.get_block_height()
        semaphore = asyncio.Semaphore(8)

        async def lookup(row):
            async with semaphore:
                invoice = await self.backend.get_invoice(row["payment_hash"])
                self._validate_invoice(invoice, row["payment_hash"], row["amount"])
                return invoice

        invoices = await asyncio.gather(*(lookup(row) for row in rows))
        now = int(time.time())
        with self.db.read() as conn:
            closed = {
                r[0]
                for r in conn.execute(
                    "SELECT payment_hash FROM quotes WHERE closed<>0 OR redeemed=amount"
                )
            }
        entries = []
        for invoice in invoices:
            if (
                invoice.payment_hash in closed
                or invoice.state != InvoiceState.accepted
                or invoice.amount_paid < invoice.amount
            ):
                continue
            deadline_height, deadline = self._deadline(invoice, height)
            if height is not None:
                if invoice.expiry_height is None or invoice.expiry_height <= height:
                    continue
            elif invoice.hold_expires_at is None or invoice.hold_expires_at <= now:
                continue
            entries.append(
                PendingHTLC(
                    payment_hash=invoice.payment_hash,
                    amount=invoice.amount,
                    invoice_expires_at=invoice.expiry,
                    htlc_expiry_height=invoice.htlc_expiry_height,
                    expiry_height=invoice.expiry_height,
                    blocks_remaining=None
                    if height is None
                    else invoice.expiry_height - height,
                    expires_at=invoice.hold_expires_at if height is None else None,
                    melt_deadline_height=deadline_height,
                    melt_deadline=deadline,
                )
            )
        return PendingHTLCsResponse(checked_at=now, block_height=height, htlcs=entries)

    async def quote(self, request: QuoteRequest) -> QuoteResponse:
        h = htlc_claim_scalar(request.payment_hash, request.amount)
        with self.db.read() as conn:
            existing = conn.execute(
                "SELECT quote,amount FROM quotes WHERE payment_hash=?",
                (request.payment_hash,),
            ).fetchone()
        if existing:
            if existing["amount"] != request.amount:
                raise MintError("payment hash already has a different amount", 409)
            return await self.get_quote(existing["quote"])
        invoice = await self.backend.create_hold_invoice(
            request.amount, request.payment_hash, request.expiry
        )
        self._validate_invoice(invoice, request.payment_hash, request.amount)
        quote_id = os.urandom(32).hex()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO quotes (quote,payment_hash,amount,h,request,expiry) VALUES (?,?,?,?,?,?)",
                    (
                        quote_id,
                        request.payment_hash,
                        request.amount,
                        f"{h:064x}",
                        invoice.request,
                        invoice.expiry,
                    ),
                )
        except sqlite3.IntegrityError:
            with self.db.read() as conn:
                existing = conn.execute(
                    "SELECT quote,amount FROM quotes WHERE payment_hash=?",
                    (request.payment_hash,),
                ).fetchone()
            if existing is None or existing["amount"] != request.amount:
                raise MintError("payment hash or HTLC claim already used", 409)
            quote_id = existing["quote"]
        return await self.get_quote(quote_id)

    async def get_quote(self, quote_id: str) -> QuoteResponse:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM quotes WHERE quote=?", (quote_id,)
            ).fetchone()
        if row is None:
            raise MintError("unknown mint quote", 404)
        invoice = await self.backend.get_invoice(row["payment_hash"])
        self._validate_invoice(invoice, row["payment_hash"], row["amount"])
        return QuoteResponse(
            quote=row["quote"],
            amount=row["amount"],
            payment_hash=row["payment_hash"],
            request=row["request"],
            expiry=row["expiry"],
            state=invoice.state,
            issued=bool(row["issued"]),
            keyset_id=self.keys.id,
        )

    async def mint(self, request: MintRequest) -> SignaturesResponse:
        self._unique_outputs(request.outputs)
        for output in request.outputs:
            self._owner(output)
        digest = request_digest(request)
        async with self._state_lock:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT * FROM quotes WHERE quote=?", (request.quote,)
                ).fetchone()
            if row is None:
                raise MintError("unknown mint quote", 404)
            if row["issued"]:
                if row["digest"] == digest:
                    return SignaturesResponse.model_validate_json(row["response"])
                raise MintError("quote already issued to another request", 409)
            if sum(p.amount for p in request.outputs) != row["amount"]:
                raise MintError(
                    "output denominations must sum to the funded HTLC amount"
                )
            await self._live_invoice(row)
            response = SignaturesResponse(
                signatures=[self._sign(p, int(row["h"], 16)) for p in request.outputs]
            )
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE quotes SET issued=1,digest=?,response=? WHERE quote=?",
                    (digest, response.model_dump_json(), request.quote),
                )
            return response

    def swap(self, request: SwapRequest) -> SignaturesResponse:
        if sum(p.amount for p in request.inputs) != sum(
            p.amount for p in request.outputs
        ):
            raise MintError("swap must conserve denomination value")
        self._unique_outputs(request.outputs)
        presentations = [
            PrivatePresentation.from_bytes(bytes.fromhex(p.presentation))
            for p in request.inputs
        ]
        nullifiers = [p.nullifier.format().hex() for p in presentations]
        if len(set(nullifiers)) != len(nullifiers):
            raise MintError("duplicate input nullifier")
        owners = [self._owner(p) for p in request.outputs]
        commitments = [
            PublicKey(compressed=bytes.fromhex(p.b), group="G1")
            for p in request.outputs
        ]
        if any(p.is_infinity() for p in commitments):
            raise MintError("invalid blinded commitment")
        binding = swap_binding(
            [(p.amount, n) for p, n in zip(request.inputs, nullifiers)], request.outputs
        )
        for item, presentation in zip(request.inputs, presentations):
            if not verify_private_presentation(
                self.keys.public[item.amount], presentation, binding
            ):
                raise MintError("invalid private spending proof")
        statements = common_h_statements(
            [
                (self.keys.public[item.amount], p)
                for item, p in zip(request.inputs, presentations)
            ],
            [
                (self.keys.public[item.amount], b)
                for item, b in zip(request.outputs, commitments)
            ],
        )
        if not verify_linear(
            statements,
            LinearProof.from_bytes(bytes.fromhex(request.equality_proof)),
            COMMON_H_DST,
            binding,
        ):
            raise MintError("invalid common HTLC equality proof")
        digest = request_digest(request)
        with self.db.transaction() as conn:
            rows = [
                conn.execute(
                    "SELECT * FROM nullifiers WHERE nullifier=?", (n,)
                ).fetchone()
                for n in nullifiers
            ]
            if any(rows):
                if all(
                    row is not None
                    and row["kind"] == "swap"
                    and row["digest"] == digest
                    for row in rows
                ):
                    return SignaturesResponse.model_validate_json(rows[0]["response"])
                raise MintError("input token is already spent or reserved", 409)
            signatures = []
            for output, owner, b in zip(request.outputs, owners, commitments):
                u, v = issue_blind(self._key(output.amount), b, owner)
                signatures.append(
                    SignatureResponse(
                        amount=output.amount,
                        keyset_id=self.keys.id,
                        u=u.format().hex(),
                        v=v.format().hex(),
                    )
                )
            response = SignaturesResponse(signatures=signatures)
            conn.executemany(
                "INSERT INTO nullifiers VALUES (?,'SPENT','swap',?,?)",
                [(n, digest, response.model_dump_json()) for n in nullifiers],
            )
        return response

    def check(self, nullifiers: list[str]) -> CheckResponse:
        states = []
        with self.db.read() as conn:
            for n in nullifiers:
                if PublicKey(compressed=bytes.fromhex(n), group="G1").is_infinity():
                    raise MintError("invalid nullifier")
                row = conn.execute(
                    "SELECT state FROM nullifiers WHERE nullifier=?", (n,)
                ).fetchone()
                states.append("UNSPENT" if row is None else row["state"])
        return CheckResponse.model_validate({"states": states})

    async def melt_quote(self, request: MeltQuoteRequest) -> MeltQuoteResponse:
        payout_hash, amount = invoice_details(request.request)
        invoice = decode(request.request)
        expiry = invoice.date + invoice.expiry
        if expiry <= int(time.time()):
            raise MintError("payout invoice has expired")
        fee = self.fee_limit_sat if request.fee_reserve is None else request.fee_reserve
        if fee > self.fee_limit_sat:
            raise MintError("requested fee reserve exceeds the mint routing fee limit")
        response = MeltQuoteResponse(
            quote=os.urandom(32).hex(),
            request=request.request,
            payment_hash=payout_hash,
            amount=amount,
            fee_reserve=fee,
            expiry=expiry,
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO melt_quotes VALUES (?,?)",
                (response.quote, response.model_dump_json()),
            )
        return response

    def _melt_quote(self, quote_id: str) -> MeltQuoteResponse:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT payload FROM melt_quotes WHERE quote=?", (quote_id,)
            ).fetchone()
        if row is None:
            raise MintError("unknown melt quote", 404)
        return MeltQuoteResponse.model_validate_json(row["payload"])

    async def melt(self, request: MeltRequest) -> MeltResponse:
        quote = self._melt_quote(request.quote)
        digest = request_digest(request)
        lock = self._melt_locks.setdefault(quote.quote, asyncio.Lock())
        async with lock:
            with self.db.read() as conn:
                previous = conn.execute(
                    "SELECT * FROM melts WHERE quote=?", (quote.quote,)
                ).fetchone()
                failed = conn.execute(
                    "SELECT digest FROM failed_melts WHERE quote=?", (quote.quote,)
                ).fetchone()
            if failed is not None:
                if failed["digest"] != digest:
                    raise MintError(
                        "melt quote already belongs to another request", 409
                    )
                raise MintError("melt payment failed; tokens are unspent", 409)
            if previous is not None:
                if previous["digest"] != digest:
                    raise MintError(
                        "melt quote already belongs to another request", 409
                    )
                if previous["state"] == "PAID":
                    return MeltResponse.model_validate_json(previous["response"])
                return await self._finish_melt(request, quote, digest)
            if quote.expiry <= int(time.time()):
                raise MintError("melt quote has expired", 409)
            if sum(p.amount for p in request.inputs) < quote.amount + quote.fee_reserve:
                raise MintError("inputs do not cover invoice amount and fee reserve")
            presentations = [
                Presentation.from_bytes(bytes.fromhex(p.presentation))
                for p in request.inputs
            ]
            nullifiers = [p.nullifier.format().hex() for p in presentations]
            if len(set(nullifiers)) != len(nullifiers):
                raise MintError("duplicate input nullifier")
            binding = melt_binding(
                quote,
                [
                    (p.amount, p.backing_amount, p.preimage, n)
                    for p, n in zip(request.inputs, nullifiers)
                ],
                request.change,
            )
            groups: OrderedDict[str, dict] = OrderedDict()
            for item, presentation in zip(request.inputs, presentations):
                payment_hash = preimage_hash(item.preimage)
                if presentation.h != htlc_claim_scalar(
                    payment_hash, item.backing_amount
                ) or not verify_presentation(
                    self.keys.public[item.amount], presentation, binding
                ):
                    raise MintError(
                        "invalid melt proof, denomination or backing preimage"
                    )
                group = groups.setdefault(
                    payment_hash,
                    {
                        "amount": 0,
                        "backing_amount": item.backing_amount,
                        "preimage": item.preimage,
                    },
                )
                if (group["backing_amount"], group["preimage"]) != (
                    item.backing_amount,
                    item.preimage,
                ):
                    raise MintError("inconsistent HTLC backing metadata")
                group["amount"] += item.amount
            self._unique_outputs(request.change)
            slots = set()
            for output in request.change:
                self._owner(output)
                slot = (output.payment_hash, output.amount)
                if slot in slots or output.payment_hash not in groups:
                    raise MintError("duplicate or unrelated change output")
                slots.add(slot)
            # Precommit all denominations that a fee outcome could need. This
            # prevents a paid melt from being unrecoverable for lack of change.
            for payment_hash, group in groups.items():
                required = {
                    (payment_hash, 1 << i) for i in range(group["amount"].bit_length())
                }
                if not required.issubset(slots):
                    raise MintError("incomplete change denomination commitments")
            async with self._state_lock:
                with self.db.read() as conn:
                    rows = {
                        payment_hash: conn.execute(
                            "SELECT * FROM quotes WHERE payment_hash=? AND issued=1",
                            (payment_hash,),
                        ).fetchone()
                        for payment_hash in groups
                    }
                for payment_hash, row in rows.items():
                    if (
                        row is None
                        or row["amount"] != groups[payment_hash]["backing_amount"]
                    ):
                        raise MintError("unknown issued backing HTLC")
                    await self._live_invoice(row)
                try:
                    with self.db.transaction() as conn:
                        if any(
                            conn.execute(
                                "SELECT 1 FROM nullifiers WHERE nullifier=?", (n,)
                            ).fetchone()
                            for n in nullifiers
                        ):
                            raise MintError(
                                "input token is already spent or reserved", 409
                            )
                        conn.execute(
                            "INSERT INTO melts VALUES (?,?,?,?, 'PENDING',NULL)",
                            (
                                quote.quote,
                                quote.payment_hash,
                                request.model_dump_json(),
                                digest,
                            ),
                        )
                        conn.executemany(
                            "INSERT INTO nullifiers VALUES (?,'PENDING','melt',?,NULL)",
                            [(n, digest) for n in nullifiers],
                        )
                        for payment_hash, group in groups.items():
                            row = conn.execute(
                                "SELECT * FROM quotes WHERE payment_hash=?",
                                (payment_hash,),
                            ).fetchone()
                            if (
                                row["closed"]
                                or row["redeemed"] + row["reserved"] + group["amount"]
                                > row["amount"]
                            ):
                                raise MintError(
                                    "backing HTLC value is exhausted or reserved", 409
                                )
                            conn.execute(
                                "UPDATE quotes SET reserved=reserved+?,preimage=? WHERE payment_hash=?",
                                (group["amount"], group["preimage"], payment_hash),
                            )
                            conn.execute(
                                "INSERT INTO melt_inputs VALUES (?,?,?)",
                                (quote.quote, payment_hash, group["amount"]),
                            )
                except sqlite3.IntegrityError as exc:
                    raise MintError(
                        "payout invoice is already reserved or paid", 409
                    ) from exc
            return await self._finish_melt(request, quote, digest, retry_failed=True)

    async def _finish_melt(
        self,
        request: MeltRequest,
        quote: MeltQuoteResponse,
        digest: str,
        retry_failed: bool = False,
    ) -> MeltResponse:
        payment = await self.backend.get_payment(quote.payment_hash)
        if (
            payment is not None
            and payment.state == PaymentState.failed
            and not retry_failed
        ):
            self._release_melt(quote.quote, digest, payment_failed=True)
            raise MintError("melt payment failed; tokens are unspent", 409)
        if payment is None or payment.state == PaymentState.failed:
            with self.db.read() as conn:
                rows = conn.execute(
                    "SELECT q.* FROM quotes q JOIN melt_inputs i ON i.payment_hash=q.payment_hash WHERE i.melt=?",
                    (quote.quote,),
                ).fetchall()
            try:
                for row in rows:
                    await self._live_invoice(row)
                if quote.expiry <= int(time.time()):
                    raise MintError("melt quote has expired", 409)
            except MintError:
                self._release_melt(quote.quote, digest)
                raise
            payment = await self.backend.pay_invoice(
                quote.request, fee_limit_sat=quote.fee_reserve
            )
        if payment.state == PaymentState.pending:
            raise MintError("melt payment is pending; retry the same request", 409)
        if payment.state == PaymentState.failed:
            self._release_melt(quote.quote, digest, payment_failed=True)
            raise MintError("melt payment failed; tokens are unspent", 409)
        if (
            payment.preimage is None
            or hashlib.sha256(payment.preimage).hexdigest() != quote.payment_hash
        ):
            raise BackendError("payout did not return a valid preimage")
        if type(payment.fee_msat) is not int or payment.fee_msat < 0:
            raise BackendError("payout did not return a valid routing fee")
        fee = (payment.fee_msat + 999) // 1000
        if fee > quote.fee_reserve:
            raise BackendError("backend exceeded the authorized routing fee reserve")
        await self.reconcile_holds()
        slots = {(p.payment_hash, p.amount): p for p in request.change}
        cost = quote.amount + fee
        signatures: list[SignatureResponse] = []
        expired_change = 0
        async with self._state_lock:
            with self.db.transaction() as conn:
                previous = conn.execute(
                    "SELECT * FROM melts WHERE quote=?", (quote.quote,)
                ).fetchone()
                if previous["state"] == "PAID":
                    return MeltResponse.model_validate_json(previous["response"])
                # First appearance of an HTLC in the inputs determines charging
                # order; the spending transcript binds that order.
                hashes = list(
                    dict.fromkeys(preimage_hash(p.preimage) for p in request.inputs)
                )
                for payment_hash in hashes:
                    entry = conn.execute(
                        "SELECT q.*,i.amount AS input_amount FROM quotes q JOIN melt_inputs i ON i.payment_hash=q.payment_hash WHERE i.melt=? AND q.payment_hash=?",
                        (quote.quote, payment_hash),
                    ).fetchone()
                    charged = min(cost, entry["input_amount"])
                    cost -= charged
                    change = entry["input_amount"] - charged
                    if entry["closed"]:
                        expired_change += change
                    else:
                        signatures.extend(
                            self._sign(
                                slots[(payment_hash, amount)],
                                int(entry["h"], 16),
                                payment_hash,
                            )
                            for amount in denominations(change)
                        )
                    conn.execute(
                        "UPDATE quotes SET redeemed=redeemed+?,reserved=reserved-? WHERE payment_hash=?",
                        (charged, entry["input_amount"], payment_hash),
                    )
                if cost:
                    raise BackendError(
                        "melt outbox does not cover the confirmed payment"
                    )
                response = MeltResponse(
                    amount=quote.amount,
                    fee=fee,
                    fee_msat=payment.fee_msat,
                    payout_hash=quote.payment_hash,
                    payout_preimage=payment.preimage.hex(),
                    change=signatures,
                    expired_change=expired_change,
                )
                conn.execute(
                    "UPDATE melts SET state='PAID',response=? WHERE quote=?",
                    (response.model_dump_json(), quote.quote),
                )
                conn.execute(
                    "UPDATE nullifiers SET state='SPENT',response=? WHERE kind='melt' AND digest=?",
                    (response.model_dump_json(), digest),
                )
        await self.reconcile_holds()
        return response

    def _release_melt(self, quote_id: str, digest: str, payment_failed: bool = False):
        with self.db.transaction() as conn:
            if payment_failed:
                conn.execute(
                    "INSERT OR IGNORE INTO failed_melts (quote,digest) SELECT quote,digest FROM melts WHERE quote=? AND digest=? AND state='PENDING'",
                    (quote_id, digest),
                )
            for row in conn.execute(
                "SELECT payment_hash,amount FROM melt_inputs WHERE melt=?", (quote_id,)
            ).fetchall():
                conn.execute(
                    "UPDATE quotes SET reserved=reserved-? WHERE payment_hash=?",
                    (row["amount"], row["payment_hash"]),
                )
            conn.execute(
                "DELETE FROM nullifiers WHERE kind='melt' AND digest=? AND state='PENDING'",
                (digest,),
            )
            conn.execute(
                "DELETE FROM melts WHERE quote=? AND state='PENDING'", (quote_id,)
            )

    async def reconcile_holds(self):
        """Close on full consumption (including fees) or the published cutoff.

        This is terminal claim accounting, never a post-settlement user balance.
        A known preimage is retained even after a definite failed payout.
        """
        async with self._state_lock:
            with self.db.read() as conn:
                rows = conn.execute(
                    "SELECT * FROM quotes WHERE issued=1 AND preimage IS NOT NULL AND closed<2"
                ).fetchall()
            for row in rows:
                try:
                    invoice = await self.backend.get_invoice(row["payment_hash"])
                    self._validate_invoice(invoice, row["payment_hash"], row["amount"])
                    terminal = invoice.state in (
                        InvoiceState.settled,
                        InvoiceState.canceled,
                    )
                    height = None if terminal else await self.backend.get_block_height()
                    full = row["redeemed"] == row["amount"] and row["reserved"] == 0
                    due = (
                        terminal
                        or row["closed"]
                        or full
                        or not self._before_deadline(invoice, height)
                    )
                    if not due:
                        continue
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE quotes SET closed=1 WHERE payment_hash=?",
                            (row["payment_hash"],),
                        )
                    if invoice.state == InvoiceState.accepted:
                        await self.backend.settle_invoice(
                            bytes.fromhex(row["preimage"])
                        )
                        invoice = await self.backend.get_invoice(row["payment_hash"])
                        self._validate_invoice(
                            invoice, row["payment_hash"], row["amount"]
                        )
                    if invoice.state in (InvoiceState.settled, InvoiceState.canceled):
                        with self.db.transaction() as conn:
                            conn.execute(
                                "UPDATE quotes SET closed=2 WHERE payment_hash=?",
                                (row["payment_hash"],),
                            )
                except (BackendError, MintError):
                    continue

    async def resume_pending_melts(self):
        await self.reconcile_holds()
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT request FROM melts WHERE state='PENDING'"
            ).fetchall()
        for row in rows:
            try:
                await self.melt(MeltRequest.model_validate_json(row["request"]))
            except (BackendError, MintError):
                continue
