"""Async request-weight limiter. / Асинхронный лимитер веса REST-запросов."""

from __future__ import annotations

import asyncio
import time


class AsyncTokenBucket:
    """A monotonic token bucket shared by REST workers. / Общая корзина токенов REST."""

    def __init__(self, capacity: float = 2_200.0, refill_per_second: float = 36.0) -> None:
        self._capacity = capacity
        self._tokens = capacity
        self._refill_per_second = refill_per_second
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float = 1.0) -> None:
        if weight <= 0 or weight > self._capacity:
            raise ValueError("weight must be positive and no greater than bucket capacity")

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._refill_per_second,
                )
                self._updated_at = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                missing = weight - self._tokens

            # Sleep outside the lock so other light requests can proceed.
            # Спим вне lock, чтобы лёгкие запросы могли выполняться параллельно.
            await asyncio.sleep(max(missing / self._refill_per_second, 0.01))
