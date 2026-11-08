import pytest

from upstatus.tracker import LAT_MIN, REBASE, Event, Tracker, incidents_from, median


def feed(t: Tracker, pattern: str, start: float = 0.0, step: float = 60.0, ms: float = 10.0):
    """'.' = good check, 'x' = failed check. Returns the events."""
    events = []
    for i, c in enumerate(pattern):
        events += t.feed(start + i * step, c == ".", ms if c == "." else None, "" if c == "." else "timeout")
    return events


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 2, 3]) == 2.5


def test_the_first_good_check_is_up_without_an_event():
    t = Tracker()
    assert feed(t, ".") == []
    assert t.status == "up"


def test_a_single_failure_is_not_an_outage():
    t = Tracker()
    assert feed(t, "...x.x..xx..") == []
    assert t.status == "up"


def test_down_after_fail_after_failures_and_it_started_at_the_first_one():
    t = Tracker(fail_after=3)
    events = feed(t, "..xxx", step=60)
    assert [e.kind for e in events] == ["down"]
    assert events[0].since == 2 * 60  # the first failed check
    assert events[0].ts == 4 * 60  # decided at the third
    assert events[0].detail == "timeout"
    assert t.status == "down"


def test_up_after_recover_after_good_checks_and_it_ended_at_the_first_one():
    t = Tracker(fail_after=2, recover_after=2)
    events = feed(t, "xx...")
    assert [e.kind for e in events] == ["down", "up"]
    assert events[1].since == 2 * 60
    assert events[1].ts == 3 * 60


def test_a_failure_during_recovery_starts_the_count_again():
    t = Tracker(fail_after=2, recover_after=3)
    events = feed(t, "xx..x...")
    assert [e.kind for e in events] == ["down", "up"]
    assert events[1].since == 5 * 60


def test_no_second_down_event_while_down():
    t = Tracker(fail_after=2)
    assert [e.kind for e in feed(t, "xxxxxxxx")] == ["down"]


def test_a_service_that_never_answers_is_down():
    t = Tracker(fail_after=3)
    assert [e.kind for e in feed(t, "xxx")] == ["down"]
    assert t.status == "down"


@pytest.mark.parametrize("fail_after", [1, 2, 5])
def test_fail_after_is_respected(fail_after):
    t = Tracker(fail_after=fail_after)
    events = feed(t, "x" * (fail_after - 1))
    assert events == [] and t.status == "unknown"
    assert [e.kind for e in feed(t, "x", start=999)] == ["down"]


def warm(t: Tracker, n: int = LAT_MIN + 5, ms: float = 10.0):
    for i in range(n):
        t.feed(i * 60.0, True, ms)
    return n * 60.0


def test_slow_needs_a_baseline():
    t = Tracker(slow_after=2)
    for i in range(LAT_MIN - 1):
        t.feed(i * 60.0, True, 10.0)
    assert t.feed(10_000, True, 5000.0) == []  # not enough history to judge


def test_degraded_after_slow_after_slow_checks_and_back():
    t = Tracker(slow_after=3)
    now = warm(t)
    ev = []
    for i in range(3):
        ev += t.feed(now + i * 60, True, 200.0)
    assert [e.kind for e in ev] == ["degraded"]
    assert ev[0].since == now  # the first slow check
    assert "usually 10 ms" in ev[0].detail
    assert t.status == "degraded"
    ev = []
    for i in range(3):
        ev += t.feed(now + 1000 + i * 60, True, 10.0)
    assert [e.kind for e in ev] == ["normal"]
    assert ev[0].since == now + 1000
    assert t.status == "up"


def test_one_slow_answer_is_not_degraded():
    t = Tracker(slow_after=3)
    now = warm(t)
    assert t.feed(now, True, 900.0) == []
    t.feed(now + 60, True, 10.0)
    assert t.status == "up" and t.slow == 0


