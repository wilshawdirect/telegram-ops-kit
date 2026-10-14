"""Single-consumer getUpdates polling with durable offset persistence.

Two rules learned the hard way:

1. **One consumer per token.** A second process calling getUpdates on the same
   token triggers 409 Conflict storms and both consumers go intermittently
   deaf. The poller surfaces ``ConflictError`` loudly instead of retrying it
   away -- a hidden second consumer is an operational bug, not a transient.

2. **Persist the offset immediately after each fetch.** This kit deliberately
   chooses at-most-once delivery: a crash mid-handling drops the batch rather
   than replaying it on restart. For an ops bot whose updates include "yes,
   buy it" taps, replaying an approval is worse than dropping one -- and the
   approval layer is idempotent anyway.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

from .transport import Transport

DEFAULT_ALLOWED_UPDATES: tuple[str, ...] = ("message", "callback_query")


class OffsetStore:
    """Atomic file persistence for the getUpdates offset (survives restarts)."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> int | None:
        """Return the persisted offset, or None if nothing was saved yet."""
        try:
            return int(json.loads(self._path.read_text(encoding="utf-8"))["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, offset: int) -> None:
        """Persist ``offset`` atomically (temp file + os.replace)."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        os.replace(tmp, self._path)


class UpdatePoller:
    """Long-poll getUpdates as the single consumer for this token."""

    def __init__(
        self,
        transport: Transport,
        offset_store: OffsetStore,
        timeout: int = 30,
        allowed_updates: Sequence[str] = DEFAULT_ALLOWED_UPDATES,
    ) -> None:
        self._transport = transport
        self._store = offset_store
        self._timeout = timeout
        self._allowed_updates = list(allowed_updates)

    def poll_once(self) -> list[dict[str, Any]]:
        """Fetch one batch of updates, persist the advanced offset, return them.

        Raises ``ConflictError`` if another consumer is polling this token.
        """
        params: dict[str, Any] = {
            "timeout": self._timeout,
            "allowed_updates": self._allowed_updates,
        }
        offset = self._store.load()
        if offset is not None:
            params["offset"] = offset
        updates: list[dict[str, Any]] = self._transport.call("getUpdates", params) or []
        if updates:
            self._store.save(updates[-1]["update_id"] + 1)
        return updates
