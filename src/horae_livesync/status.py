# SPDX-License-Identifier: Apache-2.0
"""The sidecar's status file and the health derived from it.

The sidecar writes `status.json` (atomically, 0644, no secret and no vault path in it) after
every probe; Horae's `livesync.status` source and the container's health check read it and
call `evaluate`. Schema (version 1), times as UTC ISO 8601 strings or null:

    updated_at              when the sidecar last wrote the file (stale = the sidecar is down)
    cli_version             the pinned Self-hosted LiveSync CLI
    process.state           starting | initial_replication | live | exited | config_error
    process.started_at, restarts, last_exit_code, last_exit_at
    remote.ok               the last probe of the CouchDB database succeeded (null: not yet)
    remote.checked_at, last_ok_at, error, update_seq (numeric part of the database update_seq)
    replication.last_ok_at  last successful replication request of the CLI
    replication.last_error_at, last_error
    replication.checkpoint_seq  the CLI's pull checkpoint on the remote (numeric part)
    replication.pending     remote changes the CLI has not checkpointed yet (null: unknown)
    replication.pending_since   when `pending` last went from 0 to more
    files.in, files.out     files written to the vault / sent to CouchDB since the start
    config_error            why the configuration was refused (no value in it)
"""

import json
import os
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATUS_FILE = "status.json"
MAX_STATUS_BYTES = 64 * 1024
STATES = ("starting", "initial_replication", "live", "exited", "config_error")


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) != 20:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def new_status(cli_version: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "cli_version": cli_version,
        "process": {
            "state": "starting",
            "started_at": None,
            "restarts": 0,
            "last_exit_code": None,
            "last_exit_at": None,
        },
        "remote": {
            "ok": None,
            "checked_at": None,
            "last_ok_at": None,
            "error": None,
            "update_seq": None,
        },
        "replication": {
            "last_ok_at": None,
            "last_error_at": None,
            "last_error": None,
            "checkpoint_seq": None,
            "pending": None,
            "pending_since": None,
        },
        "files": {"in": 0, "out": 0},
        "config_error": None,
    }


def write(directory: Path, status: Mapping[str, Any]) -> None:
    """Replace `<directory>/status.json` atomically (write a temporary file, then rename)."""
    data = json.dumps(status, sort_keys=True, separators=(",", ":")).encode()
    tmp = directory / f".{STATUS_FILE}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, directory / STATUS_FILE)


def read(directory: str | Path) -> dict[str, Any] | None:
    """The status in `directory`, or None if there is none. Never follows a symbolic link;
    `ValueError` when the file is not a small JSON object."""
    path = Path(directory) / STATUS_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"status file cannot be read ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("status file is not a regular file")
        data = os.read(fd, MAX_STATUS_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_STATUS_BYTES:
        raise ValueError("status file is too large")
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("status file is not JSON") from None
    if not isinstance(document, dict):
        raise ValueError("status file is not a JSON object")
    return document


def _section(status: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = status.get(name)
    return value if isinstance(value, dict) else {}


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _word(value: object, limit: int = 64) -> str | None:
    """A short code from the file (an error class, a version): printable ASCII only."""
    if (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.isascii()
        and value.isprintable()
    ):
        return value
    return None


def evaluate(
    status: Mapping[str, Any] | None,
    now: datetime,
    *,
    max_lag_s: int = 300,
    max_age_s: int = 120,
) -> dict[str, Any]:
    """Health from a status document. `healthy` is false with one `problems` code per reason:
    `status_missing`, `status_invalid`, `status_stale`, `config_error`, `not_live`,
    `remote_unreachable` (or the probe's error), `lagging`; `warnings` holds `lag_unknown`."""
    problems: list[str] = []
    warnings: list[str] = []
    if status is None:
        return {"healthy": False, "problems": ["status_missing"], "warnings": []}
    if status.get("schema_version") != SCHEMA_VERSION:
        return {"healthy": False, "problems": ["status_invalid"], "warnings": []}
    process, remote, replication = (
        _section(status, n) for n in ("process", "remote", "replication")
    )
    files = _section(status, "files")

    updated = parse_time(status.get("updated_at"))
    age = None if updated is None else max(0, int((now - updated).total_seconds()))
    if age is None or age > max_age_s:
        problems.append("status_stale")
    state = process.get("state") if process.get("state") in STATES else None
    if state == "config_error":
        problems.append("config_error")
    elif state != "live":
        problems.append("not_live")
    if remote.get("ok") is not True:
        problems.append(_word(remote.get("error"), 32) or "remote_unreachable")

    pending = _count(replication.get("pending"))
    since = parse_time(replication.get("pending_since"))
    lag = None
    if pending is None:
        warnings.append("lag_unknown")
    elif pending == 0:
        lag = 0
    elif since is not None:
        lag = max(0, int((now - since).total_seconds()))
        if lag > max_lag_s:
            problems.append("lagging")

    last_sync = parse_time(replication.get("last_ok_at"))
    return {
        "healthy": not problems,
        "problems": problems,
        "warnings": warnings,
        "state": state,
        "status_age_s": age,
        "last_sync_at": None if last_sync is None else now_iso(last_sync),
        "lag_s": lag,
        "pending_changes": pending,
        "remote_ok": remote.get("ok") is True,
        "remote_last_ok_at": _iso(remote.get("last_ok_at")),
        "last_error_at": _iso(replication.get("last_error_at")),
        "last_error": _word(replication.get("last_error"), 32),
        "files_in": _count(files.get("in")),
        "files_out": _count(files.get("out")),
        "restarts": _count(process.get("restarts")),
        "cli_version": _word(status.get("cli_version")),
    }


def _iso(value: object) -> str | None:
    parsed = parse_time(value)
    return None if parsed is None else now_iso(parsed)
