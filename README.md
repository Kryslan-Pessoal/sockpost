# sockpost

A local message bus for terminal agents. One daemon per host owns a Unix domain
socket and a durable SQLite queue; any process that knows a channel id can send
to it, listen on it, and acknowledge what it received.

Built for the case where several long running assistants, build agents or shell
sessions on the same machine need to talk to each other, and a dropped message
is worse than a slow one.

## For AI agents

If you are pointing an AI agent (Claude Code, Cursor, Codex, ...) at this repo:
the agent playbook lives in [AGENTS.md](AGENTS.md). It contains everything an
agent needs to install, join the network, and hold a conversation with another
agent, with no human in the loop after the initial instruction.

## What you can build with it

- **A planner/worker pair.** One AI agent decomposes a task and dispatches steps
  to a worker agent in another terminal; the worker reports results back on the
  planner's channel. No shared files, no polling loops.
- **A fleet inbox.** Ten agents, one channel each; any of them can drop a note to
  any other and know, via the read receipt, whether it was actually seen.
- **Crash-safe handoffs.** Send instructions to an agent that is currently down;
  the daemon queues them and re-delivers the moment its listener comes back.
- **A heartbeat that keeps idle agents alive.** `sockpost wakeup` ticks a channel
  on an interval, so a long-running assistant wakes up, checks its queue, and
  goes back to waiting, without a cron job per agent.
- **Human-in-the-loop gates.** A build agent asks for approval on your channel;
  you answer from any terminal with a one-line `sockpost send`.

## Why

Pipes die with the process. A file in `/tmp` needs a polling loop. A message
broker needs a server, a port and a deployment. sockpost sits in between: no
network, no broker, no configuration, but messages still survive a restart and
you can tell whether the other side actually read them.

## Features

- **Durable queue.** Messages live in SQLite until the recipient acknowledges
  them, so a consumer that crashes or a daemon that restarts loses nothing.
- **Read receipts.** Delivery and acknowledgement are separate events, so
  `sockpost unread` always answers "what has not been picked up yet".
- **Forwarding.** `sockpost forward` copies a message that is already in the
  queue to another channel, with a control line naming the original sender and
  counting the hops. The hop ceiling bounds a relay chain.
- **Pushed, not polled.** Connected channels receive over the open socket; the
  delivery path contains no polling loop.
- **Redelivery.** A channel that disconnects without acknowledging gets its
  messages again the moment it reconnects.
- **Single owner per channel.** A second listener on the same id takes over and
  the first one is told it was evicted, which is what you want when an agent
  restarts and the old process is still holding the socket.
- **Per channel pacing.** Each channel is drained by its own task, so one slow
  or wedged consumer cannot delay anybody else.
- **Liveness.** `sockpost ping` and `sockpost status` distinguish "registered"
  from "actually answering right now".
- **Heartbeats.** `sockpost wakeup` delivers a periodic tick to a channel,
  persisted across daemon restarts, for agents that idle until poked.
- **No dependencies.** Standard library only, Python 3.9 or newer, POSIX hosts.

## Install

From a checkout:

```sh
./install.sh          # uses pipx when available, pip otherwise
./install.sh --dev    # editable install with the test dependencies
```

Or directly, if you prefer to drive the packaging yourself:

```sh
pipx install .
pip install --user .
```

There is nothing to configure and no daemon to set up: the first command that
needs the daemon starts it in the background.

### Upgrading

A running daemon keeps running the code it started with. Installing a new
release replaces the command, not the process, so a command the new release
adds reaches a daemon that has never heard of it and fails with a message
saying so. Stop the daemon once, after upgrading:

```sh
sockpost stop     # the queue is on disk; nothing is lost
```

The next command starts the new daemon by itself. `install.sh` does this for
you, and says so when it did. Unknown *fields* are ignored in both directions,
so only new commands are affected; `send`, `listen` and `ack` keep working
across a version gap.

## Quickstart

```sh
sockpost listen --id worker &                              # terminal A
sockpost send --from planner --to worker --text "go"       # terminal B
sockpost status                                            # anywhere
```

Terminal A prints:

```
connected id=worker
message id=1 from=planner at=2026-01-31T18:04:11Z text="go"
```

## How it works

