# SPDX-License-Identifier: Apache-2.0
"""The supervisor, running a fake CLI (`tests/fake_cli.py`) instead of the LiveSync CLI."""

import base64
import dataclasses
import io
import json
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from horae_livesync import status, supervisor
from horae_livesync.config import parse
from horae_livesync.couch import Probe
from horae_livesync.logwatch import Event
from horae_livesync.supervisor import EX_CONFIG, Layout, Supervisor, Tracker, write_private

from .conftest import PASSPHRASE, PASSWORD, config_document

FAKE_CLI = Path(__file__).parent / "fake_cli.py"


class FakeProber:
    def __init__(self, *results: Probe) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def probe(self, checkpoint_ids: Sequence[str]) -> Probe:
        self.calls.append(list(checkpoint_ids))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def layout(tmp_path: Path, behaviour: str = "ok") -> Layout:
    for name in ("vault", "db", "status", "private"):
        (tmp_path / name).mkdir()
    return Layout(
        vault=tmp_path / "vault",
        database=tmp_path / "db",
        status_dir=tmp_path / "status",
        private_dir=tmp_path / "private",
        cli=(sys.executable, str(FAKE_CLI), behaviour),
        cli_version="1.0.46-patch2-cli",
    )


def make(
    tmp_path: Path,
    secrets_dir: Path,
    behaviour: str = "ok",
    prober: FakeProber | None = None,
    auth_retry_s: int = 900,
    **change: Any,
) -> Supervisor:
    document = config_document(secrets_dir, health={"probe_interval_s": 5}, **change)
    return Supervisor(
        dataclasses.replace(parse(document), auth_retry_s=auth_retry_s),
        layout(tmp_path, behaviour),
        environ={"TZ": "UTC", "SECRET_LOOKING": "x"},
        out=io.StringIO(),
        prober=prober or FakeProber(Probe(True, None, 9, 9)),
    )


def start(sup: Supervisor) -> threading.Thread:
    runner = threading.Thread(target=lambda: setattr(sup, "code", sup.run()), daemon=True)
    runner.start()
    return runner


def output(sup: Supervisor) -> str:
    assert isinstance(sup.out, io.StringIO)
    return sup.out.getvalue()


def wait_for(predicate: Any, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.05)


def test_runs_the_daemon_with_private_settings(tmp_path: Path, secrets_dir: Path) -> None:
    sup = make(tmp_path, secrets_dir)
    runner = threading.Thread(target=lambda: setattr(sup, "code", sup.run()), daemon=True)
    runner.start()
    seen = tmp_path / "db" / "seen.json"
    try:
        wait_for(seen.exists)
        wait_for(lambda: "leaked" in output(sup))
        wait_for(lambda: sup.tracker.doc["process"]["state"] == "live")
    finally:
        sup.stop()
        runner.join(30)
    assert getattr(sup, "code", None) == 0

    record = json.loads(seen.read_text())
    assert record["mode"] == 0o600
    assert record["argv"][1:] == [
        "--settings",
        record["argv"][2],
        "--vault",
        str(tmp_path / "vault"),
        "--verbose",
        "daemon",
    ]
    assert Path(record["argv"][2]).parent.parent == tmp_path / "private"
    settings = record["settings"]
    assert (settings["couchDB_PASSWORD"], settings["passphrase"]) == (PASSWORD, PASSPHRASE)
    assert settings["encrypt"] is True
    assert settings["usePathObfuscation"] is True
    # The CLI gets a minimal environment: none of the sidecar's variables.
    names = [name for name in record["env"] if not name.startswith("__CF")]  # macOS adds one
    assert names == ["HOME", "LANG", "PATH", "TZ"]

    out = output(sup)
    assert PASSWORD not in out
    assert quote(PASSWORD, safe="") not in out
    assert base64.b64encode(f"horae:{PASSWORD}".encode()).decode() not in out
    assert "[Info] leaked ***" in out
    assert '[Info] leaked *** "***"' in out
    assert "[Info] leaked Authorization: Basic ***" in out
    assert "[Daemon] LiveSync active" in out
    assert "(debug)" not in out

    final = status.read(tmp_path / "status")
    assert final is not None
    assert final["thresholds"] == {"probe_interval_s": 5, "max_age_s": 120, "max_lag_s": 300}
    assert final["process"]["state"] == "exited"
    assert final["process"]["last_exit_code"] == 0
    assert final["files"] == {"in": 1, "out": 2}
    assert final["remote"]["ok"] is True
    assert final["replication"]["pending"] == 0
    assert PASSWORD not in json.dumps(final)
    assert PASSPHRASE not in json.dumps(final)


