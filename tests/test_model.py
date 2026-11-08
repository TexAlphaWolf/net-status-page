import pytest

from upstatus.model import Result, check_host, clean, fmt_pct, fmt_span, fmt_time, parse_duration, utcnow


@pytest.mark.parametrize("value", ["example.org", "10.0.0.1", "::1", "2001:db8::1", "a-b.c1.example", "localhost"])
def test_good_hosts(value):
    assert check_host(value)


@pytest.mark.parametrize(
    "value",
    ["", "-oProxyCommand=x", "a b", "a/b", "fe80::1%eth0", "x" * 254, "a..b", "-a.example", "a_b.example", "http://x"],
)
def test_bad_hosts(value):
    with pytest.raises(ValueError):
        check_host(value)


def test_host_is_canonical():
    assert check_host("EXAMPLE.org") == "example.org"
    assert check_host("2001:0DB8::0001") == "2001:db8::1"


def test_host_must_be_text():
    with pytest.raises(ValueError):
        check_host(5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "seconds"), [("30s", 30), ("5m", 300), ("12h", 43200), ("7d", 604800), (90, 90), (1.0, 1)]
)
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "m", "5x", "-3s", 1.5, -1, True, "1 m"])
def test_bad_durations(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_clean_keeps_printable_ascii_and_cuts():
    assert clean("ok\x00\x1b[31mé") == "ok??[31m?"
    assert len(clean("x" * 500)) == 80


def test_result_needs_a_timezone_and_a_sane_time():
    from datetime import datetime

    with pytest.raises(ValueError):
        Result("a", datetime(2026, 1, 1), True)
    with pytest.raises(ValueError):
        Result("a", utcnow(), True, ms=-1.0)
    with pytest.raises(ValueError):
        Result("a", utcnow(), True, ms=float("nan"))
    assert Result("a", utcnow(), True, ms=0.0).epoch > 0


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 s"), (45, "45 s"), (89, "89 s"), (600, "10 min"), (3900, "1 h 05 min"), (90000, "1 d 1 h")],
)
def test_span(seconds, text):
    assert fmt_span(seconds) == text


def test_pct():
    assert fmt_pct(None) == "-"
    assert fmt_pct(1.0) == "100%"
    assert fmt_pct(0.99934) == "99.93%"
    assert fmt_pct(0.5) == "50.0%"


def test_fmt_time():
    assert fmt_time(0) == "1970-01-01 00:00Z"
