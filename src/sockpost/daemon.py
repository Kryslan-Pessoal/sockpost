"""The daemon: one process per host, one Unix socket, one durable queue.

Design notes
------------
* A single asyncio event loop serves every connection. All SQLite access is
  funnelled through one worker thread, so the loop never blocks on disk and
  SQLite never sees concurrent writers.
* Delivery is paced *per channel*. Each channel owns a small task that hands
  out one message at a time; a burst is spaced by ``delivery_gap`` while an
  isolated message goes out immediately. A slow channel therefore cannot delay
  any other channel.
* Ownership of a channel is "last connection wins". The previous listener is
  told it was evicted and disconnected, which is what you want when an agent
  restarts and its old process is still holding the socket.
* Delivery is at-least-once, never exactly-once. A message stays in the queue
  until it is acknowledged, and it is not re-sent within ``redeliver_gap``
  seconds of the last attempt, which keeps the duplicates rare rather than
  impossible.
"""

from __future__ import annotations

import asyncio
import fcntl
import heapq
import itertools
import json
import os
import signal
import socket
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import protocol as P
from . import store
from .config import Settings, ensure_parent, socket_path_problem

EVICTION_NOTICE = ("another client connected to this channel; this listener is "
                   "being disconnected (last connection wins)")


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

class EventLog:
    """Append-only key=value log with size based rotation.

    Message bodies are never written here. The log records routing metadata so
    an operator can reconstruct what happened without reading anyone's content.
    """

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        ensure_parent(self.path)

    @staticmethod
    def _flatten(value) -> str:
        """One log record is one line, whatever the value contains."""
        text = str(value)
        for char in ("\r", "\n", "\x00"):
            text = text.replace(char, " ")
        return text

    def write(self, event: str, **fields) -> None:
        parts = ["ts=%s" % P.now_iso(), "event=%s" % event]
        for key, value in fields.items():
            if value is None:
                continue
            parts.append("%s=%s" % (key, self._flatten(value)))
        line = " ".join(parts) + "\n"
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# small building blocks
# ---------------------------------------------------------------------------

class Conn:
    """Write side of one client connection."""

    __slots__ = ("writer", "channel")

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.channel = None

    async def send(self, obj: dict) -> None:
        self.writer.write(P.encode(obj))
        await self.writer.drain()

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass


