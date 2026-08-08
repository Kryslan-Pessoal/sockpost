"""End to end tests: real daemon, real sockets, real command line."""

from __future__ import annotations


def test_message_reaches_a_listening_channel(bus):
    bus.start_daemon()
    worker_out = bus.listen("worker")

    result = bus.run("send", "--from", "planner", "--to", "worker",
                     "--text", "build the thing")
    assert result.stdout.strip() == "queued id=1"

    text = bus.wait_for_line(worker_out, "message id=1")
    assert 'from=planner' in text
    assert 'text="build the thing"' in text

    # The listener acknowledges on delivery, so the queue drains by itself.
    bus.wait_for_condition(lambda: bus.run("unread").stdout.strip() == "unread n=0")


def test_body_with_newlines_stays_on_one_output_line(bus):
    bus.start_daemon()
    worker_out = bus.listen("worker")
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", 'two\nlines and a "quote"')
    text = bus.wait_for_line(worker_out, "message id=1")
    message_lines = [ln for ln in text.splitlines() if ln.startswith("message ")]
    assert len(message_lines) == 1
    assert message_lines[0].endswith(r'text="two\nlines and a \"quote\""')


def test_manual_ack_keeps_the_message_queued_until_confirmed(bus):
    bus.start_daemon()
    worker_out = bus.listen("worker", "--manual-ack")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "confirm me")
    bus.wait_for_line(worker_out, "message id=1")

    unread = bus.run("unread").stdout
    assert "unread n=1" in unread
    assert "to=worker" in unread

    assert bus.run("ack", "1", "--from", "worker").stdout.strip() == "ack id=1"
    assert bus.run("unread").stdout.strip() == "unread n=0"

    # Acknowledging again is idempotent and still exits zero.
    again = bus.run("ack", "1", "--from", "worker")
    assert "already acknowledged" in again.stdout


def test_a_third_party_cannot_acknowledge_someone_elses_message(bus):
    bus.start_daemon()
    bus.listen("worker", "--manual-ack")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "private")
    result = bus.run("ack", "1", "--from", "intruder", check=False)
    assert result.returncode == 1
    assert "not the recipient" in result.stdout
    assert "unread n=1" in bus.run("unread").stdout


def test_queue_survives_a_daemon_restart(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "wait for me")
    assert "unread n=1" in bus.run("unread").stdout

    bus.stop_daemon()
    assert "unread n=1" in bus.run("unread").stdout  # readable while down

    bus.start_daemon()
    worker_out = bus.listen("worker")
    text = bus.wait_for_line(worker_out, "message id=1")
    assert 'text="wait for me"' in text


def test_message_is_redelivered_after_the_consumer_is_killed(bus):
    """A consumer that dies mid-work loses nothing: it is redelivered on reconnect.

    The other half of the at-least-once guarantee, redelivery while the
    consumer stays connected, is covered in test_delivery_guarantees.py.
    """
    bus.start_daemon()
    first = bus.listen("worker", "--manual-ack")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "survive a crash")
    bus.wait_for_line(first, "message id=1")

    bus.kill_listener()  # SIGKILL: no chance to acknowledge or clean up
    assert "unread n=1" in bus.run("unread").stdout

    second = bus.listen("worker", "--manual-ack")
    text = bus.wait_for_line(second, "message id=1")
    assert 'text="survive a crash"' in text


def test_a_second_listener_evicts_the_first(bus):
    bus.start_daemon()
    first = bus.listen("worker")
    bus.listen("worker")
    text = bus.wait_for_line(first, "evicted")
    assert "reason=last_connected_wins" in text


def test_status_and_ping_report_liveness(bus):
    bus.start_daemon()
    bus.listen("worker")

    status = bus.run("status").stdout
    assert "daemon=up" in status
    assert "id=worker" in status
    assert "pong id=worker" in status
    assert "summary alive=1 dead=0" in status

    ping = bus.run("ping", "--to", "worker,ghost", "--timeout", "2", check=False)
    assert ping.returncode == 1  # ghost never answers
    assert "pong id=worker" in ping.stdout
    assert "silence id=ghost" in ping.stdout


