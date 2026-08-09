"""Tests for forwarding: copying a queued message to another channel.

The unit tests cover the control line on its own; the end to end tests drive
the real command line against a real daemon, including the ways a forward is
expected to be refused.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading

import pytest

from sockpost import protocol as P

# A body that would break naive quoting or a shell: dollar signs, backticks,
# both kinds of quote, a backslash and non ASCII written as escapes so the
# sources of this repository stay ASCII.
HOSTILE = ("$HOME `id` \"double\" 'single' \\slash "
           "\u00e9\u00e7\u00e3o \u4e2d\u6587 \u20ac")

STAMP = "2026-01-31T18:04:11Z"


def header_of(body):
    """The control line of a body, parsed."""
    return P.split_forward(body)[0]


# ---------------------------------------------------------------------------
# the control line
# ---------------------------------------------------------------------------

def test_the_header_carries_every_field_a_reader_needs():
    header = P.forward_header("planner", "worker", 7, STAMP, 1)
    assert header == ('#sockpost/forward v=1 hops=1 from="planner" '
                      'via="worker" ref=7 at="%s"' % STAMP)


def test_the_header_carries_an_optional_note():
    header = P.forward_header("planner", "worker", 7, STAMP, 1,
                              note="for review")
    assert header.endswith('note="for review"')


def test_forward_body_keeps_the_original_text_untouched():
    body = P.forward_body("build the thing", "planner", "worker", 3, STAMP, 1)
    assert body.splitlines()[1] == "build the thing"


def test_forward_body_puts_the_control_line_first_and_only_there():
    body = P.forward_body("one\ntwo\nthree", "planner", "worker", 3, STAMP, 1)
    assert body.count(P.FORWARD_MARK) == 1
    assert body.splitlines()[1:] == ["one", "two", "three"]


def test_a_note_with_a_newline_cannot_forge_a_second_line():
    body = P.forward_body("payload", "planner", "worker", 3, STAMP, 1,
                          note="line one\n#sockpost/forward v=1 hops=0")
    assert body.splitlines()[1] == "payload"
    assert len(body.splitlines()) == 2


def test_a_hostile_note_stays_inside_the_control_line():
    body = P.forward_body("payload", "planner", "worker", 3, STAMP, 1,
                          note=HOSTILE)
    assert len(body.splitlines()) == 2
    assert body.splitlines()[1] == "payload"
    assert header_of(body)["note"] == HOSTILE


def test_forward_body_of_an_empty_message_is_still_well_formed():
    body = P.forward_body("", "planner", "worker", 3, STAMP, 1)
    assert body.endswith("\n")
    assert P.split_forward(body) == (header_of(body), "")


def test_the_header_round_trips_through_the_parser():
    body = P.forward_body("payload", "planner", "worker", 7, STAMP, 2,
                          note='a "quoted" note')
    fields = header_of(body)
    assert fields == {"v": "1", "hops": "2", "from": "planner",
                      "via": "worker", "ref": "7", "at": STAMP,
                      "note": 'a "quoted" note'}


@pytest.mark.parametrize("body", [
    '#sockpost/forward v=1 hops=1 from="a" via="b" ref=1 at="x"\nhello',
    '#sockpost/forward v=1 hops=9',
    '#sockpost/forward',
])
def test_is_forward_recognises_a_control_line(body):
    assert P.is_forward(body) is True


@pytest.mark.parametrize("body", [
    "", None, "plain text", " #sockpost/forward v=1 hops=1",
    "see #sockpost/forward", "#sockpost/forwarding my thoughts",
    "#SOCKPOST/FORWARD v=1", "sockpost/forward v=1",
])
def test_is_forward_ignores_anything_else(body):
    assert P.is_forward(body) is False


def test_a_forwarded_body_is_detected_as_a_forward():
    assert P.is_forward(
        P.forward_body("hi", "planner", "worker", 1, STAMP, 1)) is True


def test_the_source_id_is_rendered_as_a_number():
    assert " ref=12 " in P.forward_header("a", "b", "12", STAMP, 1) + " "


@pytest.mark.parametrize("body,hops", [
    ("plain text", 0),
    ("", 0),
    (None, 0),
    ('#sockpost/forward v=1 hops=1 from="a" via="b" ref=1 at="x"', 1),
    ('#sockpost/forward v=1 hops=7', 7),
])
def test_forward_hops_reads_the_counter(body, hops):
    assert P.forward_hops(body) == hops


@pytest.mark.parametrize("body", [
    "#sockpost/forward v=1",
    "#sockpost/forward v=1 hops=many",
    "#sockpost/forward v=1 hops=-2",
])
def test_forward_hops_refuses_to_guess_an_unreadable_counter(body):
    assert P.forward_hops(body) is None


def test_a_value_with_a_broken_escape_is_not_a_control_line():
    assert P.parse_forward_header(r'#sockpost/forward from="bad\xescape"') is None


def test_a_garbled_field_does_not_hide_the_counter():
    """A half written header is still marked, and the hop count still rules."""
    body = '#sockpost/forward hops=1 from="unterminated\nbody'
    assert P.is_forward(body) is True
    assert P.forward_hops(body) == 1
    assert "from" not in header_of(body)


def test_split_forward_returns_a_plain_body_untouched():
    assert P.split_forward("just text\nwith lines") == (None,
                                                        "just text\nwith lines")


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_a_forwarded_message_reaches_the_destination_channel(bus):
    bus.start_daemon()
    bus.listen("worker")
    auditor_out = bus.listen("auditor")

    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor")
    assert result.stdout.strip() == "forwarded id=2 src=1 to=auditor hops=1"

    text = bus.wait_for_line(auditor_out, "message id=2")
    assert '#sockpost/forward v=1 hops=1' in text
    assert 'from=\\"planner\\" via=\\"worker\\" ref=1' in text
    assert "the news" in text


def test_the_copy_is_sent_by_the_forwarder_not_by_the_original_sender(bus):
    bus.start_daemon()
    bus.listen("worker")
    auditor_out = bus.listen("auditor", "--json")

    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    text = bus.wait_for_line(auditor_out, '"msg_id": 2')
    event = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [e for e in event if e.get("msg_id") == 2][0]
    assert message["from"] == "worker"
    fields, original = P.split_forward(message["text"])
    assert fields["from"] == "planner"
    assert fields["via"] == "worker"
    assert fields["ref"] == "1"
    assert fields["hops"] == "1"
    assert original == "the news"


def test_the_original_message_is_left_exactly_as_it_was(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "auditor", "--to", "reviewer")

    unread = bus.run("unread").stdout
    original = [ln for ln in unread.splitlines() if ln.startswith("id=1 ")][0]
    assert "from=planner" in original
    assert "to=worker" in original
    assert original.endswith('text="the news"')  # no provenance line added


def test_a_note_travels_with_the_copy(bus):
    bus.start_daemon()
    auditor_out = bus.listen("auditor")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor",
            "--note", "please double check this")

    text = bus.wait_for_line(auditor_out, "message id=2")
    assert 'note=\\"please double check this\\"' in text


def test_a_message_can_be_forwarded_to_several_channels(bus):
    bus.start_daemon()
    first = bus.listen("auditor")
    second = bus.listen("reviewer")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")

    assert "forwarded id=2" in bus.run("forward", "1", "--from", "worker",
                                       "--to", "auditor").stdout
    assert "forwarded id=3" in bus.run("forward", "1", "--from", "worker",
                                       "--to", "reviewer").stdout
    assert "the news" in bus.wait_for_line(first, "message id=2")
    assert "the news" in bus.wait_for_line(second, "message id=3")


def test_an_acknowledged_message_can_still_be_forwarded(bus):
    """Rows are never deleted, so the content outlives the delivery loop."""
    bus.start_daemon()
    worker_out = bus.listen("worker")
    auditor_out = bus.listen("auditor")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "old news")
    bus.wait_for_line(worker_out, "message id=1")
    bus.wait_for_condition(
        lambda: "unread n=0" in bus.run("unread").stdout)

    bus.run("forward", "1", "--from", "worker", "--to", "auditor")
    assert "old news" in bus.wait_for_line(auditor_out, "message id=2")


def test_a_forward_to_an_offline_channel_waits_in_the_queue(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "keep me")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    unread = bus.run("unread", "--id", "auditor").stdout
    assert "unread n=1" in unread
    assert "#sockpost/forward v=1 hops=1" in unread

    auditor_out = bus.listen("auditor")
    assert "keep me" in bus.wait_for_line(auditor_out, "message id=2")


def test_the_copy_follows_the_normal_acknowledgement_rules(bus):
    bus.start_daemon()
    bus.listen("auditor", "--manual-ack")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "confirm me")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    bus.wait_for_condition(lambda: "id=2" in bus.run("unread").stdout)
    # The forwarder is not the recipient of the copy, so it cannot clear it.
    assert "not the recipient" in bus.run("ack", "2", "--from", "worker",
                                          check=False).stdout
    assert bus.run("ack", "2", "--from", "auditor").stdout.strip() == "ack id=2"
    assert bus.run("unread", "--id", "auditor").stdout.strip() == "unread n=0"


def test_the_forwarder_defaults_to_the_environment_channel(bus):
    bus.start_daemon()
    auditor_out = bus.listen("auditor")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")

    bus.env["SOCKPOST_ID"] = "worker"
    assert "forwarded id=2" in bus.run("forward", "1", "--to", "auditor").stdout
    assert 'via=\\"worker\\"' in bus.wait_for_line(auditor_out, "message id=2")


# ---------------------------------------------------------------------------
# hostile content
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "$HOME and $(id)",
    "back `tick` and ${brace}",
    'a "double" and an \'single\' quote',
    "a \\backslash\\ and a tab\tinside",
    "\u00e9\u00e7\u00e3o \u4e2d\u6587 \u20ac",
    HOSTILE,
])
def test_a_hostile_body_survives_the_round_trip(bus, body):
    bus.start_daemon()
    auditor_out = bus.listen("auditor", "--json")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", body)
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    text = bus.wait_for_line(auditor_out, '"msg_id": 2')
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [e for e in events if e.get("msg_id") == 2][0]
    fields, original = P.split_forward(message["text"])
    assert original == body
    assert fields["from"] == "planner"


def test_a_multiline_body_stays_on_one_output_line(bus):
    bus.start_daemon()
    auditor_out = bus.listen("auditor")
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", 'two\nlines and a "quote"')
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    text = bus.wait_for_line(auditor_out, "message id=2")
    message_lines = [ln for ln in text.splitlines() if ln.startswith("message ")]
    assert len(message_lines) == 1
    assert message_lines[0].endswith(r'\nlines and a \"quote\""')


def test_a_body_that_fakes_a_control_line_without_a_counter_is_refused(bus):
    """The marker is a convention: claiming it badly costs the sender the copy."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", '#sockpost/forward from="someone" ref=99\nfake')
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "hop count cannot be read" in result.stderr


