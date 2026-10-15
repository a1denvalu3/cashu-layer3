"""Bounded, process-local caching of successful operation responses."""

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic

from pydantic import BaseModel

CacheKey = tuple[str, bytes]


@dataclass
class CachedResponse:
    body: bytes
    model: type[BaseModel]
    expires_at: float


@dataclass
class PendingRequest:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SuccessfulResponseCache:
    def __init__(
        self,
        ttl: float = 300,
        max_entries: int = 1024,
        max_bytes: int = 16 * 1024 * 1024,
    ):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._responses: OrderedDict[CacheKey, CachedResponse] = OrderedDict()
        self._pending: dict[CacheKey, PendingRequest] = {}
        self._bytes = 0

    def clear(self) -> None:
        self._responses.clear()
        self._bytes = 0

    def _remove(self, key: CacheKey) -> None:
        self._bytes -= len(self._responses.pop(key).body)

    def _get(self, key: CacheKey) -> BaseModel | None:
        cached = self._responses.get(key)
        if cached is None:
            return None
        if cached.expires_at <= monotonic():
            self._remove(key)
            return None
        self._responses.move_to_end(key)
        # Each caller gets its own model; a caller cannot mutate the cache.
        return cached.model.model_validate_json(cached.body)

    def _put(self, key: CacheKey, response: BaseModel) -> None:
        body = response.model_dump_json().encode()
        now = monotonic()
        for expired in [
            key for key, entry in self._responses.items() if entry.expires_at <= now
        ]:
            self._remove(expired)
        if self.ttl <= 0 or self.max_entries <= 0 or len(body) > self.max_bytes:
            return
        while self._responses and (
            len(self._responses) >= self.max_entries
            or self._bytes + len(body) > self.max_bytes
        ):
            self._remove(next(iter(self._responses)))
        self._responses[key] = CachedResponse(body, type(response), now + self.ttl)
        self._bytes += len(body)

    async def run(
        self,
        endpoint: str,
        request: BaseModel,
        operation: Callable[[], BaseModel | Awaitable[BaseModel]],
    ) -> BaseModel:
        # Hash validated models so field order, whitespace and omitted defaults
        # do not change the key. Never retain request secrets in cache keys.
        key = (endpoint, hashlib.sha256(request.model_dump_json().encode()).digest())
        cached = self._get(key)
        if cached is not None:
            return cached
        pending = self._pending.get(key)
        if pending is None:
            pending = self._pending[key] = PendingRequest()
        pending.users += 1
        try:
            async with pending.lock:
                cached = self._get(key)
                if cached is not None:
                    return cached
                result = operation()
                response = result if isinstance(result, BaseModel) else await result
                # Exceptions, including pending melts and cancellations, never
                # reach this point. Durable mint recovery remains authoritative.
                self._put(key, response)
                return response
        finally:
            pending.users -= 1
            if pending.users == 0:
                del self._pending[key]
