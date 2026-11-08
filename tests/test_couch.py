# SPDX-License-Identifier: Apache-2.0
"""The CouchDB probe, against a small fake CouchDB on loopback."""

import base64
import json
import socket
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.error import URLError

import pytest

from horae_livesync.couch import MAX_RESPONSE_BYTES, CouchProbe, seq_number

AUTH = "Basic " + base64.b64encode(b"horae:synthetic-pw").decode()


class FakeCouch(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[int, Any]]] = {}
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.seen.append(self.path)
        code: int
        body: Any
        if self.headers.get("Authorization") != AUTH:
            code, body = 401, {"error": "unauthorized"}
        else:
            code, body = self.routes.get(self.path, (404, {"error": "not_found"}))
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "http://example.org/")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def couch() -> Iterator[str]:
    FakeCouch.routes = {}
    FakeCouch.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCouch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def probe(url: str, password: str = "synthetic-pw", **kwargs: Any) -> CouchProbe:
    return CouchProbe(url, "my/notes", "horae", password, timeout_s=2, **kwargs)


def test_update_seq_and_pull_checkpoint(couch: str) -> None:
    FakeCouch.routes = {
        "/my%2Fnotes/": (200, {"update_seq": "12-g1AAAA", "doc_count": 3}),
        "/my%2Fnotes/_local/pull%3D%3D": (200, {"last_seq": "9-g1AAAB"}),
        "/my%2Fnotes/_local/push": (200, {"last_seq": 40}),
    }
    result = probe(couch).probe(["pull==", "push", "gone"])
    assert (result.ok, result.update_seq, result.checkpoint_seq) == (True, 12, 9)
    assert "/my%2Fnotes/_local/gone" in FakeCouch.seen


@pytest.mark.parametrize(
    ("route", "error"),
    [
        ((403, {"error": "forbidden"}), "forbidden"),
        ((404, {"error": "not_found"}), "database_missing"),
        ((500, {"error": "boom"}), "http_500"),
        ((302, {}), "http_302"),
        ((200, b"not json"), "bad_response"),
        ((200, [1, 2]), "bad_response"),
        ((200, b" " * (MAX_RESPONSE_BYTES + 1)), "bad_response"),
    ],
)
def test_errors_are_classes(couch: str, route: tuple[int, Any], error: str) -> None:
    FakeCouch.routes = {"/my%2Fnotes/": route}
    assert probe(couch).probe().error == error


def test_wrong_password(couch: str) -> None:
    result = probe(couch, password="wrong-password").probe()
    assert (result.ok, result.error) == (False, "unauthorized")


def test_checkpoint_error_fails_the_probe(couch: str) -> None:
    FakeCouch.routes = {
        "/my%2Fnotes/": (200, {"update_seq": "1-a"}),
        "/my%2Fnotes/_local/x": (500, {}),
    }
    assert probe(couch).probe(["x"]).error == "http_500"


def test_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert probe(f"http://127.0.0.1:{port}").probe().error == "unreachable"


class _Raising:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), "timeout"),
        (URLError(TimeoutError()), "timeout"),
        (URLError(ssl.SSLError()), "tls_error"),
        (OSError(), "bad_response"),
    ],
)
def test_transport_errors(error: BaseException, expected: str) -> None:
    opener: Any = _Raising(error)
    assert probe("https://couch.example.org", opener=opener).probe().error == expected


def test_repr_hides_the_credentials() -> None:
    assert "synthetic" not in repr(probe("https://couch.example.org"))


@pytest.mark.parametrize(
    ("value", "number"),
    [("42-g1AA", 42), (42, 42), ("0", 0), (-1, None), (True, None), ("x-1", None), (None, None)],
)
def test_seq_number(value: object, number: int | None) -> None:
    assert seq_number(value) == number
