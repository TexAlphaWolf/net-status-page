"""The configuration file (TOML): the site, the defaults and the list of checks. Everything is validated here."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .model import CHECK_TYPES, check_host, parse_duration

_SITE_KEYS = {"title", "description", "output", "data", "retention_days"}
_TUNING_KEYS = {"interval", "timeout", "fail_after", "recover_after", "degrade", "slow_factor", "slow_ms", "slow_after"}
_NOTIFY_KEYS = {"webhook_env", "on"}
_CHECK_KEYS = _TUNING_KEYS | {
    "name",
    "type",
    "group",
    "url",
    "host",
    "port",
    "expect_status",
    "contains",
    "expect",
    "min_days",
    "ca_file",
}
_EVENTS = ("down", "up", "degraded", "normal")

EXAMPLE = """\
# upstatus: what to check and where to write the page. Paths are relative to this file.

[site]
title = "Homelab status"
description = "Public status of my services."
output = "public"            # the static page (index.html, status.json) is written here
data = "upstatus-data"       # the history (a SQLite file) lives here
retention_days = 90

[defaults]                   # every check can override these
interval = "1m"
timeout = 5                  # seconds
fail_after = 3               # failed checks in a row before a service is called down
recover_after = 2            # good checks in a row before it is called up again

[notify]                     # optional: a webhook for changes (the URL is read from an environment variable)
# webhook_env = "UPSTATUS_WEBHOOK"
# on = ["down", "up"]

[[check]]
name = "Website"
group = "Public"
type = "http"
url = "https://example.org/"
expect_status = [200]
contains = "Example Domain"

[[check]]
name = "Website certificate"
group = "Public"
type = "tls"
host = "example.org"
min_days = 14

[[check]]
name = "DNS"
group = "Network"
type = "dns"
host = "example.org"

[[check]]
name = "Router"
group = "Network"
type = "ping"
host = "192.168.1.1"

