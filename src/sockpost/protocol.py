"""Wire protocol helpers shared by the daemon and the client.

The wire format is newline delimited JSON over a Unix domain socket. Every
object carries an ``op`` field.

Compatibility is one directional. Unknown *fields* are ignored, so a client
may add information an older daemon will not read. Unknown *operations* are
not: the daemon answers ``unknown op`` and the command fails. A client is
therefore compatible with a daemon of the same version or newer, and a daemon
started before an upgrade has to be restarted before the new commands work.
See "Upgrading" in the README.
"""

from __future__ import annotations

import json
import re
import socket as _socket
from datetime import datetime, timezone

# Client -> daemon
OP_CONNECT = "connect"
OP_SEND = "send"
OP_FORWARD = "forward"
OP_ACK = "ack"
OP_UNREAD = "unread"
OP_WAKEUP_SET = "wakeup_set"
OP_PING = "ping"
OP_PING_PROBE = "ping_probe"
OP_PING_ACK = "ping_ack"

# Daemon -> client
OP_CONNECTED = "connected"
OP_QUEUED = "queued"
OP_FORWARDED = "forwarded"
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

# A forwarded body opens with a control line, and the control line opens with
# this token. It is deliberately not something prose produces by accident: a
# leading '#', a slash separated name, and a fixed set of key=value fields that
# have to parse before the line counts as provenance. The version travels in a
# field rather than in the token so that a future header shape is still
# recognised as a forward by today's hop counter.
FORWARD_MARK = "#sockpost/forward"
FORWARD_HEADER_VERSION = 1

# key=value where the value is either a JSON string or a bare run of
# non-space characters. Anchored on nothing: the fields may appear in any
# order, and an unknown field is carried past without complaint.
_FORWARD_FIELD_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s"]+)')

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


# ---------------------------------------------------------------------------
# forwarding
# ---------------------------------------------------------------------------

def forward_header(origin: str, forwarder: str, source_id: int,
                   created_at: str, hops: int, note=None) -> str:
    """Build the control line that opens a forwarded body.

    ``#sockpost/forward v=1 hops=1 from="planner" via="worker" ref=7
    at="2026-01-31T18:04:11Z"``, plus ``note="..."`` when one was given.

    Every text value is quoted the way command output is, so nothing a caller
    supplies can add a field, break the line, or push the original body up
    into the header. Counts and ids are bare, which is how the rest of this
    tool prints numbers.
    """
    parts = ["%s v=%d hops=%d" % (FORWARD_MARK, FORWARD_HEADER_VERSION,
                                  int(hops)),
             "from=%s" % quote(origin),
             "via=%s" % quote(forwarder),
             "ref=%d" % int(source_id),
             "at=%s" % quote(created_at)]
    if note:
        parts.append("note=%s" % quote(note))
    return " ".join(parts)


def forward_body(text: str, origin: str, forwarder: str, source_id: int,
                 created_at: str, hops: int, note=None) -> str:
    """A forwarded copy: one control line, a newline, then the original text.

    Provenance travels in the body rather than in a column because a message
    is the only thing this protocol carries end to end: a consumer reading the
    text sees where it came from, with no second lookup and no schema change
    on a queue written by an older version.

    There is exactly one control line at every hop. The header is replaced,
    not stacked, so the shape of a copy never depends on how far it travelled;
    the step before this one is still on disk under ``ref``, with its own
    header, which is how a full path is walked.
    """
    return "%s\n%s" % (
        forward_header(origin, forwarder, source_id, created_at, hops, note),
        "" if text is None else str(text))


def parse_forward_header(line):
    """Parse a control line into a dict of fields, or ``None``.

    ``None`` means the line is not provenance: either it does not open with
    the marker, or what follows the marker is not a run of key=value fields.
    """
    text = "" if line is None else str(line)
    if not text.startswith(FORWARD_MARK):
        return None
    remainder = text[len(FORWARD_MARK):]
    if remainder and not remainder.startswith(" "):
        return None  # "#sockpost/forwarding my thoughts" is prose, not a header
    fields = {}
    for key, raw in _FORWARD_FIELD_RE.findall(remainder):
        if raw.startswith('"'):
            try:
                fields[key] = json.loads(raw)
            except ValueError:
                return None
        else:
            fields[key] = raw
    return fields


def split_forward(body):
    """Split a body into ``(header fields, original text)``.

    The fields are ``None`` and the text is the whole body when it is not a
    forwarded copy.
    """
    text = "" if body is None else str(body)
    head, separator, rest = text.partition("\n")
    fields = parse_forward_header(head)
    if fields is None:
        return None, text
    return fields, rest if separator else ""


def is_forward(body) -> bool:
    """True when a body opens with a control line."""
    return split_forward(body)[0] is not None


def forward_hops(body):
    """How many times a body has already been copied.

    ``0`` for an original. ``None`` when the body carries a control line whose
    hop count cannot be read, which a caller should treat as "do not copy
    this": an unreadable counter is exactly what an unbounded relay would need
    in order to keep going.
    """
    fields = split_forward(body)[0]
    if fields is None:
        return 0
    try:
        hops = int(fields.get("hops"))
    except (TypeError, ValueError):
        return None
    return hops if hops >= 0 else None
