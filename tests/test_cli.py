import json
import threading
import urllib.request

import pytest

from servers import closed_port, tcp_listener
from upstatus.cli import build_parser, main, make_server


def write_config(tmp_path, port, extra="", fail_after=1):
    f = tmp_path / "upstatus.toml"
    f.write_text(
        f"""
[site]
title = "T"
[defaults]
fail_after = {fail_after}
recover_after = 1
{extra}
[[check]]
name = "Service"
type = "tcp"
host = "127.0.0.1"
port = {port}
""",
        encoding="utf-8",
    )
    return f


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_writes_a_valid_example_and_refuses_to_overwrite(tmp_path, capsys):
    f = tmp_path / "u.toml"
    assert run(capsys, "-c", str(f), "init")[0] == 0
    assert run(capsys, "-c", str(f), "validate") == (0, "ok: 5 check(s) in 2 group(s)\n", "")
    code, _, err = run(capsys, "-c", str(f), "init")
    assert code == 2 and "exists already" in err
    assert run(capsys, "-c", str(f), "init", "--force")[0] == 0


def test_validate_reports_mistakes_with_exit_2(tmp_path, capsys):
    f = tmp_path / "u.toml"
    f.write_text("[[check]]\nname='a'\ntype='tcp'\nhost='h'\n")
    code, _, err = run(capsys, "-c", str(f), "validate")
    assert code == 2 and "port" in err
    code, _, err = run(capsys, "-c", str(tmp_path / "none.toml"), "validate")
    assert code == 2 and "upstatus init" in err


def test_run_status_incidents_build_cycle(tmp_path, capsys):
    with tcp_listener() as port:
        f = write_config(tmp_path, port)
        code, out, _ = run(capsys, "-c", str(f), "run")
        assert code == 0 and "ran 1 check(s)" in out
        code, out, _ = run(capsys, "-c", str(f), "status")
        assert code == 0 and out.startswith("Service: up, ") and " ms" in out
    # the port is closed now: the next run finds the service down (fail_after = 1) and exits 1
    code, out, _ = run(capsys, "-c", str(f), "run", "--all")
    assert code == 1 and "1 change(s)" in out
    code, out, _ = run(capsys, "-c", str(f), "status")
    assert code == 1 and "Service: DOWN" in out and ("connection refused" in out or "timeout" in out)
    code, out, _ = run(capsys, "-c", str(f), "incidents")
    assert code == 0 and "down" in out and "Service: still going" in out
    code, out, _ = run(capsys, "-c", str(f), "incidents", "--since", "1s")
    assert "Service: still going" in out  # still open, so it is listed whatever the period
    index = tmp_path / "public" / "index.html"
    index.unlink()
    assert run(capsys, "-c", str(f), "build")[0] == 0 and index.exists()
    assert "Partial outage" in index.read_text() or "Major outage" in index.read_text()
    data = json.loads((tmp_path / "public" / "status.json").read_text())
    assert data["checks"][0]["status"] == "down"


def test_incidents_when_there_are_none(tmp_path, capsys):
    f = write_config(tmp_path, closed_port())
    run(capsys, "-c", str(f), "build")
    assert run(capsys, "-c", str(f), "incidents")[1] == "no incident in the period\n"


def test_status_before_any_run(tmp_path, capsys):
    f = write_config(tmp_path, 9)
    code, out, _ = run(capsys, "-c", str(f), "status")
    assert code == 0 and out == "Service: UNKNOWN\n"


def test_run_only_runs_checks_that_are_due(tmp_path, capsys):
    with tcp_listener() as port:
        f = write_config(tmp_path, port)
        assert "ran 1" in run(capsys, "-c", str(f), "run")[1]
        assert "ran 0" in run(capsys, "-c", str(f), "run")[1]
        assert "ran 1" in run(capsys, "-c", str(f), "run", "--all")[1]


def test_watch_stops_after_the_requested_ticks(tmp_path, capsys):
    with tcp_listener() as port:
        f = write_config(tmp_path, port)
        code, _, _ = run(capsys, "-c", str(f), "watch", "--tick", "1s", "--rounds", "2")
    assert code == 0 and (tmp_path / "public" / "index.html").exists()


def test_watch_tick_limits(tmp_path, capsys):
    f = write_config(tmp_path, 9)
    assert run(capsys, "-c", str(f), "watch", "--tick", "2h")[0] == 2
    assert run(capsys, "-c", str(f), "watch", "--tick", "abc")[0] == 2


def test_watch_reports_pending_notifications(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOOK", "http://127.0.0.1:1/nope")  # nothing listens: delivery fails and stays pending
    f = write_config(tmp_path, closed_port(), extra='[notify]\nwebhook_env = "HOOK"\n')
    code, _, err = run(capsys, "-c", str(f), "watch", "--tick", "1s", "--rounds", "1")
    assert code == 0 and "pending: Service down" in err


def test_serve_needs_a_page_first_and_serves_it(tmp_path, capsys):
    f = write_config(tmp_path, 9)
    code, _, err = run(capsys, "-c", str(f), "serve")
    assert code == 2 and "upstatus run" in err
    run(capsys, "-c", str(f), "build")
    server = make_server(tmp_path / "public", "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5) as r:
            assert b"<title>T</title>" in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/status.json", timeout=5) as r:
            assert json.loads(r.read())["checks"][0]["name"] == "Service"
    finally:
        server.shutdown()
        server.server_close()
        t.join(5)


def test_a_damaged_history_is_a_clean_error(tmp_path, capsys):
    f = write_config(tmp_path, 9)
    d = tmp_path / "upstatus-data"
    d.mkdir()
    (d / "upstatus.db").write_bytes(b"garbage" * 1000)
    code, _, err = run(capsys, "-c", str(f), "status")
    assert code == 2 and "not a usable history" in err


def test_unwritable_output_is_a_clean_error(tmp_path, capsys):
    f = write_config(tmp_path, 9)
    (tmp_path / "public").write_text("a file where the folder should be")
    code, _, err = run(capsys, "-c", str(f), "build")
    assert code == 2 and err.startswith("upstatus: ")


def test_version_and_missing_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.startswith("upstatus ")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_every_command_has_help():
    parser = build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr,attr-defined]
    assert commands == {"init", "validate", "run", "watch", "build", "status", "incidents", "serve"}
