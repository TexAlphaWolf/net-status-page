"""A simulated fleet with known truth: services that are checked once a minute, with outages, blips, slow periods and
random failed checks written down BEFORE the tracker sees a single result. Used by the tests and by tools/evaluate.py."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

TICK = 60.0  # seconds between checks
T0 = 1_760_000_000.0  # an arbitrary start, aligned to a minute


@dataclass
class Window:
    start: int  # tick index, inclusive
    end: int  # tick index, exclusive
    factor: float = 1.0  # slow periods: how many times slower

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Service:
    name: str
    ticks: int
    base_ms: float
    loss: float  # probability that a single check fails for no reason
    outages: list[Window] = field(default_factory=list)  # long enough to be real (>= 6 checks)
    blips: list[Window] = field(default_factory=list)  # 1 or 2 checks
    slow: list[Window] = field(default_factory=list)
    results: list[tuple[float, bool, float | None]] = field(default_factory=list)

    def ts(self, tick: int) -> float:
        return T0 + tick * TICK


def _free(taken: list[Window], start: int, end: int, gap: int = 30) -> bool:
    return all(end + gap <= w.start or start >= w.end + gap for w in taken)


def make_service(
    rng: random.Random,
    name: str,
    ticks: int,
    *,
    outages: int = 0,
    blips: int = 0,
    slow: int = 0,
    loss: float = 0.005,
    flaky: bool = False,
) -> Service:
    base = rng.choice([8.0, 20.0, 45.0, 90.0, 180.0])
    svc = Service(name, ticks, base, 0.03 if flaky else loss)
    taken: list[Window] = []

    def place(kind: str, count: int, low: int, high: int) -> None:
        tries = 0
        while count > 0 and tries < 2000:
            tries += 1
            length = rng.randint(low, high)
            start = rng.randint(60, ticks - length - 60)
            if _free(taken, start, start + length):
                factor = rng.uniform(4, 8) if kind == "slow" else 1.0
                w = Window(start, start + length, factor)
                taken.append(w)
                getattr(svc, {"outage": "outages", "blip": "blips", "slow": "slow"}[kind]).append(w)
                count -= 1

    place("outage", outages, 6, 180)
    place("blip", blips, 1, 2)
    place("slow", slow, 15, 90)
    down = [False] * ticks
    for w in svc.outages + svc.blips:
        for i in range(w.start, w.end):
            down[i] = True
    factor = [1.0] * ticks
    for w in svc.slow:
        for i in range(w.start, w.end):
            factor[i] = w.factor
    for i in range(ticks):
        ts = svc.ts(i)
        if down[i] or rng.random() < svc.loss:
            svc.results.append((ts, False, None))
            continue
        ms = base * factor[i] * rng.lognormvariate(0, 0.08)
        if rng.random() < 0.003:  # a single slow answer now and then is normal
            ms *= rng.uniform(2.5, 6)
        svc.results.append((ts, True, ms))
    return svc


def make_fleet(seed: int, *, with_events: int, quiet: int, ticks: int, flaky: int = 0) -> list[Service]:
    rng = random.Random(seed)
    fleet: list[Service] = []
    per_day = ticks / 1440
    for i in range(with_events):
        n = max(1, round(per_day / 6))
        fleet.append(
            make_service(rng, f"svc-{i:03d}", ticks, outages=n, blips=2 * n, slow=max(1, n // 2), flaky=i < flaky)
        )
    for i in range(quiet):
        fleet.append(make_service(rng, f"quiet-{i:03d}", ticks))
    return fleet


# -- running and scoring --------------------------------------------------------------------------------------------


def run_tracker(svc: Service, **params: object):
    from upstatus.tracker import Event, Tracker

    t = Tracker(**params)  # type: ignore[arg-type]
    events: list[Event] = []
    for ts, ok, ms in svc.results:
        events += t.feed(ts, ok, ms)
    return events


def _overlaps(a0: float, a1: float, b0: float, b1: float, slack: float = 0.0) -> bool:
    return a0 < b1 + slack and b0 < a1 + slack


def score(svc: Service, events: list) -> dict:
    """Compare what the tracker decided with what was injected."""
    from upstatus.tracker import incidents_from

    end_of_time = svc.ts(svc.ticks)
    incs = incidents_from(events)
    down = [i for i in incs if i.kind == "down"]
    slow = [i for i in incs if i.kind == "degraded"]

    def window(w: Window) -> tuple[float, float]:
        return svc.ts(w.start), svc.ts(w.end)

    def span(i) -> tuple[float, float]:
        return i.start, end_of_time if i.end is None else i.end

    out: dict = {
        "outages": len(svc.outages),
        "found": 0,
        "delays": [],
        "start_err": [],
        "end_err": [],
        "false_down": 0,
        "blip_down": 0,
        "slow": len(svc.slow),
        "found_slow": 0,
        "slow_delays": [],
        "false_slow": 0,
        "down_events": sum(e.kind == "down" for e in events),
    }
    down_by_event = [e for e in events if e.kind == "down"]
    for w in svc.outages:
        a, b = window(w)
        hit = [i for i in down if _overlaps(*span(i), a, b)]
        if hit:
            out["found"] += 1
            first = min(hit, key=lambda i: i.start)
            out["start_err"].append((first.start - a) / TICK)
            last = max(hit, key=lambda i: span(i)[1])
            if last.end is not None:
                out["end_err"].append((last.end - b) / TICK)
            ev = next(e for e in down_by_event if e.since == first.start)
            out["delays"].append((ev.ts - a) / TICK)
    blip_w = [window(w) for w in svc.blips]
    out_w = [window(w) for w in svc.outages]
    for i in down:
        s0, s1 = span(i)
        if any(_overlaps(s0, s1, a, b, TICK * 2) for a, b in out_w):
            continue
        if any(_overlaps(s0, s1, a, b, TICK * 2) for a, b in blip_w):
            out["blip_down"] += 1
        else:
            out["false_down"] += 1
    slow_w = [window(w) for w in svc.slow]
    deg_events = [e for e in events if e.kind == "degraded"]
    # a slow period is meant to be found only when it is slow by the rule of the tracker: at least 3 times the usual
    # time AND 100 ms more (a service that goes from 8 ms to 40 ms is still fast, and says so on purpose)
    detectable = [w for w in svc.slow if svc.base_ms * w.factor > max(svc.base_ms * 3, svc.base_ms + 100)]
    out["slow"] = len(detectable)
    out["slow_small"] = len(svc.slow) - len(detectable)
    for w, (a, b) in zip(svc.slow, slow_w, strict=True):
        if w not in detectable:
            continue
        hit = [i for i in slow if _overlaps(*span(i), a, b)]
        if hit:
            out["found_slow"] += 1
            first = min(hit, key=lambda i: i.start)
            ev = next(e for e in deg_events if e.since == first.start)
            out["slow_delays"].append((ev.ts - a) / TICK)
    for i in slow:
        s0, s1 = span(i)
        if not any(_overlaps(s0, s1, a, b, TICK * 3) for a, b in slow_w + out_w):
            out["false_slow"] += 1
    out["tracker_down_s"] = sum(span(i)[1] - span(i)[0] for i in down)
    out["true_down_s"] = sum(w.length for w in svc.outages + svc.blips) * TICK
    out["failed_checks_s"] = sum(1 for _, ok, _ in svc.results if not ok) * TICK
    return out
