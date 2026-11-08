import sqlite3
from datetime import UTC, datetime

import pytest

from upstatus.model import Result
from upstatus.store import Store, StoreError
from upstatus.tracker import Event, incidents_from


def res(check="a", t=100.0, ok=True, ms=5.0, detail="ok"):
    return Result(check, datetime.fromtimestamp(t, UTC), ok, ms, detail)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "d") as s:
        yield s


def test_results_round_trip_and_ranges(store):
    store.add_results([res(t=100), res(t=200, ok=False, ms=None, detail="timeout"), res("b", t=150)])
    assert store.results("a", 0) == [(100.0, True, 5.0), (200.0, False, None)]
    assert store.results("a", 150, 250) == [(200.0, False, None)]
    assert store.count_results() == 3 and store.count_results("a") == 2
    assert store.first_seen("a") == 100.0 and store.first_seen("zzz") is None
    assert store.last_detail("a") == "timeout" and store.last_detail("zzz") == ""


def test_state_round_trip_and_damage(store):
    assert store.load_state("a") == {}
    store.save_state("a", {"status": "up", "lat": [1.0]})
    assert store.load_state("a") == {"status": "up", "lat": [1.0]}
    store.conn.execute("UPDATE state SET body = 'not json'")
    assert store.load_state("a") == {}
    store.conn.execute("UPDATE state SET body = '[1,2]'")
    assert store.load_state("a") == {}


def test_forget_keeps_only_listed_checks(store):
    store.save_state("a", {"x": 1})
    store.save_state("b", {"x": 2})
    store.forget(["a"])
    assert store.load_state("a") and not store.load_state("b")


def test_incidents_follow_the_events_like_the_pure_pairing(store):
    events = [
        Event("degraded", 50, 40, "300 ms"),
        Event("down", 130, 100, "timeout"),
        Event("up", 400, 380),
        Event("degraded", 600, 550),
        Event("normal", 900, 880),
        Event("down", 1000, 950, "refused"),
    ]
    store.apply_events("a", events)
    got = [i for _, i in store.incidents("a")]
    assert got == incidents_from(events)


def test_incidents_filter_by_check_and_time(store):
    store.apply_events("a", [Event("down", 130, 100), Event("up", 400, 380)])
    store.apply_events("b", [Event("down", 5000, 4900)])
    assert [n for n, _ in store.incidents()] == ["a", "b"]
    assert [n for n, _ in store.incidents(since=1000)] == ["b"]  # a ended before; b is still open
    assert [n for n, _ in store.incidents("b")] == ["b"]


def test_pending_events_and_marking(store):
    store.apply_events("a", [Event("down", 130, 100, "timeout"), Event("up", 400, 380)])
    pend = store.pending()
    assert [(p.check, p.kind, p.since, p.detail) for p in pend] == [("a", "down", 100, "timeout"), ("a", "up", 380, "")]
    store.mark_sent(pend[0].id)
    assert [p.kind for p in store.pending()] == ["up"]
    store.mark_all_sent()
    assert store.pending() == []


def test_apply_nothing_is_fine(store):
    store.apply_events("a", [])
    assert store.pending() == []


def test_prune_drops_old_results_and_closed_incidents(store):
    store.add_results([res(t=100), res(t=10_000)])
    store.apply_events("a", [Event("down", 130, 100), Event("up", 400, 380), Event("down", 20_000, 19_900)])
    store.mark_all_sent()
    assert store.prune(5000) == 1
    assert store.count_results() == 1
    assert [i.start for _, i in store.incidents(since=0)] == [19_900]
    assert store.pending() == []


def test_a_file_that_is_not_a_database(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "upstatus.db").write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(StoreError, match="not a usable history"):
        Store(d)


def test_a_newer_schema_is_refused(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    conn = sqlite3.connect(d / "upstatus.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StoreError, match="newer version"):
        Store(d)


def test_reopening_keeps_the_data(tmp_path):
    with Store(tmp_path) as s:
        s.add_results([res()])
    with Store(tmp_path) as s:
        assert s.count_results() == 1


def test_a_failed_transaction_leaves_nothing_behind(store):
    with pytest.raises(RuntimeError), store.transaction() as c:
        c.execute("INSERT INTO results VALUES ('a', 1, 1, 1, 'x')")
        raise RuntimeError("boom")
    assert store.count_results() == 0
