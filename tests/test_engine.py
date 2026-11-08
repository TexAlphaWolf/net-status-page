from datetime import UTC, datetime, timedelta

import pytest

from upstatus.config import Check, Config, Notify, Site
from upstatus.engine import due, record, run_round, tracker_for
from upstatus.model import Result
from upstatus.store import Store


class Clock:
    def __init__(self):
        self.t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def cfg(tmp_path, *checks, notify=None, retention=90):
    return Config(
        Site(output=tmp_path / "out", data=tmp_path / "data", retention_days=retention), notify or Notify(), checks
    )


def chk(name="a", **kw):
    return Check(name=name, type="tcp", host="127.0.0.1", port=1, **kw)


class Script:
    """A runner that answers from a list, per check name; 'x' fails, '.' is good."""

    def __init__(self, clock, **scripts):
        self.clock, self.scripts, self.calls = clock, {k: list(v) for k, v in scripts.items()}, []

    def __call__(self, check):
        self.calls.append(check.name)
        c = self.scripts[check.name].pop(0)
        return Result(check.name, self.clock(), c == ".", 10.0 if c == "." else None, "" if c == "." else "timeout")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "data") as s:
        yield s


def test_record_decides_and_stores(store):
    c = chk(fail_after=2, recover_after=1)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, ok in enumerate([True, False, False, True]):
        ev = record(store, c, Result("a", t0 + timedelta(minutes=i), ok, 5.0 if ok else None, "" if ok else "timeout"))
    assert [e.kind for e in ev] == ["up"]
    assert store.count_results("a") == 4
    assert [(i.kind, i.end is not None) for _, i in store.incidents("a")] == [("down", True)]
    assert tracker_for(store, c).status == "up"


def test_due_checks(store):
    a, b = chk("a", interval=60), chk("b", interval=300)
    now = 1_000_000.0
    assert [c.name for c in due(store, (a, b), now)] == ["a", "b"]  # never run
    store.save_state("a", {"last_ts": now - 30})
    store.save_state("b", {"last_ts": now - 30})
    assert due(store, (a, b), now) == []
    assert [c.name for c in due(store, (a, b), now + 40)] == ["a"]
    assert [c.name for c in due(store, (a, b), now, force=True)] == ["a", "b"]


def test_run_round_runs_due_checks_only(tmp_path, store):
    clock = Clock()
    a, b = chk("a", interval=60), chk("b", interval=300)
    config = cfg(tmp_path, a, b)
    runner = Script(clock, a=".....", b="..")
    assert run_round(config, store, runner=runner, now=clock).ran == 2
    clock.advance(60)
    assert run_round(config, store, runner=runner, now=clock).ran == 1
    assert runner.calls == ["a", "b", "a"]
    clock.advance(60)
    assert run_round(config, store, runner=runner, now=clock, force=True).ran == 2


def test_run_round_reports_events_and_sends_them(tmp_path, store):
    clock = Clock()
    config = cfg(tmp_path, chk(fail_after=2, recover_after=1), notify=Notify(webhook_env="HOOK", on=("down", "up")))
    runner = Script(clock, a="x" + "x" + "." + ".")
    sent = []
    env = {"HOOK": "https://hooks.example/abc"}

    def sender(url, body):
        sent.append((url, body))
        return True

    reports = []
    for _ in range(4):
        reports.append(run_round(config, store, runner=runner, now=clock, sender=sender, environ=env))
        clock.advance(60)
    assert [r.events for r in reports] == [0, 1, 1, 0]
    assert [r.sent for r in reports] == [0, 1, 1, 0]
    assert all(url == "https://hooks.example/abc" for url, _ in sent)
    assert b"is DOWN" in sent[0][1] and b"is back UP" in sent[1][1]


def test_a_failed_delivery_is_tried_again_in_order(tmp_path, store):
    clock = Clock()
    config = cfg(tmp_path, chk(fail_after=1, recover_after=1), notify=Notify(webhook_env="HOOK"))
    runner = Script(clock, a="x.")
    env = {"HOOK": "https://hooks.example/abc"}
    ok = {"v": False}
    calls = []

    def sender(url, body):
        calls.append(body)
        return ok["v"]

    r = run_round(config, store, runner=runner, now=clock, sender=sender, environ=env)
    assert (r.sent, r.failed) == (0, 1)
    clock.advance(60)
    r = run_round(config, store, runner=runner, now=clock, sender=sender, environ=env)
    assert (r.sent, r.failed) == (0, 1) and len(store.pending()) == 2  # still the first one first
    ok["v"] = True
    clock.advance(10)  # no check is due: the round only delivers
    r = run_round(config, store, runner=runner, now=clock, sender=sender, environ=env)
    assert (r.ran, r.sent) == (0, 2) and store.pending() == []
    assert b"is DOWN" in calls[-2] and b"is back UP" in calls[-1]


def test_no_webhook_configured_does_not_let_the_queue_grow(tmp_path, store):
    clock = Clock()
    config = cfg(tmp_path, chk(fail_after=1))
    run_round(config, store, runner=Script(clock, a="x"), now=clock)
    assert store.pending() == []


def test_events_the_config_does_not_ask_for_are_not_sent(tmp_path, store):
    clock = Clock()
    config = cfg(tmp_path, chk(fail_after=1, recover_after=1), notify=Notify(webhook_env="HOOK", on=("up",)))
    sent = []
    env = {"HOOK": "https://h.example/x"}
    runner = Script(clock, a="x.")
    for _ in range(2):
        run_round(config, store, runner=runner, now=clock, sender=lambda u, b: sent.append(b) or True, environ=env)
        clock.advance(60)
    assert len(sent) == 1 and b"back UP" in sent[0]
    assert store.pending() == []


def test_old_results_are_pruned(tmp_path, store):
    clock = Clock()
    config = cfg(tmp_path, chk(), retention=7)
    runner = Script(clock, a="." * 3)
    run_round(config, store, runner=runner, now=clock)
    clock.advance(8 * 86400)
    r = run_round(config, store, runner=runner, now=clock)
    assert r.pruned == 1 and store.count_results() == 1


def test_checks_run_in_parallel_and_a_slow_one_does_not_hold_the_others(tmp_path, store):
    import threading
    import time

    clock = Clock()
    names = [f"c{i}" for i in range(6)]
    config = cfg(tmp_path, *(chk(n) for n in names))
    barrier = threading.Barrier(6, timeout=5)

    def runner(check):
        barrier.wait()  # would deadlock if the checks ran one after the other
        return Result(check.name, clock(), True, 1.0)

    start = time.monotonic()
    assert run_round(config, store, runner=runner, now=clock).ran == 6
    assert time.monotonic() - start < 5
