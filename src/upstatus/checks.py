"""The checks: one function per kind, all returning a Result. Nothing here raises: a failure is a result.

What goes into ``detail`` is a fixed short phrase, never text taken from the network.
"""

from __future__ import annotations

import http.client
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .config import Check
from .model import Result, clean, utcnow

MAX_BODY = 65536
_PING_TIME = re.compile(r"[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms", re.IGNORECASE)

Outcome = tuple[bool, float | None, str]


def explain(exc: BaseException) -> str:
    """A short fixed phrase for an exception."""
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "certificate not valid"
    if isinstance(exc, ssl.SSLError):
        return "TLS error"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ConnectionResetError | BrokenPipeError | ConnectionAbortedError):
        return "connection closed"
    if isinstance(exc, socket.gaierror):
        return "name not found"
    if isinstance(exc, http.client.HTTPException):
        return "bad HTTP answer"
    if isinstance(exc, OSError):
        return "network unreachable"
    return "error"


def _context(check: Check) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=check.ca_file or None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def check_http(check: Check) -> Outcome:
    port = check.port or (443 if check.tls else 80)
    host = f"[{check.host}]" if ":" in check.host else check.host
    conn: http.client.HTTPConnection
    if check.tls:
        conn = http.client.HTTPSConnection(check.host, port, timeout=check.timeout, context=_context(check))
    else:
        conn = http.client.HTTPConnection(check.host, port, timeout=check.timeout)
    start = time.perf_counter()
    try:
        conn.request(
            "GET",
            check.path,
            headers={
                "Host": host if port in (80, 443) else f"{host}:{port}",
                "User-Agent": "upstatus/1.0",
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        body = resp.read(MAX_BODY) if check.contains else b""
        ms = (time.perf_counter() - start) * 1000
        status = resp.status
    except (OSError, http.client.HTTPException) as exc:
        return False, None, explain(exc)
    finally:
        conn.close()
    good = status in check.expect_status if check.expect_status else status < 400
    if not good:
        return False, ms, f"HTTP {status}"
    if check.contains and check.contains.encode("utf-8") not in body:
        return False, ms, "text not found"
    return True, ms, f"HTTP {status}"


def check_tcp(check: Check) -> Outcome:
    assert check.port is not None
    start = time.perf_counter()
    try:
        with socket.create_connection((check.host, check.port), timeout=check.timeout):
            pass
    except OSError as exc:
        return False, None, explain(exc)
    return True, (time.perf_counter() - start) * 1000, "connected"


def check_dns(check: Check) -> Outcome:
    box: list[object] = []

    def work() -> None:
        try:
            box.append(socket.getaddrinfo(check.host, None, type=socket.SOCK_STREAM))
        except OSError as exc:
            box.append(exc)

    start = time.perf_counter()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(check.timeout)  # getaddrinfo has no timeout of its own
    ms = (time.perf_counter() - start) * 1000
    if not box:
        return False, None, "timeout"
    answer = box[0]
    if isinstance(answer, BaseException):
        return False, None, explain(answer)
    addrs = {str(item[4][0]) for item in answer}  # type: ignore[attr-defined]
    if check.expect and check.expect not in addrs:
        return False, ms, "unexpected address"
    return True, ms, f"{len(addrs)} address(es)"


def check_tls(check: Check, now: Callable[[], datetime] = utcnow) -> Outcome:
    port = check.port or 443
    start = time.perf_counter()
    try:
        with (
            socket.create_connection((check.host, port), timeout=check.timeout) as raw,
            _context(check).wrap_socket(raw, server_hostname=check.host) as tls,
        ):
            cert = tls.getpeercert()
    except OSError as exc:
        return False, None, explain(exc)
    ms = (time.perf_counter() - start) * 1000
    try:
        not_after = ssl.cert_time_to_seconds(str(cert["notAfter"]))  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return False, ms, "no expiry date"
    days = math.floor((not_after - now().timestamp()) / 86400)
    if days < check.min_days:
        return False, ms, f"expires in {days} days" if days >= 0 else "expired"
    return True, ms, f"expires in {days} days"


def build_ping(system: str, host: str, timeout: float) -> list[str]:
    if system.startswith("win"):
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    if system == "darwin":
        return ["ping", "-c", "1", "-W", str(int(timeout * 1000)), host]
    return ["ping", "-c", "1", "-W", str(max(1, math.ceil(timeout))), host]


def parse_ping(text: str) -> float | None:
    """The round-trip time in ms from the output of ping in any language, or None."""
    m = _PING_TIME.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value


def check_ping(
    check: Check,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system: str = sys.platform,
) -> Outcome:
    cmd = build_ping(system, check.host, check.timeout)
    try:
        proc = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=check.timeout + 3,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, None, "ping is not installed"
    except subprocess.TimeoutExpired:
        return False, None, "timeout"
    except OSError:
        return False, None, "cannot run ping"
    ms = parse_ping(proc.stdout) if proc.returncode == 0 else None
    if ms is None:
        return False, None, "no answer"
    return True, ms, "answered"


_RUNNERS: dict[str, Callable[[Check], Outcome]] = {
    "http": check_http,
    "tcp": check_tcp,
    "dns": check_dns,
    "tls": check_tls,
    "ping": check_ping,
}


def run_check(check: Check, now: Callable[[], datetime] = utcnow) -> Result:
    """Run one check and return its Result. Never raises."""
    ts = now()
    try:
        ok, ms, detail = _RUNNERS[check.type](check)
    except Exception as exc:  # a bug in a check must not stop the others
        ok, ms, detail = False, None, f"internal error ({type(exc).__name__})"
    return Result(check.name, ts.astimezone(UTC), ok, None if ms is None else round(ms, 2), clean(detail))
