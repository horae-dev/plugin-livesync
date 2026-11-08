# SPDX-License-Identifier: Apache-2.0
"""A read-only probe of the CouchDB database: is it reachable, and how far behind is the CLI.

`GET /<db>` gives the database's `update_seq`; `GET /<db>/_local/<id>` gives the `last_seq` of
the CLI's own replication checkpoints (their ids come from the CLI's log, see `logwatch`).
The pull checkpoint holds a CouchDB sequence (`"42-g1AA..."`), the push checkpoint a local
PouchDB one (an integer), so the pending count is the database's sequence number minus the
highest string checkpoint. CouchDB sequence numbers are only roughly ordered on a cluster;
the count is an estimate, exact on a single node.

No ambient proxy, no redirect, at most 1 MiB per answer. Errors are classes
(`unauthorized`, `forbidden`, `database_missing`, `http_<code>`, `tls_error`, `timeout`,
`unreachable`, `bad_response`), never a message from the server, and `probe` never raises.

AUTH_ERRORS are the answers that mean the credentials are refused: the supervisor then stops
using them for `health.auth_retry_s` (CouchDB locks a user out after repeated failures).
"""

import base64
import json
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1024 * 1024
AUTH_ERRORS = frozenset({"unauthorized", "forbidden"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Probe:
    ok: bool
    error: str | None = None
    update_seq: int | None = None
    checkpoint_seq: int | None = None


def seq_number(value: object) -> int | None:
    """The numeric part of a CouchDB sequence (`"42-g1AA..."` or `42`)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        head = value.split("-", 1)[0]
        if head.isascii() and head.isdigit() and len(head) <= 20:
            return int(head)
    return None


class CouchProbe:
    def __init__(
        self,
        url: str,
        database: str,
        username: str,
        password: str,
        timeout_s: float = 10,
        opener: OpenerDirector | None = None,
    ) -> None:
        self._base = f"{url.rstrip('/')}/{quote(database, safe='')}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._timeout = timeout_s
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:
        return "CouchProbe(***)"

    def _get(self, path: str) -> Any:
        request = Request(self._base + path, headers=self._headers, method="GET")  # noqa: S310 (http(s) only, checked in config)
        with self._opener.open(request, timeout=self._timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return json.loads(data)

    def probe(self, checkpoint_ids: Iterable[str] = ()) -> Probe:
        try:
            info = self._get("/")
            if not isinstance(info, dict):
                return Probe(False, "bad_response")
            checkpoint = None
            for local_id in checkpoint_ids:
                seq = self._checkpoint(local_id)
                if seq is not None and (checkpoint is None or seq > checkpoint):
                    checkpoint = seq
            return Probe(True, None, seq_number(info.get("update_seq")), checkpoint)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            errors = {401: "unauthorized", 403: "forbidden", 404: "database_missing"}
            return Probe(False, errors.get(code, f"http_{code}"))
        except TimeoutError:
            return Probe(False, "timeout")
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                return Probe(False, "tls_error")
            if isinstance(exc.reason, TimeoutError):
                return Probe(False, "timeout")
            return Probe(False, "unreachable")
        except Exception:
            # Anything else (a truncated answer, `http.client.IncompleteRead`, bad JSON, a
            # protocol error) is a bad response: the probe never raises.
            return Probe(False, "bad_response")

    def _checkpoint(self, local_id: str) -> int | None:
        """The pull checkpoint's sequence, or None (missing, or a push checkpoint)."""
        try:
            doc = self._get(f"/_local/{quote(local_id, safe='')}")
        except HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            raise
        last = doc.get("last_seq") if isinstance(doc, dict) else None
        return seq_number(last) if isinstance(last, str) else None