def test_slow_must_be_much_slower_and_by_a_margin():
    t = Tracker(slow_after=2, slow_factor=3.0, slow_ms=100.0)
    now = warm(t, ms=10.0)
    for i in range(10):  # 3x slower but only 20 ms more: a fast service that is still fast
        assert t.feed(now + i * 60, True, 30.0) == []
    assert t.status == "up"


def test_degrade_can_be_switched_off():
    t = Tracker(slow_after=2, degrade=False)
    now = warm(t)
    for i in range(10):
        assert t.feed(now + i * 60, True, 5000.0) == []
    assert t.status == "up"


def test_slow_answers_do_not_move_the_baseline():
    t = Tracker(slow_after=50)
    now = warm(t)
    for i in range(30):
        t.feed(now + i * 60, True, 500.0)
    assert median(t.lat) == 10.0


def test_a_lasting_change_becomes_the_new_usual_time():
    t = Tracker(slow_after=3)
    now = warm(t)
    kinds = []
    for i in range(REBASE + 10):
        kinds += [e.kind for e in t.feed(now + i * 60, True, 300.0)]
    assert kinds == ["degraded", "normal"]
    assert t.status == "up"
    assert t.feed(now + 99999, True, 310.0) == []  # 300 ms is normal now


def test_down_closes_a_degradation_and_recovery_forgets_slow_counts():
    t = Tracker(slow_after=2, fail_after=2, recover_after=1)
    now = warm(t)
    t.feed(now, True, 300.0)
    t.feed(now + 60, True, 300.0)
    assert t.status == "degraded"
    ev = feed(t, "xx", start=now + 120) + feed(t, ".", start=now + 400)
    assert [e.kind for e in ev] == ["down", "up"]
    assert t.status == "up" and t.slow == 0


def test_failed_checks_reset_the_slow_count():
    t = Tracker(slow_after=3)
    now = warm(t)
    t.feed(now, True, 300.0)
    t.feed(now + 60, False)
    t.feed(now + 120, True, 300.0)
    assert t.status == "up" and t.slow == 1


def test_a_good_check_without_a_time_is_not_judged():
    t = Tracker(slow_after=2)
    now = warm(t)
    for i in range(5):
        assert t.feed(now + i, True, None) == []


def test_state_round_trips():
    t = Tracker(fail_after=2, slow_after=3)
    now = warm(t)
    t.feed(now, False)
    raw = t.to_dict()
    u = Tracker.from_dict(raw, fail_after=2, slow_after=3)
    assert u.to_dict() == raw
    assert [e.kind for e in u.feed(now + 60, False)] == ["down"]


def test_damaged_state_is_ignored_not_trusted():
    u = Tracker.from_dict(
        {"status": "exploded", "fails": -3, "oks": "x", "first_fail": None, "lat": [1, "a", None, 2.5], "last_ms": "no"}
    )
    assert (u.status, u.fails, u.oks, u.first_fail, u.lat, u.last_ms) == ("unknown", 0, 0, 0.0, [1.0, 2.5], None)


def ev(kind, ts, since=None, detail=""):
    return Event(kind, ts, ts if since is None else since, detail)


def test_incidents_from_events():
    events = [ev("down", 130, 100, "timeout"), ev("up", 400, 380), ev("degraded", 600, 550), ev("normal", 900, 880)]
    inc = incidents_from(events)
    assert [(i.kind, i.start, i.end) for i in inc] == [("down", 100, 380), ("degraded", 550, 880)]
    assert inc[0].detail == "timeout"


def test_open_incidents_have_no_end():
    inc = incidents_from([ev("down", 130, 100)])
    assert [(i.kind, i.end) for i in inc] == [("down", None)]
    inc = incidents_from([ev("degraded", 130, 100)])
    assert [(i.kind, i.end) for i in inc] == [("degraded", None)]


def test_going_down_closes_the_slow_period_at_the_first_failure():
    inc = incidents_from([ev("degraded", 50, 40), ev("down", 130, 100), ev("up", 400, 380)])
    assert [(i.kind, i.start, i.end) for i in inc] == [("degraded", 40, 100), ("down", 100, 380)]
