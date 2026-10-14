"""Operator-only access control plus a dead-man's heartbeat.

Every inbound update is checked against a static user-id allowlist -- a bot
that moves money or restarts services must never act on a message just because
it arrived. Sensitive commands are additionally DM-only: group chats can be
joined, forwarded into, or screen-shared, so approvals belong in a private
chat with a known operator.

The heartbeat: every *allowed* operator interaction stamps a presence file
(and fires an optional callback). Downstream automation can read that file and
implement an "operator silent too long" brake -- e.g. pause autonomous
purchases when no human has touched the bot for 72 hours.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class Verdict:
    """Outcome of an access check: allowed yes/no plus a loggable reason."""

    allowed: bool
    reason: str


class OperatorGuard:
    """Allowlist gate for updates; heartbeats on every allowed interaction."""

    def __init__(
        self,
        operator_ids: Iterable[int],
        presence_file: Path | str | None = None,
        on_heartbeat: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._operator_ids = frozenset(int(i) for i in operator_ids)
        self._presence_file = Path(presence_file) if presence_file else None
        self._on_heartbeat = on_heartbeat
        self._clock = clock

    def is_operator(self, user_id: int | None) -> bool:
        """True if ``user_id`` is on the operator allowlist."""
        return user_id is not None and user_id in self._operator_ids

    def check_update(self, update: dict[str, Any], sensitive: bool = False) -> Verdict:
        """Gate an update; ``sensitive=True`` additionally requires a private chat.

        An allowed check records a heartbeat as a side effect.
        """
        user_id, chat_type = self._extract(update)
        if user_id is None:
            return Verdict(False, "update has no identifiable sender")
        if not self.is_operator(user_id):
            return Verdict(False, f"user {user_id} is not on the operator allowlist")
        if sensitive and chat_type != "private":
            return Verdict(False, "sensitive command refused outside a private chat (DM-only)")
        self.heartbeat()
        return Verdict(True, f"operator {user_id} ok")

    def heartbeat(self) -> float:
        """Record operator presence: stamp the presence file, fire the callback."""
        now = self._clock()
        if self._presence_file is not None:
            iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
            tmp = self._presence_file.with_suffix(self._presence_file.suffix + ".tmp")
            tmp.write_text(f"{now}\t{iso}\n", encoding="utf-8")
            os.replace(tmp, self._presence_file)
        if self._on_heartbeat is not None:
            self._on_heartbeat(now)
        return now

    def seconds_since_heartbeat(self) -> float | None:
        """Age of the last recorded heartbeat, or None if none exists yet.

        Reads the presence file, so a *separate* watchdog process can call
        this to drive a dead-man brake.
        """
        if self._presence_file is None:
            return None
        try:
            stamp = float(self._presence_file.read_text(encoding="utf-8").split("\t", 1)[0])
        except (OSError, ValueError):
            return None
        return self._clock() - stamp

    @staticmethod
    def _extract(update: dict[str, Any]) -> tuple[int | None, str | None]:
        """Pull (sender user id, chat type) out of a message or callback update."""
        if "callback_query" in update:
            query = update["callback_query"]
            user = query.get("from") or {}
            chat = (query.get("message") or {}).get("chat") or {}
        else:
            message = update.get("message") or update.get("edited_message") or {}
            user = message.get("from") or {}
            chat = message.get("chat") or {}
        return user.get("id"), chat.get("type")
