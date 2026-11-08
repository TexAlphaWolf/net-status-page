"""The status page: one self-contained HTML file (no script, no external file) and a JSON file with the same facts.

The page shows names, never addresses: it is meant to be public.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from .config import Check, Config
from .model import fmt_pct, fmt_span, fmt_time
from .store import Store
from .tracker import Incident, median

DAY = 86400.0
HISTORY_DAYS = 90
SPARK_BUCKET = 900  # seconds: 96 points for 24 hours


@dataclass(frozen=True)
class Cell:
    day: str
    state: str  # ok, slow, down, none
    uptime: float | None
    down_s: float
    observed_s: float


@dataclass
class CheckView:
    check: Check
    status: str
    since: float
    ms: float | None
    uptime: float | None
    cells: list[Cell]
    spark: list[float]
    median_ms: float | None
    detail: str


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def day_cells(incidents: list[Incident], first_seen: float | None, now: float, days: int = HISTORY_DAYS) -> list[Cell]:
    """One cell per UTC day, the last one being today. Uptime is the time not in a confirmed outage."""
    midnight = now - now % DAY
    cells: list[Cell] = []
    if first_seen is None:
        return [
            Cell(datetime.fromtimestamp(midnight - i * DAY, UTC).strftime("%Y-%m-%d"), "none", None, 0.0, 0.0)
            for i in range(days - 1, -1, -1)
        ]
    for i in range(days - 1, -1, -1):
        start = midnight - i * DAY
        end = min(start + DAY, now)
        label = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d")
        seen_from = max(start, first_seen)
        observed = max(0.0, end - seen_from)
        if observed <= 0:
            cells.append(Cell(label, "none", None, 0.0, 0.0))
            continue
        down = sum(
            _overlap(seen_from, end, inc.start, now if inc.end is None else inc.end)
            for inc in incidents
            if inc.kind == "down"
        )
        slow = sum(
            _overlap(seen_from, end, inc.start, now if inc.end is None else inc.end)
            for inc in incidents
            if inc.kind == "degraded"
        )
        state = "down" if down > 0 else "slow" if slow > 0 else "ok"
        cells.append(Cell(label, state, 1 - min(down, observed) / observed, min(down, observed), observed))
    return cells


def overall_uptime(cells: list[Cell]) -> float | None:
    observed = sum(c.observed_s for c in cells)
    if observed <= 0:
        return None
    return 1 - sum(c.down_s for c in cells) / observed


def spark_points(rows: list[tuple[float, bool, float | None]], now: float) -> list[float]:
    """Median time per 15 minutes over the last 24 hours (buckets without an answer are skipped)."""
    buckets: dict[int, list[float]] = {}
    start = now - DAY
    for ts, ok, ms in rows:
        if ok and ms is not None and ts >= start:
            buckets.setdefault(int((ts - start) // SPARK_BUCKET), []).append(ms)
    return [median(v) for _, v in sorted(buckets.items())]


def views(store: Store, cfg: Config, now: float) -> list[CheckView]:
    out: list[CheckView] = []
    for check in cfg.checks:
        state = store.load_state(check.name)
        first = store.first_seen(check.name)
        incs = [i for _, i in store.incidents(check.name, now - HISTORY_DAYS * DAY)]
        cells = day_cells(incs, first, now)
        rows = store.results(check.name, now - DAY)
        spark = spark_points(rows, now)
        status = str(state.get("status", "unknown"))
        last_ms = state.get("last_ms")
        out.append(
            CheckView(
                check=check,
                status=status if status in ("up", "degraded", "down") else "unknown",
                since=float(state.get("since", 0.0) or 0.0),  # type: ignore[arg-type]
                ms=float(last_ms) if isinstance(last_ms, int | float) else None,
                uptime=overall_uptime(cells),
                cells=cells,
                spark=spark,
                median_ms=median(spark) if spark else None,
                detail=store.last_detail(check.name) if status == "down" else "",
            )
        )
    return out


def banner(vs: list[CheckView]) -> tuple[str, str]:
    """(class, text) for the whole site."""
    if not vs or all(v.status == "unknown" for v in vs):
        return "none", "Waiting for the first results"
    down = sum(v.status == "down" for v in vs)
    if down == len(vs):
        return "down", "Major outage"
    if down:
        return "down", "Partial outage"
    if any(v.status == "degraded" for v in vs):
        return "slow", "Degraded performance"
    return "ok", "All systems operational"


_LABEL = {"up": "Operational", "degraded": "Degraded", "down": "Down", "unknown": "No data yet"}
_CLASS = {"up": "ok", "degraded": "slow", "down": "down", "unknown": "none"}


def sparkline(points: list[float]) -> str:
    w, h = 160, 28
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = w / (len(points) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (p - lo) / span * (h - 4):.1f}" for i, p in enumerate(points))
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="response time, last 24 hours">'
        f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>'
    )


def _cell_title(c: Cell) -> str:
    if c.state == "none":
        return f"{c.day}: no data"
    text = f"{c.day}: {fmt_pct(c.uptime)} up"
    if c.down_s >= 1:
        text += f", down {fmt_span(c.down_s)}"
    if c.state == "slow":
        text += ", slow for a while"
    return text


def _incident_rows(store: Store, cfg: Config, now: float) -> list[tuple[str, Incident]]:
    allowed = {c.name for c in cfg.checks}
    rows = [(n, i) for n, i in store.incidents(None, now - 14 * DAY) if n in allowed]
    rows.sort(key=lambda r: r[1].start, reverse=True)
    return rows[:30]


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#17202e;--mute:#5b6678;--line:#e3e7ee;--ok:#1f9d55;--slow:#d98e04;--down:#d23a3a;--none:#cfd5df}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f141b;--card:#171e28;--fg:#e8edf4;--mute:#9aa6b8;--line:#263040;--ok:#34c274;--slow:#eab23a;--down:#ef6a6a;--none:#3a4556}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 48px}h1{font-size:26px;margin:0 0 4px}p.sub{margin:0 0 20px;color:var(--mute)}
.banner{border-radius:12px;padding:16px 20px;font-size:18px;font-weight:600;color:#fff;margin-bottom:24px}
.banner.ok{background:var(--ok)}.banner.slow{background:var(--slow)}.banner.down{background:var(--down)}.banner.none{background:var(--none);color:var(--fg)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:28px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 16px}
.row{padding:14px 0;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:0}
.top{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.name{font-weight:600;flex:1 1 auto}
.pill{font-size:12px;font-weight:600;border-radius:99px;padding:2px 10px;border:1px solid currentColor}
.pill.ok{color:var(--ok)}.pill.slow{color:var(--slow)}.pill.down{color:var(--down)}.pill.none{color:var(--mute)}
.meta{color:var(--mute);font-size:13px;display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-top:6px}
.bar{display:flex;gap:2px;margin-top:10px}.bar i{flex:1;height:26px;border-radius:2px;min-width:2px}
.bar i.ok{background:var(--ok)}.bar i.slow{background:var(--slow)}.bar i.down{background:var(--down)}.bar i.none{background:var(--none)}
.spark{color:var(--mute)}.legend{display:flex;justify-content:space-between;color:var(--mute);font-size:12px;margin-top:4px}
.inc{padding:10px 0;border-bottom:1px solid var(--line)}.inc:last-child{border-bottom:0}.inc b{font-weight:600}
.k{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin-right:6px}.k.down{color:var(--down)}.k.slow{color:var(--slow)}
footer{margin-top:32px;color:var(--mute);font-size:12px}
"""