def test_poll_mode_passes_the_interval(tmp_path: Path, secrets_dir: Path) -> None:
    sup = make(tmp_path, secrets_dir, sync={"mode": "poll", "poll_interval_s": 30})
    command = sup.prepare()
    assert command[-4:] == ["--verbose", "--interval", "30", "daemon"]


def test_a_crashing_cli_is_restarted(
    tmp_path: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "BACKOFF_S", (0.1,))
    sup = make(tmp_path, secrets_dir, behaviour="exit3")
    runner = threading.Thread(target=sup.run, daemon=True)
    runner.start()
    try:
        wait_for(lambda: sup.tracker.doc["process"]["restarts"] >= 2, timeout=30)
    finally:
        sup.stop()
        runner.join(30)
    doc = sup.tracker.doc
    assert doc["process"]["last_exit_code"] == 3
    assert doc["replication"]["last_error"] == "initial_replication_failed"
    assert "exited with code 3" in output(sup)


def test_a_missing_secret_is_a_config_error(tmp_path: Path, secrets_dir: Path) -> None:
    (secrets_dir / "LIVESYNC_E2EE_PASSPHRASE").unlink()
    sup = make(tmp_path, secrets_dir)
    assert sup.run() == EX_CONFIG
    written = status.read(tmp_path / "status")
    assert written is not None
    assert written["process"]["state"] == "config_error"
    assert "LIVESYNC_E2EE_PASSPHRASE" in written["config_error"]


def test_a_failing_init_settings_is_a_config_error(tmp_path: Path, secrets_dir: Path) -> None:
    sup = make(tmp_path, secrets_dir, behaviour="badinit")
    assert sup.run() == EX_CONFIG
    assert "CalledProcessError" in sup.tracker.doc["config_error"]


def test_status_write_failures_are_logged(tmp_path: Path, secrets_dir: Path) -> None:
    sup = make(tmp_path, secrets_dir)
    sup.layout.status_dir = tmp_path / "missing"
    sup.write_status()
    assert "cannot write the status file" in output(sup)


def test_refused_credentials_at_start_never_start_the_cli(
    tmp_path: Path, secrets_dir: Path
) -> None:
    prober = FakeProber(Probe(False, "unauthorized"), Probe(True, None, 9, 9))
    sup = make(tmp_path, secrets_dir, prober=prober, auth_retry_s=2)
    runner = start(sup)
    try:
        wait_for(lambda: sup.tracker.doc["process"]["state"] == "auth_failed")
        written = status.read(tmp_path / "status")
        assert written is not None
        assert written["remote"]["error"] == "unauthorized"
        assert written["remote"]["retry_at"] is not None
        health = status.evaluate(written, datetime.now(UTC))
        assert "unauthorized" in health["problems"]
        assert "not_live" in health["problems"]
        time.sleep(1)
        assert len(prober.calls) == 1  # no probe with the refused credentials
        assert not (tmp_path / "db" / "seen.json").exists()
        # After auth_retry_s, one probe: it passes, so the CLI starts.
        wait_for(lambda: sup.tracker.doc["process"]["state"] == "live")
    finally:
        sup.stop()
        runner.join(30)
    assert len(prober.calls) >= 2
    assert sup.tracker.doc["remote"]["retry_at"] is None
    assert "refused the credentials (unauthorized)" in output(sup)
    assert "trying the CouchDB credentials again" in output(sup)


def test_a_cli_refused_by_couchdb_is_stopped_not_restarted(
    tmp_path: Path, secrets_dir: Path
) -> None:
    sup = make(tmp_path, secrets_dir, behaviour="unauth")
    runner = start(sup)
    requests = tmp_path / "db" / "requests.log"
    try:
        wait_for(lambda: sup.tracker.doc["process"]["state"] == "auth_failed")
        count = len(requests.read_text().splitlines())
        time.sleep(1)
        assert len(requests.read_text().splitlines()) == count  # stopped: no more requests
        doc = sup.tracker.doc
        assert doc["remote"]["error"] == "unauthorized"
        assert doc["process"]["restarts"] == 0
        assert doc["replication"]["last_error"] == "unauthorized"
    finally:
        sup.stop()
        runner.join(30)
    assert getattr(sup, "code", None) == 0
    final = status.read(tmp_path / "status")
    assert final is not None
    assert final["process"]["state"] == "auth_failed"


