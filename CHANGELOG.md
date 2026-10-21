# Changelog

## Unreleased

- `UrllibTransport` raises `TransportError` (a `TelegramError`, with `maybe_delivered`) for connection, DNS and timeout failures instead of
  letting `urllib` exceptions escape; `RateLimitedSender` retries it (3 tries at 0.5 / 2 / 6 s when the request never left, once when
  the reply was lost). Code that caught `urllib.error.URLError` around a transport call should catch `TransportError`.

## 0.1.0 - 2026-07-31

Initial release, extracted and generalized from a private production multi-agent automation system.

- Full offline self-test (`python -m tgopskit --self-test`)
- MIT licensed

See the Roadmap section of the README for planned improvements - this is a maintained tool, not a snapshot.
