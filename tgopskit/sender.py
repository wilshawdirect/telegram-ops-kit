"""Rate-limited sending with retry-after respect.

Telegram answers a flood of sends with HTTP 429 and an explicit
``retry_after``. Ignoring it and hammering only extends the penalty, so this
sender (a) spaces outbound calls by a minimum interval and (b) sleeps exactly
the server-instructed backoff before retrying. A transport failure
(``TransportError``) is retried with a short backoff: freely when the request
never left (3 tries at 0.5 / 2 / 6 s), once when it was sent and only the reply
was lost (a duplicate alert beats a lost one). ``sleep`` and ``clock`` are
injectable so tests run instantly with a fake clock.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Callable

from .transport import RetryAfter, Transport, TransportError


class RateLimitedSender:
    """Wrap a Transport with outbound spacing and 429 retry handling."""

    def __init__(
        self,
        transport: Transport,
        min_interval: float = 1.0,
        max_retries: int = 3,
        transport_backoff: Sequence[float] = (0.5, 2.0, 6.0),
        ambiguous_retries: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff = tuple(transport_backoff) or (0.5,)
        self._ambiguous_retries = ambiguous_retries
        self._sleep = sleep
        self._clock = clock
        self._last_send: float | None = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke ``method`` with spacing; on 429, back off as told and retry."""
        retries = 0
        unreached = 0   # failures before the request left: safe to retry
        ambiguous = 0   # request sent, reply lost: it may have been delivered
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
            except TransportError as exc:
                if exc.maybe_delivered:
                    ambiguous += 1
                    if ambiguous > self._ambiguous_retries:
                        raise
                    self._sleep(self._backoff[0])
                else:
                    unreached += 1
                    if unreached > len(self._backoff):
                        raise
                    self._sleep(self._backoff[unreached - 1])
            else:
                self._last_send = self._clock()
                return result

    def send_message(self, chat_id: int, text: str, **extra: Any) -> Any:
        """Convenience wrapper for sendMessage (extra kwargs pass through)."""
        return self.call("sendMessage", {"chat_id": chat_id, "text": text, **extra})