```
    agent "planner"                       agent "worker"
    +------------------+                  +------------------+
    | sockpost listen  |                  | sockpost listen  |
    |   --id planner   |                  |   --id worker    |
    +--------+---------+                  +---------+--------+
             |                                      |
             |         JSON lines over a            |
             +--------- Unix domain socket ---------+
                                |
                   /tmp/sockpost-<uid>.sock  (mode 0600)
                                |
                     +----------+-----------+
                     |   sockpost daemon    |
                     |  router, pacer,      |
                     |  watchdog, wakeups   |
                     +----------+-----------+
                                |
                 ~/.local/share/sockpost/queue.db
                 messages | channels | wakeups
```

1. `send` writes the message to SQLite and returns its id. The row is
   committed before the sender's command exits.
2. The daemon hands it to the recipient's open socket, at most one message at a
   time per channel.
3. The listener acknowledges. The message leaves the delivery loop but stays in
   the database for auditing.
4. If nobody acknowledges in time, the sender is told: `delivery-timeout` when
   the recipient is connected but silent, `unreachable` when it is not there at
   all. These notices are live events, not queued messages, so they only reach
   a sender that is itself listening on its channel. A one-shot
   `sockpost send` has already exited and will not see them.

### Delivery semantics

- **At least once.** A message that is delivered but not acknowledged is
  delivered again after the channel reconnects, or after the redelivery window
  (120 seconds by default). Consumers should be able to see the same message
  twice; the message id makes that easy to detect.
- **Ordered per channel.** Messages are drained in insertion order, one at a
  time, with a one second gap between messages of a burst. An isolated message
  is not delayed, but a backlog leaves at one message per second per channel:
  this is a coordination bus, not a pipe for bulk traffic. Lower
  `SOCKPOST_DELIVERY_GAP` if your consumers can take it.
- **Only the recipient can acknowledge.** The channel id is part of the update,
  so no process can clear a queue that is not addressed to it.
- **Nothing is deleted.** Acknowledged and expired messages keep their row.
  Messages addressed to a channel that has never connected are marked expired
  after six hours, so a typo cannot grow the database forever.
- **Durability is process level, not power level.** The database runs in WAL
  mode with `synchronous=NORMAL`: a crash of any sockpost process, or of the
  whole daemon, cannot lose a committed message, but an operating system crash
  or a power cut can lose the last commits that were still in the page cache.
  That is the usual trade for not paying an fsync per message, and it is the
  right trade for coordination traffic. Do not treat this as a ledger.

### Forwarding

`sockpost forward <id> --from <channel> --to <channel>` copies a message that
is already in the database to another channel, without retyping it:

```sh
sockpost forward 7 --from worker --to auditor --note "third failure today"
```

```
forwarded id=8 src=7 to=auditor hops=1
```

The copy is a new message sent by the forwarder, so it follows the ordinary
delivery, acknowledgement and redelivery rules. The original is untouched.
Provenance travels in the body, as one control line before the original text:

```
#sockpost/forward v=1 hops=1 from="planner" via="worker" ref=7 at="2026-01-31T18:04:11Z" note="third failure today"
the original body, byte for byte
```

| Field | Meaning |
| --- | --- |
| `v` | Header format version. |
| `hops` | How many times this text has been copied. `1` is a first copy. |
| `from` | The original author, carried unchanged along the whole chain. |
| `via` | The channel that made *this* copy. |
| `ref` | The message this copy was made from, one step back. |
| `at` | When that message was created. |
| `note` | Optional, from `--note`. It belongs to this hop only. |

- **The header is in the body, not in a column.** A message is the only thing
  this protocol carries end to end, so a consumer reading the text already has
  the provenance, with no second lookup and no schema change on a queue
  written by an older version.
- **There is exactly one control line, at every hop.** The header is replaced
  rather than stacked, so a copy has the same shape however far it travelled.
  The step before it is still on disk under `ref`, with its own header, which
  is how a full path is walked back to the original.
- **A chain is bounded, not forbidden.** Every copy costs a hop, and the third
  one is refused (`SOCKPOST_MAX_FORWARD_HOPS`). That is enough for an agent to
  relay to a supervisor and the supervisor to an archive, and short enough
  that two channels forwarding to each other stop instead of feeding on
  themselves. **It is not flood protection**: nothing here stops a process
  from sending the same text a thousand times by hand, and nothing tries to.
- **Any message can be forwarded**, acknowledged or expired: rows are never
  deleted, and forwarding is about the content, not about the place a message
  holds in a delivery loop. The one refusal is a message *still queued* for
  the channel you are copying it to, which would then receive it twice.
- **There is no ownership check.** Any local process can already read the whole
  queue with `sockpost unread`, so refusing to forward somebody else's message
  would buy no secrecy. What the daemon does instead is record who forwarded
  what, in the copy and in its log.
