"""One round: run the checks that are due, record the results, decide, notify, prune, write the page."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .checks import run_check
from .config import Check, Config
from .model import Result, utcnow
from .notify import Sender, deliver, post
from .store import Store
from .tracker import Event, Tracker

Runner = Callable[[Check], Result]


def tracker_for(store: Store, check: Check) -> Tracker:
    return Tracker.from_dict(
        store.load_state(check.name),
        fail_after=check.fail_after,
        recover_after=check.recover_after,
        degrade=check.degrade,
        slow_factor=check.slow_factor,
        slow_ms=check.slow_ms,
        slow_after=check.slow_after,
    )


def record(store: Store, check: Check, result: Result) -> list[Event]:
    """Store one result, let the tracker decide, and store what it decided."""
    tracker = tracker_for(store, check)
    events = tracker.feed(result.epoch, result.ok, result.ms, result.detail)
    store.add_results([result])
    store.save_state(check.name, tracker.to_dict())
    store.apply_events(check.name, events)
    return events


def due(store: Store, checks: tuple[Check, ...], now: float, force: bool = False) -> list[Check]:
    out = []
    for check in checks:
        last = float(store.load_state(check.name).get("last_ts", 0.0) or 0.0)  # type: ignore[arg-type]
        if force or now - last >= check.interval - 0.5:
            out.append(check)
    return out


@dataclass
class RoundReport:
    ran: int = 0
    events: int = 0
    sent: int = 0
    failed: int = 0
    pruned: int = 0


def run_round(
    cfg: Config,
    store: Store,
    *,
    force: bool = False,
    runner: Runner | None = None,
    sender: Sender = post,
    now: Callable[[], datetime] = utcnow,
    environ: dict[str, str] | None = None,
    workers: int = 16,
) -> RoundReport:
    run = runner or (lambda c: run_check(c, now))
    todo = due(store, cfg.checks, now().timestamp(), force)
    report = RoundReport()
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
            results = list(pool.map(run, todo))
        for check, result in zip(todo, results, strict=True):
            report.events += len(record(store, check, result))
        report.ran = len(todo)
    report.sent, report.failed = deliver(store, cfg.notify, sender, environ)
    report.pruned = store.prune(now().timestamp() - cfg.site.retention_days * 86400)
    return report