def test_a_body_that_claims_a_high_hop_count_is_taken_at_its_word(bus):
    """Claiming to be far along the chain only buys the sender a refusal."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", "#sockpost/forward v=1 hops=99\nfake")
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "already been forwarded 99 time(s)" in result.stderr


def test_a_forged_origin_that_is_not_a_channel_id_is_dropped(bus):
    """The origin comes out of a body, so it is validated before it is trusted."""
    bus.start_daemon()
    auditor_out = bus.listen("auditor", "--json")
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", '#sockpost/forward v=1 hops=1 from="not a channel"\ntext')
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    text = bus.wait_for_line(auditor_out, '"msg_id": 2')
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [e for e in events if e.get("msg_id") == 2][0]
    assert P.split_forward(message["text"])[0]["from"] == "planner"


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def test_forwarding_an_unknown_id_is_refused(bus):
    bus.start_daemon()
    result = bus.run("forward", "404", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "no such message 404" in result.stderr
    assert bus.run("unread").stdout.strip() == "unread n=0"


def test_forwarding_to_a_channel_that_is_still_waiting_for_it_is_refused(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--from", "worker", "--to", "worker",
                     check=False)
    assert result.returncode == 1
    assert "still queued for worker" in result.stderr
    assert "unread n=1" in bus.run("unread").stdout


def test_a_channel_can_get_a_second_copy_of_what_it_already_consumed(bus):
    """Once the original is acknowledged, a copy is how you ask again."""
    bus.start_daemon()
    worker_out = bus.listen("worker")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "say again")
    bus.wait_for_line(worker_out, "message id=1")
    bus.wait_for_condition(lambda: "unread n=0" in bus.run("unread").stdout)

    assert "forwarded id=2" in bus.run("forward", "1", "--from", "auditor",
                                       "--to", "worker").stdout
    assert "say again" in bus.wait_for_line(worker_out, "message id=2")


def test_an_expired_message_can_be_recovered_with_a_copy(bus):
    """Expiry drops a message from delivery; the text is still on disk."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "ghost", "--text", "lost cargo")
    bus.stop_daemon()
    conn = sqlite3.connect(str(bus.tmp_path / "queue.db"))
    conn.execute("UPDATE messages SET state=2, resolved_at='2026-01-01T00:00:00Z'")
    conn.commit()
    conn.close()
    bus.start_daemon()

    assert bus.run("unread").stdout.strip() == "unread n=0"
    auditor_out = bus.listen("auditor")
    assert "forwarded id=2" in bus.run("forward", "1", "--from", "operator",
                                       "--to", "auditor").stdout
    assert "lost cargo" in bus.wait_for_line(auditor_out, "message id=2")


