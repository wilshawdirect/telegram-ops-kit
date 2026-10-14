# telegram-ops-kit

[![CI](https://github.com/Wilshawdirect/telegram-ops-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/Wilshawdirect/telegram-ops-kit/actions/workflows/ci.yml)

A minimal, safe pattern for running an operations/approval Telegram bot: a small
pure-stdlib library plus a runnable bot skeleton. It covers the five things that
actually matter when a Telegram bot is the human-in-the-loop control surface for
automation: single-consumer polling, an operator-only guard, verified approval
cards, rate-limit-respecting sends, and a dead-man's heartbeat.

## Why this exists

This kit was extracted (and generalized) from a production multi-agent
automation system in which a Telegram bot was the *only* channel a human
operator had for approving money-moving actions. Every pattern here is a lesson
that was paid for in an incident:

| Pattern | The lesson behind it |
| --- | --- |
| Single-consumer polling, offset persisted | Two processes calling `getUpdates` on one token produce 409 Conflict storms and *both* go intermittently deaf. The kit treats a 409 as a fatal misconfiguration, not a transient. Losing the offset on restart means replaying or skipping updates. |
| Operator guard, DM-only for sensitive commands | A bot that acts on any message it receives will eventually act for the wrong person. Every update is checked against a static user-id allowlist; sensitive commands are additionally refused outside a private chat. |
| Approval cards with nonce + idempotency + expiry | Inline-keyboard buttons are tappable by anyone who can see them, callbacks can be forged or redelivered, and humans double-tap. Cards carry an unguessable nonce, re-verify the tapping user, let the first decision win, and expire if unanswered. |
| Rate-limited sender with `retry_after` respect | Telegram answers floods with 429 plus an explicit backoff; ignoring it and hammering only extends the penalty. |
| Dead-man's heartbeat | Downstream automation needs to know a human is still watching. Every allowed operator interaction stamps a presence file, so a watchdog can implement "operator silent too long -> pause autonomous actions". |

## Quickstart

Python 3.11+ and nothing else — there are no dependencies to install.

```bash
git clone https://github.com/Wilshawdirect/telegram-ops-kit
cd telegram-ops-kit

# Prove everything works, offline, in under a second:
python -m tgopskit --self-test

# Run the demo bot for real (token from @BotFather, your numeric user id):
export BOT_TOKEN=YOUR_TOKEN
python -m tgopskit run --token-env BOT_TOKEN --operators 123456789
```

Then DM your bot: `/ping` answers `pong`; `/approve pay ACME Wholesale invoice`
posts a yes/no card and prints the verified outcome to stdout.

## Using it as a library

```python
from tgopskit import (
    ApprovalManager, OffsetStore, OperatorGuard,
    RateLimitedSender, UpdatePoller, UrllibTransport,
)

transport = UrllibTransport("YOUR_TOKEN")
guard = OperatorGuard({123456789}, presence_file="presence.txt")
sender = RateLimitedSender(transport)
approvals = ApprovalManager(sender, guard, ttl=900)
poller = UpdatePoller(transport, OffsetStore("offset.json"))

for update in poller.poll_once():
    if "callback_query" in update:
        result = approvals.handle_callback(update["callback_query"])
        if result.status == "approved":
            ...  # perform the approved action
```

## Architecture

```
tgopskit/
  transport.py   Transport protocol; UrllibTransport (real), MockTransport (scripted)
  poller.py      OffsetStore (atomic file) + UpdatePoller (single-consumer getUpdates)
  guard.py       OperatorGuard: allowlist, DM-only checks, heartbeat presence file
  approvals.py   ApprovalManager/ApprovalCard: nonce, idempotency, expiry
  sender.py      RateLimitedSender: outbound spacing + 429 retry_after handling
  selftest.py    Offline scripted scenarios (run via --self-test)
  __main__.py    CLI: `run` demo bot + `--self-test`
```

All network I/O sits behind the `Transport` protocol, so every behaviour —
guard rejection, approval round-trips, double-taps, expiry, offset resume,
rate-limit retries — is proven offline by the self-test with a scripted mock
and a fake clock. Time (`clock`) and sleeping (`sleep`) are injectable
throughout for the same reason.

One deliberate choice worth knowing: the poller persists the offset
**immediately after each fetch** (at-most-once delivery). A crash mid-handling
drops that batch rather than replaying it on restart — for an ops bot, replaying
a "yes, do it" tap is worse than dropping one, and the approval layer is
idempotent regardless.

## Limitations

- **Approval state is in-memory.** A restart forgets pending cards; late taps
  on them are answered as "unknown or stale". Persist `ApprovalManager._cards`
  yourself if you need cards to survive restarts.
- **Long polling only.** No webhook mode — polling is simpler to run on a box
  behind NAT, which is the deployment this targets.
- **Not a framework.** No handler decorators, no middleware, no conversation
  state machine. It is the safety layer you wire your own handlers into.
- **Single token, single process by design.** If you need horizontal scale,
  you need webhooks and shared state, and this kit is deliberately not that.

## License

MIT — see [LICENSE](LICENSE). Copyright 2026 ADZ.

## Roadmap

This is a maintained tool, not a snapshot - improvements land as the system it came from evolves:

- Persistent approval store (SQLite) so cards survive restarts
- Webhook mode as an alternative to polling
- Reusable message templates with required-field validation
- Multi-operator roles (approver vs viewer)

See [CHANGELOG.md](CHANGELOG.md) for release history.