[[check]]
name = "SSH"
group = "Network"
type = "tcp"
host = "192.168.1.10"
port = 22
"""


class ConfigError(ValueError):
    """A mistake in the configuration file, reported in one line."""


@dataclass(frozen=True)
class Check:
    name: str
    type: str
    group: str = "Services"
    host: str = ""
    port: int | None = None
    path: str = "/"
    tls: bool = False  # http: https
    interval: int = 60
    timeout: float = 5.0
    fail_after: int = 3
    recover_after: int = 2
    degrade: bool = True
    slow_factor: float = 3.0
    slow_ms: float = 100.0
    slow_after: int = 5
    expect_status: tuple[int, ...] = ()
    contains: str = ""
    expect: str = ""
    min_days: int = 14
    ca_file: str = ""


@dataclass(frozen=True)
class Site:
    title: str = "Status"
    description: str = ""
    output: Path = Path("public")
    data: Path = Path("upstatus-data")
    retention_days: int = 90


@dataclass(frozen=True)
class Notify:
    webhook_env: str = ""
    on: tuple[str, ...] = ("down", "up")


@dataclass(frozen=True)
class Config:
    site: Site
    notify: Notify
    checks: tuple[Check, ...]


def _table(raw: object, name: str, allowed: set[str]) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"[{name}]: unknown key {unknown[0]!r}")
    return raw


def _int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    return value


def _num(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise ConfigError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{name} must be a short text without control characters (at most {limit})")
    return value.strip()


def _tuning(raw: dict[str, object], base: dict[str, object], where: str) -> dict[str, object]:
    out = dict(base)
    for key in _TUNING_KEYS & set(raw):
        value = raw[key]
        if key == "interval":
            try:
                out[key] = parse_duration(value)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ConfigError(f"{where}: interval: {exc}") from None
            if not 10 <= int(out[key]) <= 86400:  # type: ignore[call-overload]
                raise ConfigError(f"{where}: interval must be between 10s and 1d")
        elif key == "timeout":
            out[key] = _num(value, f"{where}: timeout", 0.5, 60)
        elif key in ("fail_after", "recover_after", "slow_after"):
            out[key] = _int(value, f"{where}: {key}", 1 if key != "slow_after" else 2, 60)
        elif key == "degrade":
            if not isinstance(value, bool):
                raise ConfigError(f"{where}: degrade must be true or false")
            out[key] = value
        elif key == "slow_factor":
            out[key] = _num(value, f"{where}: slow_factor", 1.5, 50)
        elif key == "slow_ms":
            out[key] = _num(value, f"{where}: slow_ms", 0, 60000)
    return out


def _check(raw: object, index: int, defaults: dict[str, object]) -> Check:
    where = f"check #{index}"
    t = _table(raw, where, _CHECK_KEYS)
    name = _text(t.get("name"), f"{where}: name", 60)
    where = f"check {name!r}"
    kind = t.get("type")
    if kind not in CHECK_TYPES:
        raise ConfigError(f"{where}: type must be one of {', '.join(CHECK_TYPES)}")
    tuning = _tuning(t, defaults, where)
    if float(tuning.get("timeout", 5.0)) >= float(tuning.get("interval", 60)):  # type: ignore[arg-type]
        raise ConfigError(f"{where}: timeout must be shorter than the interval")
    group = _text(t["group"], f"{where}: group", 40) if "group" in t else "Services"
    fields: dict[str, object] = {"name": name, "type": kind, "group": group, **tuning}
    try:
        if kind == "http":
            url = _text(t.get("url"), f"{where}: url", 2000)
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
                raise ConfigError(f"{where}: url must be an http:// or https:// address")
            if parts.username is not None or parts.password is not None:
                raise ConfigError(f"{where}: no credentials in the url")
            fields["host"] = check_host(parts.hostname)
            fields["port"] = parts.port
            fields["tls"] = parts.scheme == "https"
            fields["path"] = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            statuses = t.get("expect_status", [])
            if not isinstance(statuses, list) or not all(
                isinstance(s, int) and not isinstance(s, bool) and 100 <= s <= 599 for s in statuses
            ):
                raise ConfigError(f"{where}: expect_status must be a list of HTTP status codes")
            fields["expect_status"] = tuple(statuses)
            if "contains" in t:
                fields["contains"] = _text(t["contains"], f"{where}: contains", 200)
            if "ca_file" in t:
                fields["ca_file"] = _text(t["ca_file"], f"{where}: ca_file", 500)
        else:
            fields["host"] = check_host(str(t.get("host", "")))
            if kind == "tcp":
                fields["port"] = _int(t.get("port"), f"{where}: port", 1, 65535)
            if kind == "tls":
                fields["port"] = _int(t.get("port", 443), f"{where}: port", 1, 65535)
                fields["min_days"] = _int(t.get("min_days", 14), f"{where}: min_days", 0, 3650)
                if "ca_file" in t:
                    fields["ca_file"] = _text(t["ca_file"], f"{where}: ca_file", 500)
            if kind == "dns" and "expect" in t:
                fields["expect"] = check_host(str(t["expect"]))
    except ValueError as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"{where}: {exc}") from None
    if kind in ("tcp", "tls", "dns", "ping"):
        for key in ("url", "contains", "expect_status"):
            if key in t:
                raise ConfigError(f"{where}: {key!r} is only for http checks")
    return Check(**fields)  # type: ignore[arg-type]


def parse_config(text: str, base: Path) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from None
    unknown = sorted(set(raw) - {"site", "defaults", "notify", "check"})
    if unknown:
        raise ConfigError(f"unknown section {unknown[0]!r}")
    s = _table(raw.get("site", {}), "site", _SITE_KEYS)
    site = Site(
        title=_text(s["title"], "site.title", 80) if "title" in s else "Status",
        description=_text(s["description"], "site.description", 300) if "description" in s else "",
        output=base / _text(s.get("output", "public"), "site.output", 200),
        data=base / _text(s.get("data", "upstatus-data"), "site.data", 200),
        retention_days=_int(s.get("retention_days", 90), "site.retention_days", 7, 3650),
    )
    n = _table(raw.get("notify", {}), "notify", _NOTIFY_KEYS)
    on = n.get("on", ["down", "up"])
    if not isinstance(on, list) or not on or not all(e in _EVENTS for e in on):
        raise ConfigError(f"notify.on must be a list of: {', '.join(_EVENTS)}")
    env = str(n.get("webhook_env", ""))
    if env and not env.replace("_", "").isalnum():
        raise ConfigError("notify.webhook_env must be the NAME of an environment variable")
    notify = Notify(webhook_env=env, on=tuple(str(e) for e in on))
    defaults = _tuning(_table(raw.get("defaults", {}), "defaults", _TUNING_KEYS), {}, "defaults")
    rows = raw.get("check", [])
    if not isinstance(rows, list) or not rows:
        raise ConfigError("at least one [[check]] is needed")
    checks = tuple(_check(row, i, defaults) for i, row in enumerate(rows, start=1))
    names = [c.name.lower() for c in checks]
    if len(set(names)) != len(names):
        raise ConfigError("two checks have the same name")
    if len(checks) > 500:
        raise ConfigError("at most 500 checks")
    return Config(site, notify, checks)


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"no configuration file {path} (create one with: upstatus init)") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path} is not UTF-8 text") from None
    return parse_config(text, path.resolve().parent)
