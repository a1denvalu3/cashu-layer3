import hashlib
import hmac
import os
import sqlite3
import subprocess
import sys

import pytest
from conftest import SEED

from tmp_cash.db import IncompatibleMintDatabase, MintDatabaseInUse
from tmp_cash.lightning import FakeBackend
from tmp_cash.mint import Mint

FINGERPRINT = hmac.new(
    SEED, b"Cashu_PS_Denominations_DB_v3", hashlib.sha256
).hexdigest()

V1_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE quotes (quote TEXT PRIMARY KEY, payment_hash TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL, h TEXT UNIQUE NOT NULL, request TEXT NOT NULL, expiry INTEGER NOT NULL, issued INTEGER NOT NULL DEFAULT 0, digest TEXT, response TEXT, preimage TEXT, redeemed INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE nullifiers (nullifier TEXT PRIMARY KEY, state TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, response TEXT);
CREATE TABLE melt_quotes (quote TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), payout_hash TEXT UNIQUE NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT);
CREATE TABLE failed_melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), digest TEXT NOT NULL);
CREATE TABLE melt_inputs (melt TEXT NOT NULL REFERENCES melts(quote) ON DELETE CASCADE, payment_hash TEXT NOT NULL REFERENCES quotes(payment_hash), amount INTEGER NOT NULL, PRIMARY KEY(melt,payment_hash));
"""


V2_SCHEMA = V1_SCHEMA.replace(
    "CREATE TABLE melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), payout_hash TEXT UNIQUE NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT);",
    "CREATE TABLE melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), payout_hash TEXT UNIQUE NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, response TEXT, attempt TEXT NOT NULL DEFAULT '', melt_lock TEXT NOT NULL DEFAULT '', submitted INTEGER NOT NULL DEFAULT 1);",
).replace(
    "CREATE TABLE failed_melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), digest TEXT NOT NULL);",
    "CREATE TABLE failed_melts (quote TEXT PRIMARY KEY REFERENCES melt_quotes(quote), digest TEXT NOT NULL, expired INTEGER NOT NULL DEFAULT 0);",
)


def make_v1_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO metadata VALUES ('protocol','ps-htlc-denominations-v3')"
        )
        conn.execute(
            "INSERT INTO metadata VALUES ('seed_fingerprint',?)", (FINGERPRINT,)
        )


def test_fresh_database_starts_at_current_schema_version(tmp_path):
    mint = Mint(tmp_path / "mint.sqlite3", SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with mint.db.read() as conn:
        version = conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        assert version[0] == "2"
        melt_columns = {r[1] for r in conn.execute("PRAGMA table_info(melts)")}
        assert {"attempt", "melt_lock", "submitted"} <= melt_columns
        failed_columns = {r[1] for r in conn.execute("PRAGMA table_info(failed_melts)")}
        assert "expired" in failed_columns


def test_v1_database_migrates_to_versioned_attempt_schema(tmp_path):
    path = tmp_path / "mint.sqlite3"
    make_v1_db(path)
    paid_quote, pending_quote, failed_quote = (os.urandom(32).hex() for _ in range(3))
    with sqlite3.connect(path) as conn:
        for quote in (paid_quote, pending_quote, failed_quote):
            conn.execute("INSERT INTO melt_quotes VALUES (?,?)", (quote, "{}"))
        conn.execute(
            "INSERT INTO melts (quote,payout_hash,request,digest,state,response) VALUES (?,?,?,?,'PAID','{}')",
            (paid_quote, os.urandom(32).hex(), "{}", os.urandom(32).hex()),
        )
        conn.execute(
            "INSERT INTO melts (quote,payout_hash,request,digest,state) VALUES (?,?,?,?,'PENDING')",
            (pending_quote, os.urandom(32).hex(), "{}", os.urandom(32).hex()),
        )
        conn.execute(
            "INSERT INTO failed_melts VALUES (?,?)",
            (failed_quote, os.urandom(32).hex()),
        )
    mint = Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with mint.db.read() as conn:
        version = conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        assert version[0] == "2"
        rows = {r["quote"]: r for r in conn.execute("SELECT * FROM melts")}
        # It cannot be proven whether old PENDING rows reached the backend, so
        # they are reconcile-only (submitted=1) and unlocked after migration.
        assert rows[paid_quote]["submitted"] == 1
        assert rows[pending_quote]["submitted"] == 1
        assert all(row["melt_lock"] == "" for row in rows.values())
        assert all(len(row["attempt"]) == 32 for row in rows.values())
        expired = conn.execute(
            "SELECT expired FROM failed_melts WHERE quote=?", (failed_quote,)
        ).fetchone()
        assert expired[0] == 0
    # Reopening a migrated database is stable.
    Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))


def test_reentrant_reopen_does_not_steal_live_executor_locks(tmp_path):
    path = tmp_path / "mint.sqlite3"
    mint = Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    quote = os.urandom(32).hex()
    with mint.db.transaction() as conn:
        conn.execute("INSERT INTO melt_quotes VALUES (?,?)", (quote, "{}"))
        conn.execute(
            "INSERT INTO melts (quote,payout_hash,request,digest,state,attempt,melt_lock,submitted) VALUES (?,?,?,?,'PENDING',?,?,1)",
            (
                quote,
                os.urandom(32).hex(),
                "{}",
                os.urandom(32).hex(),
                "a" * 32,
                "a" * 32,
            ),
        )
    # A same-process reopen is not a restart: it must not clear the lock.
    reopened = Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with reopened.db.read() as conn:
        row = conn.execute("SELECT melt_lock,attempt,submitted FROM melts").fetchone()
        assert row[:] == ("a" * 32, "a" * 32, 1)


def test_first_process_acquisition_clears_orphaned_executor_locks(tmp_path):
    path = tmp_path / "mint.sqlite3"
    quote = os.urandom(32).hex()
    # A current-schema database abandoned by a crashed process: first
    # acquisition in this process performs the orphan cleanup.
    with sqlite3.connect(path) as conn:
        conn.executescript(V2_SCHEMA)
        conn.execute(
            "INSERT INTO metadata VALUES ('protocol','ps-htlc-denominations-v3')"
        )
        conn.execute(
            "INSERT INTO metadata VALUES ('seed_fingerprint',?)", (FINGERPRINT,)
        )
        conn.execute("INSERT INTO metadata VALUES ('schema_version','2')")
        conn.execute("INSERT INTO melt_quotes VALUES (?,?)", (quote, "{}"))
        conn.execute(
            "INSERT INTO melts (quote,payout_hash,request,digest,state,attempt,melt_lock,submitted) VALUES (?,?,?,?,'PENDING',?,?,1)",
            (
                quote,
                os.urandom(32).hex(),
                "{}",
                os.urandom(32).hex(),
                "b" * 32,
                "b" * 32,
            ),
        )
    mint = Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with mint.db.read() as conn:
        row = conn.execute("SELECT melt_lock,attempt,submitted FROM melts").fetchone()
        assert row[:] == ("", "b" * 32, 1)


def test_future_schema_version_is_rejected_without_touching_the_database(tmp_path):
    path = tmp_path / "mint.sqlite3"
    Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
    with pytest.raises(IncompatibleMintDatabase, match="newer schema version"):
        Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with sqlite3.connect(path) as conn:
        version = conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        assert version[0] == "3"


@pytest.mark.parametrize("reserved,redeemed", [(-1, 0), (0, -1), (5, 6)])
def test_invalid_backing_accounting_is_rejected(tmp_path, reserved, redeemed):
    path = tmp_path / "mint.sqlite3"
    Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO quotes (quote,payment_hash,amount,h,request,expiry,reserved,redeemed) VALUES (?,?,?,?,?,?,?,?)",
            (
                os.urandom(32).hex(),
                os.urandom(32).hex(),
                10,
                os.urandom(32).hex(),
                "lnbcrt100n1placeholder",
                0,
                reserved,
                redeemed,
            ),
        )
    with pytest.raises(IncompatibleMintDatabase, match="accounting"):
        Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))


def test_mint_reopens_reentrantly_in_one_process(tmp_path):
    backend = FakeBackend(tmp_path / "fake.sqlite3")
    Mint(tmp_path / "mint.sqlite3", SEED, backend)
    Mint(tmp_path / "mint.sqlite3", SEED, backend)


def test_second_process_cannot_open_a_held_database(tmp_path):
    path = tmp_path / "mint.sqlite3"
    script = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "sys.stdout.write('locked\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, f"{path.resolve()}.lock"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(MintDatabaseInUse, match="already in use by another mint"):
            Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
    finally:
        holder.kill()
        holder.wait()
    # The database opens normally once the holder is gone.
    Mint(path, SEED, FakeBackend(tmp_path / "fake.sqlite3"))
