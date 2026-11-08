# SPDX-License-Identifier: Apache-2.0
"""The status file and the health derived from it."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from horae_livesync import status

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def at(seconds_ago: int) -> str:
    return status.now_iso(NOW - timedelta(seconds=seconds_ago))


def healthy_doc() -> dict[str, Any]:
    doc = status.new_status("1.0.46-patch2-cli", started_at=at(3600))
    doc["updated_at"] = at(10)
    doc["process"].update(state="live", restarts=1)
    doc["remote"].update(ok=True, last_ok_at=at(10))
    doc["replication"].update(last_ok_at=at(60), pending=0)
    doc["files"].update({"in": 3, "out": 4})
    return doc


def test_healthy() -> None:
    health = status.evaluate(healthy_doc(), NOW)
    assert health["healthy"] is True
    assert health["live"] is True
    assert health["problems"] == []
    assert health["warnings"] == []
    assert health["last_sync_at"] == "2026-11-08T11:59:00Z"
    assert (health["lag_s"], health["pending_changes"]) == (0, 0)
    assert (health["files_in"], health["files_out"], health["restarts"]) == (3, 4, 1)
    assert health["cli_version"] == "1.0.46-patch2-cli"
    assert health["status_age_s"] == 10


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda d: d.update(updated_at=at(600)), "status_stale"),
        (lambda d: d.update(updated_at="yesterday"), "status_stale"),
        (lambda d: d["process"].update(state="initial_replication"), "not_live"),
        (lambda d: d["process"].update(state="config_error"), "config_error"),
        (lambda d: d["remote"].update(ok=False, error="unauthorized"), "unauthorized"),
        (lambda d: d["remote"].update(ok=None), "remote_unreachable"),
        (lambda d: d["remote"].update(ok=False, error="\x1b[31mred"), "remote_unreachable"),
        (lambda d: d["replication"].update(pending=4, pending_since=at(900)), "lagging"),
        (lambda d: d.update(updated_at=at(-120)), "status_stale"),  # 2 min in the future
        (lambda d: d["process"].update(state="auth_failed"), "not_live"),
        (lambda d: d["process"].update(log_reader_error="RuntimeError"), "log_reader_failed"),
        (lambda d: d["replication"].update(pending=None), "lag_unknown"),
        (
            lambda d: d["replication"].update(
                last_error_at=at(20), last_error="unreachable", failing_since=at(45)
            ),
            "unreachable",
        ),
        (
            lambda d: d["replication"].update(
                last_error_at=at(5), last_error="\x1b", failing_since=at(60)
            ),
            "replication_error",
        ),
        (
            lambda d: d["replication"].update(last_ok_at=at(900), pending=3, pending_since=at(120)),
            "sync_stale",
        ),
        (lambda d: d["replication"].update(last_ok_at=None, pending=None), "sync_stale"),
    ],
)
def test_problems(change: Any, problem: str) -> None:
    doc = healthy_doc()
    change(doc)
    health = status.evaluate(doc, NOW)
    assert health["healthy"] is False
    assert problem in health["problems"]


def test_lag_within_bounds_and_unknown() -> None:
    doc = healthy_doc()
    doc["replication"].update(pending=2, pending_since=at(30))
    assert status.evaluate(doc, NOW)["lag_s"] == 30
    assert status.evaluate(doc, NOW)["healthy"] is True
    # An unknown lag is a warning for 3 probe intervals after the start, then a problem.
    doc["started_at"] = at(60)
    doc["replication"].update(pending=None)
    health = status.evaluate(doc, NOW)
    assert health["warnings"] == ["lag_unknown"]
    assert health["healthy"] is True
    doc["started_at"] = at(91)
    assert status.evaluate(doc, NOW)["problems"] == ["lag_unknown"]
    doc["replication"].update(pending=True)
    assert status.evaluate(doc, NOW)["pending_changes"] is None


def test_an_idle_live_feed_is_not_stale() -> None:
    doc = healthy_doc()
    doc["replication"].update(last_ok_at=at(86400), pending=0)
    assert status.evaluate(doc, NOW)["healthy"] is True
    # Just behind (the CLI has not pulled yet): not stale before a probe interval.
    doc["replication"].update(pending=1, pending_since=at(20))
    assert status.evaluate(doc, NOW)["healthy"] is True


def test_replication_errors() -> None:
    doc = healthy_doc()
    # A failure younger than a probe interval: the CLI is retrying.
    doc["replication"].update(last_error_at=at(5), last_error="unreachable")
    health = status.evaluate(doc, NOW)
    assert health["healthy"] is True
    assert health["warnings"] == ["replication_retrying"]
    assert health["last_error"] == "unreachable"
    # Recovered: a success after the error.
    doc["replication"].update(last_ok_at=at(2))
    assert status.evaluate(doc, NOW)["warnings"] == []
    # A CLI that is not live gets the cause at once.
    doc = healthy_doc()
    doc["process"]["state"] = "exited"
    doc["replication"].update(
        last_ok_at=None, last_error_at=at(1), last_error="initial_replication_failed"
    )
    problems = status.evaluate(doc, NOW)["problems"]
    assert problems[:2] == ["not_live", "initial_replication_failed"]


def test_thresholds_come_from_the_status() -> None:
    doc = healthy_doc()
    doc["thresholds"] = {"probe_interval_s": 10, "max_age_s": 30, "max_lag_s": 60}
    doc["replication"].update(pending=1, pending_since=at(90))
    health = status.evaluate(doc, NOW)
    assert "lagging" in health["problems"]
    assert (health["max_lag_s"], health["max_age_s"]) == (60, 30)
    assert status.evaluate(doc, NOW, max_lag_s=120)["healthy"] is True
    doc["updated_at"] = at(40)
    assert "status_stale" in status.evaluate(doc, NOW, max_lag_s=120)["problems"]
    assert status.probe_interval(doc) == 10
    assert status.probe_interval(None) == status.DEFAULT_PROBE_INTERVAL_S
    assert status.probe_interval({"thresholds": {"probe_interval_s": 0}}) == 30


def test_live_is_false_when_the_status_is_stale() -> None:
    doc = healthy_doc()
    doc["updated_at"] = at(600)
    assert status.evaluate(doc, NOW)["live"] is False
    assert status.evaluate(None, NOW)["live"] is False


def test_missing_and_invalid_documents() -> None:
    assert status.evaluate(None, NOW)["problems"] == ["status_missing"]
    assert status.evaluate({"schema_version": 9}, NOW)["problems"] == ["status_invalid"]
    odd = {"schema_version": 1, "process": [], "updated_at": at(1)}
    assert status.evaluate(odd, NOW)["problems"] == [
        "not_live",
        "remote_unreachable",
        "lag_unknown",
        "sync_stale",
    ]


def test_write_then_read(tmp_path: Path) -> None:
    doc = healthy_doc()
    status.write(tmp_path, doc)
    assert status.read(tmp_path) == doc
    assert oct((tmp_path / status.STATUS_FILE).stat().st_mode & 0o777) == "0o644"
    assert [p.name for p in tmp_path.iterdir()] == [status.STATUS_FILE]
    assert status.read(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"[1]", "not a JSON object"),
        (b"{nope", "not JSON"),
        (b"\xff", "not JSON"),
        (b" " * (status.MAX_STATUS_BYTES + 1), "too large"),
    ],
)
def test_read_refuses_bad_files(tmp_path: Path, content: bytes, message: str) -> None:
    (tmp_path / status.STATUS_FILE).write_bytes(content)
    with pytest.raises(ValueError, match=message):
        status.read(tmp_path)


def test_read_never_follows_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (tmp_path / status.STATUS_FILE).symlink_to(target)
    with pytest.raises(ValueError, match="cannot be read"):
        status.read(tmp_path)


def test_read_refuses_a_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / status.STATUS_FILE)
    with pytest.raises(ValueError, match="not a regular file"):
        status.read(tmp_path)


def test_parse_time() -> None:
    assert status.parse_time("2026-11-08T12:00:00Z") == NOW
    assert status.parse_time("2026-13-08T12:00:00Z") is None
    assert status.parse_time(5) is None
