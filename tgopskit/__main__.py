"""CLI entry point.

Offline self-test (no token, no network):
    python -m tgopskit --self-test

Run the demo ops bot against the real Bot API:
    python -m tgopskit run --token-env BOT_TOKEN --operators 123456789

The demo bot answers ``/ping`` (any operator chat), and ``/approve <subject>``
(operator DM only) posts an approval card whose outcome is printed to stdout.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from .approvals import ApprovalManager
from .guard import OperatorGuard
from .poller import OffsetStore, UpdatePoller
from .selftest import run_self_test
from .sender import RateLimitedSender
from .transport import ConflictError, TelegramError, UrllibTransport


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgopskit", description=__doc__)
    parser.add_argument("--self-test", action="store_true", dest="self_test",
                        help="run the offline self-test and exit")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="poll Telegram and serve the demo ops bot")
    run_parser.add_argument("--token-env", default="BOT_TOKEN",
                            help="name of the env var holding the bot token (default: BOT_TOKEN)")
    run_parser.add_argument("--operators", default="",
                            help="comma-separated operator user ids, e.g. 123456789,987654321")
    run_parser.add_argument("--offset-file", default=".tgopskit-offset.json",
                            help="where the getUpdates offset persists across restarts")
    run_parser.add_argument("--presence-file", default=".tgopskit-presence.txt",
                            help="dead-man heartbeat file stamped on operator interactions")
    run_parser.add_argument("--self-test", action="store_true", dest="self_test",
                            help="run the offline self-test instead of connecting")
    return parser


def _handle_update(update: dict[str, Any], guard: OperatorGuard,
                   sender: RateLimitedSender, approvals: ApprovalManager) -> None:
    """Route one update: callbacks to the approval manager, commands to handlers."""
    if "callback_query" in update:
        result = approvals.handle_callback(update["callback_query"])
        print(f"callback -> {result.status}: {result.note}")
        return
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        return
    chat_id = message["chat"]["id"]
    if text.startswith("/ping"):
        if guard.check_update(update).allowed:
            sender.send_message(chat_id, "pong")
        return
    if text.startswith("/approve"):
        verdict = guard.check_update(update, sensitive=True)
        if not verdict.allowed:
            print(f"refused /approve: {verdict.reason}")
            return
        subject = text.removeprefix("/approve").strip() or "unnamed action"
        card = approvals.create(chat_id, f"Approve: {subject}?")
        print(f"card {card.card_id} armed for {subject!r}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the self-test or the polling loop."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if args.command != "run":
        parser.print_help()
        return 2

    operator_ids = {int(part) for part in args.operators.split(",") if part.strip()}
    if not operator_ids:
        parser.error("refusing to start without --operators: an ops bot with no "
                     "allowlist would obey anyone who finds it")
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"environment variable {args.token_env} is not set "
                     f"(create a bot with @BotFather, then: export {args.token_env}=YOUR_TOKEN)")

    guard = OperatorGuard(operator_ids, presence_file=args.presence_file)
    sender = RateLimitedSender(UrllibTransport(token))
    approvals = ApprovalManager(sender, guard)
    poller = UpdatePoller(UrllibTransport(token), OffsetStore(args.offset_file))

    print(f"tgopskit: polling as the SINGLE consumer; operators={sorted(operator_ids)}")
    try:
        while True:
            try:
                updates = poller.poll_once()
            except ConflictError as exc:
                sys.exit("fatal 409 from getUpdates: another process is polling this token. "
                         f"Exactly one consumer per token -- find and stop it. ({exc})")
            except TelegramError as exc:
                print(f"transient API error: {exc}; backing off 5s", file=sys.stderr)
                time.sleep(5)
                continue
            for update in updates:
                _handle_update(update, guard, sender, approvals)
            for card in approvals.expire_stale():
                print(f"card {card.card_id} expired unanswered")
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
