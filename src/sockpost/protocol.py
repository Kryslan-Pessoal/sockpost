"""Wire protocol helpers shared by the daemon and the client.

The wire format is newline delimited JSON over a Unix domain socket. Every
object carries an ``op`` field. Anything the peer does not understand is
ignored, which keeps a newer client compatible with an older daemon.
"""

from __future__ import annotations

import json
import re
import socket as _socket
from datetime import datetime, timezone

# Client -> daemon
OP_CONNECT = "connect"
OP_SEND = "send"
OP_ACK = "ack"
OP_UNREAD = "unread"
OP_WAKEUP_SET = "wakeup_set"
OP_PING = "ping"
OP_PING_PROBE = "ping_probe"
OP_PING_ACK = "ping_ack"

# Daemon -> client
OP_CONNECTED = "connected"
OP_QUEUED = "queued"
OP_ACK_RESULT = "ack_result"
OP_MESSAGE = "message"
OP_WAKEUP = "wakeup"
OP_WAKEUP_OK = "wakeup_ok"
OP_PONG = "pong"
OP_PING_CHECK = "ping_check"
OP_PING_RESULT = "ping_result"
OP_EVICTED = "evicted"
OP_DELIVERY_TIMEOUT = "delivery_timeout"
OP_UNREACHABLE = "unreachable"
OP_ERROR = "error"

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# One year. Long enough for any heartbeat, short enough to stay a sane integer.
MAX_INTERVAL_SECONDS = 365 * 86400

_INTERVAL_RE = re.compile(r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|"
                          r"minutes|h|hr|hrs|hour|hours|d|day|days)?$")

_INTERVAL_UNITS = {
    None: 1,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


class ProtocolError(Exception):
    """Malformed input that the caller should report to the user."""


def now_iso() -> str:
    """Current UTC time as ``2026-01-31T23:59:00Z`` (sorts lexicographically)."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_iso(value: str):
    """Parse a timestamp produced by :func:`now_iso`. ``None`` when invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def age_seconds(value: str):
    """Seconds elapsed since ``value``. ``None`` when the timestamp is invalid."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def parse_interval(value: str) -> int:
    """Parse ``30s``, ``10min``, ``2h``, ``1d`` or a bare number of seconds."""
    if value is None:
        raise ProtocolError("empty interval")
    match = _INTERVAL_RE.match(str(value).strip().lower())
    if not match:
        raise ProtocolError(
            "invalid interval %r (expected forms: 30s, 10min, 2h, 1d)" % value)
    amount = int(match.group(1))
    seconds = amount * _INTERVAL_UNITS[match.group(2)]
    if seconds <= 0:
        raise ProtocolError("interval must be greater than zero")
    if seconds > MAX_INTERVAL_SECONDS:
        # Bounded so an absurd value cannot reach the database as an integer
        # too large for SQLite, or arm a timer that will never fire.
        raise ProtocolError(
            "interval must be at most %d days" % (MAX_INTERVAL_SECONDS // 86400))
    return seconds


def format_duration(seconds) -> str:
    """Render a duration compactly: ``45s``, ``12m``, ``3h05m``, ``2d04h``."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return "%dh%02dm" % (hours, minutes) if minutes else "%dh" % hours
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    return "%dd%02dh" % (days, hours) if hours else "%dd" % days


def validate_channel(value: str) -> str:
    """Return a normalised channel id or raise :class:`ProtocolError`.

    Channel ids are free form labels (``worker-1``, ``build.gate``).
    They are restricted to a conservative character set because they end up in
    log lines, key=value output and SQL parameters.
    """
    if value is None:
        raise ProtocolError("missing channel id")
    text = str(value).strip()
    if not CHANNEL_RE.match(text):
        raise ProtocolError(
            "invalid channel id %r (allowed: letters, digits, dot, dash, "
            "underscore; max 64 characters)" % value)
    return text


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def send_line(sock: _socket.socket, obj: dict) -> None:
    sock.sendall(encode(obj))


def quote(text) -> str:
    """Quote a value for key=value output (escapes newlines and quotes)."""
    return json.dumps("" if text is None else str(text), ensure_ascii=False)