- **Provenance is a convention, not a guarantee.** In line with the security
  model below, a sender can type a control line by hand and claim any origin.
  A body whose first line is a header with an unreadable hop count is refused
  for forwarding, which is the safe side of that mistake.

#### Reading the provenance

The control line is the first line of the body; the original text is
everything after the first newline. Values are quoted with JSON escaping, so
a note containing a quote or a newline never breaks the line.

```sh
sockpost listen --id auditor --json
```

```json
{"op": "message", "msg_id": 8, "from": "worker", "text": "#sockpost/forward v=1 hops=1 from=\"planner\" via=\"worker\" ref=7 at=\"2026-01-31T18:04:11Z\"\nbuild failed again", "created_at": "2026-01-31T18:09:02Z"}
```

```python
import json, re

FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')

event = json.loads(line)
head, _, original = event["text"].partition("\n")
if head.startswith("#sockpost/forward"):
    meta = {k: (json.loads(v) if v.startswith('"') else v)
            for k, v in FIELD.findall(head)}
    print(meta["from"], "->", meta["via"], "hops", meta["hops"])
else:
    original = event["text"]
```

From a shell, the same split is `head -1` and `tail -n +2`.

### Automatic and manual acknowledgement

By default `listen` acknowledges as soon as a message is handed to the process,
which means "received". With `--manual-ack` nothing is acknowledged
automatically and the agent runs `sockpost ack <id> --from <channel>` once the
work is done, which means "processed". Use the second form when a message must
survive a consumer that crashes halfway through.

## Using it from an agent

Two shells, two agents, one build handoff:

```sh
# terminal A: the worker waits for instructions and reports back
export SOCKPOST_ID=worker
sockpost listen --manual-ack | while read -r line; do
  id=$(printf '%s' "$line" | sed -n 's/^message id=\([0-9]*\).*/\1/p')
  [ -n "$id" ] || continue
  make build && sockpost send --to planner --text "build ok"
  sockpost ack "$id"
done
```

```sh
# terminal B: the planner dispatches and watches for the answer
export SOCKPOST_ID=planner
sockpost listen &
sockpost send --to worker --text "please build"
```

For programs rather than shell scripts, `sockpost listen --json` emits one JSON
object per line:

```json
{"op": "message", "msg_id": 7, "from": "planner", "text": "please build", "created_at": "2026-01-31T18:04:11Z"}
```

## Commands

| Command | Purpose |
| --- | --- |
| `sockpost listen --id <ch>` | Stream events for a channel until stopped or evicted. |
| `sockpost send --from <ch> --to <ch> --text <body>` | Queue a message; prints `queued id=N`. |
| `sockpost forward <id> --from <ch> --to <ch>` | Copy an existing message to another channel; prints `forwarded id=N`. |
| `sockpost ack <id> --from <ch>` | Acknowledge a message; idempotent. |
| `sockpost unread [--id <ch>]` | List queued, unacknowledged messages. |
| `sockpost status` | Connected channels, wakeups and a liveness probe. |
| `sockpost ping --to <ch,ch>` | Ask specific channels to prove they are alive. |
| `sockpost wakeup <ch> <interval\|off>` | Periodic heartbeat for a channel. |
| `sockpost daemon` | Run the daemon in the foreground (for a supervisor). |
| `sockpost stop` | Stop the running daemon. |

Output is one event per line as `key=value` pairs, with free text quoted and
escaped, so it is safe to parse with `grep`, `awk` or `cut`. The only
exceptions are the bare section headers `liveness:` printed by `status` and the
`unread n=N` count that precedes a listing.

