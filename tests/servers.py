"""Real servers on loopback for the tests of the checks."""

from __future__ import annotations

import http.server
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Behaviour:
    status = 200
    body = b"hello from the test server"
    delay = 0.0


@contextmanager
def http_server(behaviour: Behaviour | None = None) -> Iterator[tuple[int, Behaviour]]:
    state = behaviour or Behaviour()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if state.delay:
                time.sleep(state.delay)
            try:
                self.send_response(state.status)
                self.send_header("Content-Length", str(len(state.body)))
                self.end_headers()
                self.wfile.write(state.body)
            except OSError:
                pass  # the client gave up (a timeout test): not an error of the server

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(state.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def tcp_listener() -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def closed_port() -> int:
    with tcp_listener() as port:
        pass
    return port


@contextmanager
def tls_server(cert: Path, key: Path) -> Iterator[int]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        sock.close()
