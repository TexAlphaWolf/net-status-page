"""The decision: from a stream of results to the state of a service (up, degraded, down) and the events.

Pure and deterministic, so it is easy to test and to measure against a simulation. One failed check is not an outage:
a service is called down after ``fail_after`` failures in a row and up again after ``recover_after`` good checks.
The time an outage started is the first failed check, and it ended at the first good check of the recovery.
A service that is up but much slower than usual for ``slow_after`` checks in a row is called degraded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LAT_KEEP = 60  # latencies kept to know what "usual" is
LAT_MIN = 20  # latencies needed before slowness can be judged
REBASE = 180  # slow checks in a row after which the slow time is taken as the new usual one


@dataclass(frozen=True)
class Event:
    kind: str  # down, up, degraded, normal
    ts: float  # when it was decided
    since: float  # when it really started (down: first failure, up: first good check, degraded: first slow check)
    detail: str = ""


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


@dataclass
class Tracker:
    fail_after: int = 3
    recover_after: int = 2
    degrade: bool = True
    slow_factor: float = 3.0
    slow_ms: float = 100.0
    slow_after: int = 5
    status: str = "unknown"
    fails: int = 0
    oks: int = 0
    first_fail: float = 0.0
    first_ok: float = 0.0
    slow: int = 0
    slow_since: float = 0.0
    normal: int = 0
    first_normal: float = 0.0
    slow_run: int = 0
    lat: list[float] = field(default_factory=list)
    recent_slow: list[float] = field(default_factory=list)
    last_ms: float | None = None
    last_ts: float = 0.0
    since: float = 0.0  # when the current status began

    def feed(self, ts: float, ok: bool, ms: float | None = None, detail: str = "") -> list[Event]:
        self.last_ts = ts
        events: list[Event] = []
        if not ok:
            self.last_ms = None
            self.oks = 0
            self.fails += 1
            self.slow = 0
            self.normal = 0
            if self.fails == 1 and self.status != "down":
                self.first_fail = ts
            if self.status != "down" and self.fails >= self.fail_after:
                self.status = "down"
                self.since = self.first_fail
                events.append(Event("down", ts, self.first_fail, detail))
            return events
        self.last_ms = ms
        self.fails = 0
        self.oks += 1
        if self.oks == 1:
            self.first_ok = ts
        if self.status == "down":
            if self.oks >= self.recover_after:
                self.status = "up"
                self.since = self.first_ok
                self.slow = self.normal = self.slow_run = 0
                events.append(Event("up", ts, self.first_ok, ""))
            return events
        if self.status == "unknown":
            self.status = "up"
            self.since = ts
        if self.degrade and ms is not None:
            events.extend(self._latency(ts, ms))
        return events

    def _latency(self, ts: float, ms: float) -> list[Event]:
        base = median(self.lat) if len(self.lat) >= LAT_MIN else None
        slow = base is not None and ms > max(base * self.slow_factor, base + self.slow_ms)
        if self.status == "up":
            if slow:
                assert base is not None
                self.slow += 1
                if self.slow == 1:
                    self.slow_since = ts
                if self.slow >= self.slow_after:
                    self.status = "degraded"
                    self.since = self.slow_since
                    self.normal = 0
                    self.slow_run = self.slow
                    self.recent_slow = [ms]
                    return [Event("degraded", ts, self.slow_since, f"{ms:.0f} ms, usually {base:.0f} ms")]
            else:
                self.slow = 0
                self._learn(ms)
            return []
        # degraded
        if slow:
            self.normal = 0
            self.slow_run += 1
            self.recent_slow = [*self.recent_slow, ms][-LAT_KEEP:]
            if self.slow_run >= REBASE:
                self.lat = list(self.recent_slow)
                self.status = "up"
                self.since = ts
                self.slow = self.slow_run = 0
                return [Event("normal", ts, ts, f"the usual time is now {median(self.lat):.0f} ms")]
            return []
        self.normal += 1
        if self.normal == 1:
            self.first_normal = ts
        if self.normal >= self.slow_after:
            self.status = "up"
            self.since = self.first_normal
            self.slow = self.normal = self.slow_run = 0
            self._learn(ms)
            return [Event("normal", ts, self.first_normal, "")]
        return []

    def _learn(self, ms: float) -> None:
        self.lat.append(ms)
        del self.lat[:-LAT_KEEP]

    def to_dict(self) -> dict[str, object]:
        keys = [
            "status",
            "fails",
            "oks",
            "first_fail",
            "first_ok",
            "slow",
            "slow_since",
            "normal",
            "first_normal",
            "slow_run",
            "lat",
            "recent_slow",
            "last_ms",
            "last_ts",
            "since",
        ]
        return {k: getattr(self, k) for k in keys}

    @classmethod
    def from_dict(cls, raw: dict[str, object], **params: object) -> Tracker:
        t = cls(**params)  # type: ignore[arg-type]
        status = raw.get("status", "unknown")
        t.status = status if status in ("up", "degraded", "down", "unknown") else "unknown"
        for key in ("fails", "oks", "slow", "normal", "slow_run"):
            value = raw.get(key, 0)
            setattr(t, key, value if isinstance(value, int) and value >= 0 else 0)
        for key in ("first_fail", "first_ok", "slow_since", "first_normal", "last_ts", "since"):
            value = raw.get(key, 0.0)
            setattr(t, key, float(value) if isinstance(value, int | float) else 0.0)
        for key in ("lat", "recent_slow"):
            value = raw.get(key, [])
            if isinstance(value, list):
                setattr(t, key, [float(v) for v in value if isinstance(v, int | float)][-LAT_KEEP:])
        ms = raw.get("last_ms")
        t.last_ms = float(ms) if isinstance(ms, int | float) else None
        return t


@dataclass(frozen=True)
class Incident:
    kind: str  # down or degraded
    start: float
    end: float | None
    detail: str = ""


def incidents_from(events: list[Event]) -> list[Incident]:
    """Pair the events into incidents. The same rule the store applies, kept here so that it can be tested alone."""
    out: list[Incident] = []
    open_down: Event | None = None
    open_deg: Event | None = None

    def close(ev: Event | None, kind: str, end: float) -> None:
        if ev is not None:
            out.append(Incident(kind, ev.since, end, ev.detail))

    for ev in events:
        if ev.kind == "down":
            close(open_deg, "degraded", ev.since)
            open_deg = None
            open_down = ev
        elif ev.kind == "up":
            close(open_down, "down", ev.since)
            open_down = None
        elif ev.kind == "degraded":
            open_deg = ev
        elif ev.kind == "normal":
            close(open_deg, "degraded", ev.since)
            open_deg = None
    if open_down is not None:
        out.append(Incident("down", open_down.since, None, open_down.detail))
    if open_deg is not None:
        out.append(Incident("degraded", open_deg.since, None, open_deg.detail))
    return sorted(out, key=lambda i: i.start)
