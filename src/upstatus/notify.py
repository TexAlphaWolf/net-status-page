"""Webhook notifications. The events wait in the store until they have been delivered, so a failed delivery is retried."""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import Callable
from urllib.parse import urlsplit

from .config import Notify
from .model import fmt_time
from .store import PendingEvent, Store

Sender = Callable[[str, bytes], bool]

_WORDS = {
    "down": "is DOWN",
    "up": "is back UP",
    "degraded": "is slow (degraded)",
    "normal": "is back to its usual speed",
}


def message(ev: PendingEvent) -> str:
    text = f"{ev.check} {_WORDS.get(ev.kind, ev.kind)}"
    if ev.detail:
        text += f": {ev.detail}"
    return f"{text} [{fmt_time(ev.since)}]"


def payload(ev: PendingEvent, text: str) -> bytes:
    """A body that Slack ("text"), Discord ("content") and a script can all read."""
    return json.dumps(
        {"text": text, "content": text, "check": ev.check, "event": ev.kind, "since": ev.since, "detail": ev.detail},
        separators=(",", ":"),
    ).encode("utf-8")


def post(url: str, body: bytes, timeout: float = 8.0) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.request(
            "POST",
            (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
            body=body,
            headers={"Content-Type": "application/json", "User-Agent": "upstatus/1.0"},
        )
        resp = conn.getresponse()
        resp.read(1024)
        return 200 <= resp.status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def deliver(
    store: Store, notify: Notify, sender: Sender = post, environ: dict[str, str] | None = None
) -> tuple[int, int]:
    """Send the pending events. Returns (sent, failed). Events the config does not ask for are marked as sent."""
    pending = store.pending()
    if not pending:
        return 0, 0
    env = os.environ if environ is None else environ
    url = env.get(notify.webhook_env, "") if notify.webhook_env else ""
    sent = failed = 0
    for ev in pending:
        if ev.kind not in notify.on or not url:
            store.mark_sent(ev.id)  # nothing to send: do not let the queue grow
            continue
        if sender(url, payload(ev, message(ev))):
            store.mark_sent(ev.id)
            sent += 1
        else:
            failed += 1
            break  # keep the order: try again next round
    return sent, failed
