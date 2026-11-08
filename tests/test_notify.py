import json

from servers import Behaviour, http_server
from upstatus.config import Notify
from upstatus.notify import deliver, message, payload, post
from upstatus.store import PendingEvent, Store
from upstatus.tracker import Event


def pe(kind="down", check="Website", detail="timeout", since=1_760_000_000.0):
    return PendingEvent(1, check, kind, since, since, detail)


def test_messages():
    assert message(pe()) == "Website is DOWN: timeout [2025-10-09 08:53Z]"
    assert message(pe("up", detail="")).startswith("Website is back UP [")
    assert "slow" in message(pe("degraded", detail="300 ms, usually 20 ms"))
    assert "usual speed" in message(pe("normal", detail=""))
    assert "mystery" in message(pe("mystery"))


def test_payload_reads_in_slack_discord_and_scripts():
    body = json.loads(payload(pe(), "hello"))
    assert body["text"] == body["content"] == "hello"
    assert (body["check"], body["event"], body["detail"]) == ("Website", "down", "timeout")


def test_post_to_a_real_server_and_failures():
    with http_server() as (port, state):
        assert post(f"http://127.0.0.1:{port}/hook?x=1", b"{}")
        state.status = 500
        assert not post(f"http://127.0.0.1:{port}/hook", b"{}")
    assert not post(f"http://127.0.0.1:{port}/hook", b"{}", timeout=1)  # nothing listens any more
    assert not post("ftp://x/y", b"{}")
    assert not post("http:///nohost", b"{}")
    assert not post("not a url", b"{}")


def test_deliver_with_a_fake_sender(tmp_path):
    with Store(tmp_path) as s:
        s.apply_events("a", [Event("down", 100, 90, "timeout"), Event("up", 200, 190)])
        sent = []
        n = Notify(webhook_env="HOOK", on=("down", "up"))
        assert deliver(s, n, lambda u, b: sent.append((u, b)) or True, {"HOOK": "http://h/x"}) == (2, 0)
        assert [u for u, _ in sent] == ["http://h/x"] * 2
        assert deliver(s, n, lambda u, b: True, {"HOOK": "http://h/x"}) == (0, 0)


def test_the_webhook_url_comes_only_from_the_environment(tmp_path):
    with Store(tmp_path) as s:
        s.apply_events("a", [Event("down", 100, 90)])
        called = []
        # the variable is not set: nothing is sent, and the queue is emptied
        assert deliver(s, Notify(webhook_env="NOPE"), lambda u, b: called.append(1) or True, {}) == (0, 0)
        assert called == [] and s.pending() == []


def test_the_real_post_reaches_a_server_that_records_the_body(tmp_path):
    received = []

    class Rec(Behaviour):
        pass

    # use a tiny recording server
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (self.path, self.headers["Content-Type"], self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    with Store(tmp_path) as s:
        s.apply_events("Web", [Event("down", 100, 90, "timeout")])
        url = f"http://127.0.0.1:{srv.server_address[1]}/services/T/B/X"
        assert deliver(s, Notify(webhook_env="HOOK"), environ={"HOOK": url}) == (1, 0)
    srv.shutdown()
    srv.server_close()
    t.join(5)
    path, ctype, body = received[0]
    assert path == "/services/T/B/X" and ctype == "application/json"
    assert json.loads(body)["check"] == "Web"
