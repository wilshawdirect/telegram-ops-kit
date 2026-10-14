"""Approval cards: inline-keyboard yes/no with verified, idempotent callbacks.

The rules that make an approval card safe in production:

* **Unguessable callback data.** Each card carries a random nonce, so a forged
  or stale callback_query cannot decide anything.
* **Operator-verified.** The tapping user is re-checked against the allowlist
  on every callback -- buttons in a group chat are tappable by anyone.
* **Idempotent.** Telegram redelivers taps and humans double-tap; the first
  decision wins and every later tap gets a polite "already decided".
* **Expiring.** A card nobody answered must not stay armed forever; after its
  TTL it can only expire, never approve.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from .guard import OperatorGuard
from .sender import RateLimitedSender

CALLBACK_PREFIX = "apv"
_DECISIONS = {"yes": "approved", "no": "denied"}


@dataclass
class ApprovalCard:
    """One pending yes/no decision, tracked from send to resolution."""

    card_id: str
    chat_id: int
    text: str
    created_at: float
    ttl: float
    message_id: int | None = None
    state: str = "pending"  # pending | approved | denied | expired
    decided_by: int | None = None


@dataclass(frozen=True)
class CallbackResult:
    """What handling one callback_query amounted to."""

    status: str  # approved | denied | duplicate | expired | rejected | unknown | ignored
    note: str
    card: ApprovalCard | None = None


class ApprovalManager:
    """Create approval cards and adjudicate the callbacks they generate."""

    def __init__(
        self,
        sender: RateLimitedSender,
        guard: OperatorGuard,
        ttl: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sender = sender
        self._guard = guard
        self._ttl = ttl
        self._clock = clock
        self._cards: dict[str, ApprovalCard] = {}

    def create(self, chat_id: int, text: str) -> ApprovalCard:
        """Send a yes/no card to ``chat_id`` and start tracking it."""
        card_id = secrets.token_hex(8)
        keyboard = {
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": f"{CALLBACK_PREFIX}:{card_id}:yes"},
                {"text": "Deny", "callback_data": f"{CALLBACK_PREFIX}:{card_id}:no"},
            ]]
        }
        sent = self._sender.send_message(chat_id, text, reply_markup=keyboard)
        card = ApprovalCard(
            card_id=card_id,
            chat_id=chat_id,
            text=text,
            created_at=self._clock(),
            ttl=self._ttl,
            message_id=(sent or {}).get("message_id"),
        )
        self._cards[card_id] = card
        return card

    def handle_callback(self, callback_query: dict[str, Any]) -> CallbackResult:
        """Verify and apply one callback_query tap; always answers the query."""
        data = callback_query.get("data") or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
            return CallbackResult("ignored", "callback data is not an approval card")
        _, card_id, choice = parts

        verdict = self._guard.check_update({"callback_query": callback_query})
        if not verdict.allowed:
            self._answer(callback_query, "Not authorized.")
            return CallbackResult("rejected", verdict.reason)

        card = self._cards.get(card_id)
        if card is None or choice not in _DECISIONS:
            self._answer(callback_query, "Unknown or stale card.")
            return CallbackResult("unknown", f"no live card for callback data {data!r}")

        if card.state in ("approved", "denied"):
            self._answer(callback_query, f"Already {card.state}.")
            return CallbackResult("duplicate", f"card already {card.state}; tap ignored", card)

        if card.state == "expired" or self._clock() - card.created_at > card.ttl:
            self._resolve(card, "expired")
            self._answer(callback_query, "This card has expired.")
            return CallbackResult("expired", "card passed its TTL before a decision", card)

        card.decided_by = (callback_query.get("from") or {}).get("id")
        self._resolve(card, _DECISIONS[choice])
        self._answer(callback_query, f"Recorded: {card.state}.")
        return CallbackResult(card.state, f"card {card.card_id} {card.state}", card)

    def expire_stale(self) -> list[ApprovalCard]:
        """Sweep pending cards past their TTL; returns the newly expired ones."""
        expired = []
        for card in self._cards.values():
            if card.state == "pending" and self._clock() - card.created_at > card.ttl:
                self._resolve(card, "expired")
                expired.append(card)
        return expired

    def _resolve(self, card: ApprovalCard, state: str) -> None:
        """Set the terminal state and edit the message (removes the keyboard)."""
        card.state = state
        if card.message_id is not None:
            self._sender.call("editMessageText", {
                "chat_id": card.chat_id,
                "message_id": card.message_id,
                "text": f"{card.text}\n\n[{state.upper()}]",
            })

    def _answer(self, callback_query: dict[str, Any], text: str) -> None:
        self._sender.call("answerCallbackQuery", {
            "callback_query_id": callback_query.get("id"),
            "text": text,
        })