def test_a_relay_runs_to_the_ceiling_and_stops_there(bus):
    """A supervision chain is allowed; an endless one is not."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    assert "hops=1" in bus.run("forward", "1", "--from", "worker",
                               "--to", "auditor").stdout
    assert "hops=2" in bus.run("forward", "2", "--from", "auditor",
                               "--to", "archive").stdout
    assert "hops=3" in bus.run("forward", "3", "--from", "archive",
                               "--to", "vault").stdout

    stopped = bus.run("forward", "4", "--from", "vault", "--to", "sink",
                      check=False)
    assert stopped.returncode == 1
    assert "already been forwarded 3 time(s)" in stopped.stderr
    assert "unread n=4" in bus.run("unread").stdout  # nothing new was queued


def test_the_original_author_survives_every_hop(bus):
    bus.start_daemon()
    archive_out = bus.listen("archive", "--json")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")
    bus.run("forward", "2", "--from", "auditor", "--to", "archive")

    text = bus.wait_for_line(archive_out, '"msg_id": 3')
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [e for e in events if e.get("msg_id") == 3][0]
    fields, original = P.split_forward(message["text"])
    assert fields["from"] == "planner"   # the author, not the previous hop
    assert fields["via"] == "auditor"    # who made this copy
    assert fields["ref"] == "2"          # one step back, not the root
    assert fields["hops"] == "2"
    assert original == "the news"


def test_a_note_belongs_to_the_hop_that_added_it(bus):
    bus.start_daemon()
    archive_out = bus.listen("archive", "--json")
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor",
            "--note", "first hop note")
    bus.run("forward", "2", "--from", "auditor", "--to", "archive")

    text = bus.wait_for_line(archive_out, '"msg_id": 3')
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    message = [e for e in events if e.get("msg_id") == 3][0]
    fields, original = P.split_forward(message["text"])
    assert "note" not in fields          # it stayed on message 2, under ref
    assert original == "the news"


def test_a_cycle_between_two_channels_burns_out(bus):
    """Two agents bouncing the same text back and forth run out of hops."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    source, pair = "1", ["worker", "auditor"]
    for step in range(6):
        sender, target = pair[step % 2], pair[(step + 1) % 2]
        result = bus.run("forward", source, "--from", sender, "--to", target,
                         check=False)
        if result.returncode != 0:
            break
        source = result.stdout.split("id=")[1].split()[0]
    else:  # pragma: no cover - only reached if the ceiling never bites
        raise AssertionError("the cycle never stopped")
    assert "already been forwarded 3 time(s)" in result.stderr


