import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client
from cashu.lightning import FakeBackend
from cashu.mint import Mint
from cashu.models import Token

SEED = b"layer3 test mint seed............"
PREIMAGE = hashlib.sha256(b"receiver's test preimage").digest()
PAYOUT_PREIMAGE = hashlib.sha256(b"unrelated payout" + PREIMAGE).digest()


@dataclass
class Service:
    backend: FakeBackend
    mint: Mint
    client: Client
    path: Path

    def fund(self, amount: int = 1000, preimage: bytes = PREIMAGE) -> tuple[Token, str]:
        payment_hash = hashlib.sha256(preimage).hexdigest()
        payout = self.backend.receiver_invoice(
            amount, hashlib.sha256(b"unrelated payout" + preimage).digest()
        )
        quote = self.client.quote(amount, payment_hash)
        asyncio.run(self.backend.accept(payment_hash))
        token = self.client.finish_mint(
            self.client.prepare_mint(quote.quote, preimage.hex())
        )
        return token, payout


@pytest.fixture
def service(tmp_path):
    backend = FakeBackend(tmp_path / "fake.sqlite3")
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        yield Service(backend, mint, Client("http://testserver", http), tmp_path)
