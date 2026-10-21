"""telegram-ops-kit: a minimal, safe pattern for operations/approval Telegram bots.

Pure standard library. Five production-hardened pieces:

* ``UpdatePoller`` + ``OffsetStore`` -- single-consumer getUpdates polling
  with durable offset persistence.
* ``OperatorGuard`` -- user-id allowlist on every update, DM-only for
  sensitive commands, dead-man's heartbeat on every operator interaction.
* ``ApprovalManager`` -- inline-keyboard approval cards with nonce
  verification, idempotent double-tap handling, and expiry.
* ``RateLimitedSender`` -- outbound spacing, exact 429 retry_after respect and a short retry on transport failures.
* ``Transport`` seam -- ``UrllibTransport`` for the real API,
  ``MockTransport`` for fully offline tests.
"""
from .approvals import ApprovalCard, ApprovalManager, CallbackResult
from .guard import OperatorGuard, Verdict
from .poller import OffsetStore, UpdatePoller
from .selftest import run_self_test
from .sender import RateLimitedSender
from .transport import (
    ConflictError,
    MockTransport,
    RetryAfter,
    TelegramError,
    Transport,
    TransportError,
    UrllibTransport,
)

__version__ = "1.0.0"
__all__ = [
    "ApprovalCard",
    "ApprovalManager",
    "CallbackResult",
    "ConflictError",
    "MockTransport",
    "OffsetStore",
    "OperatorGuard",
    "RateLimitedSender",
    "RetryAfter",
    "TelegramError",
    "Transport",
    "TransportError",
    "UpdatePoller",
    "UrllibTransport",
    "Verdict",
    "run_self_test",
]
