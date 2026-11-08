import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from upstatus.config import Check, Config, Notify, Site
from upstatus.model import Result
from upstatus.render import (
    DAY,
    HISTORY_DAYS,
    CheckView,
    banner,
    day_cells,
    html_page,
    overall_uptime,
    spark_points,
    sparkline,
    status_json,
    views,
    write_site,
)
from upstatus.store import Store
from upstatus.tracker import Event, Incident

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC).timestamp()
MIDNIGHT = NOW - NOW % DAY


def chk(name="Website", group="Public", **kw):
    return Check(name=name, type="http", host="internal-host.lan", port=8443, group=group, path="/secret/path", **kw)


def cfg(tmp_path, *checks, title="Status", description=""):
    return Config(
        Site(title=title, description=description, output=tmp_path / "out", data=tmp_path / "d"), Notify(), checks
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "d") as s:
        yield s


def feed(store, name, values, start=None, step=300.0):
    start = NOW - 3600 if start is None else start
    rows = [
        Result(
            name, datetime.fromtimestamp(start + i * step, UTC), ms is not None, ms, "" if ms is not None else "timeout"
        )
        for i, ms in enumerate(values)
    ]
    store.add_results(rows)


def cv(status, name="c"):
    return CheckView(chk(name), status, 0.0, None, None, [], [], None, "")


def test_day_cells_without_data_and_with_a_first_seen():
    cells = day_cells([], None, NOW)
    assert len(cells) == HISTORY_DAYS and all(c.state == "none" for c in cells)
    cells = day_cells([], MIDNIGHT - DAY, NOW)  # seen since yesterday
    assert [c.state for c in cells[-3:]] == ["none", "ok", "ok"]
    assert cells[-1].day == "2026-06-15" and cells[-2].day == "2026-06-14"


def test_a_down_incident_colours_the_day_and_lowers_uptime():
    inc = Incident("down", MIDNIGHT - DAY + 3600, MIDNIGHT - DAY + 3600 + 1800)
    cells = day_cells([inc], MIDNIGHT - 5 * DAY, NOW)
    yesterday = cells[-2]
    assert yesterday.state == "down" and yesterday.down_s == 1800
    assert yesterday.uptime == pytest.approx(1 - 1800 / DAY)
    assert cells[-1].state == "ok"
    assert overall_uptime(cells) == pytest.approx(1 - 1800 / (5 * DAY + 12 * 3600))


def test_an_incident_across_midnight_is_split_between_two_days():
    inc = Incident("down", MIDNIGHT - 1800, MIDNIGHT + 1800)
    cells = day_cells([inc], MIDNIGHT - 5 * DAY, NOW)
    assert (cells[-2].down_s, cells[-1].down_s) == (1800, 1800)


def test_an_open_incident_lasts_until_now():
    cells = day_cells([Incident("down", NOW - 600, None)], NOW - DAY, NOW)
    assert cells[-1].down_s == 600


def test_a_slow_period_is_yellow_not_downtime():
    cells = day_cells([Incident("degraded", MIDNIGHT + 100, MIDNIGHT + 4000)], MIDNIGHT - DAY, NOW)
    assert cells[-1].state == "slow" and cells[-1].down_s == 0 and cells[-1].uptime == 1.0


def test_overall_uptime_without_observation():
    assert overall_uptime(day_cells([], None, NOW)) is None


def test_banner_texts():
    assert banner([])[1] == "Waiting for the first results"
    assert banner([cv("unknown")]) == ("none", "Waiting for the first results")
    assert banner([cv("up"), cv("up")]) == ("ok", "All systems operational")
    assert banner([cv("up"), cv("degraded")]) == ("slow", "Degraded performance")
    assert banner([cv("up"), cv("down")]) == ("down", "Partial outage")
    assert banner([cv("down"), cv("down")]) == ("down", "Major outage")
    assert banner([cv("degraded"), cv("down")]) == ("down", "Partial outage")


def test_spark_points_are_medians_per_15_minutes():
    start = NOW - DAY
    rows = [
        (start + 10, True, 10.0),
        (start + 20, True, 30.0),
        (start + 30, True, 20.0),
        (start + 1000, True, 50.0),
        (start + 1100, False, None),
        (start - 5, True, 999.0),
    ]
    assert spark_points(rows, NOW) == [20.0, 50.0]


def test_sparkline_needs_two_points():
    assert sparkline([5.0]) == "" and sparkline([]) == ""
    svg = sparkline([1.0, 5.0, 3.0])
    assert svg.startswith("<svg") and "polyline" in svg and "<script" not in svg
    assert "nan" not in sparkline([2.0, 2.0])  # a flat line does not divide by zero


