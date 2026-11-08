"""Render docs/demo.png: the status page for a SYNTHETIC set of services (60 days of simulated checks, one service
down and one slow right now). Nothing here is a real service.

pip install -e ".[dev]" && playwright install chromium
python tools/screenshot.py
"""

from __future__ import annotations

import random
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from sim import T0, make_service, run_tracker  # noqa: E402
from upstatus.config import Check, Config, Notify, Site  # noqa: E402
from upstatus.engine import tracker_for  # noqa: E402
from upstatus.model import Result  # noqa: E402
from upstatus.render import write_site  # noqa: E402
from upstatus.store import Store  # noqa: E402
from upstatus.tracker import Tracker  # noqa: E402

SERVICES = [
    ("Public", "Website", 45.0),
    ("Public", "Customer portal", 90.0),
    ("Public", "API", 20.0),
    ("Public", "Website certificate", 180.0),
    ("Internal", "Mail server", 20.0),
    ("Internal", "VPN gateway", 45.0),
    ("Internal", "File server", 8.0),
    ("Network", "Core router", 8.0),
    ("Network", "DNS resolver", 8.0),
]
DAYS = 60


def main() -> None:
    rng = random.Random(11)
    ticks = DAYS * 1440
    target_end = datetime(2026, 11, 8, 22, 0, tzinfo=UTC).timestamp()
    shift = target_end - (T0 + ticks * 60)  # the simulated days end "now"
    end_ts = target_end
    out = ROOT / "docs" / "demo.png"
    with tempfile.TemporaryDirectory() as tmp:
        checks = tuple(
            Check(name=name, group=group, type="tcp", host=f"198.51.100.{i + 10}", port=443)
            for i, (group, name, _) in enumerate(SERVICES)
        )
        cfg = Config(
            Site(
                title="Acme status",
                description="Live status of the services we run.",
                output=Path(tmp) / "out",
                data=Path(tmp) / "d",
            ),
            Notify(),
            checks,
        )
        with Store(cfg.site.data) as store:
            for i, ((_, name, base), check) in enumerate(zip(SERVICES, checks, strict=True)):
                svc = make_service(
                    rng,
                    name,
                    ticks,
                    outages=2 if i % 3 == 0 else 1 if i % 2 else 0,
                    blips=3,
                    slow=1 if i in (1, 5) else 0,
                )
                svc.base_ms = base
                rows = list(svc.results)
                if name == "Mail server":  # down right now
                    rows[-6:] = [(ts, False, None) for ts, _, _ in rows[-6:]]
                if name == "Customer portal":  # slow right now
                    rows[-25:] = [(ts, True, ms * 10 if ms else base * 10) for ts, ok, ms in rows[-25:]]
                rows = [(ts + shift, ok, ms) for ts, ok, ms in rows]
                svc.results = rows
                events = run_tracker(
                    svc,
                    **{
                        k: getattr(check, k)
                        for k in ("fail_after", "recover_after", "degrade", "slow_factor", "slow_ms", "slow_after")
                    },
                )
                keep = [r for j, r in enumerate(rows) if j % 30 == 0 or j >= len(rows) - 1440 or r[0] == rows[0][0]]
                store.add_results(
                    [
                        Result(name, datetime.fromtimestamp(ts, UTC), ok, ms, "" if ok else "timeout")
                        for ts, ok, ms in keep
                    ]
                )
                tracker = Tracker(
                    **{
                        k: getattr(check, k)
                        for k in ("fail_after", "recover_after", "degrade", "slow_factor", "slow_ms", "slow_after")
                    }
                )
                for ts, ok, ms in rows:
                    tracker.feed(ts, ok, ms)
                store.save_state(name, tracker.to_dict())
                store.apply_events(name, events)
                assert tracker_for(store, check).status == tracker.status
            write_site(store, cfg, end_ts)
        html = (cfg.site.output / "index.html").read_text(encoding="utf-8")
        assert "198.51.100" not in html
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 1200}, device_scale_factor=2)
            page.set_content(html)
            page.screenshot(path=str(out), full_page=True, clip={"x": 0, "y": 0, "width": 900, "height": 1000})
            browser.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
