"""Unit tests for the protocol helpers."""

import pytest

from sockpost import protocol as P


@pytest.mark.parametrize("text,seconds", [
    ("30s", 30),
    ("45", 45),
    ("10min", 600),
    ("10m", 600),
    ("2h", 7200),
    ("1d", 86400),
    ("1 hour", 3600),
])
def test_parse_interval_accepts_common_forms(text, seconds):
    assert P.parse_interval(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "-5s", "0s", "5 weeks", None])
def test_parse_interval_rejects_garbage(text):
    with pytest.raises(P.ProtocolError):
        P.parse_interval(text)


@pytest.mark.parametrize("value", ["a", "worker-1", "build.gate", "A_b.9-x", "ci2"])
def test_validate_channel_accepts_labels(value):
    assert P.validate_channel(value) == value


@pytest.mark.parametrize("value", ["", "-leading", "with space", "sla/sh",
                                   "quote\"", "x" * 65, None])
def test_validate_channel_rejects_unsafe_ids(value):
    with pytest.raises(P.ProtocolError):
        P.validate_channel(value)


def test_timestamps_round_trip():
    stamp = P.now_iso()
    assert P.parse_iso(stamp) is not None
    assert P.parse_iso("not a timestamp") is None
    assert P.age_seconds(stamp) < 5


def test_timestamps_sort_lexicographically():
    assert P.iso_at(1000) < P.iso_at(2000)


@pytest.mark.parametrize("seconds,text", [
    (0, "0s"), (45, "45s"), (60, "1m"), (3599, "59m"),
    (3600, "1h"), (3660, "1h01m"), (86400, "1d"), (90000, "1d01h"),
])
def test_format_duration(seconds, text):
    assert P.format_duration(seconds) == text


def test_quote_keeps_output_on_one_line():
    quoted = P.quote('two\nlines and a "quote"')
    assert "\n" not in quoted
    assert quoted.startswith('"') and quoted.endswith('"')


def test_encode_is_newline_delimited_json():
    raw = P.encode({"op": "send", "text": "hi"})
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