class Scheduler:
    """One heap of timers served by one task.

    Timers are addressed by a logical key, for example ``("ack", 12)``. Arming
    the same key again invalidates the previous timer, so there are no ghost
    callbacks after a reschedule.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, log=None) -> None:
        self.loop = loop
        self.log = log
        self._heap = []
        self._generation = {}
        # Number of heap entries still referencing each key. A key's state is
        # only forgotten once nothing in the heap can name it again, which is
        # what keeps one entry per message from accumulating forever.
        self._outstanding = {}
        self._ordinal = itertools.count()
        self._signal = asyncio.Event()
        self._task = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def arm(self, key, delay: float, callback) -> None:
        generation = self._generation.get(key, 0) + 1
        self._generation[key] = generation
        self._outstanding[key] = self._outstanding.get(key, 0) + 1
        deadline = self.loop.time() + max(0.0, float(delay))
        heapq.heappush(self._heap,
                       (deadline, next(self._ordinal), key, generation, callback))
        self._signal.set()

    def cancel(self, key) -> None:
        if key in self._generation:
            self._generation[key] += 1

    def _release(self, key) -> None:
        remaining = self._outstanding.get(key, 0) - 1
        if remaining > 0:
            self._outstanding[key] = remaining
            return
        self._outstanding.pop(key, None)
        self._generation.pop(key, None)

    async def _run(self) -> None:
        while True:
            if not self._heap:
                self._signal.clear()
                await self._signal.wait()
                continue
            deadline, _ordinal, key, generation, callback = self._heap[0]
            now = self.loop.time()
            if deadline > now:
                self._signal.clear()
                try:
                    await asyncio.wait_for(self._signal.wait(), timeout=deadline - now)
                except asyncio.TimeoutError:
                    pass
                continue
            heapq.heappop(self._heap)
            live = self._generation.get(key) == generation
            self._release(key)
            if live:
                try:
                    callback()
                except Exception as exc:
                    # A broken timer must not take the scheduler with it, but
                    # it must not disappear silently either.
                    if self.log is not None:
                        self.log.write("timer_failed", key=key,
                                       error=type(exc).__name__)


class ChannelPacer:
    """Serialises delivery for a single channel."""

    def __init__(self, daemon: "Daemon", channel: str) -> None:
        self.daemon = daemon
        self.channel = channel
        self._signal = asyncio.Event()
        self._task = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def poke(self) -> None:
        self._signal.set()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        # Drain first, wait afterwards: a poke that arrives while a delivery is
        # in flight must not be swallowed, so the event is cleared only after a
        # wait actually returned.
        while True:
            delivered, more = await self.daemon.deliver_one(self.channel)
            if delivered:
                if more:
                    await asyncio.sleep(self.daemon.settings.delivery_gap)
                continue
            await self._signal.wait()
            self._signal.clear()


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

def _socket_is_listening(path) -> bool:
    """True when something accepts connections on ``path`` right now."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = EventLog(settings.log_path, settings.log_max_bytes)
        self.receivers = {}      # channel -> [Conn] (index 0 is the owner)
        self.pacers = {}         # channel -> ChannelPacer
        self.inflight = set()    # message ids handed to a socket, not acked yet
        self.wakeup_generation = {}
        self.ping_probes = {}
        self.ping_sequence = 0
        self._tasks = set()      # detached tasks, held so they are not collected
        self.loop = None
        self.scheduler = None
        self.db = None
        self._db_executor = ThreadPoolExecutor(max_workers=1,
                                               thread_name_prefix="sockpost-db")
        self._pid_handle = None
        self._stop = None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        problem = socket_path_problem(self.settings.socket_path)
        if problem:
            sys.stderr.write("error: %s\n" % problem)
            return 1
        ensure_parent(self.settings.pid_path)
        ensure_parent(self.settings.socket_path)
        ensure_parent(self.settings.db_path)

        # Open without truncating: a second daemon that loses the race must not
        # destroy the pid of the one that is running, or 'sockpost stop' would
        # no longer be able to find it. The file is only rewritten after the
        # lock has been acquired.
        descriptor = os.open(str(self.settings.pid_path),
                             os.O_RDWR | os.O_CREAT, 0o600)
        self._pid_handle = os.fdopen(descriptor, "r+")
        try:
            fcntl.flock(self._pid_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write("error: another daemon already holds %s\n"
                             % self.settings.pid_path)
            self._pid_handle.close()
            self._pid_handle = None
            return 1
        self._pid_handle.seek(0)
        self._pid_handle.truncate()
        self._pid_handle.write(str(os.getpid()))
        self._pid_handle.flush()

        self.db = store.connect(self.settings.db_path)
        store.init_schema(self.db)
        store.reset_all_channels(self.db)

        if self.settings.socket_path.exists():
            # A leftover socket file is normal after a crash and is safe to
            # remove. A socket somebody is still listening on is not: unlinking
            # it would leave two daemons serving the same path with different
            # queues. This can only happen through a partial override (a custom
            # pid path with the default socket path), so it is reported loudly.
            if _socket_is_listening(self.settings.socket_path):
                sys.stderr.write(
                    "error: another process is already listening on %s; "
                    "override SOCKPOST_SOCKET as well if this is intentional\n"
                    % self.settings.socket_path)
                return 1
            try:
                self.settings.socket_path.unlink()
            except OSError:
                pass

        try:
            asyncio.run(self._serve())
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()
        return 0

    def _cleanup(self) -> None:
        try:
            if self.settings.socket_path.exists():
                self.settings.socket_path.unlink()
        except OSError:
            pass
        # Drain the worker before closing the connection it operates on,
        # otherwise an in-flight job meets a closed database.
        self._db_executor.shutdown(wait=True)
        try:
            if self.db is not None:
                self.db.close()
        except sqlite3.Error:
            pass
        if self._pid_handle is not None:
            try:
                fcntl.flock(self._pid_handle, fcntl.LOCK_UN)
                self._pid_handle.close()
            except OSError:
                pass
        self.log.write("daemon_stopped")

    async def _serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.scheduler = Scheduler(self.loop, log=self.log)
        self.scheduler.start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

        await self._restore_wakeups()
        await self._sweep_orphans()
        self._schedule_orphan_sweep()

        # The umask makes the socket owner-only from the moment it exists;
        # chmod afterwards would leave a window in which anyone could connect.
        # The stream reader buffers a whole line before returning it, and its
        # default ceiling is 64 KiB. Left alone it would reject any message
        # larger than that with an exception, well below the documented body
        # limit, so the ceiling is derived from that limit instead. The factor
        # of two covers JSON escaping of a worst case body.
        read_limit = max(2 ** 16, self.settings.max_body_bytes * 2 + 2 ** 16)
        previous_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(
                self._on_client, path=str(self.settings.socket_path),
                limit=read_limit)
        finally:
            os.umask(previous_umask)
        try:
            os.chmod(self.settings.socket_path, 0o600)
        except OSError:
            pass

        self.log.write("daemon_started", pid=os.getpid(),
                       socket=self.settings.socket_path)
        # Announce readiness on stdout so a supervisor sees a single clear line.
        print("daemon=up socket=%s db=%s"
              % (self.settings.socket_path, self.settings.db_path), flush=True)

        async with server:
            await self._stop.wait()
        self.scheduler.stop()

    # -- database ----------------------------------------------------------

    async def _db(self, fn):
        return await self.loop.run_in_executor(self._db_executor, fn, self.db)

    # -- talking to peers --------------------------------------------------

    def _spawn(self, coro) -> None:
        """Run a coroutine detached, keeping a reference to it.

        A task that nobody holds can be collected while it is still running,
        so every fire and forget task is parked in a set until it finishes.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, conn: Conn, payload: dict) -> bool:
        """Send to a peer, giving up if it has stopped reading.

        A consumer that is alive but never reads fills the daemon's write
        buffer, and an unbounded await would then block whoever is writing to
        it. That is how a wedged listener used to be able to hold its channel
        against the takeover that was supposed to replace it. On timeout the
        peer is treated as gone and its connection is closed.
        """
        try:
            await asyncio.wait_for(conn.send(payload),
                                   timeout=self.settings.write_timeout)
            return True
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError,
                OSError):
            self.log.write("peer_write_failed", channel=conn.channel,
                           op=payload.get("op"))
            conn.close()
            return False

    # -- connection handling ----------------------------------------------

    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        conn = Conn(writer)
        channel = None
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # The peer sent more than the reader will buffer for one
                    # line. The stream cannot be resynchronised, so the client
                    # is told why and the connection ends here.
                    try:
                        await conn.send({
                            "op": P.OP_ERROR,
                            "error": "request exceeds the read buffer; a "
                                     "message body must stay under "
                                     "SOCKPOST_MAX_BODY_BYTES (%d)"
                                     % self.settings.max_body_bytes})
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    self.log.write("request_too_large", channel=channel)
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    request = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(request, dict):
                    await conn.send({"op": P.OP_ERROR,
                                     "error": "each request must be a JSON object"})
                    continue
                try:
                    await self._dispatch(conn, request)
                except Exception as exc:
                    # One malformed request must cost one error reply, not a
                    # dead connection and a stack trace in the operator's log.
                    self.log.write("request_failed", channel=channel,
                                   op=request.get("op"),
                                   error=type(exc).__name__)
                    try:
                        await conn.send({"op": P.OP_ERROR,
                                         "error": "could not process request: %s"
                                                  % type(exc).__name__})
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    continue
                if request.get("op") == P.OP_CONNECT and conn.channel:
                    channel = conn.channel
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            await self._handle_disconnect(channel, conn)
            conn.close()

    async def _dispatch(self, conn: Conn, request: dict) -> None:
        """Route one request. Raising here costs an error reply, not the loop."""
        op = request.get("op")

        if op == P.OP_CONNECT:
            # Validated here and not only in send: an id reaches the event
            # log, the key=value output and the SQL parameters, so a channel
            # called "a\nqueued msg_id=1" must never be able to forge a line
            # anywhere downstream.
            try:
                channel = P.validate_channel(request.get("id"))
            except P.ProtocolError as exc:
                await conn.send({"op": P.OP_ERROR, "error": str(exc)})
                return
            conn.channel = channel
            await self._handle_connect(conn, channel)
        elif op == P.OP_SEND:
            await self._handle_send(conn, request)
        elif op == P.OP_FORWARD:
            await self._handle_forward(conn, request)
        elif op == P.OP_ACK:
            await self._handle_ack(conn, request)
        elif op == P.OP_WAKEUP_SET:
            await self._handle_wakeup_set(conn, request)
        elif op == P.OP_PING:
            await conn.send({"op": P.OP_PONG})
        elif op == P.OP_PING_PROBE:
            targets = [str(t) for t in request.get("to", [])]
            try:
                timeout = float(request.get("timeout", 5))
            except (TypeError, ValueError):
                timeout = 5.0
            # Clamped: the probe holds a task and a table entry until it ends,
            # and the caller does not get to decide how long that is.
            timeout = min(max(timeout, 0.1), 60.0)
            self._spawn(self._run_ping_probe(conn, targets, timeout))
        elif op == P.OP_PING_ACK:
            self._collect_ping_ack(request)
        else:
            await conn.send({"op": P.OP_ERROR, "error": "unknown op %r" % op})

    async def _handle_connect(self, conn: Conn, channel: str) -> None:
        previous = self.receivers.get(channel, [])
        self.receivers[channel] = [conn]
        for old in previous:
            # Detached on purpose: the takeover must not wait on the peer being
            # replaced, which is quite possibly the reason it is being replaced.
            self._spawn(self._evict(old, channel))
        if previous:
            self.log.write("evicted", channel=channel)

        pending = await self._db(lambda c: store.reset_pending(c, channel))
        self.inflight -= pending
        await self._db(lambda c: store.set_channel_state(c, channel, True))
        await conn.send({"op": P.OP_CONNECTED, "id": channel})
        self.log.write("connected", channel=channel, pending=len(pending))
        self._schedule_delivery(channel)

    async def _evict(self, old: Conn, channel: str) -> None:
        await self._write(old, {"op": P.OP_EVICTED, "id": channel,
                                "reason": "last_connected_wins",
                                "detail": EVICTION_NOTICE})
        old.close()

    async def _handle_disconnect(self, channel, conn: Conn) -> None:
        if not channel:
            return
        connections = self.receivers.get(channel, [])
        if conn not in connections:
            return
        connections.remove(conn)
        if connections:
            return
        # Drop the bookkeeping for a channel nobody is on: the entries are
        # rebuilt on the next connect, and keeping them would mean one
        # permanent task and two dictionary entries per channel id ever seen.
        self.receivers.pop(channel, None)
        pacer = self.pacers.pop(channel, None)
        if pacer is not None:
            pacer.stop()
        pending = await self._db(lambda c: store.pending_ids(c, channel))
        self.inflight -= pending
        for message_id in pending:
            self.scheduler.cancel(("redeliver", message_id))
        await self._db(lambda c: store.set_channel_state(c, channel, False))
        self.log.write("disconnected", channel=channel)

    # -- operations --------------------------------------------------------

    async def _handle_send(self, conn: Conn, request: dict) -> None:
        try:
            sender = P.validate_channel(request.get("from"))
            recipient = P.validate_channel(request.get("to"))
        except P.ProtocolError as exc:
            await conn.send({"op": P.OP_ERROR, "error": str(exc)})
            return
        body = request.get("text", "")
        if not isinstance(body, str):
            body = str(body)
        size = len(body.encode("utf-8"))
        if size > self.settings.max_body_bytes:
            await conn.send({"op": P.OP_ERROR, "error":
                             "message body is %d bytes, the limit is %d "
                             "(raise SOCKPOST_MAX_BODY_BYTES or send a path "
                             "instead of a payload)"
                             % (size, self.settings.max_body_bytes)})
            return
        try:
            message_id = await self._db(
                lambda c: store.insert_message(c, recipient, sender, body))
        except sqlite3.Error as exc:
            await conn.send({"op": P.OP_ERROR, "error": "database write failed: %s" % exc})
            return
        await conn.send({"op": P.OP_QUEUED, "msg_id": message_id})
        self.log.write("queued", msg_id=message_id, sender=sender,
                       recipient=recipient, bytes=size)
        self._route(message_id, sender, recipient)

    def _route(self, message_id: int, sender: str, recipient: str) -> None:
        """Hand a freshly queued message to the delivery path."""
        if self.receivers.get(recipient):
            # Connected: the delivery loop will arm the watchdog when the
            # message actually goes out.
            self._schedule_delivery(recipient)
        else:
            # Nobody is listening, so no delivery is coming. Watch for the
            # recipient to show up; if it does not, the sender is told. No
            # pacer is created for an absent channel: connecting creates one,
            # and a flood of messages to channels that do not exist should not
            # leave a task behind for each invented name.
            self._arm_ack_watchdog(message_id, sender, recipient)

    async def _handle_forward(self, conn: Conn, request: dict) -> None:
        """Copy a message that is already in the queue to another channel.

        The copy is a new message from the forwarder, so it follows the normal
        delivery, acknowledgement and redelivery rules; the original is left
        exactly as it was. Provenance is prepended to the body.

        There is no ownership check. Any local process can already read the
        whole queue with ``sockpost unread``, so refusing to forward somebody
        else's message would cost the caller nothing and buy no secrecy. What
        the daemon does instead is record who forwarded what, in the copy and
        in its log.
        """
        try:
            forwarder = P.validate_channel(request.get("from"))
            recipient = P.validate_channel(request.get("to"))
        except P.ProtocolError as exc:
            await conn.send({"op": P.OP_ERROR, "error": str(exc)})
            return
        try:
            source_id = int(request.get("src_id"))
        except (TypeError, ValueError):
            await conn.send({"op": P.OP_ERROR,
                             "error": "message id must be a number, got %r"
                                      % (request.get("src_id"),)})
            return
        row = await self._db(lambda c: store.get_message(c, source_id))
        if row is None:
            await conn.send({"op": P.OP_ERROR,
                             "error": "no such message %d" % source_id})
            return
        source_recipient, source_sender, body = str(row[1]), str(row[2]), row[3]
        if source_recipient == recipient:
            await conn.send({"op": P.OP_ERROR, "error":
                             "message %d is already addressed to %s; a copy "
                             "there would carry nothing new"
                             % (source_id, recipient)})
            return
        if P.is_forward(body):
            # A forward is terminal. Without this, three agents forwarding to
            # each other would grow one message into an unbounded chain, each
            # copy longer than the last.
            await conn.send({"op": P.OP_ERROR, "error":
                             "message %d is itself a forward, and a forward "
                             "is terminal; send the original (ref in its "
                             "first line) if it has to travel further"
                             % source_id})
            return
        note = request.get("note")
        if note is not None and not isinstance(note, str):
            note = str(note)
        copy = P.forward_body(source_sender, forwarder, source_id, body, note)
        size = len(copy.encode("utf-8"))
        if size > self.settings.max_body_bytes:
            # The provenance line makes the copy larger than the original, so
            # a message that was accepted at the limit cannot be forwarded.
            await conn.send({"op": P.OP_ERROR, "error":
                             "the forwarded copy is %d bytes with its "
                             "provenance line, the limit is %d (raise "
                             "SOCKPOST_MAX_BODY_BYTES)"
                             % (size, self.settings.max_body_bytes)})
            return
        try:
            message_id = await self._db(
                lambda c: store.insert_message(c, recipient, forwarder, copy))
        except sqlite3.Error as exc:
            await conn.send({"op": P.OP_ERROR,
                             "error": "database write failed: %s" % exc})
            return
        await conn.send({"op": P.OP_FORWARDED, "msg_id": message_id,
                         "src_id": source_id, "to": recipient})
        self.log.write("forwarded", msg_id=message_id, src_id=source_id,
                       sender=forwarder, recipient=recipient, bytes=size)
        self._route(message_id, forwarder, recipient)

    async def _handle_ack(self, conn: Conn, request: dict) -> None:
        try:
            message_id = int(request.get("msg_id"))
        except (TypeError, ValueError):
            await conn.send({"op": P.OP_ACK_RESULT, "msg_id": request.get("msg_id"),
                             "updated": 0, "reason": "invalid id"})
            return
        try:
            recipient = P.validate_channel(request.get("from"))
        except P.ProtocolError as exc:
            await conn.send({"op": P.OP_ACK_RESULT, "msg_id": message_id,
                             "updated": 0, "reason": str(exc)})
            return
        updated = await self._db(lambda c: store.ack_message(c, message_id, recipient))
        if updated:
            self.inflight.discard(message_id)
            self.scheduler.cancel(("ack", message_id))
            self.scheduler.cancel(("redeliver", message_id))
            self.log.write("acked", msg_id=message_id, channel=recipient)
            await conn.send({"op": P.OP_ACK_RESULT, "msg_id": message_id, "updated": 1})
            return
        status = await self._db(lambda c: store.message_status(c, message_id))
        if status is None:
            reason = "no such message"
        elif status[0] == store.STATE_ACKED:
            reason = "already acknowledged"
        elif status[0] == store.STATE_EXPIRED:
            reason = "message expired"
        elif str(status[1]) != recipient:
            reason = "channel %s is not the recipient" % (recipient or "<unset>")
        else:
            reason = "not acknowledged"
        await conn.send({"op": P.OP_ACK_RESULT, "msg_id": message_id,
                         "updated": 0, "reason": reason})

    async def _handle_wakeup_set(self, conn: Conn, request: dict) -> None:
        try:
            channel = P.validate_channel(request.get("channel"))
        except P.ProtocolError as exc:
            await conn.send({"op": P.OP_ERROR, "error": str(exc)})
            return
        if request.get("off"):
            await self._db(lambda c: store.delete_wakeup(c, channel))
            self._stop_wakeup(channel)
            await conn.send({"op": P.OP_WAKEUP_OK, "channel": channel, "off": True})
            self.log.write("wakeup_off", channel=channel)
            return
        try:
            seconds = int(request.get("seconds", 0))
        except (TypeError, ValueError):
            seconds = 0
        interval = str(request.get("interval") or ("%ds" % seconds))
        if seconds <= 0 or seconds > P.MAX_INTERVAL_SECONDS:
            await conn.send({"op": P.OP_ERROR,
                             "error": "wakeup interval must be between 1 second "
                                      "and %d days"
                                      % (P.MAX_INTERVAL_SECONDS // 86400)})
            return
        await self._db(lambda c: store.save_wakeup(c, channel, interval, seconds))
        self._start_wakeup(channel, seconds)
        await conn.send({"op": P.OP_WAKEUP_OK, "channel": channel,
                         "interval": interval, "seconds": seconds})
        self.log.write("wakeup_set", channel=channel, seconds=seconds)

    # -- delivery ----------------------------------------------------------

    def _schedule_delivery(self, channel: str) -> None:
        pacer = self.pacers.get(channel)
        if pacer is None:
            pacer = ChannelPacer(self, channel)
            self.pacers[channel] = pacer
            pacer.start()
        pacer.poke()

    async def deliver_one(self, channel: str):
        """Deliver at most one message. Returns ``(delivered, has_more)``."""
        connections = list(self.receivers.get(channel, []))
        if not connections:
            return False, False
        cutoff = P.iso_at(time.time() - self.settings.redeliver_gap)
        rows = await self._db(
            lambda c: store.pending_batch(c, channel, self.settings.drain_batch))
        eligible = [
            row for row in rows
            if int(row[0]) not in self.inflight
            and not (row[4] is not None and row[4] >= cutoff)
        ]
        if not eligible:
            return False, False
        message_id, sender, body, created_at, _delivered_at = eligible[0]
        payload = {"op": P.OP_MESSAGE, "msg_id": int(message_id), "from": sender,
                   "text": body, "created_at": created_at}
        for conn in connections:
            if await self._write(conn, payload):
                break
        else:
            return False, False
        self.inflight.add(int(message_id))
        try:
            await self._db(lambda c: store.mark_delivered(c, int(message_id)))
        except sqlite3.Error:
            pass
        self.log.write("delivered", msg_id=int(message_id), channel=channel)
        # Armed here rather than at enqueue: a message still waiting its turn
        # behind a burst has not been offered to anybody yet, and reporting it
        # as unacknowledged would accuse a consumer that is keeping up.
        self._arm_ack_watchdog(int(message_id), sender, channel)
        self._arm_redelivery(int(message_id), channel)
        return True, len(eligible) > 1

    def _arm_redelivery(self, message_id: int, channel: str) -> None:
        """Let an unacknowledged message become eligible again.

        Without this, a message handed to a consumer that stays connected and
        never acknowledges would sit in ``inflight`` for the lifetime of the
        daemon: the redelivery window would be documented but unreachable.
        """
        # One second of slack past the window: eligibility is decided by
        # comparing second-resolution timestamps, so a timer that fires at
        # exactly the window still finds the message inside it.
        delay = self.settings.redeliver_gap + 1

        def retry():
            if message_id not in self.inflight:
                return
            self.inflight.discard(message_id)
            self.log.write("redelivery_due", msg_id=message_id, channel=channel)
            self._schedule_delivery(channel)
            # Keep trying. A successful delivery re-arms this key from
            # deliver_one, an acknowledgement cancels it, and a disconnect
            # cancels it as well, so this cannot outlive the message.
            self.scheduler.arm(("redeliver", message_id), delay, retry)

        self.scheduler.arm(("redeliver", message_id), delay, retry)

    def _arm_ack_watchdog(self, message_id: int, sender: str, recipient: str) -> None:
        online = bool(self.receivers.get(recipient))
        delay = (self.settings.ack_timeout_online if online
                 else self.settings.ack_timeout_offline)
        self.scheduler.arm(
            ("ack", message_id), delay,
            lambda: self._spawn(
                self._fire_ack_watchdog(message_id, sender, recipient, delay)))

    async def _fire_ack_watchdog(self, message_id: int, sender: str,
                                 recipient: str, waited: int) -> None:
        status = await self._db(lambda c: store.message_status(c, message_id))
        if not status or status[0] != store.STATE_PENDING:
            return
        if status[2] is not None:
            return  # this failure cycle was already reported
        await self._db(lambda c: store.mark_failure_reported(c, message_id))
        if self.receivers.get(recipient):
            self.log.write("delivery_timeout", msg_id=message_id,
                           channel=recipient, waited=waited)
            await self._notify(sender, {"op": P.OP_DELIVERY_TIMEOUT,
                                        "msg_id": message_id,
                                        "target": recipient, "waited": waited})
        else:
            self.log.write("unreachable", msg_id=message_id, channel=recipient)
            await self._notify(sender, {"op": P.OP_UNREACHABLE,
                                        "msg_id": message_id, "target": recipient})

    async def _notify(self, channel: str, payload: dict) -> None:
        """Best effort, queue-less delivery of an operational event.

        Operational events reach a channel only while it is listening; they are
        not queued. A one-shot command that has already exited never sees them.
        """
        for conn in list(self.receivers.get(channel, [])):
            await self._write(conn, payload)

    # -- wakeups -----------------------------------------------------------

    async def _restore_wakeups(self) -> None:
        rows = await self._db(lambda c: store.list_wakeups(c))
        for channel, _interval, seconds in rows:
            self._start_wakeup(str(channel), int(seconds))
        if rows:
            self.log.write("wakeups_restored", count=len(rows))

    def _start_wakeup(self, channel: str, seconds: int) -> None:
        generation = self.wakeup_generation.get(channel, 0) + 1
        self.wakeup_generation[channel] = generation

        def fire():
            if self.wakeup_generation.get(channel) != generation:
                return
            self._spawn(self._fire_wakeup(channel, seconds, generation))

        self.scheduler.arm(("wakeup", channel), seconds, fire)

    def _stop_wakeup(self, channel: str) -> None:
        self.wakeup_generation[channel] = self.wakeup_generation.get(channel, 0) + 1
        self.scheduler.cancel(("wakeup", channel))

    async def _fire_wakeup(self, channel: str, seconds: int, generation: int) -> None:
        if self.wakeup_generation.get(channel) != generation:
            return
        await self._notify(channel, {"op": P.OP_WAKEUP, "ts": P.now_iso()})

        def again():
            if self.wakeup_generation.get(channel) != generation:
                return
            self._spawn(self._fire_wakeup(channel, seconds, generation))

        self.scheduler.arm(("wakeup", channel), seconds, again)

    # -- housekeeping ------------------------------------------------------

    def _schedule_orphan_sweep(self) -> None:
        def fire():
            self._spawn(self._sweep_orphans())
            self._schedule_orphan_sweep()

        self.scheduler.arm(("orphan_sweep",), self.settings.orphan_sweep_interval, fire)

    async def _sweep_orphans(self) -> None:
        cutoff = P.iso_at(time.time() - self.settings.orphan_ttl)
        try:
            expired = await self._db(lambda c: store.expire_orphans(c, cutoff))
        except sqlite3.Error:
            return
        if expired:
            self.log.write("orphans_expired", count=expired)

    # -- ping --------------------------------------------------------------

    async def _run_ping_probe(self, conn: Conn, targets, timeout: float) -> None:
        self.ping_sequence += 1
        probe_id = "p%d-%d" % (self.ping_sequence, int(time.time() * 1000))
        done = asyncio.Event()
        probe = {"sent": {}, "acks": {}, "event": done}
        self.ping_probes[probe_id] = probe
        for target in targets:
            connections = list(self.receivers.get(target, []))
            if not connections:
                continue
            probe["sent"][target] = time.time()
            try:
                await connections[0].send({"op": P.OP_PING_CHECK, "probe_id": probe_id})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        if probe["sent"]:
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        acks = dict(self.ping_probes.pop(probe_id, {}).get("acks", {}))
        try:
            await conn.send({"op": P.OP_PING_RESULT,
                             "results": {t: acks.get(t) for t in targets}})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _collect_ping_ack(self, request: dict) -> None:
        probe = self.ping_probes.get(request.get("probe_id"))
        target = str(request.get("id"))
        if not probe or target not in probe["sent"]:
            return
        probe["acks"][target] = (time.time() - probe["sent"][target]) * 1000.0
        if len(probe["acks"]) >= len(probe["sent"]):
            probe["event"].set()


def run_daemon(settings: Settings) -> int:
    return Daemon(settings).run()
