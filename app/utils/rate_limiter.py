"""Async token-bucket rate limiter.

Paces outbound work that a remote service meters — Google photo requests, and
separately S3 puts. A bucket refills at `rate_per_second` and holds at most
`burst` tokens, so a run that has been idle may surge briefly and then settles to
the configured rate.

The clock and the sleep are injected. That is not decoration: it lets a test
assert the pacing arithmetic exactly, in microseconds, instead of sleeping for
real and asserting on wall-clock timing that is flaky under load.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional


class AsyncRateLimiter:
    """Token bucket. `acquire()` returns the seconds it waited."""

    def __init__(
        self,
        rate_per_second: float,
        burst: Optional[float] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], Awaitable[None]]] = None,
    ):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = float(rate_per_second)
        # Default burst is one second's worth, floored at 1, so a limiter of
        # 0.5/s can still admit a single request without waiting a full bucket.
        self.burst = float(burst if burst is not None else max(1.0, self.rate))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._tokens = self.burst
        self._updated = self._clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until `tokens` are available. Returns the seconds slept.

        Serialised by a lock so concurrent callers queue deterministically
        rather than all observing the same refill and overdrawing the bucket.
        """
        if tokens > self.burst:
            # Never deadlock on a request larger than the bucket can ever hold.
            tokens = self.burst
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            deficit = tokens - self._tokens
            wait = deficit / self.rate
            await self._sleep(wait)
            self._refill()
            # Spend what was waited for; clamp at zero so float drift in the
            # injected clock can never leave a negative balance.
            self._tokens = max(0.0, self._tokens - tokens)
            return wait


def is_throttled(error: BaseException) -> bool:
    """True when an exception looks like a rate-limit or transient server error.

    Reads both shapes we actually see: a bare `status_code` attribute, and
    httpx's `error.response.status_code`.
    """
    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        try:
            code = int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if code == 429 or 500 <= code <= 599:
            return True
    return False


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 30.0,
                  jitter: float = 0.0) -> float:
    """Exponential backoff for retry `attempt` (1-based), optionally jittered.

    `jitter` is a fraction of the delay; the caller supplies the random factor so
    the schedule stays deterministic under test.
    """
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay * (1.0 + max(0.0, jitter))