def test_a_failing_log_reader_marks_the_status(
    tmp_path: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_line = Tracker.line

    def line(self: Tracker, text: str) -> None:
        if "LOG-READER-BOOM" in text:
            raise RuntimeError("boom")
        real_line(self, text)

    monkeypatch.setattr(Tracker, "line", line)
    sup = make(tmp_path, secrets_dir, behaviour="badoutput")
    runner = start(sup)
    try:
        wait_for(lambda: sup.tracker.doc["process"]["log_reader_error"] == "RuntimeError")
        wait_for(lambda: "log_reader_failed" in health_of(tmp_path)["problems"])
    finally:
        sup.stop()
        runner.join(30)
    assert "stopped reading the CLI's output (RuntimeError)" in output(sup)
    assert getattr(sup, "code", None) == 0


def health_of(tmp_path: Path) -> dict[str, Any]:
    return status.evaluate(status.read(tmp_path / "status"), datetime.now(UTC))


def test_tracker_pending_and_checkpoints() -> None:
    clock = [datetime(2026, 11, 8, tzinfo=UTC)]
    tracker = Tracker("v", now=lambda: clock[0])
    for index in range(12):
        tracker.event(Event("checkpoint", f"id{index}"))
    tracker.event(Event("checkpoint", "id5"))
    assert list(tracker.checkpoints)[-1] == "id5"
    assert len(tracker.checkpoints) == 8

    tracker.probed(Probe(True, None, 10, 7))
    assert tracker.doc["replication"]["pending"] == 3
    since = tracker.doc["replication"]["pending_since"]
    clock[0] += timedelta(seconds=30)
    tracker.probed(Probe(True, None, 12, 7))
    assert tracker.doc["replication"]["pending_since"] == since  # no progress since then
    clock[0] += timedelta(seconds=30)
    tracker.probed(Probe(True, None, 14, 8))
    # The checkpoint moved: the lag counts from this progress, not from the first pending.
    assert tracker.doc["replication"]["pending_since"] == "2026-11-08T00:01:00Z"
    tracker.probed(Probe(False, "timeout"))
    clock[0] += timedelta(seconds=600)
    tracker.probed(Probe(True, None, 14, 8))
    # An outage carries over: the checkpoint did not move, so the lag includes it.
    assert tracker.doc["replication"]["pending_since"] == "2026-11-08T00:01:00Z"
    tracker.probed(Probe(True, None, 12, 12))
    assert tracker.doc["replication"]["pending_since"] is None
    tracker.probed(Probe(True, None, 12, None))
    assert tracker.doc["replication"]["pending"] is None
    tracker.probed(Probe(False, "unauthorized"))
    assert tracker.doc["remote"]["ok"] is False
    assert tracker.doc["remote"]["error"] == "unauthorized"
    assert tracker.doc["remote"]["update_seq"] == 12  # kept from the last good probe


def test_tracker_replication_errors() -> None:
    clock = [datetime(2026, 11, 8, tzinfo=UTC)]
    tracker = Tracker("v", now=lambda: clock[0])
    tracker.event(Event("sync_error", "unreachable"))
    first = tracker.doc["replication"]["failing_since"]
    clock[0] += timedelta(seconds=10)
    tracker.event(Event("sync_error", "server_error"))
    assert tracker.doc["replication"]["failing_since"] == first
    assert tracker.doc["replication"]["last_error"] == "server_error"
    assert tracker.auth_error is None
    tracker.event(Event("sync_ok"))
    assert tracker.doc["replication"]["failing_since"] is None
    tracker.event(Event("sync_error", "forbidden"))
    assert tracker.auth_error == "forbidden"


def test_write_private_replaces_and_never_follows_links(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / "settings.json").symlink_to(target)
    path = write_private(tmp_path, "settings.json", {"a": 1})
    assert not path.is_symlink()
    assert json.loads(path.read_text()) == {"a": 1}
    assert target.read_text() == "keep"
    assert path.stat().st_mode & 0o777 == 0o600


def test_child_environment_keeps_only_basics() -> None:
    env = supervisor.child_environment({"PATH": "/evil", "NODE_OPTIONS": "--require x"})
    assert env == {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}


def test_terminate_kills_a_stuck_cli(
    tmp_path: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(supervisor, "STOP_GRACE_S", 0.2)
    sup = make(tmp_path, secrets_dir)
    child = subprocess.Popen(
        text=True,
        args=[
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
    )
    time.sleep(0.3)
    sup._terminate(child)
    assert child.returncode is not None
    assert child.returncode < 0
