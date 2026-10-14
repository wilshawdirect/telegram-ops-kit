"""Offline self-test: proves every safety pattern in the kit with MockTransport.

Run with ``python -m tgopskit --self-test``. No network, no token, no sleeps --
time is a fake clock and Telegram is a scripted mock.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .approvals import ApprovalManager
from .guard import OperatorGuard
from .poller import OffsetStore, UpdatePoller
from .sender import RateLimitedSender
from .transport import MockTransport, RetryAfter

OPERATOR_ID = 111111
STRANGER_ID = 222222
DM_CHAT = {"id": 4242, "type": "private"}
GROUP_CHAT = {"id": -100123, "type": "supergroup"}


class FakeClock:
    """Deterministic clock: calling it returns now; sleep() advances + records."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _message(update_id: int, user_id: int, chat: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "from": {"id": user_id}, "chat": chat, "text": text},
    }


def _tap(card_id: str, choice: str, user_id: int = OPERATOR_ID, message_id: int = 101) -> dict[str, Any]:
    return {
        "id": f"cbq-{card_id}-{choice}-{user_id}",
        "from": {"id": user_id},
        "message": {"message_id": message_id, "chat": DM_CHAT},
        "data": f"apv:{card_id}:{choice}",
    }


def _stack(tmp: str, ttl: float = 900.0) -> tuple[MockTransport, OperatorGuard, ApprovalManager, FakeClock]:
    clock = FakeClock()
    transport = MockTransport()
    guard = OperatorGuard({OPERATOR_ID}, presence_file=Path(tmp) / "presence.txt", clock=clock)
    sender = RateLimitedSender(transport, min_interval=0.0, sleep=clock.sleep, clock=clock)
    return transport, guard, ApprovalManager(sender, guard, ttl=ttl, clock=clock), clock


def test_operator_guard_and_heartbeat() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        beats: list[float] = []
        guard = OperatorGuard(
            {OPERATOR_ID}, presence_file=Path(tmp) / "presence.txt",
            on_heartbeat=beats.append, clock=FakeClock(),
        )
        verdict = guard.check_update(_message(1, STRANGER_ID, DM_CHAT, "/approve x"))
        assert not verdict.allowed and "allowlist" in verdict.reason
        verdict = guard.check_update(_message(2, OPERATOR_ID, GROUP_CHAT, "/approve x"), sensitive=True)
        assert not verdict.allowed and "private" in verdict.reason
        assert not beats, "denied checks must not heartbeat"
        verdict = guard.check_update(_message(3, OPERATOR_ID, DM_CHAT, "/approve x"), sensitive=True)
        assert verdict.allowed and len(beats) == 1
        age = guard.seconds_since_heartbeat()
        assert age is not None and age < 1.0, "presence file must be readable and fresh"


def test_offset_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        offset_path = Path(tmp) / "offset.json"
        transport = MockTransport()
        transport.queue_updates([_message(7, OPERATOR_ID, DM_CHAT, "a"), _message(8, OPERATOR_ID, DM_CHAT, "b")])
        transport.queue_updates([_message(9, OPERATOR_ID, DM_CHAT, "c")])
        poller = UpdatePoller(transport, OffsetStore(offset_path), timeout=0)
        assert len(poller.poll_once()) == 2
        assert OffsetStore(offset_path).load() == 9, "offset must persist as last id + 1"
        assert len(poller.poll_once()) == 1
        calls = transport.calls_for("getUpdates")
        assert "offset" not in calls[0] and calls[1]["offset"] == 9
        # A brand-new poller (simulated restart) resumes from the file, not from zero.
        UpdatePoller(transport, OffsetStore(offset_path), timeout=0).poll_once()
        assert transport.calls_for("getUpdates")[2]["offset"] == 10


