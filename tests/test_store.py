"""Unit tests for the durable queue."""

import pytest

from sockpost import store
from sockpost.protocol import iso_at


@pytest.fixture()
def db(tmp_path):
    conn = store.connect(tmp_path / "queue.db")
    store.init_schema(conn)
    yield conn
    conn.close()


def test_insert_and_list_unread(db):
    first = store.insert_message(db, "worker", "planner", "one")
    second = store.insert_message(db, "worker", "planner", "two")
    assert second > first
    rows = store.list_unread(db)
    assert [row[0] for row in rows] == [first, second]
    assert store.list_unread(db, "nobody") == []


def test_ack_removes_from_the_queue(db):
    message_id = store.insert_message(db, "worker", "planner", "hello")
    assert store.ack_message(db, message_id, "worker") == 1
    assert store.list_unread(db) == []
    # Acknowledging twice is a no-op, not an error.
    assert store.ack_message(db, message_id, "worker") == 0


def test_ack_from_a_third_party_is_refused(db):
    message_id = store.insert_message(db, "worker", "planner", "hello")
    assert store.ack_message(db, message_id, "intruder") == 0
    assert len(store.list_unread(db)) == 1
    assert store.message_status(db, message_id)[0] == store.STATE_PENDING


def test_reset_pending_clears_delivery_bookkeeping(db):
    message_id = store.insert_message(db, "worker", "planner", "hello")
    store.mark_delivered(db, message_id)
    store.mark_failure_reported(db, message_id)
    assert store.reset_pending(db, "worker") == {message_id}
    row = db.execute(
        "SELECT delivered_at, failure_reported_at FROM messages WHERE id=?",
        (message_id,)).fetchone()
    assert row == (None, None)


def test_expire_orphans_spares_channels_that_have_connected(db):
    known = store.insert_message(db, "worker", "planner", "for a real channel")
    orphan = store.insert_message(db, "typo", "planner", "for a channel that never was")
    db.execute("UPDATE messages SET created_at=?", (iso_at(0),))
    db.commit()
    store.set_channel_state(db, "worker", False)  # seen before, merely offline

    assert store.expire_orphans(db, iso_at(1)) == 1
    assert store.message_status(db, known)[0] == store.STATE_PENDING
    assert store.message_status(db, orphan)[0] == store.STATE_EXPIRED


def test_channel_registry_tracks_connections(db):
    store.set_channel_state(db, "worker", True)
    assert [row[0] for row in store.list_connected(db)] == ["worker"]
    store.set_channel_state(db, "worker", False)
    assert store.list_connected(db) == []
    store.set_channel_state(db, "worker", True)
    store.reset_all_channels(db)
    assert store.list_connected(db) == []


def test_wakeups_are_persisted(db):
    store.save_wakeup(db, "worker", "10min", 600)
    store.save_wakeup(db, "worker", "1h", 3600)  # upsert, not a duplicate
    assert store.list_wakeups(db) == [("worker", "1h", 3600)]
    assert store.delete_wakeup(db, "worker") == 1
    assert store.list_wakeups(db) == []


def test_readonly_connection_cannot_write(tmp_path):
    path = tmp_path / "queue.db"
    conn = store.connect(path)
    store.init_schema(conn)
    conn.close()
    reader = store.connect(path, readonly=True)
    with pytest.raises(Exception):
        store.insert_message(reader, "worker", "planner", "nope")
    reader.close()
