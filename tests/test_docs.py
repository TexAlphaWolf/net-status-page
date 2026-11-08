"""The numbers quoted in the docs must be the ones in docs/report.json, and the documented things must exist."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from upstatus.cli import build_parser
from upstatus.config import EXAMPLE, parse_config

ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "docs" / "report.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
EVAL = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
P = REPORT["pipeline"]
T, N = P["tracker"], P["naive_single_check"]


def test_report_is_a_full_run():
    assert REPORT["environment"]["quick"] is False
    assert (P["days"], P["services_with_events"], P["services_quiet"]) == (30, 40, 20)
    assert REPORT["environment"]["seed"] >= 1000


def test_the_claims_are_true_in_the_report():
    assert T["found"] == T["outages"] and T["false_down"] == 0 and T["false_slow"] == 0
    assert T["found_slow"] == T["slow"]
    assert T["delay_checks_median"] == 2
    assert N["down_events"] > 10 * T["down_events"]
    assert abs(T["downtime_tracker_min"] - T["downtime_true_min"]) / T["downtime_true_min"] < 0.04
    assert N["downtime_tracker_min"] > 1.5 * T["downtime_true_min"]
    assert P["quiet_tracker"]["down_events"] == 0
    cycle = REPORT["real"]["cycle"]
    assert (
        cycle["events"] == ["down", "up"] and cycle["incident_start_error_min"] == 0 == cycle["incident_end_error_min"]
    )
    assert len(REPORT["real"]["failure_phrases"]) == 6
    rows = {r["fail_after"]: r for r in REPORT["sweep"]["rows"]}
    assert rows[3]["steady_found"] == rows[3]["steady_outages"] and rows[3]["steady_false_outages"] <= 2
    assert rows[1]["steady_false_outages"] > 100 and rows[4]["steady_false_outages"] == 0


def test_numbers_in_the_readme():
    assert f"**{N['down_events']:,}** outage alerts for **{T['outages']}**" in README
    assert f"**{T['found']} of {T['outages']}** real outages, raised {T['down_events']} alerts" in README
    pct_below = round((T["downtime_true_min"] - T["downtime_tracker_min"]) / T["downtime_true_min"] * 100)
    pct_over = round((N["downtime_tracker_min"] - T["downtime_true_min"]) / T["downtime_true_min"] * 100)
    assert f"{pct_below}% below the truth" in README and f"overstated it by {pct_over}%" in README
    assert f"{P['services_with_events'] + P['services_quiet']} services" in README
    assert f"outages found **{T['found']} of {T['outages']}**" in README
    assert f"a median of {T['delay_checks_median']:g} checks" in README
    assert f"slow periods found **{T['found_slow']} of {T['slow']}**" in README
    c = REPORT["cost"]
    assert f"({c['results']:,} results) are written in {c['insert_s']:.1f} s" in README
    assert f"the page takes {c['page_s']:g} s" in README
    assert f"about {c['mib_per_check_year_1min']:.0f} MiB per check and year" in README
    errors = [x["error_ms"] for x in REPORT["real"]["http_latency"] if x["server_delay_ms"] > 0]
    assert "adds about 1 ms" in README and all(0 <= e < 2 for e in errors)


def test_the_evaluation_document_is_generated_from_the_report(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "report.json").write_text((ROOT / "docs" / "report.json").read_text(encoding="utf-8"))
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_evaluation_doc.py")], cwd=tmp_path, check=True)
    assert (tmp_path / "docs" / "evaluation.md").read_text(encoding="utf-8") == EVAL


def test_every_command_in_the_readme_exists_and_the_other_way_round():
    commands = set(build_parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr,attr-defined]
    documented = set(re.findall(r"^\| `([a-z-]+)[ `]", README, re.MULTILINE)) - {"http", "tcp", "dns", "tls", "ping"}
    assert documented == commands


def test_the_check_types_in_the_readme_are_the_real_ones():
    from upstatus.model import CHECK_TYPES

    in_table = set(re.findall(r"^\| `(http|tcp|dns|tls|ping)` \|", README, re.MULTILINE))
    assert in_table == set(CHECK_TYPES)


def test_the_readme_configuration_is_valid():
    block = re.search(r"```toml\n(.*?)```", README, re.DOTALL)
    assert block
    cfg = parse_config(block.group(1), Path("/x"))
    assert [c.type for c in cfg.checks] == ["http", "ping"]
    assert cfg.notify.webhook_env == "UPSTATUS_WEBHOOK"


def test_the_example_configurations_load():
    cfg = parse_config((ROOT / "examples" / "upstatus.toml").read_text(encoding="utf-8"), Path("/x"))
    assert {c.type for c in cfg.checks} == {"http", "tls", "dns", "ping", "tcp"}
    assert cfg.checks[-1].ca_file and cfg.notify.on == ("down", "up", "degraded")
    assert len(parse_config(EXAMPLE, Path("/x")).checks) == 5
