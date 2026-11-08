"""Shared small pieces: host validation, the result of one check, durations, text helpers."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_DURATION = re.compile(r"^(\d{1,6})([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

CHECK_TYPES = ("http", "tcp", "dns", "tls", "ping")
STATUSES = ("up", "degraded", "down", "unknown")


def check_host(value: str) -> str:
    """Return a host name (lower case) or IP address in canonical form, or raise ValueError.

    The value goes into a connection and, for ``ping``, into an argument list: no leading ``-``, no spaces, no path
    characters, no IPv6 zone.
    """
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("a host must be an IP address or a host name of at most 253 characters")
    if "%" not in value:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    if _HOSTNAME.match(value) is None:
        raise ValueError(f"not a valid IP address or host name: {value[:60]!r}")
    return value.lower()


def parse_duration(text: str | int | float) -> int:
    """Seconds from ``30s``, ``5m``, ``12h``, ``7d`` or a plain number of seconds."""
    if isinstance(text, bool):
        raise ValueError("not a duration")
    if isinstance(text, int | float):
        if text != int(text) or text < 0:
            raise ValueError(f"not a duration: {text!r}")
        return int(text)
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"not a duration (use 30s, 5m, 12h or 7d): {text[:20]!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def clean(text: str, limit: int = 80) -> str:
    """Printable ASCII only and short: what a check reports about a failure never carries raw network text."""
    return "".join(c if " " <= c <= "~" else "?" for c in text)[:limit]


@dataclass(frozen=True)
class Result:
    """The outcome of one check."""

    check: str
    ts: datetime
    ok: bool
    ms: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("a result needs a time zone aware timestamp")
        if self.ms is not None and not (0 <= self.ms < 1e7):
            raise ValueError(f"time out of range: {self.ms!r}")

    @property
    def epoch(self) -> float:
        return self.ts.timestamp()


def utcnow() -> datetime:
    return datetime.now(UTC)


def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%MZ")


def fmt_span(seconds: float) -> str:
    """``45 s``, ``12 min``, ``3 h 05 min``, ``2 d 4 h``."""
    s = max(0, round(seconds))
    if s < 90:
        return f"{s} s"
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def fmt_pct(fraction: float | None) -> str:
    if fraction is None:
        return "-"
    pct = fraction * 100
    if pct >= 100:
        return "100%"
    return f"{pct:.2f}%" if pct >= 99 else f"{pct:.1f}%"
