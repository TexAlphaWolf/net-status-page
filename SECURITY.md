# Security

## Model

upstatus is a command line program with **no network service of its own** (`serve` is a convenience that binds to
127.0.0.1 and serves one folder of static files). It opens outgoing connections to the services you list, runs the
system `ping`, and writes a SQLite file and two static files.

- **Where the checks may go is decided by the configuration only**; it is read from a file you own. There is no
  remote input: nothing a service answers can change what upstatus does next, and nothing from the network is put on
  the page except fixed phrases ("timeout", "HTTP 503", "connection refused", ...).
- **No shell.** `ping` runs with an argument list; hosts are accepted only as IP addresses or plain host names (no
  leading `-`, no spaces, no path characters, no IPv6 zone).
- **No secrets in the file.** The webhook URL (usually a secret) is read from an environment variable named in the
  file; `webhook = "..."` is refused. URLs with a user name or password are refused.
- **HTTPS is verified**, TLS 1.2 or later; a private CA is given with `ca_file`. There is no switch to turn verification off.
- **Limits**: every check has a timeout, the body read for `contains` is at most 64 KiB.
- The status page has **no script and no external file**, a `default-src 'none'` policy, and everything is escaped.
  It shows **names, not addresses**: hosts, ports and paths of the checks are not in `index.html` or `status.json`.
- The history file holds results and incidents, not credentials. A damaged or newer-version file is refused, not repaired.
- The Docker image runs as an unprivileged user and works with a read-only root filesystem and no capabilities.

What it does not protect:

- The names you give to your checks, and whether each service is up, are public once you publish the page.
- `ping` and `tcp` checks reveal nothing, but a check against an internal address from a machine that can reach it is
  a request made on your behalf: do not point `url` at an endpoint that changes something when it is requested (it is a GET).
- The webhook receives the check name and a short phrase.

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.
