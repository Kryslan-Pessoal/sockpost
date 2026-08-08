"""Client side commands.

Every command is a short lived process that speaks to the daemon over the Unix
socket, except ``listen`` which stays connected and streams events until it is
stopped or evicted.

Output is deliberately machine readable: one event per line, lowercase
``key=value`` pairs, free text values quoted with JSON escaping so a message
body can never break the line format.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time

from . import protocol as P
from . import store
from .config import Settings, ensure_parent, socket_path_problem

CONNECT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fail(message: str) -> int:
    sys.stderr.write("error: %s\n" % message)
    return 1


def daemon_alive(settings: Settings) -> bool:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(settings.socket_path))
        sock.close()
        return True
    except OSError:
        return False


def _spawn_detached(argv, log_handle) -> bool:
    """Start a process that outlives this one and leaves nothing to reap.

    A plain Popen would make the daemon a child of whoever started it. That is
    harmless for a command that exits immediately, but ``listen`` runs for
    days: if the daemon then exited, its zombie would sit in the process table
    for the whole session. Forking an intermediate that exits at once hands the
    daemon to init, and the intermediate is reaped here and now.
    """
    try:
        pid = os.fork()
    except (AttributeError, OSError):
        pid = None
    if pid is None:  # pragma: no cover - platforms without fork
        try:
            subprocess.Popen(argv, stdout=log_handle, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            return False
    if pid == 0:
        try:
            subprocess.Popen(argv, stdout=log_handle, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            os._exit(1)
        os._exit(0)
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return True
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def ensure_daemon(settings: Settings) -> bool:
    """Start the daemon in the background if it is not answering yet.

    Racing clients are harmless: the daemon takes an exclusive lock on its pid
    file, so any extra process exits immediately without touching the queue.
    """
    if daemon_alive(settings):
        return True
    if not settings.autostart or socket_path_problem(settings.socket_path):
        return False
    ensure_parent(settings.log_path)
    ensure_parent(settings.socket_path)
    try:
        log_handle = open(settings.log_path, "a")
    except OSError:
        log_handle = subprocess.DEVNULL
    if not _spawn_detached([sys.executable, "-m", "sockpost", "daemon"],
                           log_handle):
        return False
    for _ in range(50):  # up to 10 seconds
        if daemon_alive(settings):
            return True
        time.sleep(0.2)
    return False


def _open(settings: Settings, timeout: float = CONNECT_TIMEOUT,
          autostart: bool = True) -> socket.socket:
    if autostart:
        ensure_daemon(settings)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(settings.socket_path))
    return sock


def _roundtrip(settings: Settings, payload: dict, expect, timeout: float = CONNECT_TIMEOUT):
    """Send one request and wait for the first reply whose op is expected."""
    wanted = set(expect) | {P.OP_ERROR}
    sock = _open(settings, timeout=timeout)
    try:
        P.send_line(sock, payload)
        handle = sock.makefile("r", encoding="utf-8")
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("op") in wanted:
                return obj
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return None


def _resolve_id(settings: Settings, value, flag: str):
    """Resolve a channel id from the flag or from SOCKPOST_ID."""
    candidate = value or settings.default_id
    if not candidate:
        raise P.ProtocolError(
            "missing %s (pass the flag or export SOCKPOST_ID)" % flag)
    return P.validate_channel(candidate)


def _unreachable_hint(settings: Settings) -> str:
    problem = socket_path_problem(settings.socket_path)
    if problem:
        return problem
    return ("daemon is not answering on %s (start it with 'sockpost daemon' or "
            "check SOCKPOST_SOCKET)" % settings.socket_path)


# ---------------------------------------------------------------------------
# listen
# ---------------------------------------------------------------------------

def _print_event(obj: dict, channel: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False), flush=True)
        return
    op = obj.get("op")
    if op == P.OP_CONNECTED:
        print("connected id=%s" % channel, flush=True)
    elif op == P.OP_MESSAGE:
        print("message id=%s from=%s at=%s text=%s"
              % (obj.get("msg_id"), obj.get("from"), obj.get("created_at"),
                 P.quote(obj.get("text"))), flush=True)
    elif op == P.OP_WAKEUP:
        print("wakeup ts=%s" % obj.get("ts"), flush=True)
    elif op == P.OP_DELIVERY_TIMEOUT:
        print("delivery-timeout id=%s target=%s waited=%ss"
              % (obj.get("msg_id"), obj.get("target"), obj.get("waited")), flush=True)
    elif op == P.OP_UNREACHABLE:
        print("unreachable id=%s target=%s"
              % (obj.get("msg_id"), obj.get("target")), flush=True)
    elif op == P.OP_EVICTED:
        print("evicted id=%s reason=%s" % (channel, obj.get("reason")), flush=True)
    elif op == P.OP_ERROR:
        sys.stderr.write("error: %s\n" % obj.get("error"))


def cmd_listen(settings: Settings, channel_arg, manual_ack: bool,
               as_json: bool) -> int:
    """Stream events for one channel until stopped or evicted."""
    try:
        channel = _resolve_id(settings, channel_arg, "--id")
    except P.ProtocolError as exc:
        return _fail(str(exc))

    stopping = {"flag": False}

    def _on_signal(_signum, _frame):
        stopping["flag"] = True
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # pragma: no cover - non main thread
            pass

    backoff = 1.0
    warned = False
    sock = None
    while True:
        try:
            sock = _open(settings, timeout=None)
            P.send_line(sock, {"op": P.OP_CONNECT, "id": channel})
            handle = sock.makefile("r", encoding="utf-8")
            backoff = 1.0
            warned = False
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _print_event(obj, channel, as_json)
                op = obj.get("op")
                if op == P.OP_MESSAGE and not manual_ack:
                    # Automatic acknowledgement means "handed to the process".
                    # With --manual-ack it means "processed by the agent", and
                    # the agent is responsible for running 'sockpost ack'.
                    try:
                        P.send_line(sock, {"op": P.OP_ACK,
                                           "msg_id": obj.get("msg_id"),
                                           "from": channel})
                    except OSError:
                        pass
                elif op == P.OP_PING_CHECK:
                    try:
                        P.send_line(sock, {"op": P.OP_PING_ACK,
                                           "probe_id": obj.get("probe_id"),
                                           "id": channel})
                    except OSError:
                        pass
                elif op == P.OP_EVICTED:
                    return 0
            raise ConnectionResetError("daemon closed the connection")
        except KeyboardInterrupt:
            return 0
        except (ConnectionRefusedError, FileNotFoundError, BrokenPipeError,
                ConnectionResetError, OSError):
            if stopping["flag"]:
                return 0
            if not warned:
                # Say it once, then reconnect quietly: a listener that prints a
                # line per attempt would bury the events it exists to show.
                sys.stderr.write("waiting for the daemon on %s\n"
                                 % settings.socket_path)
                warned = True
            # Jitter keeps many listeners from reconnecting in lockstep after
            # the daemon restarts.
            try:
                time.sleep(backoff * (0.75 + 0.5 * random.random()))
            except KeyboardInterrupt:
                return 0
            backoff = min(backoff * 2, 30.0)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None


# ---------------------------------------------------------------------------
# send / ack
# ---------------------------------------------------------------------------

def cmd_send(settings: Settings, from_arg, to_arg, text) -> int:
    try:
        sender = _resolve_id(settings, from_arg, "--from")
        recipient = P.validate_channel(to_arg)
    except P.ProtocolError as exc:
        return _fail(str(exc))
    if text is None:
        return _fail("missing --text")
    try:
        reply = _roundtrip(settings, {"op": P.OP_SEND, "from": sender,
                                      "to": recipient, "text": text},
                           expect=[P.OP_QUEUED])
    except (BrokenPipeError, ConnectionResetError):
        # The connection was accepted and then dropped mid-write. For a send
        # that means the request was too large for the daemon to buffer, and
        # saying "not answering" would send the user looking in the wrong place.
        return _fail("the daemon closed the connection while receiving this "
                     "message; it is most likely larger than the daemon will "
                     "accept (see SOCKPOST_MAX_BODY_BYTES)")
    except OSError:
        return _fail(_unreachable_hint(settings))
    if reply is None:
        return _fail("no reply from daemon")
    if reply.get("op") == P.OP_ERROR:
        return _fail(str(reply.get("error")))
    print("queued id=%s" % reply.get("msg_id"))
    return 0


def cmd_ack(settings: Settings, message_id, from_arg) -> int:
    try:
        channel = _resolve_id(settings, from_arg, "--from")
    except P.ProtocolError as exc:
        return _fail(str(exc))
    try:
        numeric_id = int(message_id)
    except (TypeError, ValueError):
        return _fail("message id must be a number, got %r" % message_id)
    try:
        reply = _roundtrip(settings, {"op": P.OP_ACK, "msg_id": numeric_id,
                                      "from": channel},
                           expect=[P.OP_ACK_RESULT])
    except OSError:
        return _fail(_unreachable_hint(settings))
    if reply is None:
        return _fail("no reply from daemon")
    if reply.get("op") == P.OP_ERROR:
        return _fail(str(reply.get("error")))
    if reply.get("updated"):
        print("ack id=%d" % numeric_id)
        return 0
    reason = reply.get("reason", "not acknowledged")
    print("ack id=%d status=%s" % (numeric_id, P.quote(reason)))
    # Acknowledging twice is a normal, idempotent outcome, not a failure.
    return 0 if reason == "already acknowledged" else 1


# ---------------------------------------------------------------------------
# unread / status
# ---------------------------------------------------------------------------

def cmd_unread(settings: Settings, only_id) -> int:
    if not settings.db_path.exists():
        print("unread n=0")
        return 0
    try:
        conn = store.connect(settings.db_path, readonly=True)
    except Exception as exc:  # sqlite3.Error and OSError both possible
        return _fail("cannot read %s: %s" % (settings.db_path, exc))
    try:
        channel = P.validate_channel(only_id) if only_id else None
    except P.ProtocolError as exc:
        conn.close()
        return _fail(str(exc))
    try:
        rows = store.list_unread(conn, channel)
    except Exception as exc:
        conn.close()
        return _fail("cannot read the queue: %s" % exc)
    conn.close()
    print("unread n=%d" % len(rows))
    for message_id, sender, recipient, body, created_at in rows:
        age = P.age_seconds(created_at)
        waiting = P.format_duration(age) if age is not None else "unknown"
        print("id=%s from=%s to=%s waiting=%s text=%s"
              % (message_id, sender, recipient, waiting, P.quote(body)))
    return 0


def _ping_targets(settings: Settings, targets, timeout: float):
    try:
        reply = _roundtrip(settings, {"op": P.OP_PING_PROBE, "to": targets,
                                      "timeout": timeout},
                           expect=[P.OP_PING_RESULT], timeout=timeout + 5.0)
    except OSError:
        return None
    if not reply or reply.get("op") == P.OP_ERROR:
        return None
    return reply.get("results", {})


def cmd_status(settings: Settings) -> int:
    alive = daemon_alive(settings)
    rows = []
    if settings.db_path.exists():
        try:
            conn = store.connect(settings.db_path, readonly=True)
            rows = store.list_connected(conn)
            conn.close()
        except Exception:
            rows = []
    size = ""
    try:
        # The write-ahead log holds the most recent data and can dwarf the main
        # file, so reporting only the .db would understate the real footprint.
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = settings.db_path.with_name(settings.db_path.name + suffix)
            if path.exists():
                total += path.stat().st_size
        size = " db=%.1fMB" % (total / (1024 * 1024))
    except OSError:
        pass
    print("daemon=%s channels=%d%s" % ("up" if alive else "down", len(rows), size))
    for channel, since, interval, _seconds in rows:
        wakeup = " wakeup=%s" % interval if interval else ""
        print("id=%s since=%s%s" % (channel, since, wakeup))
    if not alive or not rows:
        return 0

    print("liveness:")
    results = _ping_targets(settings, [str(r[0]) for r in rows], 5.0) or {}
    alive_ids, dead_ids = [], []
    for row in rows:
        channel = str(row[0])
        latency = results.get(channel)
        if latency is None:
            print("silence id=%s timeout=5s" % channel)
            dead_ids.append(channel)
        else:
            print("pong id=%s rtt=%.0fms" % (channel, latency))
            alive_ids.append(channel)
    if dead_ids:
        print("summary alive=%d dead=%d dead_ids=%s"
              % (len(alive_ids), len(dead_ids), ",".join(dead_ids)))
        return 1
    print("summary alive=%d dead=0" % len(alive_ids))
    return 0


def cmd_ping(settings: Settings, targets_csv, timeout: float) -> int:
    try:
        targets = [P.validate_channel(t) for t in str(targets_csv).split(",") if t.strip()]
    except P.ProtocolError as exc:
        return _fail(str(exc))
    if not targets:
        return _fail("missing --to")
    if not daemon_alive(settings):
        return _fail(_unreachable_hint(settings))
    results = _ping_targets(settings, targets, timeout)
    if results is None:
        return _fail("the daemon did not answer the probe")
    dead = 0
    for target in targets:
        latency = results.get(target)
        if latency is None:
            print("silence id=%s timeout=%gs" % (target, timeout))
            dead += 1
        else:
            print("pong id=%s rtt=%.0fms" % (target, latency))
    return 0 if dead == 0 else 1


# ---------------------------------------------------------------------------
# wakeup
# ---------------------------------------------------------------------------

def cmd_wakeup(settings: Settings, channel_arg, interval_arg) -> int:
    try:
        channel = _resolve_id(settings, channel_arg, "channel")
    except P.ProtocolError as exc:
        return _fail(str(exc))
    if str(interval_arg).strip().lower() == "off":
        payload = {"op": P.OP_WAKEUP_SET, "channel": channel, "off": True}
    else:
        try:
            seconds = P.parse_interval(interval_arg)
        except P.ProtocolError as exc:
            return _fail(str(exc))
        payload = {"op": P.OP_WAKEUP_SET, "channel": channel,
                   "seconds": seconds, "interval": str(interval_arg)}
    try:
        reply = _roundtrip(settings, payload, expect=[P.OP_WAKEUP_OK])
    except OSError:
        return _fail(_unreachable_hint(settings))
    if reply is None:
        return _fail("no reply from daemon")
    if reply.get("op") == P.OP_ERROR:
        return _fail(str(reply.get("error")))
    if reply.get("off"):
        print("wakeup id=%s off" % channel)
    else:
        print("wakeup id=%s interval=%s seconds=%s"
              % (channel, reply.get("interval"), reply.get("seconds")))
    return 0


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def _running_daemon_pid(settings: Settings):
    """Pid of the daemon that currently holds the lock, or ``None``.

    The pid file alone is not trustworthy: after a crash it can point at a pid
    the operating system has since handed to an unrelated process. The lock is
    the authority, so the file is only believed while the lock is held.
    """
    if not settings.pid_path.exists():
        return None
    try:
        handle = open(settings.pid_path, "r+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # somebody holds it: a daemon is alive
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            return None  # nobody holds it: the file is stale
        try:
            pid = int((handle.read() or "0").strip())
        except ValueError:
            return None
        return pid if pid > 0 else None
    finally:
        handle.close()


def cmd_stop(settings: Settings) -> int:
    """Ask the running daemon to shut down."""
    pid = _running_daemon_pid(settings)
    if pid is None:
        print("daemon=down")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("daemon=down")
        return 0
    except PermissionError:
        return _fail("not allowed to signal pid %d" % pid)
    for _ in range(50):
        if not daemon_alive(settings):
            print("daemon=stopped pid=%d" % pid)
            return 0
        time.sleep(0.1)
    return _fail("daemon %d did not stop within 5s" % pid)
