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
    doc = status.new_status("1.0.46-patch2-cli")
    doc["updated_at"] = at(10)
    doc["process"].update(state="live", restarts=1)
    doc["remote"].update(ok=True, last_ok_at=at(10))
    doc["replication"].update(last_ok_at=at(60), pending=0)
    doc["files"].update({"in": 3, "out": 4})
    return doc


def test_healthy() -> None:
    health = status.evaluate(healthy_doc(), NOW)
    assert health["healthy"] is True
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
    doc["replication"].update(pending=None)
    health = status.evaluate(doc, NOW)
    assert health["warnings"] == ["lag_unknown"]
    assert health["healthy"] is True
    doc["replication"].update(pending=True)
    assert status.evaluate(doc, NOW)["pending_changes"] is None


def test_missing_and_invalid_documents() -> None:
    assert status.evaluate(None, NOW)["problems"] == ["status_missing"]
    assert status.evaluate({"schema_version": 9}, NOW)["problems"] == ["status_invalid"]
    odd = {"schema_version": 1, "process": [], "updated_at": at(1)}
    assert status.evaluate(odd, NOW)["problems"] == ["not_live", "remote_unreachable"]


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
