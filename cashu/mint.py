import asyncio
import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

from .crypto.bls import PublicKey
from .crypto.ps import (
    DlogEqProof,
    LinearProof,
    MintPrivateKeyPS,
    Presentation,
    PrivatePresentation,
    blind_base_for_nullifier,
    issue,
    issue_blind,
    issue_blind_legacy,
    verify_blind_transfer,
    verify_owner_secret,
    verify_presentation,
)
from .db import BackendError, Database, MintError
from .lightning import HoldInvoiceBackend, Invoice, PaymentState, invoice_details
from .models import (
    BurnRequest,
    BurnResponse,
    CheckResponse,
    InvoiceState,
    KeysetResponse,
    MintRequest,
    QuoteRequest,
    QuoteResponse,
    SignatureResponse,
    SwapRequest,
    burn_binding,
    htlc_claim_scalar,
    preimage_hash,
    swap_binding,
)


def request_digest(request: MintRequest | SwapRequest | BurnRequest) -> str:
    exclude: set[str] = set()
    if isinstance(request, SwapRequest) and request.version == 1:
        exclude.add("version")
    if isinstance(request, BurnRequest) and request.preimage is None:
        exclude.add("preimage")
    return hashlib.sha256(request.model_dump_json(exclude=exclude).encode()).hexdigest()