def _row(v: CheckView, idx: int) -> str:
    cls = _CLASS[v.status]
    bar = "".join(f'<i class="{c.state}" title="{escape(_cell_title(c))}"></i>' for c in v.cells)
    meta = [f"<span>Uptime {escape(fmt_pct(v.uptime))} (90 days)</span>"]
    if v.status in ("up", "degraded") and v.ms is not None:
        meta.append(f"<span>Last answer {v.ms:.0f} ms</span>")
    if v.median_ms is not None:
        meta.append(f"<span>Median 24 h {v.median_ms:.0f} ms</span>")
    if v.status == "down" and v.detail:
        meta.append(f"<span>{escape(v.detail)}</span>")
    if v.since and v.status != "unknown":
        meta.append(f"<span>Since {escape(fmt_time(v.since))}</span>")
    spark = sparkline(v.spark)
    return (
        f'<div class="row" id="c{idx}"><div class="top"><span class="name">{escape(v.check.name)}</span>'
        f'<span class="pill {cls}">{_LABEL[v.status]}</span></div>'
        f'<div class="meta">{"".join(meta)}{spark}</div>'
        f'<div class="bar" role="img" aria-label="daily status, last 90 days">{bar}</div>'
        f'<div class="legend"><span>90 days ago</span><span>today</span></div></div>'
    )