Exit codes: `0` success, `1` failure, `2` usage error. `ping` and `status`
return `1` when a channel does not answer.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SOCKPOST_ID` | unset | Default channel for `--id` and `--from`. |
| `SOCKPOST_SOCKET` | `$XDG_RUNTIME_DIR/sockpost/daemon.sock`, else `$TMPDIR/sockpost-<uid>.sock` | Socket path. |
| `SOCKPOST_DB` | `$XDG_DATA_HOME/sockpost/queue.db` | Queue database. |
| `SOCKPOST_PID` | `$XDG_STATE_HOME/sockpost/daemon.pid` | Lock file. |
| `SOCKPOST_LOG` | `$XDG_STATE_HOME/sockpost/daemon.log` | Daemon event log (metadata only, never message bodies). |
| `SOCKPOST_AUTOSTART` | `1` | Set to `0` to never start the daemon implicitly. |
| `SOCKPOST_DELIVERY_GAP` | `1.0` | Seconds between messages of a burst, per channel. |
| `SOCKPOST_REDELIVER_GAP` | `120` | Seconds before an unacknowledged message is sent again. |
| `SOCKPOST_ACK_TIMEOUT_ONLINE` | `30` | Seconds before a connected but silent recipient is reported. |
| `SOCKPOST_ACK_TIMEOUT_OFFLINE` | `60` | Seconds before an absent recipient is reported. |
| `SOCKPOST_ORPHAN_TTL` | `21600` | Seconds before messages to a channel that never connected expire. |
| `SOCKPOST_MAX_BODY_BYTES` | `1048576` | Largest accepted message body. |
| `SOCKPOST_MAX_FORWARD_HOPS` | `3` | How many times one message may be copied onward. |
| `SOCKPOST_DRAIN_BATCH` | `10` | Messages examined per delivery round, per channel. |
| `SOCKPOST_WRITE_TIMEOUT` | `5.0` | Seconds the daemon waits on a write before dropping a peer. |
| `SOCKPOST_ORPHAN_SWEEP_INTERVAL` | `900` | Seconds between expiry sweeps. |

### Running under a supervisor

The daemon starts on demand, but you can also run it as a service.

systemd (user unit, `~/.config/systemd/user/sockpost.service`):

```ini
[Service]
ExecStart=%h/.local/bin/sockpost daemon
Restart=always

[Install]
WantedBy=default.target
```

launchd (`~/Library/LaunchAgents/sockpost.plist`) works the same way with
`ProgramArguments` set to the `sockpost daemon` command and `KeepAlive` true.

## Security model

sockpost is a **single user, single host** tool. The socket is created with
mode `0600` and the daemon inherits the privileges of the user who started it.
Any process running as that user can send as any channel id: there is no
authentication between local processes in this release. Do not use it as a
trust boundary, and do not put secrets in message bodies you would not put in
your own home directory.

The daemon log records routing metadata (ids, sizes, timestamps) and never
message bodies.

## Development

```sh
python3 -m pip install -e ".[dev]"

python3 -m pytest         # unit and end to end tests
tools/smoke.sh            # framework free end to end check
tools/leak-check.sh                    # pre-publication scan of the files
                                       # git would publish
tools/leak-check.sh --all              # same, including ignored build output
tools/leak-check.sh --commits main..HEAD   # authors and commit messages
```

Continuous integration runs the tests on Linux and macOS against Python 3.9,
3.11 and 3.13, and runs the scan on every pull request.

The tests spawn real daemons on throwaway sockets and databases, so they never
disturb a running instance.

## FAQ

**Can two daemons run at once?**
No. The daemon holds an exclusive lock on its pid file; a second one exits
immediately. Two independent instances are possible only with different
`SOCKPOST_SOCKET` and `SOCKPOST_DB` values, which is how the test suite works.

**What happens if the daemon dies with messages in the queue?**
Nothing is lost. The queue is on disk, `sockpost unread` still reads it while
the daemon is down, and delivery resumes when it comes back.

**What if two processes listen on the same id?**
The newest wins. The older one receives an `evicted` event and exits without
reconnecting, so the two cannot fight over the channel.

**Does it work over the network?**
No, and that is deliberate. Unix sockets keep the tool free of ports,
certificates and firewall questions. If you need remote agents, put a real
broker between hosts and use sockpost inside each one.

**Why does it say "socket path too long"?**
The kernel limits Unix socket paths to about 100 bytes. Point
`SOCKPOST_SOCKET` at something short, such as `/tmp/sockpost.sock`.

**Is the message body size limited?**
Yes, one mebibyte by default, adjustable with `SOCKPOST_MAX_BODY_BYTES`. The
transport is designed for coordination messages, not file transfer: send a
path, not a payload.

## Roadmap

Not in 0.2.0, listed in the order they are likely to be built:

- Scheduled messages and alarms with snooze.
- Per channel tokens and an access control list, so a channel id cannot be
  impersonated by another process of the same user.
- Subscriptions: a standing rule that forwards everything addressed to one
  channel onto another, for supervision and audit, instead of one command per
  message.
- Named groups with fan out delivery.
- A small Python client library, so programs do not have to shell out.
- Retention: acknowledged messages are kept forever today, so a busy host grows
  its database without bound. A `prune` command with an age policy is needed.

## License

MIT. See [LICENSE](LICENSE).
