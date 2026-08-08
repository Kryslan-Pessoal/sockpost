"""Tests for the promises the README makes about delivery.

Each test here corresponds to a sentence in the documentation. They exist
because every one of these behaviours was either broken or unproven at some
point: a claim without a test is a claim waiting to become false.
"""

from __future__ import annotations

import json
import socket
import time


def test_an_unacknowledged_message_is_delivered_again_after_the_window(bus):
    """"delivered again ... after the redelivery window".

    A consumer that stays connected and never acknowledges used to keep the
    message parked in memory forever, so the documented window was dead code.
    """
    bus.env["SOCKPOST_REDELIVER_GAP"] = "2"
    bus.start_daemon()
    worker_out = bus.listen("worker", "--manual-ack")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "say it twice")

    def delivered_twice():
        return worker_out.read_text().count("message id=1") >= 2

    bus.wait_for_condition(delivered_twice, timeout=15)
    assert "unread n=1" in bus.run("unread").stdout


def test_a_burst_does_not_accuse_a_consumer_that_is_keeping_up(bus):
    """No delivery-timeout for messages that were still queued.

    The watchdog used to start when a message was queued rather than when it
    was delivered, so anything behind a burst was reported as unacknowledged
    before it had ever been offered to anyone.
    """
    bus.env["SOCKPOST_DELIVERY_GAP"] = "0.4"
    bus.env["SOCKPOST_ACK_TIMEOUT_ONLINE"] = "1"
    bus.start_daemon()
    planner_out = bus.listen("planner")
    worker_out = bus.listen("worker")

    for index in range(8):
        bus.run("send", "--from", "planner", "--to", "worker",
                "--text", "message %d" % index)

    bus.wait_for_line(worker_out, "message id=8", timeout=30)
    bus.wait_for_condition(
        lambda: bus.run("unread").stdout.strip() == "unread n=0", timeout=15)
    time.sleep(2)  # past the watchdog for the last message

    assert "delivery-timeout" not in planner_out.read_text()


def test_a_listener_that_stopped_reading_cannot_hold_its_channel(bus):
    """"the first one is told it was evicted ... the old process is still there".

    The daemon used to await a write to the peer being replaced with no
    timeout, so a consumer that stopped reading blocked the very takeover that
    was supposed to displace it.
    """
    bus.env["SOCKPOST_WRITE_TIMEOUT"] = "1"
    bus.start_daemon()
    wedged = bus.wedged_listener("worker")
    try:
        # One body large enough to overflow the socket buffer, so the daemon's
        # write to the wedged peer cannot complete.
        bus.run("send", "--from", "planner", "--to", "worker",
                "--text", "z" * 900_000)
        time.sleep(2)

        started = time.time()
        worker_out = bus.listen("worker")  # raises if it does not connect
        assert time.time() - started < 15
        # The channel works again: the message reaches the new owner.
        bus.wait_for_line(worker_out, "message id=1", timeout=20)
    finally:
        wedged.close()


def test_the_sender_is_told_when_a_recipient_never_appears(bus):
    bus.env["SOCKPOST_ACK_TIMEOUT_OFFLINE"] = "1"
    bus.start_daemon()
    planner_out = bus.listen("planner")
    bus.run("send", "--from", "planner", "--to", "ghost", "--text", "anyone there")
    text = bus.wait_for_line(planner_out, "unreachable", timeout=15)
    assert "target=ghost" in text


def test_the_queue_survives_a_daemon_that_is_killed_outright(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "outlive SIGKILL")
    bus.kill_daemon()

    assert "unread n=1" in bus.run("unread").stdout

    bus.start_daemon()
    worker_out = bus.listen("worker")
    assert 'text="outlive SIGKILL"' in bus.wait_for_line(worker_out, "message id=1")


def test_turning_a_wakeup_off_actually_stops_the_ticks(bus):
    bus.start_daemon()
    worker_out = bus.listen("worker")
    bus.run("wakeup", "worker", "1s")
    bus.wait_for_line(worker_out, "wakeup ts=")

    bus.run("wakeup", "worker", "off")
    time.sleep(0.5)
    before = worker_out.read_text().count("wakeup ts=")
    time.sleep(3)  # three intervals with nothing scheduled
    assert worker_out.read_text().count("wakeup ts=") == before


def test_a_malformed_request_costs_one_error_not_the_connection(bus):
    """Bad input must not end in a stack trace and a dropped connection."""
    bus.start_daemon()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(bus.socket_path))
    handle = sock.makefile("r")

    for payload in ('["not", "an", "object"]',
                    '{"op": "ping_probe", "to": ["a"], "timeout": "soon"}',
                    '{"op": "wakeup_set", "channel": "wk", "seconds": '
                    '99999999999999999999}',
                    '{"op": "nonsense"}'):
        sock.sendall((payload + "\n").encode())
        reply = json.loads(handle.readline())
        assert reply.get("op") in ("error", "ping_result"), reply

    # Still usable afterwards.
    sock.sendall(b'{"op": "ping"}\n')
    assert json.loads(handle.readline())["op"] == "pong"
    sock.close()

    log = (bus.tmp_path / "daemon.log").read_text()
    assert "Traceback" not in log


def test_an_absurd_interval_is_refused_by_the_command(bus):
    bus.start_daemon()
    result = bus.run("wakeup", "wk", "99999999999999999999s", check=False)
    assert result.returncode == 1
    assert "at most 365 days" in result.stderr


def test_the_daemon_starts_itself_on_first_use(bus):
    """The behaviour the README leads with, which the suite never exercised."""
    bus.env["SOCKPOST_AUTOSTART"] = "1"
    assert not bus.socket_path.exists()
    try:
        assert bus.run("send", "--from", "planner", "--to", "worker",
                       "--text", "wake up").stdout.strip() == "queued id=1"
        assert bus.wait_for_socket(timeout=5)
        assert "daemon=up" in bus.run("status").stdout
    finally:
        bus.run("stop", check=False)
