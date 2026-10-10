"""Mint, split, transfer, partially melt, and close an expiring HTLC claim."""

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client
from cashu.lightning import FakeBackend
from cashu.mint import Mint
from cashu.models import Token, preimage_hash
from cashu.token import decode_token
from cashu.wallet import Wallet


def main():
    with tempfile.TemporaryDirectory(prefix="cashu-v3-demo-") as directory:
        path = Path(directory)
        backend = FakeBackend(path / "fake.sqlite3")
        mint = Mint(path / "mint.sqlite3", os.urandom(32), backend)
        preimage = os.urandom(32).hex()
        with TestClient(create_app(mint, recovery=False)) as http:
            client = Client("http://testserver", http)
            alice = Wallet(path / "alice.sqlite3")
            bob = Wallet(path / "bob.sqlite3")
            quote = client.quote(1000, preimage_hash(preimage))
            asyncio.run(backend.accept(quote.payment_hash))
            operation = client.prepare_mint(quote.quote, preimage)
            alice.save(operation)
            token = alice.retry(client, operation.id)
            assert isinstance(token, Token)
            print(
                f"Alice minted 1000 sats as denominations: {[p.amount for p in token.proofs]}"
            )
            sent = decode_token(alice.send(client, 10))
            swap = client.prepare_swap(sent)
            bob.save(swap)
            received = bob.retry(client, swap.id)
            assert isinstance(received, Token)
            print(
                "Alice sent 10 sats; Bob privately refreshed 8+2 sats; Alice retained 990 sats"
            )
            invoice = backend.receiver_invoice(9, os.urandom(32))
            melt = client.prepare_melt(received, invoice, fee_reserve=1)
            bob.save(melt)
            receipt = bob.retry(client, melt.id)
            print(
                f"Bob paid 9 sats; unused fee reserve returned as 1 sat: {receipt.model_dump_json()}"
            )
            print("Original Lightning payment is still held")
            with backend.db.transaction() as conn:
                import time

                conn.execute(
                    "UPDATE invoices SET expiry=? WHERE payment_hash=?",
                    (int(time.time()) + mint.settle_margin_seconds, quote.payment_hash),
                )
            asyncio.run(mint.reconcile_holds())
            alice.refresh(client)
            bob.refresh(client)
            print(
                "At the published cutoff the mint settled; Alice's 990 and Bob's 1 sat expired"
            )


if __name__ == "__main__":
    main()