def test_views_of_a_working_a_down_and_a_new_service(tmp_path, store):
    c = cfg(tmp_path, chk("A"), chk("B"), chk("C"))
    feed(store, "A", [10.0] * 10)
    store.save_state("A", {"status": "up", "last_ms": 10.0, "since": NOW - 3000})
    feed(store, "B", [None] * 5)
    store.save_state("B", {"status": "down", "since": NOW - 1500})
    store.apply_events("B", [Event("down", NOW - 1200, NOW - 1500, "timeout")])
    vs = views(store, c, NOW)
    assert [v.status for v in vs] == ["up", "down", "unknown"]
    assert vs[0].median_ms == 10.0 and vs[0].uptime == 1.0
    assert vs[1].detail == "timeout" and vs[1].uptime < 1.0
    assert vs[2].uptime is None


def build(tmp_path, store, **kw):
    feed(store, "Website", [12.0] * 12)
    store.save_state("Website", {"status": "up", "last_ms": 12.0, "since": NOW - 3000})
    return html_page(store, cfg(tmp_path, chk(), **kw), NOW)


def test_page_is_self_contained_and_does_not_leak_addresses(tmp_path, store):
    page = build(tmp_path, store)
    assert (
        "<script" not in page.lower()
        and "http://" not in page
        and "https://" not in page.replace("https://www.w3.org", "")
    )
    assert "default-src 'none'" in page
    for secret in ("internal-host.lan", "8443", "/secret/path"):
        assert secret not in page
    assert "All systems operational" in page and "Website" in page and "Operational" in page
    assert "No incident in the last 14 days." in page
    assert '<i class="ok"' in page


def test_page_escapes_names_titles_and_descriptions(tmp_path, store):
    nasty = "<img src=x onerror=alert(1)>&\"'"
    c = cfg(tmp_path, chk(nasty, group=nasty), title=nasty, description=nasty)
    feed(store, nasty, [5.0] * 3)
    page = html_page(store, c, NOW)
    assert "<img" not in page and "onerror=alert(1)>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_page_lists_incidents_and_groups(tmp_path, store):
    c = cfg(tmp_path, chk("Web", group="Public"), chk("DB", group="Internal"))
    feed(store, "Web", [5.0] * 5)
    store.apply_events("Web", [Event("down", NOW - 7000, NOW - 7200, "timeout"), Event("up", NOW - 5000, NOW - 5400)])
    store.apply_events("DB", [Event("degraded", NOW - 900, NOW - 1000, "x")])
    store.save_state("Web", {"status": "up"})
    store.save_state("DB", {"status": "degraded", "since": NOW - 1000})
    page = html_page(store, c, NOW)
    assert page.index("Public") < page.index("Internal")
    assert "Outage" in page and "lasted 30 min" in page and "still going" in page
    assert "Degraded performance" in page and "Degraded" in page


def test_incidents_of_checks_removed_from_the_config_are_not_shown(tmp_path, store):
    store.apply_events("Gone", [Event("down", NOW - 100, NOW - 200, "x")])
    feed(store, "Website", [1.0] * 3)
    page = html_page(store, cfg(tmp_path, chk()), NOW)
    assert "Gone" not in page


def test_status_json(tmp_path, store):
    feed(store, "Website", [12.0] * 12)
    feed(store, "Website", [12.0], start=NOW - 3 * DAY)  # the history starts three days ago
    store.save_state("Website", {"status": "up", "last_ms": 12.34, "since": NOW - 3000})
    store.apply_events(
        "Website", [Event("down", NOW - 7000, NOW - 7200, "timeout"), Event("up", NOW - 5000, NOW - 5400)]
    )
    data = json.loads(status_json(store, cfg(tmp_path, chk()), NOW))
    assert data["status"] == {"class": "ok", "text": "All systems operational"}
    (c,) = data["checks"]
    assert c["name"] == "Website" and c["status"] == "up" and c["last_ms"] == 12.3 and 0.99 < c["uptime_90d"] < 1
    assert data["incidents"][0]["kind"] == "down"
    assert "internal-host.lan" not in json.dumps(data)


def test_write_site_writes_both_files_atomically(tmp_path, store):
    c = cfg(tmp_path, chk())
    feed(store, "Website", [1.0] * 3)
    write_site(store, c, NOW)
    write_site(store, c, NOW)
    out = Path(c.site.output)
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "status.json"]  # no temporary file left
    assert out.joinpath("index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
