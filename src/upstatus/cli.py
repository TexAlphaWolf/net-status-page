"""Command line: init, validate, run, watch, build, status, incidents, serve."""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import EXAMPLE, Config, ConfigError, load_config
from .engine import run_round
from .model import fmt_span, fmt_time, parse_duration, utcnow
from .render import write_site
from .store import Store, StoreError


class UsageError(Exception):
    """A mistake in the command line: reported in one line, exit code 2."""


def _config(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config))


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        raise UsageError(f"{path} exists already (use --force to replace it)")
    path.write_text(EXAMPLE, encoding="utf-8")
    print(f"wrote {path}: edit it, then run 'upstatus validate' and 'upstatus run'")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"ok: {len(cfg.checks)} check(s) in {len({c.group for c in cfg.checks})} group(s)")
    return 0


def _status_lines(store: Store, cfg: Config) -> tuple[list[str], int]:
    lines = []
    bad = 0
    for check in cfg.checks:
        state = store.load_state(check.name)
        status = str(state.get("status", "unknown"))
        ms = state.get("last_ms")
        parts = [status.upper() if status != "up" else "up"]
        if isinstance(ms, int | float) and status != "down":
            parts.append(f"{ms:.0f} ms")
        since = state.get("since")
        if isinstance(since, int | float) and since and status != "unknown":
            parts.append(f"since {fmt_time(float(since))}")
        if status == "down":
            bad += 1
            detail = store.last_detail(check.name)
            if detail:
                parts.append(detail)
        lines.append(f"{check.name}: {', '.join(parts)}")
    return lines, bad


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    with Store(cfg.site.data) as store:
        report = run_round(cfg, store, force=args.all)
        write_site(store, cfg, utcnow().timestamp())
        print(
            f"ran {report.ran} check(s), {report.events} change(s), {report.sent} notification(s) sent"
            + (f", {report.failed} not delivered" if report.failed else "")
        )
        _, bad = _status_lines(store, cfg)
    return 1 if bad else 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _config(args)
    tick = parse_duration(args.tick)
    if not 1 <= tick <= 3600:
        raise UsageError("--tick must be between 1s and 1h")
    rounds = 0
    with Store(cfg.site.data) as store:
        try:
            while True:
                started = time.monotonic()
                report = run_round(cfg, store)
                if report.ran or rounds == 0:
                    write_site(store, cfg, utcnow().timestamp())
                for e in store.pending():  # undelivered notifications are retried next tick
                    print(f"pending: {e.check} {e.kind}", file=sys.stderr)
                rounds += 1
                if args.rounds and rounds >= args.rounds:
                    break
                time.sleep(max(0.0, tick - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print("stopped", file=sys.stderr)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cfg = _config(args)
    with Store(cfg.site.data) as store:
        write_site(store, cfg, utcnow().timestamp())
    print(f"wrote {cfg.site.output / 'index.html'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config(args)
    with Store(cfg.site.data) as store:
        lines, bad = _status_lines(store, cfg)
    print("\n".join(lines))
    return 1 if bad else 0


def cmd_incidents(args: argparse.Namespace) -> int:
    cfg = _config(args)
    since = utcnow().timestamp() - parse_duration(args.since)
    with Store(cfg.site.data) as store:
        rows = store.incidents(None, since)
    names = {c.name for c in cfg.checks}
    shown = 0
    for name, inc in sorted(rows, key=lambda r: r[1].start, reverse=True):
        if name not in names:
            continue
        end = "still going" if inc.end is None else f"{fmt_span(inc.end - inc.start)}"
        print(f"{fmt_time(inc.start)}  {inc.kind:<8} {name}: {end}" + (f" ({inc.detail})" if inc.detail else ""))
        shown += 1
    if not shown:
        print("no incident in the period")
    return 0


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def make_server(directory: Path, bind: str, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_Handler, directory=str(directory))
    return http.server.ThreadingHTTPServer((bind, port), handler)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if not cfg.site.output.is_dir():
        raise UsageError(f"{cfg.site.output} does not exist yet: run 'upstatus run' first")
    with make_server(cfg.site.output, args.bind, args.port) as server:
        print(f"serving {cfg.site.output} on http://{args.bind}:{server.server_address[1]}/ (Ctrl+C stops it)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("stopped", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="upstatus", description="Service checks, history and a static status page.")
    ap.add_argument("--version", action="version", version=f"upstatus {__version__}")
    ap.add_argument("--config", "-c", default="upstatus.toml", help="configuration file (default: upstatus.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write an example configuration file")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("validate", help="read the configuration and report any mistake")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("run", help="run the checks that are due, update the page; exit 1 if something is down")
    p.add_argument("--all", action="store_true", help="run every check now, due or not")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("watch", help="keep running the checks when they are due and keep the page up to date")
    p.add_argument("--tick", default="5s", help="how often to look for checks that are due (default 5s)")
    p.add_argument("--rounds", type=int, default=0, help="stop after this many ticks (default: never)")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("build", help="write the page from the stored history, without running any check")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("status", help="one line per check; exit 1 if one is down")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("incidents", help="list outages and slow periods")
    p.add_argument("--since", default="30d", help="how far back, e.g. 12h, 7d (default 30d)")
    p.set_defaults(fn=cmd_incidents)

    p = sub.add_parser("serve", help="serve the page on a local port (for a look; use a real web server in production)")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (UsageError, ConfigError, StoreError, ValueError) as exc:
        print(f"upstatus: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"upstatus: {exc.strerror or 'file error'}: {exc.filename}", file=sys.stderr)
        return 2
