"""Transport layer: every network call in this kit goes through a Transport.

``UrllibTransport`` speaks to the real Bot API using nothing but the standard
library. ``MockTransport`` is a fully scripted stand-in so every behaviour in
the kit can be exercised offline (see ``tgopskit.selftest``). Code that wants
to talk to Telegram should accept *any* Transport and never import urllib
directly -- that seam is what makes the whole kit testable without a token.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Protocol

API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """A Bot API call returned ok=false (or the response was unreadable)."""

    def __init__(self, description: str, error_code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class RetryAfter(TelegramError):
    """HTTP 429: Telegram told us to back off for ``seconds`` seconds."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"rate limited, retry after {seconds}s", 429)
        self.seconds = seconds


class TransportError(TelegramError):
    """The request never produced a Bot API answer (connection refused, DNS failure, timeout, reset).

    ``maybe_delivered`` says whether Telegram may nevertheless have received the request: ``False`` for a failure
    before the request left (connect, DNS), ``True`` once it was sent and the reply was lost (read timeout, reset).
    A sender may retry the first kind freely; retrying the second can duplicate a message, which for an ops alert
    is still better than losing it.
    """

    def __init__(self, description: str, maybe_delivered: bool) -> None:
        super().__init__(description)
        self.maybe_delivered = maybe_delivered


class ConflictError(TelegramError):
    """HTTP 409 from getUpdates: ANOTHER consumer is polling this token.

    Two pollers on one token steal updates from each other and both spend
    their lives in 409 storms. Exactly one process may long-poll a token.
    """

    def __init__(self, description: str) -> None:
        super().__init__(description, 409)


class Transport(Protocol):
    """Minimal contract: call a Bot API method, return its ``result``."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke ``method`` with ``params``; raise TelegramError on failure."""
        ...


class UrllibTransport:
    """Real Bot API transport built on ``urllib`` -- no external deps."""

    def __init__(self, token: str, base_url: str = API_BASE, timeout: float = 65.0) -> None:
        self._url_prefix = f"{base_url}/bot{token}/"
        self._timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """POST ``params`` as JSON to ``method`` and unwrap the response."""
        request = urllib.request.Request(
            self._url_prefix + method,
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                try:
                    payload = json.load(response)
                except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                    # the request was sent; only the reply was lost
                    raise TransportError(f"reply lost after the request was sent: {type(exc).__name__}", True) from exc
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)  # Telegram sends JSON bodies on errors too
            except (ValueError, OSError):
                raise TelegramError(f"HTTP {exc.code} with unreadable body", exc.code) from exc
        except urllib.error.URLError as exc:
            # connection refused, DNS failure, connect timeout: nothing reached Telegram
            raise TransportError(f"could not reach the Bot API: {exc.reason}", False) from exc
        except TimeoutError as exc:
            raise TransportError("timed out waiting for the Bot API", True) from exc
        if payload.get("ok"):
            return payload.get("result")
        code = payload.get("error_code")
        description = payload.get("description", "unknown Bot API error")
        if code == 429:
            raise RetryAfter(int(payload.get("parameters", {}).get("retry_after", 1)))
        if code == 409:
            raise ConflictError(description)
        raise TelegramError(description, code)


class MockTransport:
    """Scripted offline transport: queue updates, program failures, inspect calls.

    Used by the self-test; also handy for unit-testing your own handlers.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._update_batches: list[list[dict[str, Any]]] = []
        self._planned_errors: dict[str, list[Exception]] = {}
        self._next_message_id = 100

    def queue_updates(self, batch: list[dict[str, Any]]) -> None:
        """Queue one getUpdates response batch (consumed in FIFO order)."""
        self._update_batches.append(batch)

    def fail_next(self, method: str, error: Exception) -> None:
        """Make the next call to ``method`` raise ``error`` (then recover)."""
        self._planned_errors.setdefault(method, []).append(error)

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        """Return the params of every recorded call to ``method``."""
        return [params for name, params in self.calls if name == method]

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((method, params))
        planned = self._planned_errors.get(method)
        if planned:
            raise planned.pop(0)
        if method == "getUpdates":
            batch = self._update_batches.pop(0) if self._update_batches else []
            offset = params.get("offset")
            if offset is not None:
                batch = [u for u in batch if u["update_id"] >= offset]
            return batch
        if method == "sendMessage":
            self._next_message_id += 1
            return {"message_id": self._next_message_id, "chat": {"id": params.get("chat_id")}}
        return True  # answerCallbackQuery, editMessageText, ...
