"""Store individual denomination proofs and reserve complete input sets."""

import os
from pathlib import Path

from .client import Client, MeltOperation, MintOperation, SwapOperation
from .db import Database
from .keyset import denominations
from .models import MeltResponse, Token, preimage_hash
from .token import encode_token

Operation = MintOperation | SwapOperation | MeltOperation


class Wallet:
    def __init__(self, path: Path):
        self.db = Database(path)
        os.chmod(path, 0o600)
        with self.db.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            protocol = conn.execute(
                "SELECT value FROM metadata WHERE key='protocol'"
            ).fetchone()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if (protocol is not None and protocol[0] != "ps-wallet-v3") or (
                protocol is None and "tokens" in tables
            ):
                raise ValueError(
                    "wallet database uses a previous protocol; use a new v3 wallet"
                )
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('protocol','ps-wallet-v3')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'LIVE')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS operation_sources (source TEXT PRIMARY KEY REFERENCES tokens(id), operation TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE)"
            )

    def save(self, operation: Operation):
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT payload FROM operations WHERE id=?", (operation.id,)
            ).fetchone()
            if previous:
                if previous[0] != operation.model_dump_json():
                    raise ValueError("operation id already belongs to another request")
                return
            conn.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (operation.id, operation.kind, operation.model_dump_json()),
            )
            if operation.kind != "mint":
                for proof in operation.token.proofs:
                    source = Client.nullifier(proof)
                    row = conn.execute(
                        "SELECT state FROM tokens WHERE id=?", (source,)
                    ).fetchone()
                    allowed = {"LIVE", "SENT"} if operation.kind == "swap" else {"LIVE"}
                    if row is not None and row[0] not in allowed:
                        raise ValueError(
                            "token is already spent or has a pending operation"
                        )
                    token = Token(mint=operation.token.mint, proofs=[proof])
                    conn.execute(
                        "INSERT OR IGNORE INTO tokens VALUES (?,?,'LIVE')",
                        (source, token.model_dump_json()),
                    )
                    conn.execute(
                        "INSERT INTO operation_sources VALUES (?,?)",
                        (source, operation.id),
                    )
                    conn.execute(
                        "UPDATE tokens SET state='PENDING',token=? WHERE id=?",
                        (token.model_dump_json(), source),
                    )

    def operation(self, operation_id: str) -> Operation:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown pending operation")
        if row["kind"] == "mint":
            return MintOperation.model_validate_json(row["payload"])
        if row["kind"] == "swap":
            return SwapOperation.model_validate_json(row["payload"])
        return MeltOperation.model_validate_json(row["payload"])

    def pending_mint(self, quote_id: str) -> MintOperation | None:
        with self.db.read() as conn:
            operations = [
                MintOperation.model_validate_json(r[0])
                for r in conn.execute(
                    "SELECT payload FROM operations WHERE kind='mint'"
                )
            ]
        matches = [o for o in operations if o.quote.quote == quote_id]
        if len(matches) > 1:
            raise ValueError(
                "multiple saved requests for this quote; use pending and retry"
            )
        return matches[0] if matches else None

    def set_mint_preimage(self, operation_id: str, preimage: str) -> MintOperation:
        operation = self.operation(operation_id)
        if not isinstance(operation, MintOperation):
            raise ValueError("not a mint operation")
        if preimage_hash(preimage) != operation.quote.payment_hash:
            raise ValueError("HTLC preimage does not match the mint quote")
        operation = MintOperation.model_validate(
            operation.model_dump() | {"preimage": preimage}
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE operations SET payload=? WHERE id=?",
                (operation.model_dump_json(), operation_id),
            )
        return operation

    def pending(self) -> list[dict]:
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute("SELECT id,kind FROM operations")]

    def complete(self, operation_id: str, token: Token | None):
        with self.db.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM operations WHERE id=?", (operation_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("unknown pending operation")
            conn.execute(
                "UPDATE tokens SET state='SPENT' WHERE id IN (SELECT source FROM operation_sources WHERE operation=?)",
                (operation_id,),
            )
            if token:
                for proof in token.proofs:
                    single = Token(mint=token.mint, proofs=[proof])
                    conn.execute(
                        "INSERT INTO tokens VALUES (?,?,'LIVE')",
                        (Client.nullifier(proof), single.model_dump_json()),
                    )
            conn.execute("DELETE FROM operations WHERE id=?", (operation_id,))

    def retry(
        self, client: Client, operation_id: str, kind: str | None = None
    ) -> Token | MeltResponse:
        operation = self.operation(operation_id)
        if kind is not None and kind != operation.kind:
            raise ValueError("incorrect operation kind")
        if isinstance(operation, MintOperation):
            token = client.finish_mint(operation)
            self.complete(operation_id, token)
            return token
        if isinstance(operation, SwapOperation):
            token = client.finish_swap(operation)
            self.complete(operation_id, token)
            return token
        response = client.finish_melt(operation)
        self.complete(operation_id, client.melt_change(operation, response))
        return response

    def abandon(self, client: Client, operation_id: str):
        operation = self.operation(operation_id)
        if isinstance(operation, MintOperation):
            raise ValueError("retry mint operations to preserve recovery secrets")
        nullifiers = [Client.nullifier(p) for p in operation.token.proofs]
        if client.check(nullifiers).states != ["UNSPENT"] * len(nullifiers):
            raise ValueError("mint has spent or reserved an input; retry the operation")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tokens SET state='LIVE' WHERE id IN (SELECT source FROM operation_sources WHERE operation=?)",
                (operation_id,),
            )
            conn.execute("DELETE FROM operations WHERE id=?", (operation_id,))

    def tokens(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM tokens ORDER BY rowid").fetchall()
        return [
            {
                "id": row["id"],
                "state": row["state"],
                "mint": token.mint,
                "amount": token.amount,
                "payment_hash": token.payment_hash,
                "backing_amount": token.proofs[0].backing_amount,
            }
            for row in rows
            for token in [Token.model_validate_json(row["token"])]
        ]

    def token(self, token_id: str, state: str = "LIVE") -> Token:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT token FROM tokens WHERE id=? AND state=?", (token_id, state)
            ).fetchone()
        if row is None:
            raise ValueError("unknown token or token is not available")
        return Token.model_validate_json(row[0])

    def select_token(
        self,
        amount: int,
        mint: str,
        payment_hash: str | None = None,
        token_ids: list[str] | None = None,
    ) -> Token:
        if amount <= 0:
            raise ValueError("amount must be positive")
        candidates = [
            r
            for r in self.tokens()
            if r["state"] == "LIVE"
            and r["mint"].rstrip("/") == mint.rstrip("/")
            and (payment_hash is None or r["payment_hash"] == payment_hash)
            and (token_ids is None or r["id"] in token_ids)
        ]
        selected = []
        total = 0
        for row in sorted(candidates, key=lambda r: r["amount"], reverse=True):
            selected.extend(self.token(row["id"]).proofs)
            total += row["amount"]
            if total >= amount:
                return Token(mint=mint, proofs=selected)
        raise ValueError("insufficient available tokens for amount and fee reserve")

    def refresh(self, client: Client):
        snapshot = client.pending_htlcs()
        expired = []
        for row in self.tokens():
            if (
                row["state"] not in {"LIVE", "SENT"}
                or row["mint"].rstrip("/") != client.mint_url
            ):
                continue
            token = self.token(row["id"], row["state"])
            from .client import ClientError

            try:
                client._check_backing(token.proofs[0], snapshot)
            except ClientError:
                expired.append(row["id"])
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE tokens SET state='EXPIRED' WHERE id=? AND state IN ('LIVE','SENT')",
                [(n,) for n in expired],
            )

    def send(
        self,
        client: Client,
        amount: int,
        payment_hash: str | None = None,
        token_ids: list[str] | None = None,
        preimage: str | None = None,
    ) -> str:
        if token_ids:
            rows = {r["id"]: r for r in self.tokens()}
            if all(n in rows and rows[n]["state"] == "SENT" for n in token_ids):
                tokens = [self.token(n, "SENT") for n in token_ids]
                recovered = Token(
                    mint=client.mint_url, proofs=[p for t in tokens for p in t.proofs]
                )
                if recovered.amount != amount or any(
                    t.mint.rstrip("/") != client.mint_url for t in tokens
                ):
                    raise ValueError(
                        "sent tokens do not match requested amount or mint"
                    )
                return encode_token(recovered)
        totals: dict[str, int] = {}
        for row in self.tokens():
            if (
                row["state"] == "LIVE"
                and row["mint"].rstrip("/") == client.mint_url
                and (token_ids is None or row["id"] in token_ids)
            ):
                totals[row["payment_hash"]] = (
                    totals.get(row["payment_hash"], 0) + row["amount"]
                )
        matches = [
            h
            for h, total in totals.items()
            if total >= amount and (payment_hash is None or h == payment_hash)
        ]
        if len(matches) != 1:
            raise ValueError(
                "send needs one HTLC with enough value; select --payment-hash if ambiguous"
            )
        token = self.select_token(amount, client.mint_url, matches[0], token_ids)
        if preimage is not None:
            token = token.with_preimage(preimage)
        if any(p.preimage is None for p in token.proofs):
            raise ValueError("token has no backing preimage; supply --preimage")
        client.check_backing(token)
        if token.amount != amount:
            send_amounts = denominations(amount)
            operation = client.prepare_swap(
                token, send_amounts + denominations(token.amount - amount)
            )
            self.save(operation)
            received = self.retry(client, operation.id)
            assert isinstance(received, Token)
            token = Token(
                mint=received.mint, proofs=received.proofs[: len(send_amounts)]
            )
        with self.db.transaction() as conn:
            for proof in token.proofs:
                single = Token(mint=token.mint, proofs=[proof])
                updated = conn.execute(
                    "UPDATE tokens SET state='SENT',token=? WHERE id=? AND state='LIVE'",
                    (single.model_dump_json(), Client.nullifier(proof)),
                )
                if updated.rowcount != 1:
                    raise ValueError("token is already sent or has a pending operation")
        return encode_token(token)
