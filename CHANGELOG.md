# Changelog

## 1.0.0

First release.

- Checks: HTTP (status, text), TCP, DNS, TLS certificate expiry, ping.
- A tracker that needs several failed checks in a row to call a service down, dates the outage from the first one,
  and calls a service degraded when it is much slower than usual.
- History in one SQLite file; incidents; retention.
- A static status page (no script, no addresses) and `status.json`; a webhook for changes, retried until delivered.
- `run`, `watch`, `status` (exit codes for monitoring), `incidents`, `build`, `serve`, `validate`, `init`.
