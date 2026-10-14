"""Rate-limited sending with retry-after respect.

Telegram answers a flood of sends with HTTP 429 and an explicit
``retry_after``. Ignoring it and hammering only extends the penalty, so this
sender (a) spaces outbound calls by a minimum interval and (b) sleeps exactly
the server-instructed backoff before retrying. ``sleep`` and ``clock`` are
injectable so tests run instantly with a fake clock.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .transport import RetryAfter, Transport


class RateLimitedSender:
    """Wrap a Transport with outbound spacing and 429 retry handling."""

    def __init__(
        self,
        transport: Transport,
        min_interval: float = 1.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_send: float | None = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke ``method`` with spacing; on 429, back off as told and retry."""
        retries = 0
        while True:
            if self._last_send is not None:
                wait = self._min_interval - (self._clock() - self._last_send)
                if wait > 0:
                    self._sleep(wait)
            try:
                result = self._transport.call(method, dict(params or {}))
            except RetryAfter as exc:
                retries += 1
                if retries > self._max_retries:
                    raise
                self._sleep(float(exc.seconds))
            else:
                self._last_send = self._clock()
                return result

    def send_message(self, chat_id: int, text: str, **extra: Any) -> Any:
        """Convenience wrapper for sendMessage (extra kwargs pass through)."""
        return self.call("sendMessage", {"chat_id": chat_id, "text": text, **extra})
