import shutil
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from servers import Behaviour, closed_port, http_server, tcp_listener, tls_server
from upstatus.checks import (
    build_ping,
    check_dns,
    check_ping,
    check_tcp,
    check_tls,
    explain,
    parse_ping,
    run_check,
)
from upstatus.config import Check, parse_config

LOCAL = Path("/x")
# a closed port on Windows is retried for about two seconds, so a short timeout may fire first
REFUSED = ("connection refused", "timeout") if sys.platform == "win32" else ("connection refused",)


def mk(**kw) -> Check:
    base = {"name": "t", "type": "http", "host": "127.0.0.1", "timeout": 2.0}
    base.update(kw)
    return Check(**base)


def http_check(port, **kw) -> Check:
    return mk(type="http", port=port, **kw)


def test_http_ok_and_time():
    with http_server() as (port, _):
        r = run_check(http_check(port))
    assert r.ok and r.detail == "HTTP 200" and r.ms is not None and r.ms < 1000


def test_http_measures_the_delay_of_the_server():
    with http_server(Behaviour()) as (port, state):
        state.delay = 0.15
        r = run_check(http_check(port))
    assert r.ok and r.ms is not None and 140 <= r.ms < 600


@pytest.mark.parametrize(
    ("status", "ok"), [(200, True), (204, True), (301, True), (404, False), (500, False), (503, False)]
)
def test_http_default_status_rule(status, ok):
    with http_server() as (port, state):
        state.status = status
        r = run_check(http_check(port))
    assert r.ok is ok
    assert r.detail == f"HTTP {status}"
    assert r.ms is not None  # the server answered: the time is known even when the status is wrong


def test_http_expected_status_list():
    with http_server() as (port, state):
        state.status = 401
        assert run_check(http_check(port, expect_status=(401,))).ok
        assert not run_check(http_check(port, expect_status=(200,))).ok


def test_http_body_text():
    with http_server() as (port, _):
        assert run_check(http_check(port, contains="hello")).ok
        r = run_check(http_check(port, contains="absent"))
    assert not r.ok and r.detail == "text not found"


def test_http_connection_refused():
    r = run_check(http_check(closed_port()))
    assert not r.ok and r.detail in REFUSED and r.ms is None


def test_http_timeout():
    with http_server() as (port, state):
        state.delay = 1.5
        r = run_check(http_check(port, timeout=0.3))
    assert not r.ok and r.detail == "timeout"


def test_http_garbage_answer():
    with tcp_listener() as port:
        # a server that answers with something that is not HTTP
        import socket
        import threading

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        p = srv.getsockname()[1]

        def serve():
            c, _ = srv.accept()
            c.recv(1024)
            c.sendall(b"this is not http\r\n\r\n")
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        r = run_check(http_check(p))
        t.join(5)
        srv.close()
    assert not r.ok and r.detail in ("bad HTTP answer", "connection closed")
    assert port  # the listener only reserved a port


def test_http_to_a_tls_port_with_plain_http_fails_cleanly():
    with http_server() as (port, _):
        r = run_check(mk(type="http", port=port, tls=True))
    assert not r.ok and r.detail in ("TLS error", "connection closed", "certificate not valid")


def test_tcp_open_and_closed():
    with tcp_listener() as port:
        r = run_check(mk(type="tcp", port=port))
        assert r.ok and r.detail == "connected" and r.ms is not None
    r = run_check(mk(type="tcp", port=closed_port()))
    assert not r.ok and r.detail in REFUSED


def test_dns_resolves_localhost_and_checks_the_address():
    r = run_check(mk(type="dns", host="localhost"))
    assert r.ok and r.ms is not None
    assert run_check(mk(type="dns", host="localhost", expect="127.0.0.1")).ok
    r = run_check(mk(type="dns", host="localhost", expect="203.0.113.77"))
    assert not r.ok and r.detail == "unexpected address"


def test_dns_unknown_name():
    r = run_check(mk(type="dns", host="no-such-name.invalid"))
    assert not r.ok and r.detail == "name not found"


def test_dns_that_never_answers_times_out(monkeypatch):
    import socket
    import time

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: time.sleep(2))
    r = check_dns(mk(type="dns", host="slow.example", timeout=0.2))
    assert r == (False, None, "timeout")


# -- ping ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ms"),
    [
        (
            "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms\n--- rtt min/avg/max/mdev = 12.3/12.3/12.3/0.000 ms",
            12.3,
        ),
        ("64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=0.8 ms", 0.8),
        ("Reply from 1.1.1.1: bytes=32 time=14ms TTL=57", 14.0),
        ("Reply from 1.1.1.1: bytes=32 time<1ms TTL=128", 1.0),
        ("Risposta da 1.1.1.1: byte=32 durata=14ms TTL=57", 14.0),
        ("64 bytes from fe80::1: icmp_seq=1 ttl=64 time=1,5 ms", 1.5),
        ("Request timed out.", None),
        ("Reply from 192.168.1.1: Destination host unreachable.", None),
        ("", None),
    ],
)
def test_parse_ping(text, ms):
    assert parse_ping(text) == ms