def html_page(store: Store, cfg: Config, now: float) -> str:
    vs = views(store, cfg, now)
    cls, text = banner(vs)
    groups: dict[str, list[tuple[int, CheckView]]] = {}
    for i, v in enumerate(vs):
        groups.setdefault(v.check.group, []).append((i, v))
    sections = "".join(
        f'<h2>{escape(g)}</h2><div class="card">{"".join(_row(v, i) for i, v in items)}</div>'
        for g, items in groups.items()
    )
    incs = _incident_rows(store, cfg, now)
    if incs:
        lines = []
        for name, inc in incs:
            kind = "down" if inc.kind == "down" else "slow"
            word = "Outage" if inc.kind == "down" else "Slow"
            end = "still going" if inc.end is None else f"lasted {fmt_span(inc.end - inc.start)}"
            lines.append(
                f'<div class="inc"><span class="k {kind}">{word}</span><b>{escape(name)}</b> '
                f"from {escape(fmt_time(inc.start))}, {escape(end)}</div>"
            )
        history = "".join(lines)
    else:
        history = '<div class="inc">No incident in the last 14 days.</div>'
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    desc = f'<p class="sub">{escape(cfg.site.description)}</p>' if cfg.site.description else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>{escape(cfg.site.title)}</title><style>{CSS}</style></head><body><main>"
        f"<h1>{escape(cfg.site.title)}</h1>{desc}"
        f'<div class="banner {cls}">{escape(text)}</div>{sections}'
        f'<h2>Incidents, last 14 days</h2><div class="card" style="padding:4px 16px">{history}</div>'
        f"<footer>Updated {stamp}. Uptime counts the time a service was confirmed down; "
        f"a single failed check is not an outage.</footer></main></body></html>"
    )


def status_json(store: Store, cfg: Config, now: float) -> str:
    vs = views(store, cfg, now)
    cls, text = banner(vs)
    return json.dumps(
        {
            "generated": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": {"class": cls, "text": text},
            "checks": [
                {
                    "name": v.check.name,
                    "group": v.check.group,
                    "status": v.status,
                    "since": v.since or None,
                    "last_ms": None if v.ms is None else round(v.ms, 1),
                    "uptime_90d": None if v.uptime is None else round(v.uptime, 6),
                }
                for v in vs
            ],
            "incidents": [
                {"check": n, "kind": i.kind, "start": i.start, "end": i.end} for n, i in _incident_rows(store, cfg, now)
            ],
        },
        indent=2,
    )


def write_site(store: Store, cfg: Config, now: float) -> None:
    out = cfg.site.output
    out.mkdir(parents=True, exist_ok=True)
    for name, body in (("index.html", html_page(store, cfg, now)), ("status.json", status_json(store, cfg, now))):
        tmp = out / f".{name}.tmp"
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(out / name)  # a visitor never sees half a file
