"""Write docs/evaluation.md from docs/report.json (run from the repository root)."""

import json
from pathlib import Path

d = json.loads(Path("docs/report.json").read_text())
E_, P, S, C, R = d["environment"], d["pipeline"], d["sweep"], d["cost"], d["real"]
T, N, Q, QN = P["tracker"], P["naive_single_check"], P["quiet_tracker"], P["quiet_naive"]
rows = "\n".join(
    f"| {r['fail_after']} | {r['steady_found']} of {r['steady_outages']} | {r['steady_false_outages']} | {r['steady_blip_alerts']} | "
    f"{r['steady_delay_checks_median']:g} | {r['flaky_found']} of {r['flaky_outages']} | {r['flaky_false_outages']} |"
    for r in S["rows"]
)
lat = "\n".join(
    f"| {x['server_delay_ms']} ms | {x['measured_median_ms']:g} ms | {x['error_ms']:+g} ms |" for x in R["http_latency"]
)
phr = "\n".join(f"| {k} | `{v}` |" for k, v in R["failure_phrases"].items())
tim = "\n".join(f"| {k} | {v['ok']} of {v['of']} | {v['median_ms']:g} ms |" for k, v in R["timing"].items())
cyc = R["cycle"]
txt = f"""# Evaluation

Produced by `python tools/evaluate.py --out docs/report.json` (machine: {E_["system"]}, Python {E_["python"]}, seed {E_["seed"]}).

**What this is and is not.** The checks are measured against real servers on loopback (section 1). Whether the
*decisions* are right needs a situation where the truth is known, and no real service fails on command, so
`tests/sim.py` generates a fleet of services that are checked once a minute, with outages, short blips, slow periods
and random failed checks written down **before** the tracker sees a single result. The truth is compared with what the
tracker decided, independently of the tracker's own code. This shows that the machinery does what it claims. It does
not show how often a real network behaves like the simulator: random failures that are independent from one check to
the next are an assumption (see [limitations](limitations.md)). Seeds used while developing were other ones; the numbers
below use seed {E_["seed"]} and the next two.

## 1. The checks on real servers (loopback)

A web server on loopback that waits a set time before answering, {R["runs_each"]} checks per row.

| the server waits | measured median | error |
|---|---|---|
{lat}

The error is the cost of the check itself: connection, request, answer. What a failure is called, with the real
failure each time:

| situation | what the check reports |
|---|---|
{phr}

Other kinds of check against loopback, {R["runs_each"]} runs each:

| check | answered | median time |
|---|---|---|
{tim}

**A whole cycle through the engine.** The real check ran every minute of a stepped clock for {cyc["rounds"]} rounds; the server
answered 503 for {cyc["failed_rounds"]} of them. Events: {" then ".join(cyc["events"])}. The outage was decided at the
{cyc["down_decided_after_rounds"]}rd failed check, and the incident starts at the first failed check (error {cyc["incident_start_error_min"]:g} min)
and ends at the first good check of the recovery (error {cyc["incident_end_error_min"]:g} min).

## 2. A simulated fleet with known truth

{P["services_with_events"]} services with events and {P["services_quiet"]} quiet ones, {P["days"]} days, one check a minute: {P["checks"]:,} checks. Failed checks
at random: 0.5% of all checks. The events: real outages of 6 to 180 minutes, {P["blips_injected"]} blips of 1 or 2 minutes,
and slow periods (4 to 8 times slower for 15 to 90 minutes). The baseline is "one failed check is an outage, one good
check ends it".

| | upstatus | single failed check |
|---|---|---|
| real outages found | {T["found"]} of {T["outages"]} | {N["found"]} of {N["outages"]} |
| outage alerts sent | {T["down_events"]} | {N["down_events"]:,} |
| outages that did not exist | {T["false_down"]} | {N["false_down"]:,} |
| median delay from the real start to the alert | {T["delay_checks_median"]:g} checks | {N["delay_checks_median"]:g} checks |
| start of the outage, worst error | {T["start_error_checks_max"]:g} check | {N["start_error_checks_max"]:g} check |
| end of the outage, worst error | {T["end_error_checks_max"]:g} check | {N["end_error_checks_max"]:g} check |
| downtime reported, minutes (truth {T["downtime_true_min"]:,}) | {T["downtime_tracker_min"]:,} | {N["downtime_tracker_min"]:,} |
| slow periods found (those meant to be found) | {T["found_slow"]} of {T["slow"]} | - |
| slow periods that did not exist | {T["false_slow"]} | - |

- The {T["down_events"] - T["outages"]} alerts above the {T["outages"]} real outages are blips of two minutes that happened next to a random failed check:
  three failures in a row look like an outage, and they were one.
- The downtime is {T["downtime_true_min"] - T["downtime_tracker_min"]} minutes short of the truth: that is the blips shorter than three checks, which are left out on purpose.
  The failed checks counted one by one overstate it by {N["downtime_tracker_min"] - T["downtime_true_min"]:,} minutes.
- A slow period is meant to be found when it is at least 3 times the usual time **and** 100 ms more. {T["slow_small"]} more
  injected periods were below that (a service that goes from 8 ms to 40 ms is still fast) and are left alone on purpose.
- The median delay of a slow period is {T["slow_delay_checks_median"]:g} checks after it began (the rule needs 5 slow checks in a row).
- On the {P["services_quiet"]} quiet services ({P["quiet_checks"]:,} checks, only random failures): upstatus raised {Q["down_events"]} outages and {Q["false_slow"]} slow periods;
  the single-failed-check rule raised {QN["down_events"]:,} outages, and an alert for every answer more than twice the usual time would have raised
  {P["quiet_naive_slow_alerts"]:,} alerts for {P["quiet_answers"]:,} answers.

## 3. The price of confirmation

`fail_after` is the number of failed checks in a row before a service is called down. Two fleets: 20 steady services
(0.5% random failures) and 10 flaky ones (3% random failures), {S["days"]} days each, with real outages and blips.

| `fail_after` | steady: found | steady: false outages | steady: blips alerted | steady: median delay (checks) | flaky: found | flaky: false outages |
|---|---|---|---|---|---|---|
{rows}

The default is 3. It finds everything and has one false outage in the steady fleet (three random failures in a row,
about 0.1 are expected in that many checks), and a few on services that fail 3% of the time. Waiting for 4 removes
those at the price of one more minute of delay. With 1 every random failed check is an alert. Raise it for a
service that is flaky by nature; lower it only where a minute matters more than a false alarm.

## 4. Cost

{C["checks"]} checks, {C["days"]} days of one check a minute ({C["results"]:,} results):

| step | result |
|---|---|
| store the results | {C["insert_s"]:g} s |
| build the page (and `status.json`) | {C["page_s"]:g} s ({C["page_kib"]:g} KiB; the JSON file {C["json_s"]:g} s) |
| record one result: read the state, decide, write | {C["record_ms"]:g} ms |
| the history file | {C["db_mib"]:g} MiB: {C["bytes_per_result"]:g} bytes per result, about {C["mib_per_check_year_1min"]:g} MiB per check and year at one a minute |

Results older than `retention_days` are deleted; the incidents and the daily figures stay.
"""
Path("docs/evaluation.md").write_text(txt)
