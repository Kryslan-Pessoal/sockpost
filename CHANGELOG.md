# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 - 2026-08-08

First release.

### Added

- `sockpost daemon`: single instance per host, guarded by an exclusive lock on
  the pid file, serving a Unix domain socket created with mode `0600`.
- Durable message queue in SQLite, with separate delivery and acknowledgement
  states and no destructive deletes.
- `sockpost listen`: streams events for one channel over the open socket, with
  automatic acknowledgement by default, `--manual-ack` for at-least-once
  processing, `--json` for programmatic consumers, and jittered reconnection.
- `sockpost send`, `sockpost ack`, `sockpost unread`: the core send, confirm
  and audit loop. Only the recipient of a message can acknowledge it.
- `sockpost status` and `sockpost ping`: registered channels plus a live probe
  that separates "connected" from "answering".
- `sockpost wakeup`: periodic heartbeat per channel, persisted across daemon
  restarts.
- `sockpost stop`: shutdown that verifies the lock before signalling a pid, so
  a stale pid file can never take down an unrelated process.
- Redelivery of unacknowledged messages, both on reconnect and after the
  redelivery window while the consumer stays connected; per channel delivery
  pacing; watchdog notices to the sender (`delivery-timeout`, `unreachable`)
  raised only once a message has actually been offered to a consumer; expiry
  of messages addressed to channels that never connected.
- A write timeout towards peers (`SOCKPOST_WRITE_TIMEOUT`), so a consumer that
  stops reading is dropped instead of blocking the daemon or holding its
  channel against a takeover.
- Automatic daemon start on first use, disabled with `SOCKPOST_AUTOSTART=0`.
- A message body size limit, one mebibyte by default, so a single sender
  cannot grow the database without bound (`SOCKPOST_MAX_BODY_BYTES`).
- Test suite: unit tests for the protocol and the store, end to end tests that
  drive real processes, and `tools/smoke.sh` for environments without pytest.
- `tools/leak-check.sh`: pre-publication scan for credentials, contact
  details, developer machine paths and non-ASCII content.
- Continuous integration running the tests on Linux and macOS against Python
  3.9, 3.11 and 3.13.
