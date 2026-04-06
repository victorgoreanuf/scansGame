"""Adaptive per-account rate limiter."""

import asyncio
import time


class RateLimiter:
    def __init__(self, base_delay: float = 2.5, max_delay: float = 15.0):
        self.base_delay = base_delay
        self.delay = base_delay
        self.max_delay = max_delay
        self._last_request: float = 0

    async def wait(self) -> None:
        """Wait the appropriate amount of time before the next request."""
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self.delay:
            await asyncio.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def on_success(self) -> None:
        """Gradually reduce delay back toward base on success."""
        if self.delay > self.base_delay:
            self.delay = max(self.base_delay, self.delay - 0.5)

    def on_rate_limit(self) -> None:
        """Increase delay when rate limited."""
        self.delay = min(self.max_delay, self.delay + 1.0)

    def reset(self) -> None:
        self.delay = self.base_delay
