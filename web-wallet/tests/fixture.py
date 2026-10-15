"""Disposable, loopback-only Python mint for browser/protocol interoperability tests."""

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Literal

import uvicorn
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tmp_cash.api import create_app
from tmp_cash.lightning import FakeBackend
from tmp_cash.mint import Mint


class Funding(BaseModel):
    payment_hash: str


class Receiver(BaseModel):
    amount: int


class Quote(BaseModel):
    quote: str


class PaymentControl(BaseModel):
    payment_hash: str
    state: Literal["IN_FLIGHT", "FAILED"] | None


class FeePolicy(BaseModel):
    fee_limit_sat: int = Field(ge=0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3339)
    parser.add_argument("--fee-limit-sat", type=int, default=2)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tmp-cash-web-test-") as directory:
        path = Path(directory)
        backend = FakeBackend(
            path / "fake.sqlite3",
            fee_msat=1500,
            fee_limit_sat=args.fee_limit_sat,
        )
        mint = Mint(path / "mint.sqlite3", bytes.fromhex("42" * 32), backend)
        app = create_app(mint)

        @app.get("/test/health")
        async def health():
            return {"test_fixture": True}

        @app.post("/test/accept")
        async def accept(request: Funding):
            await backend.accept(request.payment_hash)
            return {"accepted": True}

        @app.post("/test/settle")
        async def settle(request: Funding):
            # Simulate settlement/expiry without exposing a production control API.
            with backend.db.transaction() as conn:
                conn.execute(
                    "UPDATE invoices SET state='SETTLED' WHERE payment_hash=?",
                    (request.payment_hash,),
                )
            return {"settled": True}

        @app.post("/test/receiver")
        async def receiver(request: Receiver):
            return {"request": backend.receiver_invoice(request.amount, os.urandom(32))}

        @app.post("/test/expire-melt-quote")
        async def expire_melt_quote(request: Quote):
            quote = mint._melt_quote(request.quote).model_copy(update={"expiry": 0})
            with mint.db.transaction() as conn:
                conn.execute(
                    "UPDATE melt_quotes SET payload=? WHERE quote=?",
                    (quote.model_dump_json(), quote.quote),
                )
            return {"expired": True}

        @app.post("/test/payment-state")
        async def payment_state(request: PaymentControl):
            with backend.db.transaction() as conn:
                if request.state is None:
                    conn.execute(
                        "DELETE FROM payments WHERE payment_hash=?",
                        (request.payment_hash,),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO payments VALUES (?,?,NULL,0)",
                        (request.payment_hash, request.state),
                    )
            return {"updated": True}

        @app.post("/test/fee-policy")
        async def fee_policy(request: FeePolicy):
            mint.fee_limit_sat = request.fee_limit_sat
            backend.fee_limit_sat = request.fee_limit_sat
            return {"configured": True}

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
