# Limitations

- **The evaluation uses a simulator.** Failures that are independent from one check to the next are an assumption.
  Real failures come in bursts (that is what the confirmation is for), but a service that flaps every other minute for
  an hour would be called up by a rule that needs 3 failures in a row. Use a lower `fail_after`, or a shorter `interval`.
- **The alert comes late by design**: at the `fail_after`-th failed check. With the defaults a service is called down about two minutes after it failed (the outage is still dated
  from the first failure).
- **Outages shorter than `fail_after` checks are not reported**, and are not part of the uptime. That is the point of the confirmation, and it is the main way the uptime figure differs from the truth.
- **A check is one viewpoint.** It runs where upstatus runs. If that machine or its network is down, every service looks down; run it somewhere that is not
  on the same failure path as the services, or accept that "everything is down at once" can mean "I am offline".
- **Not a monitoring system**: no agents, no metrics from inside the services, no alert routing, no maintenance windows, no multiple locations.
- **HTTP**: redirects are not followed (a 301 counts as an answer unless `expect_status` says otherwise); only the first 64 KiB are searched for `contains`; one request per check.
- **DNS** uses the resolver of the machine; it cannot query a chosen server. A resolver cache can hide a broken authoritative server.
- **ping** needs the `ping` program (it is in the Docker image's base only if installed: the provided image installs it). A host that drops ICMP will look down even if its services work.
- **TLS** checks the certificate the server presents for the name you gave; it does not check the whole chain served to other names.
- **Time**: the daily cells are in UTC.
- **SQLite**: one writer. Run one `upstatus watch` per data directory. Reading (`status`, `build`, `serve`) while it runs is fine.
- The history is not a backup of anything: results older than `retention_days` are deleted.
