"""Denominated, expiring HTLC credentials and a durable Lightning melt outbox."""

import asyncio
import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Hashable, Iterable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
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
from .db import (
    BackendError,
    Database,
    IncompatibleMintDatabase,
    MintError,
    acquire_singleton_lock,
)
from .keyset import COMMON_H_DST, Keyset, common_h_statements, denominations
from .lightning import (
    HoldInvoiceBackend,
    Invoice,
    Payment,
    PaymentState,
    invoice_details,
)
from .models import (
    CheckResponse,
    HTLCCheckRequest,
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


SCHEMA_VERSION = 2


class _KeyedLocks:
    """Reference-counted asyncio locks; idle entries are evicted."""

    def __init__(self) -> None:
        self._entries: dict[Hashable, list] = {}
        self._mutex = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        async with self._mutex:
            entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
            entry[1] += 1
        try:
            await entry[0].acquire()
        except BaseException:
            # A cancelled waiter must not leak its reference count.
            await self._drop(key, entry)
            raise
        try:
            yield
        finally:
            entry[0].release()
            await self._drop(key, entry)

    async def _drop(self, key: Hashable, entry: list) -> None:
        async with self._mutex:
            entry[1] -= 1
            if entry[1] == 0:
                self._entries.pop(key, None)

    @asynccontextmanager
    async def hold_many(self, keys: Iterable[str]) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for key in sorted(set(keys)):
                await stack.enter_async_context(self.hold(key))
            yield


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
        acquire = acquire_singleton_lock(path)
        self.db = Database(path)
        self.backend = backend
        self.settle_margin_blocks = settle_margin_blocks
        self.settle_margin_seconds = settle_margin_seconds
        self.fee_limit_sat = getattr(backend, "fee_limit_sat", 10)
        self.keys = Keyset(seed)
        self._melt_locks = _KeyedLocks()
        self._hold_locks = _KeyedLocks()
        self._sync_work = asyncio.Semaphore(4)
        self._last_prune = 0.0
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
            # An existing v3 database without a version is schema version 1.
            raw_version = metadata.get(
                "schema_version", "1" if "quotes" in tables else str(SCHEMA_VERSION)
            )
            try:
                version = int(raw_version)
            except ValueError:
                raise IncompatibleMintDatabase(
                    "mint database has an invalid schema version"
                ) from None
            if not 1 <= version <= SCHEMA_VERSION:
                raise IncompatibleMintDatabase(
                    "mint database uses a newer schema version; upgrade tmp.cash"
                )
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
                "CREATE TABLE IF NOT EXISTS melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), payout_hash TEXT UNIQUE NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT, attempt TEXT NOT NULL DEFAULT '', melt_lock TEXT NOT NULL DEFAULT '', submitted INTEGER NOT NULL DEFAULT 1)",
                "CREATE TABLE IF NOT EXISTS failed_melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), digest TEXT NOT NULL, expired INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS melt_inputs (melt TEXT NOT NULL REFERENCES melts(quote) ON DELETE CASCADE, payment_hash TEXT NOT NULL REFERENCES quotes(payment_hash), amount INTEGER NOT NULL, PRIMARY KEY(melt,payment_hash))",
            ):
                conn.execute(statement)
            if version < 2:
                for table, column, definition in (
                    ("melts", "attempt", "TEXT NOT NULL DEFAULT ''"),
                    ("melts", "melt_lock", "TEXT NOT NULL DEFAULT ''"),
                    # Existing PENDING rows may or may not have reached the
                    # backend, so they are reconcile-only after migration.
                    ("melts", "submitted", "INTEGER NOT NULL DEFAULT 1"),
                    ("failed_melts", "expired", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in {
                        r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                    }:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
                conn.execute(
                    "UPDATE melts SET attempt=lower(hex(randomblob(16))) WHERE attempt=''"
                )
            if conn.execute(
                "SELECT 1 FROM quotes WHERE reserved<0 OR redeemed<0 "
                "OR redeemed+reserved>amount LIMIT 1"
            ).fetchone():
                raise IncompatibleMintDatabase(
                    "mint database contains invalid backing accounting"
                )
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            # Single-process ownership makes clearing orphaned executor locks
            # from a previous run safe (Nutshell PR #8 startup recovery) — but
            # only on this process's first acquisition; a reentrant in-process
            # reopen must not steal locks from live executors.
            if acquire:
                conn.execute("UPDATE melts SET melt_lock='' WHERE melt_lock<>''")

    async def _run_sync(self, operation, *args):
        """Bounded admission for expensive synchronous proof/signature work.

        At most four operations run concurrently, offloaded from the event
        loop; admission waits at most half a second before asking the caller
        to retry instead of queueing unbounded unauthenticated work.
        """
        try:
            await asyncio.wait_for(self._sync_work.acquire(), 0.5)
        except asyncio.TimeoutError:
            raise MintError(
                "mint is busy with cryptographic work; retry the request", 503
            ) from None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, operation, *args)
        finally:
            self._sync_work.release()

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
        if row["issued"] and (
            invoice.state == InvoiceState.open
            or (
                invoice.state == InvoiceState.accepted
                and invoice.amount_paid < row["amount"]
            )
        ):
            raise BackendError("issued backing HTLC state is unavailable")
        if row["issued"] and invoice.state in (
            InvoiceState.settled,
            InvoiceState.canceled,
        ):
            raise MintError("backing HTLC is closed; its tokens have expired", 409)
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

    async def check_htlcs(self, request: HTLCCheckRequest) -> PendingHTLCsResponse:
        hashes = list(dict.fromkeys(request.payment_hashes))
        placeholders = ",".join("?" for _ in hashes)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM quotes WHERE payment_hash IN ({placeholders}) "
                "AND issued=1 AND closed=0 AND redeemed<amount ORDER BY payment_hash",
                hashes,
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
                    f"SELECT payment_hash FROM quotes WHERE payment_hash IN ({placeholders}) "
                    "AND (closed<>0 OR redeemed=amount)",
                    hashes,
                )
            }
        entries = []
        for invoice in invoices:
            if invoice.state == InvoiceState.open or (
                invoice.state == InvoiceState.accepted
                and invoice.amount_paid < invoice.amount
            ):
                raise BackendError("issued backing HTLC state is unavailable")
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

    def _validate_outputs(self, request: MintRequest):
        self._unique_outputs(request.outputs)
        for output in request.outputs:
            self._owner(output)

    def _commit_issuance(
        self, request: MintRequest, row, digest: str
    ) -> SignaturesResponse:
        response = SignaturesResponse(
            signatures=[self._sign(p, int(row["h"], 16)) for p in request.outputs]
        )
        with self.db.transaction() as conn:
            updated = conn.execute(
                "UPDATE quotes SET issued=1,digest=?,response=? WHERE quote=? AND issued=0",
                (digest, response.model_dump_json(), request.quote),
            )
            if updated.rowcount != 1:
                current = conn.execute(
                    "SELECT digest,response FROM quotes WHERE quote=?",
                    (request.quote,),
                ).fetchone()
                if current["digest"] == digest:
                    return SignaturesResponse.model_validate_json(current["response"])
                raise MintError("quote already issued to another request", 409)
        return response

    async def mint(self, request: MintRequest) -> SignaturesResponse:
        await self._run_sync(self._validate_outputs, request)
        digest = request_digest(request)
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM quotes WHERE quote=?", (request.quote,)
            ).fetchone()
        if row is None:
            raise MintError("unknown mint quote", 404)
        async with self._hold_locks.hold(row["payment_hash"]):
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT * FROM quotes WHERE quote=?", (request.quote,)
                ).fetchone()
            if row["issued"]:
                if row["digest"] == digest:
                    return SignaturesResponse.model_validate_json(row["response"])
                raise MintError("quote already issued to another request", 409)
            if sum(p.amount for p in request.outputs) != row["amount"]:
                raise MintError(
                    "output denominations must sum to the funded HTLC amount"
                )
            await self._live_invoice(row)
            return await self._run_sync(self._commit_issuance, request, row, digest)

    async def swap(self, request: SwapRequest) -> SignaturesResponse:
        return await self._run_sync(self._swap, request)

    def _swap(self, request: SwapRequest) -> SignaturesResponse:
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

    async def check(self, nullifiers: list[str]) -> CheckResponse:
        return await self._run_sync(self._check, nullifiers)

    def _check(self, nullifiers: list[str]) -> CheckResponse:
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
        if expiry > int(time.time()) + 24 * 60 * 60:
            raise MintError("payout invoice expiry exceeds 24 hours")
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

    def _prepare_melt(
        self, request: MeltRequest, quote: MeltQuoteResponse
    ) -> tuple[list[str], OrderedDict]:
        """Synchronous melt proof, binding and change-commitment validation."""
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
                raise MintError("invalid melt proof, denomination or backing preimage")
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
        return nullifiers, groups

    async def melt(self, request: MeltRequest) -> MeltResponse:
        quote = self._melt_quote(request.quote)
        digest = request_digest(request)
        async with self._melt_locks.hold(quote.quote):
            with self.db.read() as conn:
                previous = conn.execute(
                    "SELECT * FROM melts WHERE quote=?", (quote.quote,)
                ).fetchone()
                failed = conn.execute(
                    "SELECT digest,expired FROM failed_melts WHERE quote=?",
                    (quote.quote,),
                ).fetchone()
            if failed is not None:
                if failed["digest"] != digest:
                    raise MintError(
                        "melt quote already belongs to another request", 409
                    )
                if failed["expired"]:
                    raise MintError("melt payment failed; backing has expired", 410)
                raise MintError("melt payment failed; tokens are unspent", 409)
            if previous is not None:
                if previous["digest"] != digest:
                    raise MintError(
                        "melt quote already belongs to another request", 409
                    )
                if previous["state"] == "PAID":
                    return MeltResponse.model_validate_json(previous["response"])
                if previous["melt_lock"] or not self._claim_melt(previous):
                    # An executor owns this payment attempt; pollers must not
                    # consult backend status or release inputs behind its back.
                    raise MintError(
                        "melt payment is pending; retry the same request", 409
                    )
                return await self._execute_melt(
                    request,
                    quote,
                    digest,
                    previous["attempt"],
                    bool(previous["submitted"]),
                )
            if quote.expiry <= int(time.time()):
                raise MintError("melt quote has expired", 409)
            if sum(p.amount for p in request.inputs) < quote.amount + quote.fee_reserve:
                raise MintError("inputs do not cover invoice amount and fee reserve")
            nullifiers, groups = await self._run_sync(
                self._prepare_melt, request, quote
            )
            # Hold the involved backing locks in sorted order so reconciliation
            # cannot close or settle a hold underneath this reservation.
            async with self._hold_locks.hold_many(groups):
                with self.db.read() as conn:
                    rows = {
                        payment_hash: conn.execute(
                            "SELECT * FROM quotes WHERE payment_hash=? AND issued=1",
                            (payment_hash,),
                        ).fetchone()
                        for payment_hash in groups
                    }
                height = await self.backend.get_block_height()
                for payment_hash, row in rows.items():
                    if (
                        row is None
                        or row["amount"] != groups[payment_hash]["backing_amount"]
                    ):
                        raise MintError("unknown issued backing HTLC")
                    invoice = await self._live_invoice(row)
                    if self._within_melt_lockout(invoice, height):
                        raise MintError(
                            "backing HTLC is too close to its settlement cutoff "
                            "for a new melt",
                            409,
                        )
                # Arm the outbox and its executor lock atomically with the
                # reservation: the attempt owns these inputs from this point.
                attempt = uuid.uuid4().hex
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
                            "INSERT INTO melts (quote,payout_hash,request,digest,state,response,attempt,melt_lock,submitted) VALUES (?,?,?,?,'PENDING',NULL,?,?,0)",
                            (
                                quote.quote,
                                quote.payment_hash,
                                request.model_dump_json(),
                                digest,
                                attempt,
                                attempt,
                            ),
                        )
                        conn.executemany(
                            "INSERT INTO nullifiers VALUES (?,'PENDING','melt',?,NULL)",
                            [(n, digest) for n in nullifiers],
                        )
                        for payment_hash, group in groups.items():
                            updated = conn.execute(
                                "UPDATE quotes SET reserved=reserved+?,preimage=? WHERE payment_hash=? AND closed=0 AND redeemed+reserved+?<=amount",
                                (
                                    group["amount"],
                                    group["preimage"],
                                    payment_hash,
                                    group["amount"],
                                ),
                            )
                            if updated.rowcount != 1:
                                raise MintError(
                                    "backing HTLC value is exhausted or reserved", 409
                                )
                            conn.execute(
                                "INSERT INTO melt_inputs VALUES (?,?,?)",
                                (quote.quote, payment_hash, group["amount"]),
                            )
                except sqlite3.IntegrityError as exc:
                    with self.db.read() as conn:
                        occupied = conn.execute(
                            "SELECT state FROM melts WHERE payout_hash=?",
                            (quote.payment_hash,),
                        ).fetchone()
                    if occupied is not None and occupied["state"] == "PAID":
                        raise MintError("payout invoice is already paid", 409) from exc
                    raise MintError(
                        "payout invoice is already reserved or paid", 409
                    ) from exc
            return await self._execute_melt(request, quote, digest, attempt, False)

    def _within_melt_lockout(self, invoice: Invoice, height: int | None) -> bool:
        """Conservative submission lockout: twice the settlement margin."""
        if height is not None:
            if invoice.expiry_height is None:
                raise BackendError("accepted backing HTLC expiry is unavailable")
            return height + 2 * self.settle_margin_blocks >= invoice.expiry_height
        if invoice.hold_expires_at is None:
            raise BackendError("accepted backing HTLC expiry is unavailable")
        return (
            int(time.time()) + 2 * self.settle_margin_seconds >= invoice.hold_expires_at
        )

    def _claim_melt(self, row) -> bool:
        """Arm this process as the executor of an unlocked outbox (PR #8 CAS)."""
        with self.db.transaction() as conn:
            updated = conn.execute(
                "UPDATE melts SET melt_lock=attempt WHERE quote=? AND digest=? "
                "AND state='PENDING' AND melt_lock='' AND attempt=?",
                (row["quote"], row["digest"], row["attempt"]),
            )
            return updated.rowcount == 1

    def _unlock_melt(self, quote_id: str, attempt: str):
        """Explicit pending: keep the outbox but release the executor lock."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE melts SET melt_lock='' WHERE quote=? AND state='PENDING' "
                "AND attempt=? AND melt_lock=?",
                (quote_id, attempt, attempt),
            )

    async def _execute_melt(
        self,
        request: MeltRequest,
        quote: MeltQuoteResponse,
        digest: str,
        attempt: str,
        submitted: bool,
    ) -> MeltResponse:
        """Drive one payment attempt while holding its durable executor lock."""
        if not submitted:
            try:
                payment = await self.backend.get_payment(quote.payment_hash)
                if payment is None or payment.state == PaymentState.failed:
                    # A FAILED record seen before the first submission can only
                    # come from an older quote for the same payout invoice; the
                    # new attempt may still send (the backend deduplicates).
                    with self.db.read() as conn:
                        rows = conn.execute(
                            "SELECT q.* FROM quotes q JOIN melt_inputs i ON i.payment_hash=q.payment_hash WHERE i.melt=?",
                            (quote.quote,),
                        ).fetchall()
                    try:
                        height = await self.backend.get_block_height()
                        cltv_limit = None
                        for row in rows:
                            invoice = await self._live_invoice(row)
                            if self._within_melt_lockout(invoice, height):
                                raise MintError(
                                    "backing HTLC is too close to its settlement "
                                    "cutoff for a new melt",
                                    409,
                                )
                            if height is not None:
                                # The outgoing HTLC must resolve before the
                                # earliest backing cutoff; longer routes refused.
                                if invoice.expiry_height is None:
                                    raise BackendError(
                                        "accepted backing HTLC expiry is unavailable"
                                    )
                                remaining = (
                                    invoice.expiry_height
                                    - height
                                    - self.settle_margin_blocks
                                )
                                cltv_limit = (
                                    remaining
                                    if cltv_limit is None
                                    else min(cltv_limit, remaining)
                                )
                        if quote.expiry <= int(time.time()):
                            raise MintError("melt quote has expired", 409)
                    except MintError:
                        # Never submitted: the reservation can safely be released.
                        self._release_melt(quote.quote, digest, attempt)
                        raise
                    with self.db.transaction() as conn:
                        updated = conn.execute(
                            "UPDATE melts SET submitted=1 WHERE quote=? AND state='PENDING' "
                            "AND attempt=? AND melt_lock=?",
                            (quote.quote, attempt, attempt),
                        )
                        if updated.rowcount != 1:
                            raise BackendError("melt outbox changed before submission")
            except BackendError:
                # Nothing was submitted yet, so the failure is not uncertain:
                # unlock the outbox and let a later retry submit it. Fail-closed
                # locking applies only once the send becomes uncertain (below).
                self._unlock_melt(quote.quote, attempt)
                raise
            if payment is None or payment.state == PaymentState.failed:
                # Retries of this outbox reuse the same attempt-derived key, so
                # a Spark resume is idempotent; a new quote or attempt or a
                # different invoice derives a fresh key.
                idempotency_key = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{quote.quote}:{attempt}:{quote.request}",
                    )
                )
                # An exception after this send is uncertain: the executor lock
                # stays held with submitted=1 and the outbox fails closed until
                # restart/orphan recovery instead of risking a second send.
                payment = await self.backend.pay_invoice(
                    quote.request,
                    fee_limit_sat=quote.fee_reserve,
                    idempotency_key=idempotency_key,
                    cltv_limit=cltv_limit,
                )
        else:
            payment = await self.backend.get_payment(quote.payment_hash)
            if payment is None:
                # submitted=1 with no tracked payment: the first send may or
                # may not have reached the backend. Reconcile only; never send
                # again, and never release merely because expiry moved.
                payment = Payment(PaymentState.pending)
        if payment.state == PaymentState.pending:
            self._unlock_melt(quote.quote, attempt)
            raise MintError("melt payment is pending; retry the same request", 409)
        if payment.state == PaymentState.failed:
            if await self._fail_melt(quote.quote, digest, attempt):
                raise MintError("melt payment failed; backing has expired", 410)
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
        # First appearance of an HTLC in the inputs determines charging order;
        # the spending transcript binds that order. Reconcile only those holds.
        hashes = list(dict.fromkeys(preimage_hash(p.preimage) for p in request.inputs))
        await self.reconcile_holds(hashes)
        try:
            response = await self._run_sync(
                self._finalize_paid, request, quote, digest, attempt, payment, fee
            )
        except MintError as exc:
            if exc.status_code != 503:
                raise
            # The payout succeeded externally but signing admission is busy:
            # leave the durable outbox unlocked for recovery; never convert a
            # paid external payout into a failure or release.
            self._unlock_melt(quote.quote, attempt)
            raise MintError(
                "melt payment is pending; retry the same request", 409
            ) from None
        await self.reconcile_holds(hashes)
        return response

    def _finalize_paid(
        self,
        request: MeltRequest,
        quote: MeltQuoteResponse,
        digest: str,
        attempt: str,
        payment: Payment,
        fee: int,
    ) -> MeltResponse:
        """Synchronous paid-melt signing and guarded database finalization."""
        if payment.preimage is None:
            raise BackendError("payout did not return a valid preimage")
        slots = {(p.payment_hash, p.amount): p for p in request.change}
        cost = quote.amount + fee
        signatures: list[SignatureResponse] = []
        expired_change = 0
        hashes = list(dict.fromkeys(preimage_hash(p.preimage) for p in request.inputs))
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT * FROM melts WHERE quote=?", (quote.quote,)
            ).fetchone()
            if previous is None:
                raise BackendError("melt outbox disappeared during finalization")
            if previous["state"] == "PAID":
                if previous["digest"] == digest:
                    return MeltResponse.model_validate_json(previous["response"])
                raise BackendError("melt outbox is owned by another request")
            if (
                previous["digest"] != digest
                or previous["attempt"] != attempt
                or previous["melt_lock"] != attempt
            ):
                raise BackendError("melt outbox is owned by another attempt")
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
                updated = conn.execute(
                    "UPDATE quotes SET redeemed=redeemed+?,reserved=reserved-? WHERE payment_hash=? AND reserved>=? AND redeemed+?<=amount",
                    (
                        charged,
                        entry["input_amount"],
                        payment_hash,
                        entry["input_amount"],
                        charged,
                    ),
                )
                if updated.rowcount != 1:
                    raise BackendError(
                        "backing accounting conflict during finalization"
                    )
            if cost:
                raise BackendError("melt outbox does not cover the confirmed payment")
            response = MeltResponse(
                amount=quote.amount,
                fee=fee,
                fee_msat=payment.fee_msat,
                payout_hash=quote.payment_hash,
                payout_preimage=payment.preimage.hex(),
                change=signatures,
                expired_change=expired_change,
            )
            updated = conn.execute(
                "UPDATE melts SET state='PAID',response=?,melt_lock='' WHERE quote=? "
                "AND state='PENDING' AND attempt=? AND melt_lock=?",
                (response.model_dump_json(), quote.quote, attempt, attempt),
            )
            if updated.rowcount != 1:
                raise BackendError("melt outbox changed during finalization")
            conn.execute(
                "UPDATE nullifiers SET state='SPENT',response=? WHERE kind='melt' AND digest=? AND state='PENDING'",
                (response.model_dump_json(), digest),
            )
        return response

    async def _fail_melt(self, quote_id: str, digest: str, attempt: str) -> bool:
        """Release a definite failure; terminal once any backing is closed.

        Returns True when an involved backing HTLC is already closed or past
        its cutoff: the reservation is released, but the credentials must
        never be presented as reusable again (failed_melts.expired=1).

        All involved hold locks are held across reconciliation, the liveness
        read and the release, so no settlement can land in between. If backing
        liveness cannot be determined, the outbox is kept (unlocked — the
        payout is already definitively failed) for a later retry instead of
        falsely reporting an ordinary unspent failure.
        """
        with self.db.read() as conn:
            hashes = [
                row[0]
                for row in conn.execute(
                    "SELECT payment_hash FROM melt_inputs WHERE melt=?", (quote_id,)
                )
            ]
        async with self._hold_locks.hold_many(hashes):
            unavailable = False
            for payment_hash in hashes:
                try:
                    await self._reconcile_hold_locked(payment_hash)
                except Exception:
                    unavailable = True
            if unavailable:
                self._unlock_melt(quote_id, attempt)
                raise BackendError(
                    "backing liveness unavailable; retry to finish the failed melt"
                )
            with self.db.read() as conn:
                expired = any(
                    conn.execute(
                        "SELECT closed FROM quotes WHERE payment_hash=?",
                        (payment_hash,),
                    ).fetchone()["closed"]
                    for payment_hash in hashes
                )
            self._release_melt(
                quote_id, digest, attempt, payment_failed=True, expired=expired
            )
            return expired

    def _release_melt(
        self,
        quote_id: str,
        digest: str,
        attempt: str,
        payment_failed: bool = False,
        expired: bool = False,
    ):
        """Compare-and-set release; paid outboxes are terminal, never released."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM melts WHERE quote=?", (quote_id,)
            ).fetchone()
            if row is None:
                return
            if row["state"] != "PENDING":
                raise BackendError("paid melt outbox is terminal; cannot release")
            if (
                row["digest"] != digest
                or row["attempt"] != attempt
                or row["melt_lock"] != attempt
            ):
                raise BackendError("melt outbox is owned by another attempt")
            if payment_failed:
                conn.execute(
                    "INSERT OR IGNORE INTO failed_melts (quote,digest,expired) VALUES (?,?,?)",
                    (quote_id, digest, int(expired)),
                )
            for input_row in conn.execute(
                "SELECT payment_hash,amount FROM melt_inputs WHERE melt=?", (quote_id,)
            ).fetchall():
                updated = conn.execute(
                    "UPDATE quotes SET reserved=reserved-? WHERE payment_hash=? AND reserved>=?",
                    (
                        input_row["amount"],
                        input_row["payment_hash"],
                        input_row["amount"],
                    ),
                )
                if updated.rowcount != 1:
                    raise BackendError("backing reservation accounting conflict")
            conn.execute(
                "DELETE FROM nullifiers WHERE kind='melt' AND digest=? AND state='PENDING'",
                (digest,),
            )
            conn.execute(
                "DELETE FROM melts WHERE quote=? AND state='PENDING'", (quote_id,)
            )

    async def reconcile_holds(self, payment_hashes: Iterable[str] | None = None):
        """Close on full consumption (including fees) or the published cutoff.

        This is terminal claim accounting, never a post-settlement user balance.
        A known preimage is retained even after a definite failed payout.
        Rows are reconciled concurrently, each under its own payment-hash lock;
        one failing hold never blocks the others.
        """
        statement = (
            "SELECT payment_hash FROM quotes "
            "WHERE issued=1 AND preimage IS NOT NULL AND closed<2"
        )
        parameters: tuple = ()
        if payment_hashes is not None:
            hashes = sorted(set(payment_hashes))
            if not hashes:
                return
            placeholders = ",".join("?" for _ in hashes)
            statement += f" AND payment_hash IN ({placeholders})"
            parameters = tuple(hashes)
        with self.db.read() as conn:
            rows = conn.execute(statement, parameters).fetchall()
        semaphore = asyncio.Semaphore(8)

        async def reconcile(payment_hash: str):
            async with semaphore:
                try:
                    await self._reconcile_hold(payment_hash)
                except Exception:
                    # One malformed or unreachable hold must not stop others.
                    return

        await asyncio.gather(*(reconcile(row[0]) for row in rows))

    async def _reconcile_hold(self, payment_hash: str):
        async with self._hold_locks.hold(payment_hash):
            await self._reconcile_hold_locked(payment_hash)

    async def _reconcile_hold_locked(self, payment_hash: str):
        """Reconcile one hold; caller must hold its payment-hash lock.

        Backend or data failures propagate so callers can distinguish "not
        due" from "liveness unknown"; the full-scan wrapper isolates them.
        """
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM quotes WHERE payment_hash=?", (payment_hash,)
            ).fetchone()
        if (
            row is None
            or not row["issued"]
            or row["preimage"] is None
            or row["closed"] >= 2
        ):
            return
        invoice = await self.backend.get_invoice(payment_hash)
        self._validate_invoice(invoice, payment_hash, row["amount"])
        terminal = invoice.state in (
            InvoiceState.settled,
            InvoiceState.canceled,
        )
        height = None if terminal else await self.backend.get_block_height()
        full = row["redeemed"] == row["amount"] and row["reserved"] == 0
        # closed=1 means settlement is in progress and is retried here.
        due = (
            terminal
            or row["closed"]
            or full
            or not self._before_deadline(invoice, height)
        )
        if not due:
            return
        if not row["closed"]:
            with self.db.transaction() as conn:
                updated = conn.execute(
                    "UPDATE quotes SET closed=1 WHERE payment_hash=? AND closed=0",
                    (payment_hash,),
                )
                if updated.rowcount != 1:
                    return
        if invoice.state == InvoiceState.accepted:
            await self.backend.settle_invoice(bytes.fromhex(row["preimage"]))
            invoice = await self.backend.get_invoice(payment_hash)
            self._validate_invoice(invoice, payment_hash, row["amount"])
        if invoice.state in (InvoiceState.settled, InvoiceState.canceled):
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE quotes SET closed=2 WHERE payment_hash=? AND closed=1",
                    (payment_hash,),
                )

    def _prune_melt_quotes(self):
        """Delete expired quotes with no recovery value, at most every 5 minutes.

        Quotes with a pending or paid melt or a failed tombstone are retained
        for pending, paid, failed and exact-retry recovery.
        """
        now = time.time()
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM melt_quotes WHERE json_extract(payload,'$.expiry')<=? "
                "AND quote NOT IN (SELECT quote FROM melts) "
                "AND quote NOT IN (SELECT quote FROM failed_melts)",
                (int(now),),
            )

    async def resume_pending_melts(self):
        await self.reconcile_holds()
        self._prune_melt_quotes()
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT request FROM melts WHERE state='PENDING' AND melt_lock=''"
            ).fetchall()
        for row in rows:
            try:
                await self.melt(MeltRequest.model_validate_json(row["request"]))
            except Exception:
                # One malformed or failing row must not stop later rows;
                # task cancellation (BaseException) still propagates.
                continue
