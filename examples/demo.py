"""Run a complete transfer and redemption with the persistent fake backend."""

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client
from cashu.lightning import FakeBackend
from cashu.mint import Mint
from cashu.wallet import Wallet


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cashu-demo-") as directory:
        path = Path(directory)
        backend = FakeBackend(path / "fake.sqlite3")
        mint = Mint(path / "mint.sqlite3", os.urandom(32), backend)
        # The holder chooses the backing preimage and carries it in the token.
        preimage = os.urandom(32)
        payout = backend.receiver_invoice(1000, os.urandom(32))
        payment_hash = hashlib.sha256(preimage).hexdigest()
        with TestClient(create_app(mint, recovery=False)) as http:
            client = Client("http://testserver", http)
            alice = Wallet(path / "alice.sqlite3")
            bob = Wallet(path / "bob.sqlite3")
            carol = Wallet(path / "carol.sqlite3")
            quote = client.quote(1000, payment_hash)
            print("Quote created with the backing hash; mint has no preimage")
            asyncio.run(backend.accept(payment_hash))
            operation = client.prepare_mint(quote.quote, preimage.hex())
            alice.save(operation)
            token = alice.retry(client, operation.id, "mint")
            print("Alice minted a 1000-sat PS token while the HTLC is held")
            for name, wallet in (("Bob", bob), ("Carol", carol)):
                received = client.prepare_swap(token)
                wallet.save(received)
                token = wallet.retry(client, received.id, "swap")
                print(
                    f"{name} received the token; its payment hash stayed hidden during the swap"
                )
            burn = client.prepare_burn(token, payout)
            carol.save(burn)
            receipt = carol.retry(client, burn.id, "burn")
            print(
                f"Carol redeemed {receipt.amount} sats; preimage settled the original hold"
            )


if __name__ == "__main__":
    main()
