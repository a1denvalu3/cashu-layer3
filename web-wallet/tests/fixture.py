"""Disposable, loopback-only Python mint for browser/protocol interoperability tests."""

import argparse
import os
import sys
import tempfile
from pathlib import Path

import uvicorn
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cashu.api import create_app
from cashu.lightning import FakeBackend
from cashu.mint import Mint


class Funding(BaseModel):
    payment_hash: str


class Receiver(BaseModel):
    amount: int


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3339)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cashu-web-test-") as directory:
        path = Path(directory)
        backend = FakeBackend(path / "fake.sqlite3", fee_msat=1500)
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

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
