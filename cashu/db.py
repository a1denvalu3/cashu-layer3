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
