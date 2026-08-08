"""Durable state: the SQLite queue, the channel registry and the wakeup table.

All functions here are synchronous and take an open connection. The daemon
calls them from a single dedicated worker thread, which keeps SQLite in its
favourite regime (one writer) without blocking the event loop.

Message states
--------------
``0`` pending  - queued, not acknowledged yet; eligible for delivery.
``1`` acked    - the recipient confirmed receipt; leaves the delivery loop.
``2`` expired  - dropped by a retention rule. Rows are never deleted so the
                 history stays auditable.

``resolved_at`` records when a message left the pending state, whichever way it
left: it is the acknowledgement time for state 1 and the expiry time for state
2. The state column says which.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from .protocol import now_iso

STATE_PENDING = 0
STATE_ACKED = 1
STATE_EXPIRED = 2

SCHEMA_VERSION = 1


def connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    """Open the queue database.

    Read-only callers (``status``, ``unread``) use SQLite URI mode so they can
    never create or corrupt the file when the daemon is not running.
    """
    if readonly:
        # The path becomes part of a URI, so it has to be escaped: a database
        # under a directory containing '?' or '#' would otherwise be silently
        # reinterpreted as query parameters or a fragment.
        uri = "file:%s?mode=ro" % quote(str(db_path))
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient           TEXT    NOT NULL,
            sender              TEXT    NOT NULL,
            body                TEXT    NOT NULL,
            created_at          TEXT    NOT NULL,
            state               INTEGER NOT NULL DEFAULT 0,
            resolved_at         TEXT,
            delivered_at        TEXT,
            failure_reported_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_recipient_state "
        "ON messages(recipient, state)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id         TEXT PRIMARY KEY,
            connected  INTEGER NOT NULL DEFAULT 0,
            since      TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wakeups (
            channel  TEXT PRIMARY KEY,
            interval TEXT NOT NULL,
            seconds  INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def insert_message(conn: sqlite3.Connection, recipient: str, sender: str,
                   body: str) -> int:
    cur = conn.execute(
        "INSERT INTO messages (recipient, sender, body, created_at, state) "
        "VALUES (?,?,?,?,0)", (recipient, sender, body, now_iso()))
    conn.commit()
    return int(cur.lastrowid)


def pending_batch(conn: sqlite3.Connection, recipient: str, limit: int):
    """Oldest pending messages for one recipient, in insertion order."""
    return conn.execute(
        "SELECT id, sender, body, created_at, delivered_at FROM messages "
        "WHERE recipient=? AND state=0 ORDER BY id LIMIT ?",
        (recipient, limit)).fetchall()


def pending_ids(conn: sqlite3.Connection, recipient: str):
    rows = conn.execute(
        "SELECT id FROM messages WHERE recipient=? AND state=0",
        (recipient,)).fetchall()
    return {int(row[0]) for row in rows}


def mark_delivered(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute("UPDATE messages SET delivered_at=? WHERE id=?",
                 (now_iso(), message_id))
    conn.commit()


def mark_failure_reported(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute("UPDATE messages SET failure_reported_at=? WHERE id=?",
                 (now_iso(), message_id))
    conn.commit()


def reset_pending(conn: sqlite3.Connection, recipient: str):
    """Clear delivery bookkeeping for a channel that just (re)connected.

    The previous owner of the channel is gone and never acknowledged, so the
    messages must be handed to the new owner immediately instead of waiting for
    the redelivery window. Returns the ids that were pending.
    """
    ids = pending_ids(conn, recipient)
    conn.execute(
        "UPDATE messages SET delivered_at=NULL, failure_reported_at=NULL "
        "WHERE recipient=? AND state=0", (recipient,))
    conn.commit()
    return ids


def ack_message(conn: sqlite3.Connection, message_id: int, recipient: str) -> int:
    """Acknowledge a message. Returns 1 when the state changed, 0 otherwise.

    The recipient is part of the WHERE clause on purpose: a channel can only
    acknowledge what was addressed to it, so a buggy or hostile client cannot
    clear somebody else's queue.
    """
    row = conn.execute(
        "SELECT 1 FROM messages WHERE id=? AND state=0 AND recipient=?",
        (message_id, recipient)).fetchone()
    if row is None:
        return 0
    conn.execute("UPDATE messages SET state=1, resolved_at=? WHERE id=?",
                 (now_iso(), message_id))
    conn.commit()
    return 1


def message_status(conn: sqlite3.Connection, message_id: int):
    return conn.execute(
        "SELECT state, recipient, failure_reported_at FROM messages WHERE id=?",
        (message_id,)).fetchone()


def list_unread(conn: sqlite3.Connection, recipient: str | None = None):
    if recipient is None:
        return conn.execute(
            "SELECT id, sender, recipient, body, created_at FROM messages "
            "WHERE state=0 ORDER BY id").fetchall()
    return conn.execute(
        "SELECT id, sender, recipient, body, created_at FROM messages "
        "WHERE state=0 AND recipient=? ORDER BY id", (recipient,)).fetchall()


def expire_orphans(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """Expire pending messages addressed to a channel that never connected.

    A channel that is merely offline keeps its queue forever: it is a real peer
    that may come back. A channel that has never been seen at all is a typo or
    a decommissioned agent, and its backlog would otherwise grow without bound.
    """
    cur = conn.execute(
        "UPDATE messages SET state=2, resolved_at=? "
        "WHERE state=0 AND created_at < ? "
        "AND recipient NOT IN (SELECT id FROM channels)",
        (now_iso(), cutoff_iso))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

def set_channel_state(conn: sqlite3.Connection, channel: str, connected: bool) -> None:
    stamp = now_iso()
    cur = conn.execute(
        "UPDATE channels SET connected=?, updated_at=? WHERE id=?",
        (1 if connected else 0, stamp, channel))
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO channels (id, connected, since, updated_at) VALUES (?,?,?,?)",
            (channel, 1 if connected else 0, stamp if connected else None, stamp))
    elif connected:
        conn.execute("UPDATE channels SET since=? WHERE id=?", (stamp, channel))
    conn.commit()


def reset_all_channels(conn: sqlite3.Connection) -> None:
    """A fresh daemon has no live connections yet."""
    conn.execute("UPDATE channels SET connected=0, updated_at=?", (now_iso(),))
    conn.commit()


def list_connected(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT c.id, c.since, w.interval, w.seconds "
        "FROM channels c LEFT JOIN wakeups w ON w.channel = c.id "
        "WHERE c.connected=1 ORDER BY c.id").fetchall()


# ---------------------------------------------------------------------------
# wakeups
# ---------------------------------------------------------------------------

def save_wakeup(conn: sqlite3.Connection, channel: str, interval: str,
                seconds: int) -> None:
    conn.execute(
        "INSERT INTO wakeups (channel, interval, seconds) VALUES (?,?,?) "
        "ON CONFLICT(channel) DO UPDATE SET interval=excluded.interval, "
        "seconds=excluded.seconds", (channel, interval, int(seconds)))
    conn.commit()


def delete_wakeup(conn: sqlite3.Connection, channel: str) -> int:
    cur = conn.execute("DELETE FROM wakeups WHERE channel=?", (channel,))
    conn.commit()
    return cur.rowcount


def list_wakeups(conn: sqlite3.Connection):
    return conn.execute("SELECT channel, interval, seconds FROM wakeups").fetchall()
