import json
import os
from pathlib import Path

from .client import BurnOperation, Client, MintOperation, SwapOperation
from .db import Database
from .models import BurnResponse, Token
from .token import encode_token


class Wallet:
    """Store bearer tokens and save recovery material before a spending POST."""

    def __init__(self, path: Path):
        self.db = Database(path)
        os.chmod(path, 0o600)
        with self.db.read() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY, token TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'LIVE'
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    source TEXT UNIQUE
                );
                """
            )

    def save(self, operation: MintOperation | SwapOperation | BurnOperation) -> None:
        source = None
        source_token: Token | None = None
        if operation.kind == "swap" or operation.kind == "burn":
            source = Client.nullifier(operation.token)
            source_token = operation.token
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT payload FROM operations WHERE id=?", (operation.id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != operation.model_dump_json():
                    raise ValueError("operation id already belongs to another request")
                return
            if source is not None and source_token is not None:
                row = conn.execute(
                    "SELECT state FROM tokens WHERE id=?", (source,)
                ).fetchone()
                allowed = {"LIVE", "SENT"} if operation.kind == "swap" else {"LIVE"}
                if row is not None and row["state"] not in allowed:
                    raise ValueError(
                        "token is already spent or has a pending operation"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO tokens VALUES (?,?,'LIVE')",
                    (source, source_token.model_dump_json()),
                )
                conn.execute(
                    "UPDATE tokens SET state='PENDING',token=? WHERE id=?",
                    (source_token.model_dump_json(), source),
                )
            conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (operation.id, operation.kind, operation.model_dump_json(), source),
            )

    def mint_operation(self, operation_id: str) -> MintOperation:
        return MintOperation.model_validate_json(self._operation(operation_id, "mint"))

    def pending_mint(self, quote_id: str) -> MintOperation | None:
        with self.db.read() as conn:
            operations = [
                MintOperation.model_validate_json(row["payload"])
                for row in conn.execute(
                    "SELECT payload FROM operations WHERE kind='mint'"
                )
            ]
        matches = [
            operation for operation in operations if operation.quote.quote == quote_id
        ]
        if len(matches) > 1:
            raise ValueError(
                "multiple saved requests for this quote; use pending and retry"
            )
        return matches[0] if matches else None

    def swap_operation(self, operation_id: str) -> SwapOperation:
        return SwapOperation.model_validate_json(self._operation(operation_id, "swap"))

    def set_mint_preimage(self, operation_id: str, preimage: str) -> MintOperation:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM operations WHERE id=? AND kind='mint'",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown mint operation")
            operation = MintOperation.model_validate(
                json.loads(row["payload"]) | {"preimage": preimage}
            )
            conn.execute(
                "UPDATE operations SET payload=? WHERE id=?",
                (operation.model_dump_json(), operation_id),
            )
        return operation

    def burn_operation(self, operation_id: str) -> BurnOperation:
        return BurnOperation.model_validate_json(self._operation(operation_id, "burn"))

    def _operation(self, operation_id: str, kind: str) -> str:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if row is None or row["kind"] != kind:
            raise ValueError("unknown operation or incorrect operation kind")
        return row["payload"]

    def pending(self) -> list[dict[str, str]]:
        with self.db.read() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT id,kind FROM operations").fetchall()
            ]

    def token(self, token_id: str) -> Token:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT token,state FROM tokens WHERE id=?", (token_id,)
            ).fetchone()
        if row is None or row["state"] != "LIVE":
            raise ValueError("unknown token or token is not live")
        return Token.model_validate_json(row["token"])

    def tokens(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT id,token,state FROM tokens").fetchall()
        return [
            {
                "id": r["id"],
                "state": r["state"],
                "amount": Token.model_validate_json(r["token"]).amount,
                "payment_hash": Token.model_validate_json(r["token"]).payment_hash,
            }
            for r in rows
        ]

    def select_token(
        self,
        amount: int,
        token_id: str | None = None,
        payment_hash: str | None = None,
        mint: str | None = None,
    ) -> Token:
        """Select exactly one whole HTLC claim; never split or combine claims."""
        with self.db.read() as conn:
            rows = conn.execute("SELECT id,token FROM tokens WHERE state='LIVE'")
            matches = [
                token
                for row in rows
                if (token_id is None or row["id"] == token_id)
                and (token := Token.model_validate_json(row["token"])).amount == amount
                and (payment_hash is None or token.payment_hash == payment_hash)
                and (mint is None or token.mint.rstrip("/") == mint.rstrip("/"))
            ]
        if not matches:
            raise ValueError(
                "no available token matches this whole HTLC claim; "
                "amounts cannot be split or combined"
            )
        if len(matches) != 1:
            raise ValueError("multiple tokens match; select one with --token-id")
        return matches[0]

    def send(
        self,
        amount: int,
        token_id: str | None = None,
        mint: str | None = None,
        preimage: str | None = None,
    ) -> str:
        # An explicit ID can recover a send whose stdout was lost.
        if token_id is not None:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT token FROM tokens WHERE id=? AND state='SENT'", (token_id,)
                ).fetchone()
            if row is not None:
                token = Token.model_validate_json(row["token"])
                if preimage is not None:
                    token = token.with_preimage(preimage)
                if token.preimage is None:
                    raise ValueError(
                        "token has no HTLC preimage; supply --preimage with send"
                    )
                if token.amount != amount or (
                    mint is not None and token.mint.rstrip("/") != mint.rstrip("/")
                ):
                    raise ValueError("sent token does not match the requested claim")
                with self.db.transaction() as conn:
                    updated = conn.execute(
                        "UPDATE tokens SET token=? WHERE id=? AND state='SENT'",
                        (token.model_dump_json(), token_id),
                    )
                    if updated.rowcount != 1:
                        raise ValueError("token has a pending operation")
                return encode_token(token)
        token = self.select_token(amount, token_id=token_id, mint=mint)
        if preimage is not None:
            token = token.with_preimage(preimage)
        if token.preimage is None:
            raise ValueError("token has no HTLC preimage; supply --preimage with send")
        encoded = encode_token(token)
        with self.db.transaction() as conn:
            updated = conn.execute(
                "UPDATE tokens SET state='SENT',token=? WHERE id=? AND state='LIVE'",
                (token.model_dump_json(), Client.nullifier(token)),
            )
            if updated.rowcount != 1:
                raise ValueError("token is already sent or has a pending operation")
        return encoded

    def complete(self, operation_id: str, token: Token | None) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT source FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown pending operation")
            if row["source"] is not None:
                conn.execute(
                    "UPDATE tokens SET state='SPENT' WHERE id=?", (row["source"],)
                )
            if token is not None:
                conn.execute(
                    "INSERT INTO tokens VALUES (?,?,'LIVE')",
                    (Client.nullifier(token), token.model_dump_json()),
                )
            conn.execute("DELETE FROM operations WHERE id=?", (operation_id,))

    def retry(
        self, client: Client, operation_id: str, kind: str
    ) -> Token | BurnResponse:
        if kind == "mint":
            token = client.finish_mint(self.mint_operation(operation_id))
            self.complete(operation_id, token)
            return token
        if kind == "swap":
            token = client.finish_swap(self.swap_operation(operation_id))
            self.complete(operation_id, token)
            return token
        if kind == "burn":
            response = client.finish_burn(self.burn_operation(operation_id))
            self.complete(operation_id, None)
            return response
        raise ValueError("unknown operation kind")

    def abandon(self, client: Client, operation_id: str) -> None:
        """Release a failed spend only after confirming it is unspent at the mint."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown operation")
        if row["source"] is None:
            raise ValueError("retry mint operations to preserve their recovery secrets")
        if client.check([row["source"]]).states != ["UNSPENT"]:
            raise ValueError(
                "mint has spent or reserved this token; retry the operation"
            )
        with self.db.transaction() as conn:
            conn.execute("UPDATE tokens SET state='LIVE' WHERE id=?", (row["source"],))
            conn.execute("DELETE FROM operations WHERE id=?", (operation_id,))