def test_wakeup_is_delivered_and_can_be_turned_off(bus):
    bus.start_daemon()
    worker_out = bus.listen("worker")
    assert "interval=1s" in bus.run("wakeup", "worker", "1s").stdout
    bus.wait_for_line(worker_out, "wakeup ts=")
    assert "off" in bus.run("wakeup", "worker", "off").stdout


def test_a_channel_id_cannot_forge_a_line_in_the_daemon_log(bus):
    """Ids reach the log, so they are validated on every path, not only send."""
    import json
    import socket as socket_module

    bus.start_daemon()
    hostile = "worker\nevent=acked msg_id=1 channel=worker"
    for payload in ({"op": "connect", "id": hostile},
                    {"op": "ack", "msg_id": 1, "from": hostile},
                    {"op": "wakeup_set", "channel": hostile, "seconds": 60}):
        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(bus.socket_path))
        sock.sendall((json.dumps(payload) + "\n").encode())
        reply = json.loads(sock.makefile("r").readline())
        assert reply.get("op") in ("error", "ack_result")
        assert "invalid channel id" in json.dumps(reply)
        sock.close()

    log = (bus.tmp_path / "daemon.log").read_text()
    assert "event=acked" not in log
    for line in log.splitlines():
        assert line.startswith("ts=")


def test_an_invalid_channel_id_is_rejected_before_anything_is_queued(bus):
    bus.start_daemon()
    result = bus.run("send", "--from", "planner", "--to", "bad id",
                     "--text", "nope", check=False)
    assert result.returncode == 1
    assert "invalid channel id" in result.stderr
    assert bus.run("unread").stdout.strip() == "unread n=0"


def test_a_losing_second_daemon_does_not_disturb_the_running_one(bus):
    """A second daemon must refuse to start without damaging the first.

    The pid file is the regression: opening it with "w" truncated the pid of
    the daemon that was already running, after which nothing could stop it.
    """
    bus.start_daemon()
    second = bus.run("daemon", check=False)
    assert second.returncode == 1
    assert "already holds" in second.stderr

    assert "daemon=up" in bus.run("status").stdout
    assert "daemon=stopped" in bus.run("stop").stdout
    bus.stop_daemon()  # reap the process the fixture is still tracking


def test_a_body_larger_than_the_stream_buffer_is_still_delivered(bus):
    """Regression: the reader's default 64 KiB line limit truncated the product.

    Bodies above that ceiling used to kill the connection task with a
    ValueError, well below the documented limit, and the sender only saw
    "daemon is not answering".
    """
    bus.start_daemon()
    worker_out = bus.listen("worker")
    body = "y" * 200_000
    assert bus.run("send", "--from", "planner", "--to", "worker",
                   "--text", body).stdout.strip() == "queued id=1"
    text = bus.wait_for_line(worker_out, "message id=1")
    assert '"%s"' % body in text


def test_oversized_body_is_refused_before_it_reaches_the_database(bus):
    bus.env["SOCKPOST_MAX_BODY_BYTES"] = "64"
    bus.start_daemon()
    result = bus.run("send", "--from", "planner", "--to", "worker",
                     "--text", "x" * 100, check=False)
    assert result.returncode == 1
    assert "limit is 64" in result.stderr
    assert bus.run("unread").stdout.strip() == "unread n=0"


def test_socket_is_reachable_only_by_its_owner(bus):
    import stat

    bus.start_daemon()
    mode = bus.socket_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_json_output_mode_emits_raw_events(bus):
    import json

    bus.start_daemon()
    worker_out = bus.listen("worker", "--json")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "structured")
    text = bus.wait_for_line(worker_out, '"op": "message"')
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [event for event in events if event["op"] == "message"][0]
    assert message["from"] == "planner"
    assert message["text"] == "structured"
