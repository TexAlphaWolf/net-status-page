# Evaluation

Produced by `python tools/evaluate.py --out docs/report.json` (machine: Linux x86_64, Python 3.13.16, seed 1000).

**What this is and is not.** The checks are measured against real servers on loopback (section 1). Whether the
*decisions* are right needs a situation where the truth is known, and no real service fails on command, so
`tests/sim.py` generates a fleet of services that are checked once a minute, with outages, short blips, slow periods
and random failed checks written down **before** the tracker sees a single result. The truth is compared with what the
tracker decided, independently of the tracker's own code. This shows that the machinery does what it claims. It does
not show how often a real network behaves like the simulator: random failures that are independent from one check to
the next are an assumption (see [limitations](limitations.md)). Seeds used while developing were other ones; the numbers
below use seed 1000 and the next two.

## 1. The checks on real servers (loopback)

A web server on loopback that waits a set time before answering, 20 checks per row.

| the server waits | measured median | error |
|---|---|---|
| 0 ms | 0.41 ms | +0.41 ms |
| 25 ms | 25.91 ms | +0.91 ms |
| 100 ms | 101.25 ms | +1.25 ms |
| 250 ms | 251.13 ms | +1.13 ms |

The error is the cost of the check itself: connection, request, answer. What a failure is called, with the real
failure each time:

| situation | what the check reports |
|---|---|
| answer 200 | `HTTP 200` |
| answer 503 | `HTTP 503` |
| text not in the page | `text not found` |
| no answer in time | `timeout` |
| nothing listening | `connection refused` |
| name that does not exist | `name not found` |

Other kinds of check against loopback, 20 runs each:

| check | answered | median time |
|---|---|---|
| tcp | 17 of 20 | 0.01 ms |
| dns (localhost) | 20 of 20 | 0.11 ms |
| ping (loopback) | 20 of 20 | 0.01 ms |

**A whole cycle through the engine.** The real check ran every minute of a stepped clock for 40 rounds; the server
answered 503 for 10 of them. Events: down then up. The outage was decided at the
3rd failed check, and the incident starts at the first failed check (error 0 min)
and ends at the first good check of the recovery (error 0 min).

## 2. A simulated fleet with known truth

40 services with events and 20 quiet ones, 30 days, one check a minute: 2,592,000 checks. Failed checks
at random: 0.5% of all checks. The events: real outages of 6 to 180 minutes, 400 blips of 1 or 2 minutes,
and slow periods (4 to 8 times slower for 15 to 90 minutes). The baseline is "one failed check is an outage, one good
check ends it".

| | upstatus | single failed check |
|---|---|---|
| real outages found | 200 of 200 | 200 of 200 |
| outage alerts sent | 202 | 13,369 |
| outages that did not exist | 0 | 12,762 |
| median delay from the real start to the alert | 2 checks | 0 checks |
| start of the outage, worst error | 1 check | 1 check |
| end of the outage, worst error | 1 check | 1 check |
| downtime reported, minutes (truth 19,410) | 18,807 | 32,253 |
| slow periods found (those meant to be found) | 65 of 65 | - |
| slow periods that did not exist | 0 | - |

- The 2 alerts above the 200 real outages are blips of two minutes that happened next to a random failed check:
  three failures in a row look like an outage, and they were one.
- The downtime is 603 minutes short of the truth: that is the blips shorter than three checks, which are left out on purpose.
  The failed checks counted one by one overstate it by 12,843 minutes.
- A slow period is meant to be found when it is at least 3 times the usual time **and** 100 ms more. 15 more
  injected periods were below that (a service that goes from 8 ms to 40 ms is still fast) and are left alone on purpose.
- The median delay of a slow period is 4 checks after it began (the rule needs 5 slow checks in a row).
- On the 20 quiet services (864,000 checks, only random failures): upstatus raised 0 outages and 0 slow periods;
  the single-failed-check rule raised 4,359 outages, and an alert for every answer more than twice the usual time would have raised
  2,630 alerts for 859,621 answers.

## 3. The price of confirmation

`fail_after` is the number of failed checks in a row before a service is called down. Two fleets: 20 steady services
(0.5% random failures) and 10 flaky ones (3% random failures), 30 days each, with real outages and blips.

| `fail_after` | steady: found | steady: false outages | steady: blips alerted | steady: median delay (checks) | flaky: found | flaky: false outages |
|---|---|---|---|---|---|---|
| 1 | 100 of 100 | 4215 | 200 | 0 | 50 of 50 | 12138 |
| 2 | 100 of 100 | 22 | 91 | 1 | 50 of 50 | 371 |
| 3 | 100 of 100 | 1 | 2 | 2 | 50 of 50 | 14 |
| 4 | 100 of 100 | 0 | 0 | 3 | 50 of 50 | 0 |
| 5 | 100 of 100 | 0 | 0 | 4 | 50 of 50 | 0 |

The default is 3. It finds everything and has one false outage in the steady fleet (three random failures in a row,
about 0.1 are expected in that many checks), and a few on services that fail 3% of the time. Waiting for 4 removes
those at the price of one more minute of delay. With 1 every random failed check is an alert. Raise it for a
service that is flaky by nature; lower it only where a minute matters more than a false alarm.

## 4. Cost

50 checks, 7 days of one check a minute (504,000 results):

| step | result |
|---|---|
| store the results | 1.48 s |
| build the page (and `status.json`) | 0.075 s (296.2 KiB; the JSON file 0.075 s) |
| record one result: read the state, decide, write | 1.78 ms |
| the history file | 29.7 MiB: 61.7 bytes per result, about 30.9 MiB per check and year at one a minute |

Results older than `retention_days` are deleted; the incidents and the daily figures stay.