def test_approval_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        transport, _, approvals, _ = _stack(tmp)
        card = approvals.create(DM_CHAT["id"], "Approve: pay ACME Wholesale invoice #1042?")
        buttons = transport.calls_for("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0]
        assert buttons[0]["callback_data"] == f"apv:{card.card_id}:yes"
        result = approvals.handle_callback(_tap(card.card_id, "yes", message_id=card.message_id))
        assert result.status == "approved" and card.state == "approved"
        assert card.decided_by == OPERATOR_ID
        assert len(transport.calls_for("editMessageText")) == 1, "card message must be finalized"
        assert len(transport.calls_for("answerCallbackQuery")) == 1


def test_double_tap_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        transport, _, approvals, _ = _stack(tmp)
        card = approvals.create(DM_CHAT["id"], "Approve: restart the sync worker?")
        first = approvals.handle_callback(_tap(card.card_id, "yes", message_id=card.message_id))
        again = approvals.handle_callback(_tap(card.card_id, "yes", message_id=card.message_id))
        flipped = approvals.handle_callback(_tap(card.card_id, "no", message_id=card.message_id))
        assert first.status == "approved"
        assert again.status == "duplicate" and flipped.status == "duplicate"
        assert card.state == "approved", "later taps must never flip the decision"
        assert len(transport.calls_for("editMessageText")) == 1, "no re-edit on duplicate taps"
        assert len(transport.calls_for("answerCallbackQuery")) == 3, "every tap still gets answered"


def test_forged_and_unauthorized_callbacks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _, _, approvals, _ = _stack(tmp)
        card = approvals.create(DM_CHAT["id"], "Approve: rotate the deploy key?")
        forged = approvals.handle_callback(_tap("deadbeefdeadbeef", "yes"))
        assert forged.status == "unknown", "a guessed nonce must not resolve to a card"
        stranger = approvals.handle_callback(_tap(card.card_id, "yes", user_id=STRANGER_ID))
        assert stranger.status == "rejected" and card.state == "pending"
        noise = approvals.handle_callback({"id": "x", "from": {"id": OPERATOR_ID}, "data": "hello"})
        assert noise.status == "ignored"


def test_card_expiry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _, _, approvals, clock = _stack(tmp, ttl=60.0)
        card = approvals.create(DM_CHAT["id"], "Approve: publish the changelog?")
        clock.now += 61.0
        late = approvals.handle_callback(_tap(card.card_id, "yes", message_id=card.message_id))
        assert late.status == "expired" and card.state == "expired", "a late tap can only expire"
        second = approvals.create(DM_CHAT["id"], "Approve: archive stale threads?")
        clock.now += 61.0
        swept = approvals.expire_stale()
        assert swept == [second] and second.state == "expired"


def test_rate_limited_sender() -> None:
    transport = MockTransport()
    clock = FakeClock()
    sender = RateLimitedSender(transport, min_interval=1.0, max_retries=3, sleep=clock.sleep, clock=clock)
    sender.send_message(DM_CHAT["id"], "first")
    sender.send_message(DM_CHAT["id"], "second")
    assert any(abs(s - 1.0) < 1e-9 for s in clock.sleeps), "second send must wait min_interval"
    transport.fail_next("sendMessage", RetryAfter(5))
    sender.send_message(DM_CHAT["id"], "third")
    assert 5.0 in clock.sleeps, "429 retry_after must be respected exactly"
    sends = transport.calls_for("sendMessage")
    assert len(sends) == 4 and sends[-1]["text"] == "third", "the 429'd send must be retried"


TESTS = [
    test_operator_guard_and_heartbeat,
    test_offset_persistence,
    test_approval_roundtrip,
    test_double_tap_is_idempotent,
    test_forged_and_unauthorized_callbacks,
    test_card_expiry,
    test_rate_limited_sender,
]


def run_self_test() -> int:
    """Run all scripted scenarios; return 0 when everything passes."""
    failed = 0
    for test in TESTS:
        name = test.__name__.removeprefix("test_")
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            failed += 1
            print(f"[FAIL] {name}: {exc!r}")
        else:
            print(f"[PASS] {name}")
    total = len(TESTS)
    verdict = "OK" if failed == 0 else "FAILED"
    print(f"self-test {verdict}: {total - failed}/{total} passed (offline, MockTransport, no network)")
    return 0 if failed == 0 else 1
