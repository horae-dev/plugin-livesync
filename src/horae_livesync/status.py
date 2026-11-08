# SPDX-License-Identifier: Apache-2.0
"""The sidecar's status file and the health derived from it.

The sidecar writes `status.json` (atomically, 0644, no secret and no vault path in it) after
every probe; Horae's `livesync.status` source and the container's health check read it and
call `evaluate`. Schema (version 1), times as UTC ISO 8601 strings or null:

    updated_at              when the sidecar last wrote the file (stale = the sidecar is down)
    started_at              when the sidecar started (the grace periods count from it)
    cli_version             the pinned Self-hosted LiveSync CLI
    thresholds              probe_interval_s, max_age_s, max_lag_s from `livesync.yaml`
    process.state           starting | initial_replication | live | exited | config_error
                            | auth_failed (credentials refused: waiting `remote.retry_at`)
    process.started_at, restarts, last_exit_code, last_exit_at
    process.log_reader_error    the sidecar stopped reading the CLI's output (an error class)
    remote.ok               the last probe of the CouchDB database succeeded (null: not yet)
    remote.checked_at, last_ok_at, error, update_seq (numeric part of the database update_seq)
    remote.retry_at         credentials refused: when they are tried again (null otherwise)
    replication.last_ok_at  last successful replication request of the CLI
    replication.last_error_at, last_error
    replication.failing_since   the first error since the last success (null: not failing)
    replication.checkpoint_seq  the CLI's pull checkpoint on the remote (numeric part)
    replication.pending     remote changes the CLI has not checkpointed yet (null: unknown)
    replication.pending_since   while `pending` > 0: when the checkpoint last moved (or when
                            `pending` went from 0 to more); the lag counts from it
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
STATES = ("starting", "initial_replication", "live", "exited", "config_error", "auth_failed")
DEFAULT_PROBE_INTERVAL_S = 30
DEFAULT_MAX_LAG_S = 300
DEFAULT_MAX_AGE_S = 120
MAX_FUTURE_S = 60
"""An `updated_at` further in the future than this is not believed (a skewed clock)."""
GRACE_PROBES = 3
"""How many probe intervals after the sidecar's start an unknown lag is only a warning."""


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) != 20:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def new_status(
    cli_version: str, thresholds: Mapping[str, int] | None = None, started_at: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "started_at": started_at or now_iso(),
        "cli_version": cli_version,
        "thresholds": dict(
            thresholds
            or {
                "probe_interval_s": DEFAULT_PROBE_INTERVAL_S,
                "max_age_s": DEFAULT_MAX_AGE_S,
                "max_lag_s": DEFAULT_MAX_LAG_S,
            }
        ),
        "process": {
            "state": "starting",
            "started_at": None,
            "restarts": 0,
            "last_exit_code": None,
            "last_exit_at": None,
            "log_reader_error": None,
        },
        "remote": {
            "ok": None,
            "checked_at": None,
            "last_ok_at": None,
            "error": None,
            "update_seq": None,
            "retry_at": None,
        },
        "replication": {
            "last_ok_at": None,
            "last_error_at": None,
            "last_error": None,
            "failing_since": None,
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


def _positive(value: object) -> int | None:
    count = _count(value)
    return count if count else None


def probe_interval(status: Mapping[str, Any] | None) -> int:
    """The sidecar's probe interval, from the status's `thresholds` (or the default)."""
    thresholds = _section(status or {}, "thresholds")
    return _positive(thresholds.get("probe_interval_s")) or DEFAULT_PROBE_INTERVAL_S


def evaluate(
    status: Mapping[str, Any] | None,
    now: datetime,
    *,
    max_lag_s: int | None = None,
    max_age_s: int | None = None,
) -> dict[str, Any]:
    """Health from a status document. `max_lag_s` and `max_age_s` default to the status's own
    `thresholds` (from `livesync.yaml`). `healthy` is false with one `problems` code per reason:

    - `status_missing`, `status_invalid`, `status_stale` (older than `max_age_s`, or more than
      60 s in the future), `config_error`, `not_live`;
    - the probe's error (`unauthorized`, `forbidden`, `timeout`...) or `remote_unreachable`;
    - the CLI's replication error (`initial_replication_failed`, `unreachable`...) when it is
      newer than its last success and has lasted more than one probe interval (at once when
      the CLI is not live), else the warning `replication_retrying`;
    - `lagging`: remote changes pending and the CLI's checkpoint has not moved for `max_lag_s`;
    - `lag_unknown`: the lag cannot be measured more than 3 probe intervals after the
      sidecar's start (a warning before);
    - `sync_stale`: the CLI's last successful replication request is older than `max_lag_s`
      while the copy is not known to be current (changes pending for more than a probe
      interval, or an unknown lag past the grace);
    - `log_reader_failed`: the sidecar stopped reading the CLI's output.

    `live` is true when the status is fresh and the CLI is live: consumers that act on the
    vault folder (an undo, a delete) refuse when it is false (README, "Deploying")."""
    problems: list[str] = []
    warnings: list[str] = []

    def problem(code: str) -> None:
        if code not in problems:
            problems.append(code)

    if status is None:
        return {"healthy": False, "live": False, "problems": ["status_missing"], "warnings": []}
    if status.get("schema_version") != SCHEMA_VERSION:
        return {"healthy": False, "live": False, "problems": ["status_invalid"], "warnings": []}
    process, remote, replication = (
        _section(status, n) for n in ("process", "remote", "replication")
    )
    files, thresholds = _section(status, "files"), _section(status, "thresholds")
    probe = probe_interval(status)
    max_lag = max_lag_s or _positive(thresholds.get("max_lag_s")) or DEFAULT_MAX_LAG_S
    max_age = max_age_s or _positive(thresholds.get("max_age_s")) or DEFAULT_MAX_AGE_S

    updated = parse_time(status.get("updated_at"))
    if updated is not None and (updated - now).total_seconds() > MAX_FUTURE_S:
        updated = None
    age = None if updated is None else max(0, int((now - updated).total_seconds()))
    stale = age is None or age > max_age
    if stale:
        problem("status_stale")
    state = process.get("state") if process.get("state") in STATES else None
    if state == "config_error":
        problem("config_error")
    elif state != "live":
        problem("not_live")
    if remote.get("ok") is not True:
        problem(_word(remote.get("error"), 32) or "remote_unreachable")
    if process.get("log_reader_error") is not None:
        problem("log_reader_failed")

    last_ok = parse_time(replication.get("last_ok_at"))
    last_error = parse_time(replication.get("last_error_at"))
    if last_error is not None and (last_ok is None or last_error > last_ok):
        failing_since = parse_time(replication.get("failing_since")) or last_error
        if state != "live" or (now - failing_since).total_seconds() > probe:
            problem(_word(replication.get("last_error"), 32) or "replication_error")
        else:
            warnings.append("replication_retrying")

    started = parse_time(status.get("started_at"))
    past_grace = started is None or (now - started).total_seconds() > GRACE_PROBES * probe
    pending = _count(replication.get("pending"))
    since = parse_time(replication.get("pending_since"))
    lag = None
    if pending is None:
        if past_grace:
            problem("lag_unknown")
        else:
            warnings.append("lag_unknown")
    elif pending == 0:
        lag = 0
    elif since is not None:
        lag = max(0, int((now - since).total_seconds()))
        if lag > max_lag:
            problem("lagging")
    # Not current: the lag is unknown (past the grace), or changes have been pending for more
    # than a probe interval (an idle live feed makes no request, so a quiet but current copy
    # is never stale).
    not_current = (pending is None and past_grace) or (lag is not None and lag > probe)
    if not_current and (last_ok is None or (now - last_ok).total_seconds() > max_lag):
        problem("sync_stale")

    return {
        "healthy": not problems,
        "live": state == "live" and not stale,
        "problems": problems,
        "warnings": warnings,
        "state": state,
        "status_age_s": age,
        "last_sync_at": None if last_ok is None else now_iso(last_ok),
        "lag_s": lag,
        "pending_changes": pending,
        "remote_ok": remote.get("ok") is True,
        "remote_last_ok_at": _iso(remote.get("last_ok_at")),
        "remote_retry_at": _iso(remote.get("retry_at")),
        "last_error_at": _iso(replication.get("last_error_at")),
        "last_error": _word(replication.get("last_error"), 32),
        "files_in": _count(files.get("in")),
        "files_out": _count(files.get("out")),
        "restarts": _count(process.get("restarts")),
        "cli_version": _word(status.get("cli_version")),
        "max_lag_s": max_lag,
        "max_age_s": max_age,
    }


def _iso(value: object) -> str | None:
    parsed = parse_time(value)
    return None if parsed is None else now_iso(parsed)