def test_build_ping_per_system():
    assert build_ping("linux", "h", 2.0) == ["ping", "-c", "1", "-W", "2", "h"]
    assert build_ping("linux", "h", 0.5) == ["ping", "-c", "1", "-W", "1", "h"]
    assert build_ping("darwin", "h", 2.0) == ["ping", "-c", "1", "-W", "2000", "h"]
    assert build_ping("win32", "h", 2.0) == ["ping", "-n", "1", "-w", "2000", "h"]


def fake(stdout="", code=0, exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, code, stdout, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_ping_ok_with_an_argument_list_and_no_shell():
    run = fake("64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=3.2 ms")
    assert check_ping(mk(type="ping", host="10.0.0.1"), run=run, system="linux") == (True, 3.2, "answered")
    cmd, kw = run.calls[0]
    assert isinstance(cmd, list) and "shell" not in kw and kw["env"]["LC_ALL"] == "C"


def test_ping_failure_modes():
    c = mk(type="ping", host="10.0.0.1")
    assert check_ping(c, run=fake("Request timed out", code=1))[2] == "no answer"
    assert check_ping(c, run=fake("time=1 ms", code=1))[0] is False  # a non-zero exit is a failure whatever the text
    assert check_ping(c, run=fake(exc=FileNotFoundError()))[2] == "ping is not installed"
    assert check_ping(c, run=fake(exc=subprocess.TimeoutExpired("ping", 1)))[2] == "timeout"
    assert check_ping(c, run=fake(exc=PermissionError()))[2] == "cannot run ping"


@pytest.mark.skipif(shutil.which("ping") is None, reason="no ping program")
def test_ping_the_loopback_for_real():
    r = run_check(mk(type="ping", host="127.0.0.1"))
    assert r.ok and r.ms is not None


# -- TLS ----------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl program")
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(d / "k.pem"),
            "-out",
            str(d / "c.pem"),
            "-days",
            "30",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return d / "c.pem", d / "k.pem"


def tls_check(port, cert_file, **kw):
    return mk(type="tls", host="127.0.0.1", port=port, ca_file=str(cert_file), **kw)


def test_tls_certificate_days(cert):
    with tls_server(*cert) as port:
        r = run_check(tls_check(port, cert[0], min_days=14))
        assert r.ok and r.detail in ("expires in 29 days", "expires in 30 days")
        r = run_check(tls_check(port, cert[0], min_days=60))
        assert not r.ok and r.detail.startswith("expires in")


def test_tls_expired_with_a_clock_in_the_future(cert):
    later = lambda: datetime.now(UTC) + timedelta(days=45)  # noqa: E731
    with tls_server(*cert) as port:
        ok, _, detail = check_tls(tls_check(port, cert[0]), now=later)
    assert not ok and detail == "expired"


def test_tls_untrusted_certificate_is_refused(cert):
    with tls_server(*cert) as port:
        r = run_check(mk(type="tls", host="127.0.0.1", port=port))  # no ca_file: the system store does not know it
    assert not r.ok and r.detail == "certificate not valid"


def test_tls_on_a_plain_port():
    with http_server() as (port, _):
        r = run_check(mk(type="tls", host="127.0.0.1", port=port))
    assert not r.ok and r.detail in ("TLS error", "connection closed", "certificate not valid")


def test_tls_nothing_listening():
    r = run_check(mk(type="tls", host="127.0.0.1", port=closed_port()))
    assert not r.ok and r.detail in REFUSED


def test_https_check_with_a_private_ca(cert):
    import threading

    # an HTTPS server speaking just enough HTTP
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert[0]), str(cert[1]))
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        with ctx.wrap_socket(c, server_side=True) as t:
            t.recv(2048)
            t.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    r = run_check(mk(type="http", tls=True, port=port, host="127.0.0.1", ca_file=str(cert[0]), contains="ok"))
    t.join(5)
    srv.close()
    assert r.ok and r.detail == "HTTP 200"


# -- the rest -----------------------------------------------------------------------------------------------------


def test_explain_covers_the_usual_errors():
    import http.client
    import socket

    assert explain(TimeoutError()) == "timeout"
    assert explain(ConnectionRefusedError()) == "connection refused"
    assert explain(ConnectionResetError()) == "connection closed"
    assert explain(socket.gaierror()) == "name not found"
    assert explain(http.client.BadStatusLine("x")) == "bad HTTP answer"
    assert explain(OSError()) == "network unreachable"
    assert explain(ssl.SSLError()) == "TLS error"
    assert explain(KeyError()) == "error"


def test_run_check_never_raises_and_never_leaks_text(monkeypatch):
    from upstatus import checks

    def boom(check):
        raise RuntimeError("secret internal text \x00")

    monkeypatch.setitem(checks._RUNNERS, "tcp", boom)
    r = run_check(mk(type="tcp", port=1))
    assert not r.ok and r.detail == "internal error (RuntimeError)"


def test_run_check_uses_the_clock():
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with tcp_listener() as port:
        r = run_check(mk(type="tcp", port=port), now=lambda: fixed)
    assert r.ts == fixed


def test_check_tcp_needs_a_port():
    with pytest.raises(AssertionError):
        check_tcp(mk(type="tcp"))


def test_config_and_check_agree_on_the_example():
    from upstatus.config import EXAMPLE

    cfg = parse_config(EXAMPLE, LOCAL)
    assert {c.type for c in cfg.checks} == {"http", "tls", "dns", "ping", "tcp"}