def test_the_hop_ceiling_is_configurable(bus):
    bus.env["SOCKPOST_MAX_FORWARD_HOPS"] = "1"
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    assert "hops=1" in bus.run("forward", "1", "--from", "worker",
                               "--to", "auditor").stdout
    stopped = bus.run("forward", "2", "--from", "auditor", "--to", "archive",
                      check=False)
    assert stopped.returncode == 1
    assert "the limit is 1" in stopped.stderr


def test_forwarding_with_a_dead_daemon_says_so(bus):
    """No daemon, no autostart: the error names the socket, not the message."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.stop_daemon()

    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "daemon is not answering" in result.stderr


def test_a_non_numeric_id_is_refused_before_the_daemon_is_contacted(bus):
    bus.start_daemon()
    result = bus.run("forward", "abc", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "must be a number" in result.stderr


@pytest.mark.parametrize("bad_id", ["0", "-1", "9" * 25, str(2 ** 63)])
def test_an_id_no_row_can_hold_is_answered_as_missing(bus, bad_id):
    """Out of range used to reach SQLite and come back as an OverflowError."""
    bus.start_daemon()
    result = bus.run("forward", bad_id, "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "no such message" in result.stderr
    assert "OverflowError" not in result.stderr


@pytest.mark.parametrize("target", ["bad id", "sla/sh", "", "x" * 65,
                                    'quote"', "new\nline"])
def test_an_invalid_destination_is_refused(bus, target):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--from", "worker", "--to", target,
                     check=False)
    assert result.returncode == 1
    assert "invalid channel id" in result.stderr
    assert "unread n=1" in bus.run("unread").stdout


def test_forward_without_a_destination_is_a_usage_error(bus):
    bus.start_daemon()
    result = bus.run("forward", "1", "--from", "worker", check=False)
    assert result.returncode == 2
    assert "--to" in result.stderr


def test_forward_without_a_forwarder_asks_for_one(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--to", "auditor", check=False)
    assert result.returncode == 1
    assert "SOCKPOST_ID" in result.stderr


def test_a_copy_that_would_exceed_the_body_limit_is_refused(bus):
    """The provenance line makes the copy bigger than the original."""
    bus.env["SOCKPOST_MAX_BODY_BYTES"] = "80"
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "x" * 70)
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "the limit is 80" in result.stderr
    assert "unread n=1" in bus.run("unread").stdout


def test_a_refused_forward_leaves_the_connection_usable(bus):
    bus.start_daemon()
    result = bus.run("forward", "9", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "daemon=up" in bus.run("status").stdout


# ---------------------------------------------------------------------------
# the log
# ---------------------------------------------------------------------------

def test_the_daemon_logs_the_forward_without_the_body(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", "a secret worth keeping")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    log = (bus.tmp_path / "daemon.log").read_text()
    assert "event=forwarded" in log
    assert "src_id=1" in log
    assert "a secret worth keeping" not in log


def test_a_hostile_note_cannot_forge_a_line_in_the_daemon_log(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor",
            "--note", "x\nevent=acked msg_id=1 channel=worker")

    log = (bus.tmp_path / "daemon.log").read_text()
    assert "event=acked" not in log
    for line in log.splitlines():
        assert line.startswith("ts=")


# ---------------------------------------------------------------------------
# the awkward neighbours: an old daemon, a wedged peer, an oversized request
# ---------------------------------------------------------------------------

class _StubDaemon:
    """A socket that answers every request the way a 0.1.0 daemon would.

    Nothing here pretends to be a queue. The point is the one reply a client
    of this release will meet on a machine where the daemon was started before
    the upgrade: an operation it has never heard of.
    """

    def __init__(self, path):
        self.path = str(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            try:
                handle = conn.makefile("rwb")
                line = handle.readline()
                if line:
                    request = json.loads(line.decode())
                    handle.write((json.dumps(
                        {"op": "error",
                         "error": "unknown op %r" % request.get("op")})
                        + "\n").encode())
                    handle.flush()
            except (OSError, ValueError):
                pass
            finally:
                conn.close()

    def close(self):
        self.server.close()


def test_a_daemon_older_than_the_client_says_what_to_do(bus):
    """The upgrade trap: new command, old process, and a wire error nobody typed."""
    stub = _StubDaemon(bus.socket_path)
    try:
        result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                         check=False)
    finally:
        stub.close()
    assert result.returncode == 1
    assert "older than this client" in result.stderr
    assert "sockpost stop" in result.stderr
    assert "unknown op" not in result.stderr


def test_a_recipient_that_stopped_reading_cannot_block_a_forward(bus):
    """A wedged consumer must not hold up the sender of a copy."""
    bus.env["SOCKPOST_WRITE_TIMEOUT"] = "1"
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", "z" * 900_000)
    wedged = bus.wedged_listener("auditor")
    try:
        result = bus.run("forward", "1", "--from", "worker", "--to", "auditor")
        assert "forwarded id=2" in result.stdout
        # The daemon is still serving everybody else. status exits non zero
        # because the wedged channel does not answer its liveness probe, which
        # is the point: it is reported, not waited on.
        status = bus.run("status", check=False)
        assert "daemon=up" in status.stdout
        assert "silence id=auditor" in status.stdout
        assert "id=2" in bus.run("unread", "--id", "auditor").stdout
    finally:
        wedged.close()


def test_a_note_too_large_for_the_read_buffer_is_reported_not_swallowed(bus):
    bus.env["SOCKPOST_MAX_BODY_BYTES"] = "64"
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "small")
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     "--note", "n" * 200_000, check=False)
    assert result.returncode == 1
    assert "SOCKPOST_MAX_BODY_BYTES" in result.stderr
    assert bus.run("unread", "--id", "auditor").stdout.strip() == "unread n=0"
    # The daemon survived the oversized request.
    assert "daemon=up" in bus.run("status").stdout
