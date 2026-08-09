"""Tests for forwarding: copying a queued message to another channel.

The unit tests cover the provenance line on its own; the end to end tests
drive the real command line against a real daemon, including the ways a
forward is expected to be refused.
"""

from __future__ import annotations

import json

import pytest

from sockpost import protocol as P

# A body that would break naive quoting or a shell: dollar signs, backticks,
# both kinds of quote, a backslash and non ASCII written as escapes so the
# sources of this repository stay ASCII.
HOSTILE = ("$HOME `id` \"double\" 'single' \\slash "
           "\u00e9\u00e7\u00e3o \u4e2d\u6587 \u20ac")


# ---------------------------------------------------------------------------
# the provenance line
# ---------------------------------------------------------------------------

def test_forward_header_names_both_channels_and_the_source():
    header = P.forward_header("planner", "worker", 7)
    assert header == "forwarded-from=planner via=worker ref=7"


def test_forward_header_carries_an_optional_note():
    header = P.forward_header("planner", "worker", 7, note="for review")
    assert header.endswith('note="for review"')


def test_forward_body_keeps_the_original_text_untouched():
    body = P.forward_body("planner", "worker", 3, "build the thing")
    assert body.splitlines()[1] == "build the thing"


def test_forward_body_puts_the_provenance_on_the_first_line_only():
    body = P.forward_body("planner", "worker", 3, "one\ntwo\nthree")
    assert body.count(P.FORWARD_MARK) == 1
    assert body.splitlines()[1:] == ["one", "two", "three"]


def test_a_note_with_a_newline_cannot_forge_a_second_line():
    body = P.forward_body("planner", "worker", 3, "payload",
                          note="line one\nforwarded-from=nobody")
    assert body.splitlines()[1] == "payload"
    assert len(body.splitlines()) == 2


def test_a_hostile_note_stays_inside_the_header_line():
    body = P.forward_body("planner", "worker", 3, "payload", note=HOSTILE)
    assert len(body.splitlines()) == 2
    assert body.splitlines()[1] == "payload"


def test_forward_body_of_an_empty_message_is_still_well_formed():
    body = P.forward_body("planner", "worker", 3, "")
    assert body == "forwarded-from=planner via=worker ref=3\n"


@pytest.mark.parametrize("body", [
    "forwarded-from=planner via=worker ref=1\nhello",
    "forwarded-from=x via=y ref=99",
])
def test_is_forward_recognises_a_forwarded_body(body):
    assert P.is_forward(body) is True


@pytest.mark.parametrize("body", [
    "", None, "plain text", " forwarded-from=planner",
    "see forwarded-from=planner", "FORWARDED-FROM=planner",
])
def test_is_forward_ignores_anything_else(body):
    assert P.is_forward(body) is False


def test_a_forwarded_body_is_detected_as_a_forward():
    assert P.is_forward(P.forward_body("planner", "worker", 1, "hi")) is True


def test_the_source_id_is_rendered_as_a_number():
    assert P.forward_header("a", "b", "12") == "forwarded-from=a via=b ref=12"


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_a_forwarded_message_reaches_the_destination_channel(bus):
    bus.start_daemon()
    bus.listen("worker")
    auditor_out = bus.listen("auditor")

    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor")
    assert result.stdout.strip() == "forwarded id=2 src=1 to=auditor"

    text = bus.wait_for_line(auditor_out, "message id=2")
    assert "forwarded-from=planner via=worker ref=1" in text
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
    assert message["text"].startswith("forwarded-from=planner via=worker ref=1")
    assert message["text"].endswith("the news")


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
    assert "forwarded-from=planner" in unread

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
    assert "via=worker" in bus.wait_for_line(auditor_out, "message id=2")


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
    assert message["text"] == P.forward_body("planner", "worker", 1, body)


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


def test_a_body_that_fakes_a_provenance_line_is_not_forwardable(bus):
    """The marker is a convention: claiming it costs the sender the copy."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker",
            "--text", "forwarded-from=someone via=nobody ref=99\nfake")
    result = bus.run("forward", "1", "--from", "worker", "--to", "auditor",
                     check=False)
    assert result.returncode == 1
    assert "is itself a forward" in result.stderr


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


def test_forwarding_to_the_channel_that_already_has_it_is_refused(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    result = bus.run("forward", "1", "--from", "worker", "--to", "worker",
                     check=False)
    assert result.returncode == 1
    assert "already addressed to worker" in result.stderr
    assert "unread n=1" in bus.run("unread").stdout


def test_a_forward_cannot_be_forwarded_again(bus):
    """Three agents forwarding to each other must not grow one message forever."""
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")

    result = bus.run("forward", "2", "--from", "auditor", "--to", "reviewer",
                     check=False)
    assert result.returncode == 1
    assert "is itself a forward" in result.stderr
    assert "unread n=2" in bus.run("unread").stdout  # nothing new was queued


def test_a_forward_loop_between_two_channels_stops_at_the_first_copy(bus):
    bus.start_daemon()
    bus.run("send", "--from", "planner", "--to", "worker", "--text", "the news")
    bus.run("forward", "1", "--from", "worker", "--to", "auditor")
    for _ in range(3):
        back = bus.run("forward", "2", "--from", "auditor", "--to", "worker",
                       check=False)
        assert back.returncode == 1
    assert "unread n=2" in bus.run("unread").stdout


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
