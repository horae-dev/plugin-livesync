# SPDX-License-Identifier: Apache-2.0
"""Reading the CLI's output, on logs recorded from CLI 1.0.46 against a test CouchDB."""

from collections import Counter
from pathlib import Path

import pytest

from horae_livesync.logwatch import Event, events, forwarded

FIXTURES = Path(__file__).parent / "fixtures"


def all_events(name: str) -> list[Event]:
    lines = (FIXTURES / name).read_text("utf-8").splitlines()
    return [event for line in lines for event in events(line)]


def test_a_healthy_run() -> None:
    found = all_events("cli-daemon.log")
    phases = [event.value for event in found if event.kind == "phase"]
    assert phases == ["initial_replication", "live"]
    kinds = Counter(event.kind for event in found)
    assert kinds["sync_ok"] > 3
    assert kinds["sync_error"] == 0
    assert kinds["file_out"] == 2  # welcome.md at start, later.md while live
    assert kinds["file_in"] == 1  # inbox/remote.md from the other device
    checkpoints = {event.value for event in found if event.kind == "checkpoint"}
    assert checkpoints == {"r3nmCGX_0OHM25Hc21tOEA==", "W2nBGaBMXPIFt72b6NRU6g=="}


def test_an_unreachable_couchdb() -> None:
    found = all_events("cli-initial-failure.log")
    assert Event("sync_error", "unreachable") in found
    assert found[-1] == Event("sync_error", "initial_replication_failed")
    assert not any(event.kind == "sync_ok" for event in found)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("[Daemon] Polling mode: syncing every 60s", [Event("phase", "live")]),
        ("[Daemon] Poll error (2 consecutive): Error: x", [Event("sync_error", "poll_failed")]),
        ("[Daemon] Replication recovered", [Event("sync_ok")]),
        ("[Daemon] something new", []),
        (
            "(debug) [R]  [REQ] (POST) http://c:5984/db/_changes -> 401",
            [Event("sync_error", "unauthorized")],
        ),
        (
            "(debug) [R]  [REQ] (POST) http://c:5984/db/_bulk_docs -> 403",
            [Event("sync_error", "forbidden")],
        ),
        (
            "(debug) [R]  [REQ] (POST) http://c:5984/db/_bulk_get -> 503",
            [Event("sync_error", "server_error")],
        ),
        ("(debug) [R]  [REQ] (GET) http://c:5984/db/_all_docs -> 200", []),
        ("(debug) [R]  [REQ] (GET) http://[bad/db/_changes -> 200", []),
        (
            "(debug) [R]  [REQ] (GET) http://c:5984/db/_local/a%2Fb -> failed",
            [Event("checkpoint", "a/b"), Event("sync_error", "unreachable")],
        ),
        ("(debug) [R]  [REQ] (GET) http://c:5984/db/_local/" + "x" * 200 + " -> 200", []),
        ("(Verbose) [SF:OfflineScanner]  STORAGE <- DB :notes/a.md", [Event("file_in")]),
        ("(Info) [S]  STORAGE <- DB : big.md has been skipped due to file size", []),
        ("(Info) [S]  STORAGE <- DB : Cloud not read x.md, possibly deleted", []),
        ("(Info) [S]  STORAGE -> DB :notes/a.md", [Event("file_out")]),
        ("(Info) [S]  STORAGE -> DB : big.md has been skipped due to file size", []),
        ("(debug) [S]  STORAGE ? DB :a.md Checking content", []),
        ("plain text", []),
    ],
)
def test_single_lines(line: str, expected: list[Event]) -> None:
    assert events(line) == expected


def test_forwarded_shows_only_what_the_cli_prints_without_verbose() -> None:
    lines = (FIXTURES / "cli-daemon.log").read_text("utf-8").splitlines()
    shown = [text for line in lines if (text := forwarded(line, ())) is not None]
    assert "[Daemon] LiveSync active" in shown
    assert "[Ready] LiveSync is running" in shown
    assert not any(text.startswith(("(debug)", "(Info)", "(Verbose)", " ")) for text in shown)
    assert "{" not in shown
    assert "}" not in shown


def test_forwarded_verbose_keeps_everything_but_secrets() -> None:
    line = "(debug) [R]  [REQ] (GET) http://c/db/ -> 200 abcdefgh9"
    assert forwarded(line, ["abcdefgh9"], verbose=True) == line.replace("abcdefgh9", "***")
    assert forwarded("    at stack", (), verbose=True) == "    at stack"


def test_forwarded_masks_secrets() -> None:
    assert forwarded("[Info] pw=abcdefgh9 again abcdefgh9\n", ["abcdefgh9", ""]) == (
        "[Info] pw=*** again ***"
    )
    assert forwarded("   ", ["x"]) is None
