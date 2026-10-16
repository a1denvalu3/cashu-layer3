import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """Short SQLite transactions; network calls never hold a write lock."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.read() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class MintError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class BackendError(Exception):
    pass


class IncompatibleMintDatabase(ValueError):
    pass


class MintDatabaseInUse(RuntimeError):
    pass


_singleton_locks: dict[Path, int] = {}


def acquire_singleton_lock(path: Path) -> bool:
    """Hold an exclusive flock next to the mint database for process lifetime.

    One mint process per database: a second OS process fails closed instead of
    inheriting orphaned melt executor locks. Reentrant per canonical path so
    tests and embedders can reopen a mint inside the same process. Returns
    True only for this process's first acquisition of the database.
    """
    key = path.resolve()
    if key in _singleton_locks:
        return False
    key.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key.with_name(key.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise MintDatabaseInUse(
            "mint database is already in use by another mint process"
        ) from None
    _singleton_locks[key] = fd
    return True
