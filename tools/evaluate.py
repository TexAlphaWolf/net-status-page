"""Measure upstatus: real checks against servers on loopback, the tracker against a simulated fleet whose truth is
known, the price of confirmation, and the cost of the page.

python tools/evaluate.py                       # the full run, a minute or two
python tools/evaluate.py --quick               # fewer days and services, a few seconds
python tools/evaluate.py --out docs/report.json --check
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from servers import Behaviour, closed_port, http_server, tcp_listener  # noqa: E402
from sim import T0, TICK, make_fleet, run_tracker, score  # noqa: E402
from upstatus.checks import run_check  # noqa: E402
from upstatus.config import Check, Config, Notify, Site  # noqa: E402
from upstatus.engine import record, run_round  # noqa: E402
from upstatus.model import Result  # noqa: E402
from upstatus.render import html_page, status_json  # noqa: E402
from upstatus.store import Store  # noqa: E402

SEED = 1000  # the development used other seeds; the numbers in the docs come from these


def med(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2)


# -- 1. real checks on loopback ---------------------------------------------------------------------------------------


def real(quick: bool) -> dict:
    n = 8 if quick else 20
    out: dict = {"runs_each": n}

    def http_chk(port: int, timeout: float = 3.0, **kw: object) -> Check:
        return Check(name="t", type="http", host="127.0.0.1", port=port, timeout=timeout, **kw)  # type: ignore[arg-type]

    delays = []
    with http_server(Behaviour()) as (port, state):
        for delay_ms in (0, 25, 100, 250):
            state.delay = delay_ms / 1000
            ms = [run_check(http_chk(port)).ms or 0.0 for _ in range(n)]
            delays.append(
                {
                    "server_delay_ms": delay_ms,
                    "measured_median_ms": med(ms),
                    "error_ms": round(statistics.median(ms) - delay_ms, 2),
                }
            )
        state.delay = 0
        out["http_latency"] = delays

        classes: dict[str, str] = {}
        classes["answer 200"] = run_check(http_chk(port)).detail
        state.status = 503
        classes["answer 503"] = run_check(http_chk(port)).detail
        state.status = 200
        classes["text not in the page"] = run_check(http_chk(port, contains="absent text")).detail
        state.delay = 1.5
        classes["no answer in time"] = run_check(http_chk(port, timeout=0.3)).detail
        state.delay = 0
    classes["nothing listening"] = run_check(http_chk(closed_port())).detail
    classes["name that does not exist"] = run_check(
        Check(name="t", type="dns", host="no-such-name.invalid", timeout=3.0)
    ).detail
    out["failure_phrases"] = classes

    timing: dict[str, dict] = {}
    with tcp_listener() as tcp_port:
        for name, chk in (
            ("tcp", Check(name="t", type="tcp", host="127.0.0.1", port=tcp_port, timeout=3.0)),
            ("dns (localhost)", Check(name="t", type="dns", host="localhost", timeout=3.0)),
        ):
            rows = [run_check(chk) for _ in range(n)]
            timing[name] = {"ok": sum(r.ok for r in rows), "of": n, "median_ms": med([r.ms or 0.0 for r in rows])}
    if shutil.which("ping"):
        rows = [run_check(Check(name="t", type="ping", host="127.0.0.1", timeout=3.0)) for _ in range(n)]
        timing["ping (loopback)"] = {
            "ok": sum(r.ok for r in rows),
            "of": n,
            "median_ms": med([r.ms or 0.0 for r in rows]),
        }
    out["timing"] = timing

    # a whole cycle through the engine: the real check runs every "minute" of a stepped clock
    with tempfile.TemporaryDirectory() as tmp, http_server(Behaviour()) as (port, state):
        chk = Check(
            name="svc",
            type="http",
            host="127.0.0.1",
            port=port,
            timeout=3.0,
            fail_after=3,
            recover_after=2,
            degrade=False,
        )
        cfg = Config(Site(output=Path(tmp) / "out", data=Path(tmp) / "data"), Notify(), (chk,))
        start = datetime(2026, 1, 1, tzinfo=UTC)
        tick = {"i": 0}
        now = lambda: start + timedelta(seconds=60 * tick["i"])  # noqa: E731
        up_rounds, down_rounds = 20, 10
        with Store(cfg.site.data) as store:
            for i in range(up_rounds + down_rounds + 10):
                tick["i"] = i
                state.status = 503 if up_rounds <= i < up_rounds + down_rounds else 200
                run_round(cfg, store, now=now)
            incidents = [inc for _, inc in store.incidents()]
            events = store.conn.execute("SELECT kind, ts, since FROM events ORDER BY id").fetchall()
        t0 = start.timestamp()
        out["cycle"] = {
            "rounds": up_rounds + down_rounds + 10,
            "failed_rounds": down_rounds,
            "events": [e[0] for e in events],
            "incident_start_error_min": round((incidents[0].start - (t0 + 60 * up_rounds)) / 60, 2),
            "incident_end_error_min": round((incidents[0].end - (t0 + 60 * (up_rounds + down_rounds))) / 60, 2),
            "down_decided_after_rounds": round((events[0][1] - (t0 + 60 * up_rounds)) / 60) + 1,
        }
    return out


# -- 2. the simulated fleet --------------------------------------------------------------------------------------------


def summarize(fleet: list, **params: object) -> dict:
    tot: dict = {
        k: 0
        for k in (
            "outages",
            "found",
            "false_down",
            "blip_down",
            "slow",
            "found_slow",
            "slow_small",
            "false_slow",
            "down_events",
        )
    }
    delays: list[float] = []
    start_err: list[float] = []
    end_err: list[float] = []
    slow_delays: list[float] = []
    down_s = true_s = raw_s = 0.0
    for svc in fleet:
        s = score(svc, run_tracker(svc, **params))
        for k in tot:
            tot[k] += s[k]
        delays += s["delays"]
        start_err += s["start_err"]
        end_err += s["end_err"]
        slow_delays += s["slow_delays"]
        down_s += s["tracker_down_s"]
        true_s += s["true_down_s"]
        raw_s += s["failed_checks_s"]
    return {
        **tot,
        "delay_checks_median": med(delays),
        "delay_checks_max": max(delays) if delays else None,
        "start_error_checks_max": max((abs(e) for e in start_err), default=None),
        "end_error_checks_max": max((abs(e) for e in end_err), default=None),
        "slow_delay_checks_median": med(slow_delays),
        "downtime_true_min": round(true_s / 60),
        "downtime_tracker_min": round(down_s / 60),
        "downtime_failed_checks_min": round(raw_s / 60),
    }


def pipeline(quick: bool) -> dict:
    days = 7 if quick else 30
    ticks = days * 1440
    with_events, quiet = (12, 8) if quick else (40, 20)
    fleet = make_fleet(SEED, with_events=with_events, quiet=quiet, ticks=ticks)
    total_checks = sum(len(s.results) for s in fleet)
    tracker = summarize(fleet)
    naive = summarize(fleet, fail_after=1, recover_after=1)
    quiet_fleet = [s for s in fleet if s.name.startswith("quiet")]
    q = summarize(quiet_fleet)
    qn = summarize(quiet_fleet, fail_after=1, recover_after=1)
    # a naive slowness rule: an alert for every single answer more than twice as slow as the median of its service
    naive_slow = 0
    quiet_answers = 0
    for svc in quiet_fleet:
        times = [ms for _, ok, ms in svc.results if ok and ms is not None]
        m = statistics.median(times)
        naive_slow += sum(1 for ms in times if ms > 2 * m)
        quiet_answers += len(times)
    return {
        "days": days,
        "interval_s": int(TICK),
        "services_with_events": with_events,
        "services_quiet": quiet,
        "checks": total_checks,
        "tracker": tracker,
        "naive_single_check": naive,
        "quiet_tracker": q,
        "quiet_naive": qn,
        "quiet_naive_slow_alerts": naive_slow,
        "quiet_answers": quiet_answers,
        "quiet_checks": sum(len(s.results) for s in quiet_fleet),
        "blips_injected": sum(len(s.blips) for s in fleet),
    }


def sweep(quick: bool) -> dict:
    days = 7 if quick else 30
    ticks = days * 1440
    flaky = make_fleet(SEED + 1, with_events=6 if quick else 10, quiet=0, ticks=ticks, flaky=100)
    steady = make_fleet(SEED + 2, with_events=6 if quick else 20, quiet=0, ticks=ticks)
    rows = []
    for n in (1, 2, 3, 4, 5):
        a = summarize(steady, fail_after=n)
        b = summarize(flaky, fail_after=n)
        rows.append(
            {
                "fail_after": n,
                "steady_found": a["found"],
                "steady_outages": a["outages"],
                "steady_false_outages": a["false_down"],
                "steady_blip_alerts": a["blip_down"],
                "steady_delay_checks_median": a["delay_checks_median"],
                "flaky_found": b["found"],
                "flaky_outages": b["outages"],
                "flaky_false_outages": b["false_down"],
                "flaky_delay_checks_median": b["delay_checks_median"],
            }
        )
    return {"days": days, "flaky_loss": 0.03, "steady_loss": 0.005, "rows": rows}


# -- 3. cost -----------------------------------------------------------------------------------------------------------


def cost(quick: bool) -> dict:
    n_checks, days = (20, 3) if quick else (50, 7)
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory() as tmp:
        checks = tuple(
            Check(name=f"Service {i:02d}", group=f"Group {i % 5}", type="tcp", host="10.0.0.1", port=80)
            for i in range(n_checks)
        )
        cfg = Config(Site(output=Path(tmp) / "out", data=Path(tmp) / "data"), Notify(), checks)
        now = T0 + days * 86400
        with Store(cfg.site.data) as store:
            t = time.perf_counter()
            rows = []
            for c in checks:
                for i in range(days * 1440):
                    ok = rng.random() > 0.004
                    rows.append(
                        Result(
                            c.name,
                            datetime.fromtimestamp(T0 + i * 60, UTC),
                            ok,
                            rng.uniform(5, 15) if ok else None,
                            "" if ok else "timeout",
                        )
                    )
                    if len(rows) >= 20000:
                        store.add_results(rows)
                        rows = []
            store.add_results(rows)
            insert_s = time.perf_counter() - t
            n_results = store.count_results()
            for k, c in enumerate(checks):  # a state and a few incidents each
                from upstatus.tracker import Event

                store.save_state(c.name, {"status": "up", "last_ms": 9.0, "since": T0})
                store.apply_events(
                    c.name,
                    [
                        Event("down", T0 + 86400 * (1 + k % 3), T0 + 86400 * (1 + k % 3) - 120),
                        Event("up", T0 + 86400 * (1 + k % 3) + 600, T0 + 86400 * (1 + k % 3) + 540),
                    ],
                )
            t = time.perf_counter()
            page = html_page(store, cfg, now)
            page_s = time.perf_counter() - t
            t = time.perf_counter()
            status_json(store, cfg, now)
            json_s = time.perf_counter() - t
            size = cfg.site.data.joinpath("upstatus.db").stat().st_size
            t = time.perf_counter()
            for i in range(200):
                record(store, checks[0], Result(checks[0].name, datetime.fromtimestamp(now + i, UTC), True, 8.0, ""))
            record_ms = (time.perf_counter() - t) / 200 * 1000
    return {
        "checks": n_checks,
        "days": days,
        "results": n_results,
        "insert_s": round(insert_s, 2),
        "page_s": round(page_s, 3),
        "page_kib": round(len(page.encode()) / 1024, 1),
        "json_s": round(json_s, 3),
        "db_mib": round(size / 1048576, 1),
        "bytes_per_result": round(size / n_results, 1),
        "record_ms": round(record_ms, 2),
        "mib_per_check_year_1min": round(size / n_results * 525600 / 1048576, 1),
    }


# -- main --------------------------------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", choices=("real", "pipeline", "sweep", "cost"))
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true", help="exit 1 if a claim of the README does not hold")
    args = ap.parse_args()

    report: dict = {
        "environment": {
            "python": platform.python_version(),
            "system": f"{platform.system()} {platform.machine()}",
            "quick": args.quick,
            "seed": SEED,
        }
    }
    steps = {"real": real, "pipeline": pipeline, "sweep": sweep, "cost": cost}
    for name, fn in steps.items():
        if args.only in (None, name):
            t = time.perf_counter()
            report[name] = fn(args.quick)
            print(f"[{name}] {time.perf_counter() - t:.1f} s", file=sys.stderr)

    problems: list[str] = []
    if "pipeline" in report:
        p = report["pipeline"]
        t, n = p["tracker"], p["naive_single_check"]
        print(f"outages found: {t['found']} of {t['outages']}")
        print(
            f"false outages: {t['false_down']} (single failed check: {n['false_down'] + n['blip_down']} outside real outages)"
        )
        print(
            f"outage alerts: {t['down_events']} (single failed check: {n['down_events']}) for {t['outages']} real outages"
        )
        print(f"slow periods found: {t['found_slow']} of {t['slow']}, false: {t['false_slow']}")
        print(
            f"downtime in minutes: true {t['downtime_true_min']}, upstatus {t['downtime_tracker_min']}, failed checks {t['downtime_failed_checks_min']}"
        )
        if t["found"] != t["outages"]:
            problems.append("an outage was missed")
        if t["false_down"] or t["false_slow"]:
            problems.append("a false outage or slow period")
        if t["found_slow"] < t["slow"] - 1:
            problems.append("slow periods missed")
        if t["delay_checks_median"] != 2:
            problems.append("detection delay is not the third failed check")
    if "real" in report:
        r = report["real"]
        c = r["cycle"]
        print(
            f"real cycle: events {c['events']}, start error {c['incident_start_error_min']} min, end error {c['incident_end_error_min']} min"
        )
        if (
            c["events"] != ["down", "up"]
            or abs(c["incident_start_error_min"]) > 0.01
            or abs(c["incident_end_error_min"]) > 0.01
        ):
            problems.append("the real cycle did not give one exact incident")
        expected = {
            "answer 200": "HTTP 200",
            "answer 503": "HTTP 503",
            "text not in the page": "text not found",
            "no answer in time": "timeout",
            "nothing listening": "connection refused",
            "name that does not exist": "name not found",
        }
        if r["failure_phrases"] != expected:
            problems.append("a failure was described wrongly")
    if "sweep" in report:
        row3 = report["sweep"]["rows"][2]
        # three random failures in a row on a 0.5%-loss service: about 0.1 expected in the whole fleet, so 1 or 2 is chance
        if row3["steady_found"] != row3["steady_outages"] or row3["steady_false_outages"] > 2:
            problems.append("default confirmation misses outages or invents too many on steady services")
    if "cost" in report:
        c = report["cost"]
        print(f"cost: {c['results']} results, page {c['page_s']} s, database {c['db_mib']} MiB")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