class Mint:
    def __init__(self, path: Path, seed: bytes, backend: HoldInvoiceBackend):
        if len(seed) < 32:
            raise ValueError("mint seed must contain at least 32 bytes")
        self.db = Database(path)
        self.backend = backend
        derived = hmac.new(seed, b"Cashu_PS_HTLC_Key_v2", hashlib.sha256).digest()
        self.signing_key = MintPrivateKeyPS.from_seed(derived)
        self._burn_locks: dict[str, asyncio.Lock] = {}
        with self.db.read() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quotes (
                    quote TEXT PRIMARY KEY, payment_hash TEXT UNIQUE NOT NULL,
                    amount INTEGER NOT NULL, h TEXT UNIQUE NOT NULL,
                    request TEXT NOT NULL, expiry INTEGER NOT NULL,
                    issued INTEGER NOT NULL DEFAULT 0,
                    digest TEXT, response TEXT
                );
                CREATE TABLE IF NOT EXISTS nullifiers (
                    nullifier TEXT PRIMARY KEY, state TEXT NOT NULL,
                    kind TEXT NOT NULL, digest TEXT NOT NULL, response TEXT
                );
                CREATE TABLE IF NOT EXISTS burns (
                    nullifier TEXT PRIMARY KEY REFERENCES nullifiers(nullifier),
                    payment_hash TEXT UNIQUE NOT NULL,
                    request TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, preimage BLOB, response TEXT
                );
                """
            )
        fingerprint = hmac.new(
            seed, b"Cashu_PS_Layer3_DB_v1", hashlib.sha256
        ).hexdigest()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='seed_fingerprint'"
            ).fetchone()
            if row is not None and row["value"] != fingerprint:
                raise ValueError("mint database belongs to a different seed")
            protocol = conn.execute(
                "SELECT value FROM metadata WHERE key='protocol'"
            ).fetchone()
            if protocol is not None and protocol["value"] != "ps-htlc-claim-v2":
                raise ValueError("mint database belongs to a different protocol")
            if (
                protocol is None
                and conn.execute(
                    "SELECT 1 FROM quotes UNION ALL SELECT 1 FROM nullifiers "
                    "UNION ALL SELECT 1 FROM burns LIMIT 1"
                ).fetchone()
            ):
                raise ValueError(
                    "mint database uses the previous payment-hash protocol; "
                    "use a new database for HTLC claims"
                )
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('seed_fingerprint',?)",
                (fingerprint,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('protocol','ps-htlc-claim-v2')"
            )
            # Old redemptions used their backing hash as the payout hash too.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(burns)")}
            if "payout_hash" not in columns:
                conn.execute("ALTER TABLE burns ADD COLUMN payout_hash TEXT")
            conn.execute(
                "UPDATE burns SET payout_hash=payment_hash WHERE payout_hash IS NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS burns_payout_hash ON burns(payout_hash)"
            )

    def keyset(self) -> KeysetResponse:
        """One parameter set for whole HTLC claims of any supported amount."""
        public = self.signing_key.public_key
        return KeysetResponse(id=public.keyset_id, public_key=public.to_bytes().hex())

    def _validate_invoice(
        self, invoice: Invoice, payment_hash: str, amount: int
    ) -> None:
        if invoice.payment_hash != payment_hash or invoice.amount != amount:
            raise BackendError("backend invoice does not match the quote")
        if invoice_details(invoice.request) != (payment_hash, amount):
            raise BackendError("backend BOLT11 does not match the quote")

    async def quote(self, request: QuoteRequest) -> QuoteResponse:
        # Issuance is public: the mint derives h from the whole invoice claim.
        h = htlc_claim_scalar(request.payment_hash, request.amount)
        with self.db.read() as conn:
            existing = conn.execute(
                "SELECT quote,amount FROM quotes WHERE payment_hash=?",
                (request.payment_hash,),
            ).fetchone()
            alias = conn.execute(
                "SELECT payment_hash FROM quotes WHERE h=?",
                (f"{h:064x}",),
            ).fetchone()
        if existing is not None:
            if existing["amount"] != request.amount:
                raise MintError("payment hash already has a different amount", 409)
            return await self.get_quote(existing["quote"])
        if alias is not None:
            raise MintError("another HTLC claim has the same scalar", 409)
        invoice = await self.backend.create_hold_invoice(
            request.amount, request.payment_hash, request.expiry
        )
        self._validate_invoice(invoice, request.payment_hash, request.amount)
        quote_id = os.urandom(32).hex()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO quotes (quote,payment_hash,amount,h,request,expiry) "
                    "VALUES (?,?,?,?,?,?)",
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
            # Concurrent exact quote requests return the one durable quote.
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT quote,amount FROM quotes WHERE payment_hash=?",
                    (request.payment_hash,),
                ).fetchone()
            if row is None or row["amount"] != request.amount:
                raise MintError("payment hash or HTLC claim already used", 409)
            quote_id = row["quote"]
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
            keyset_id=self.signing_key.public_key.keyset_id,
        )

    async def mint(self, request: MintRequest) -> SignatureResponse:
        C = PublicKey(compressed=bytes.fromhex(request.commitment), group="G1")
        proof = DlogEqProof.from_bytes(bytes.fromhex(request.proof))
        if not verify_owner_secret(C, proof):
            raise MintError("invalid owner commitment proof")
        digest = request_digest(request)
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM quotes WHERE quote=?", (request.quote,)
            ).fetchone()
        if row is None:
            raise MintError("unknown mint quote", 404)
        if row["issued"]:
            if row["digest"] == digest:
                return SignatureResponse.model_validate_json(row["response"])
            raise MintError("quote already issued to another request", 409)
        invoice = await self.backend.get_invoice(row["payment_hash"])
        self._validate_invoice(invoice, row["payment_hash"], row["amount"])
        if (
            invoice.state != InvoiceState.accepted
            or invoice.amount_paid < row["amount"]
        ):
            raise MintError("hold invoice must be fully accepted before issuance", 409)
        key = self.signing_key
        # C=s*G1. Fresh k is essential; a fixed G1 base permits linear forgery.
        u, v = issue(key, int(row["h"], 16), C)
        response = SignatureResponse(
            keyset_id=key.public_key.keyset_id,
            u=u.format().hex(),
            v=v.format().hex(),
        )
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT issued,digest,response FROM quotes WHERE quote=?",
                (request.quote,),
            ).fetchone()
            if current["issued"]:
                if current["digest"] == digest:
                    return SignatureResponse.model_validate_json(current["response"])
                raise MintError("quote already issued to another request", 409)
            conn.execute(
                "UPDATE quotes SET issued=1,digest=?,response=? WHERE quote=?",
                (digest, response.model_dump_json(), request.quote),
            )
        return response

    def swap(self, request: SwapRequest) -> SignatureResponse:
        # The equality proof preserves the issued h while keeping it hidden.
        key = self.signing_key
        pres = PrivatePresentation.from_bytes(bytes.fromhex(request.presentation))
        C = PublicKey(compressed=bytes.fromhex(request.commitment), group="G1")
        owner_proof = DlogEqProof.from_bytes(bytes.fromhex(request.owner_proof))
        B = PublicKey(compressed=bytes.fromhex(request.b), group="G1")
        proof = LinearProof.from_bytes(bytes.fromhex(request.equality_proof))
        legacy_base = None
        if request.version == 1:
            _, legacy_base = blind_base_for_nullifier(key, pres.nullifier.format())
        binding = swap_binding(C.format())
        if not verify_owner_secret(C, owner_proof) or not verify_blind_transfer(
            key.public_key, pres, B, proof, legacy_base, binding
        ):
            raise MintError("invalid private swap proof")
        digest = request_digest(request)
        nullifier = pres.nullifier.format().hex()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM nullifiers WHERE nullifier=?", (nullifier,)
            ).fetchone()
            if row is not None:
                if row["kind"] == "swap" and row["digest"] == digest:
                    return SignatureResponse.model_validate_json(row["response"])
                raise MintError("token is already spent or reserved", 409)
            if request.version == 1:
                k, u = blind_base_for_nullifier(key, pres.nullifier.format())
                v = issue_blind_legacy(key, k, u, B, C)
            else:
                u, v = issue_blind(key, B, C)
            response = SignatureResponse(
                keyset_id=key.public_key.keyset_id,
                u=u.format().hex(),
                v=v.format().hex(),
            )
            conn.execute(
                "INSERT INTO nullifiers VALUES (?,'SPENT','swap',?,?)",
                (nullifier, digest, response.model_dump_json()),
            )
        return response

    def check(self, nullifiers: list[str]) -> CheckResponse:
        states = []
        with self.db.read() as conn:
            for n in nullifiers:
                point = PublicKey(compressed=bytes.fromhex(n), group="G1")
                if point.is_infinity():
                    raise MintError("invalid nullifier")
                row = conn.execute(
                    "SELECT state FROM nullifiers WHERE nullifier=?", (n,)
                ).fetchone()
                states.append("UNSPENT" if row is None else row["state"])
        return CheckResponse.model_validate({"states": states})

    async def burn(self, request: BurnRequest) -> BurnResponse:
        pres = Presentation.from_bytes(bytes.fromhex(request.presentation))
        public = self.signing_key.public_key
        payout_hash, amount = invoice_details(request.request)
        payment_hash = (
            preimage_hash(request.preimage)
            if request.preimage is not None
            else payout_hash
        )
        if amount != request.amount or pres.h != htlc_claim_scalar(
            payment_hash, amount
        ):
            raise MintError(
                "backing HTLC preimage or payout amount does not match the token"
            )
        if not verify_presentation(
            public, pres, burn_binding(request.amount, request.request)
        ):
            raise MintError("invalid burn proof")
        nullifier = pres.nullifier.format().hex()
        digest = request_digest(request)
        lock = self._burn_locks.setdefault(nullifier, asyncio.Lock())
        async with lock:
            with self.db.read() as conn:
                receipt = conn.execute(
                    "SELECT * FROM burns WHERE nullifier=?", (nullifier,)
                ).fetchone()
                quote = conn.execute(
                    "SELECT * FROM quotes WHERE payment_hash=? AND amount=? AND issued=1",
                    (payment_hash, amount),
                ).fetchone()
                payout = conn.execute(
                    "SELECT nullifier FROM burns WHERE payout_hash=?", (payout_hash,)
                ).fetchone()
            if receipt is not None:
                if receipt["digest"] != digest:
                    raise MintError("token is reserved for another redemption", 409)
                if receipt["state"] == "SETTLED":
                    return BurnResponse.model_validate_json(receipt["response"])
                return await self._finish_burn(nullifier, request, payment_hash)
            if quote is None:
                raise MintError("no issued hold invoice backs this token")
            if payout is not None:
                raise MintError(
                    "payout invoice already used by another redemption", 409
                )
            if quote["request"] == request.request:
                raise MintError("redemption invoice must be a receiver invoice")
            invoice = await self.backend.get_invoice(payment_hash)
            self._validate_invoice(invoice, payment_hash, amount)
            if invoice.state not in (InvoiceState.accepted, InvoiceState.settled):
                raise MintError("backing HTLC was canceled or is not accepted", 409)
            if invoice.amount_paid < amount:
                raise MintError("backing invoice is underfunded", 409)
            previous = await self.backend.get_payment(payout_hash)
            if previous is not None and previous.state != PaymentState.failed:
                raise MintError("payout invoice already paid or pending", 409)
            try:
                with self.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO nullifiers VALUES (?,'PENDING','burn',?,NULL)",
                        (nullifier, digest),
                    )
                    conn.execute(
                        "INSERT INTO burns (nullifier,payment_hash,request,digest,state,payout_hash) VALUES (?,?,?,?,'PENDING',?)",
                        (
                            nullifier,
                            payment_hash,
                            request.model_dump_json(),
                            digest,
                            payout_hash,
                        ),
                    )
            except sqlite3.IntegrityError:
                raise MintError(
                    "token, backing HTLC, or payout invoice is already spent or reserved",
                    409,
                )
            return await self._finish_burn(nullifier, request, payment_hash)

    async def _finish_burn(
        self, nullifier: str, request: BurnRequest, payment_hash: str
    ) -> BurnResponse:
        payout_hash, _ = invoice_details(request.request)
        with self.db.read() as conn:
            receipt = conn.execute(
                "SELECT preimage,response FROM burns WHERE nullifier=?", (nullifier,)
            ).fetchone()
        preimage = receipt["preimage"]
        if preimage is None:
            payment = await self.backend.get_payment(payout_hash)
            if payment is None or payment.state == PaymentState.failed:
                payment = await self.backend.pay_invoice(request.request)
            if payment.state == PaymentState.pending:
                raise MintError(
                    "redemption payment is pending; retry the same request", 409
                )
            if payment.state == PaymentState.failed:
                # Only a definite payment failure makes the original token spendable.
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM burns WHERE nullifier=?", (nullifier,))
                    conn.execute(
                        "DELETE FROM nullifiers WHERE nullifier=?", (nullifier,)
                    )
                raise MintError("redemption payment failed; token is unspent", 409)
            payout_preimage = payment.preimage
            if payout_preimage is None or len(payout_preimage) != 32:
                raise BackendError("payout did not return a valid preimage")
            if hashlib.sha256(payout_preimage).hexdigest() != payout_hash:
                raise BackendError("payout preimage does not match the payout invoice")
            preimage = (
                bytes.fromhex(request.preimage)
                if request.preimage is not None
                else payout_preimage
            )
            response = BurnResponse(
                amount=request.amount,
                payment_hash=payment_hash,
                preimage=bytes(preimage).hex(),
                payout_hash=payout_hash,
                payout_preimage=payout_preimage.hex(),
            )
            # Persist the secret before touching the upstream hold. A crash here is
            # recovered by idempotent settlement, without paying the recipient again.
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE burns SET preimage=?,response=? WHERE nullifier=?",
                    (preimage, response.model_dump_json(), nullifier),
                )
        elif receipt["response"] is not None:
            response = BurnResponse.model_validate_json(receipt["response"])
        else:
            # Recover the earlier outbox, whose confirmed payout shared this hash.
            if payout_hash != payment_hash:
                raise BackendError("confirmed payout receipt is missing")
            response = BurnResponse(
                amount=request.amount,
                payment_hash=payment_hash,
                preimage=bytes(preimage).hex(),
                payout_hash=payout_hash,
                payout_preimage=bytes(preimage).hex(),
            )
        await self.backend.settle_invoice(bytes(preimage))
        invoice = await self.backend.get_invoice(payment_hash)
        if invoice.state != InvoiceState.settled:
            raise BackendError("upstream hold invoice has not settled yet")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE burns SET state='SETTLED',response=? WHERE nullifier=?",
                (response.model_dump_json(), nullifier),
            )
            conn.execute(
                "UPDATE nullifiers SET state='SPENT',response=? WHERE nullifier=?",
                (response.model_dump_json(), nullifier),
            )
        return response

    async def resume_pending_burns(self) -> None:
        """Reconcile the durable payout/settlement outbox after a restart."""
        with self.db.read() as conn:
            pending = conn.execute(
                "SELECT request FROM burns WHERE state='PENDING'"
            ).fetchall()
        for row in pending:
            try:
                await self.burn(BurnRequest.model_validate_json(row["request"]))
            except (BackendError, MintError):
                # PENDING remains visible through checkstate and exact retries.
                continue
