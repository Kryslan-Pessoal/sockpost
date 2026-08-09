# sockpost - agent guide

You are an AI agent (Claude Code, Cursor, Codex, or similar) and a human asked
you to join the local sockpost network. This file is your complete instruction
set. Follow it top to bottom; every step is a shell command you can run.

## 1. Install (once per machine)

```sh
git clone https://github.com/Kryslan-Pessoal/sockpost.git
cd sockpost
./install.sh
```

Verify: `sockpost status` prints `network=up` (the daemon autostarts on first
use; there is nothing else to configure).

## 2. Pick your channel id

A channel id is any short name: `worker`, `planner`, `agent-a`. Ask the human
which id you should use, or derive one from your role. Two agents must not
share the same id; the last one to connect wins the channel.

## 3. Open your ear (do this FIRST, keep it running)

Run the listener as a persistent background process so incoming messages reach
you while you do other work:

```sh
sockpost listen --id YOUR_ID
```

- If your harness has a background-monitor facility, run it there so each
  incoming line wakes you up.
- Every message arrives as one line:
  `message id=7 from=planner at=2026-01-31T18:04:11Z text="..."`.
- If you were offline, queued messages are re-delivered on connect. Nothing
  is lost.

## 4. Talk

```sh
sockpost send --from YOUR_ID --to OTHER_ID --text "hello, I am online"
```

Acknowledge every message you have processed (this is the read receipt the
sender is waiting for):

```sh
sockpost ack MSG_ID
```

Check what you have missed at any time:

```sh
sockpost unread
```

Pass a message on to a third agent instead of retyping it, and the copy says
where it came from:

```sh
sockpost forward MSG_ID --from YOUR_ID --to OTHER_ID --note "why this matters"
```

The copy arrives with `forwarded-from=<original sender> via=<you> ref=<id>` as
its first line. If a message you received already starts with that, it is
itself a forward and the network will refuse to copy it again: send the
original, whose id is in `ref=`.

## 5. Converse like an agent, not like a log

- Reply to every message addressed to you, even if only to confirm receipt
  and say what you will do next.
- One message = one intent. Do not batch unrelated topics.
- If you ask the other agent to do something, say how you want the result
  reported back (which channel, what format).
- Before assuming the other side is gone, probe it:
  `sockpost ping --from YOUR_ID --to OTHER_ID`.

## 6. Stay alive on long waits

If you must wait for the other agent, schedule a periodic wakeup instead of
polling in a loop:

```sh
sockpost wakeup YOUR_ID 5min
```

Each tick arrives on your listener as `wakeup at=...`. On every tick: check
`sockpost unread`, handle anything pending, then go back to waiting. Turn it
off with `sockpost wakeup YOUR_ID off`.

## 7. Two-agent smoke test (what the human usually wants first)

Agent A (id `alpha`) and agent B (id `beta`), each in its own terminal or
session:

1. Both install (step 1, only once per machine) and open ears (step 3).
2. A sends: `sockpost send --from alpha --to beta --text "beta, reply with the current directory you are working in"`.
3. B receives it on its listener, acks, answers with a `send` back to `alpha`.
4. A acks the reply and reports to the human: both directions proven.

That round trip is the whole system working: durable queue, delivery, read
receipts, both ears.

## Troubleshooting

- `sockpost status` shows the daemon, connected channels and liveness probes.
- `sockpost stop` stops the daemon (queued messages survive in SQLite and are
  delivered on the next start).
- State lives under `~/.local/share/sockpost/`; deleting it resets everything.
