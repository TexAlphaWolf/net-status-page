"""The tracker against a simulated fleet whose truth is known."""

import random
from statistics import median

import pytest

from sim import TICK, make_fleet, make_service, run_tracker, score

TICKS = 3 * 1440


@pytest.fixture(scope="module")
def fleet():
    return make_fleet(7, with_events=12, quiet=8, ticks=TICKS)


def test_the_simulator_writes_down_its_truth():
    svc = make_service(random.Random(1), "s", 2000, outages=2, blips=3, slow=1)
    assert len(svc.outages) == 2 and len(svc.blips) == 3 and len(svc.slow) == 1
    assert all(w.length >= 6 for w in svc.outages) and all(1 <= w.length <= 2 for w in svc.blips)
    assert len(svc.results) == 2000
    for w in svc.outages:
        assert all(not svc.results[i][1] for i in range(w.start, w.end))
    for w in svc.slow:
        assert all(svc.results[i][1] for i in range(w.start, w.end)) or svc.loss > 0  # slow checks answer (loss aside)


def test_every_real_outage_is_found_with_a_fixed_delay(fleet):
    found = total = 0
    delays = []
    for svc in fleet:
        s = score(svc, run_tracker(svc))
        found += s["found"]
        total += s["outages"]
        delays += s["delays"]
    assert total >= 12 and found == total
    assert median(delays) == 2  # the third failed check


def test_the_start_and_end_of_an_outage_are_exact(fleet):
    start, end = [], []
    for svc in fleet:
        s = score(svc, run_tracker(svc))
        start += s["start_err"]
        end += s["end_err"]
    assert all(e == 0 for e in start)  # the first failed check
    assert all(abs(e) <= 1 for e in end)  # the first good check of the recovery


def test_no_false_outage_and_blips_are_not_reported_alone(fleet):
    assert sum(score(svc, run_tracker(svc))["false_down"] for svc in fleet) == 0


def test_a_single_failed_check_policy_raises_many_false_alarms(fleet):
    naive = sum(score(svc, run_tracker(svc, fail_after=1, recover_after=1))["down_events"] for svc in fleet)
    real = sum(score(svc, run_tracker(svc))["down_events"] for svc in fleet)
    outages = sum(len(svc.outages) for svc in fleet)
    assert real <= outages + 2
    assert naive > 5 * outages


def test_uptime_is_close_to_the_truth_where_the_raw_ratio_is_not(fleet):
    err_tracker = err_raw = 0.0
    for svc in fleet:
        s = score(svc, run_tracker(svc))
        err_tracker += abs(s["tracker_down_s"] - s["true_down_s"])
        err_raw += abs(s["failed_checks_s"] - s["true_down_s"])
    assert err_tracker < err_raw / 2


def test_slow_periods_are_found(fleet):
    found = total = 0
    for svc in fleet:
        s = score(svc, run_tracker(svc))
        found += s["found_slow"]
        total += s["slow"]
    assert total >= 3 and found >= total - 1


def test_a_quiet_service_never_alerts(fleet):
    for svc in fleet:
        if svc.name.startswith("quiet"):
            events = run_tracker(svc)
            assert [e.kind for e in events if e.kind in ("down", "degraded")] == []


def test_a_flaky_service_gets_false_outages_with_few_confirmations():
    svc = make_service(random.Random(3), "flaky", 20 * 1440, loss=0.12)
    assert score(svc, run_tracker(svc, fail_after=1))["false_down"] > 100
    assert score(svc, run_tracker(svc, fail_after=3))["false_down"] < 100
    assert (
        score(svc, run_tracker(svc, fail_after=5))["false_down"]
        < score(svc, run_tracker(svc, fail_after=3))["false_down"]
    )


def test_the_cost_of_confirmation_is_the_delay():
    svc = make_service(random.Random(4), "s", 4000, outages=3)
    delays = {n: median(score(svc, run_tracker(svc, fail_after=n))["delays"]) for n in (1, 3, 5)}
    assert delays == {1: 0, 3: 2, 5: 4}


def test_the_tick_is_one_minute():
    assert TICK == 60.0
