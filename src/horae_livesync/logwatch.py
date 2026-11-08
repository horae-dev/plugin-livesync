# SPDX-License-Identifier: Apache-2.0
"""Read the LiveSync CLI's output: what it says about replication, and what to forward.

The CLI runs with `--verbose` so that every request to CouchDB is logged
(`(debug) [RemoteService]  [REQ] (POST) http://host:5984/db/_changes -> 200`, CLI 1.0.46).
Those lines tell when replication last succeeded or failed and which `_local` checkpoint
documents are the CLI's own. The parsing is best effort: a line it does not know is ignored,
so a CLI that changes its wording makes the health `lag_unknown`, never wrong.

Only what the CLI prints without `--verbose` is forwarded to the container's log (the
`[Daemon]`, `[Ready]`, `(Notice)` lines), with every secret value replaced by `***`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# Endpoints of a replication round (a pull or a push).
_REPLICATION = ("_changes", "_bulk_docs", "_revs_diff", "_bulk_get")
# `_local` documents of LiveSync itself, not replication checkpoints.
_NOT_CHECKPOINTS = frozenset({"obsidian_livesync_sync_parameters", "obsydian_livesync_milestone"})
_REQ = re.compile(r"\[REQ\] \((?P<method>[A-Z]+)\) (?P<url>\S+) -> (?P<code>\d{3}|failed)")
_FAILED = re.compile(r"HTTP:[A-Z]+ to:\S* -> failed")
# A file written to the vault: a replicated document reflected (live), or the startup scan.
_FILE_IN = re.compile(r"\]\s+Processing .*: Done\s*$|STORAGE <- DB :(?!.*(skipped|not read))")
# A file sent to the database: the watcher (`STORAGE -> DB (plain) x`) or the startup scan.
_FILE_OUT = re.compile(r"STORAGE -> DB (\(|:)(?!.*skipped)")
_VERBOSE = ("(Info)", "(Verbose)", "(debug)")
MAX_CHECKPOINTS = 8


@dataclass(frozen=True, slots=True)
class Event:
    """What one line means. `kind` is one of `phase` (`value` = the state), `sync_ok`,
    `sync_error` (`value` = an error class), `checkpoint` (`value` = a `_local` id),
    `file_in`, `file_out`."""

    kind: str
    value: str = ""


def events(line: str) -> list[Event]:
    """The events of one output line of the CLI (usually none)."""
    if line.startswith("[Daemon] "):
        return _daemon(line)
    if _FILE_IN.search(line):
        return [Event("file_in")]
    if _FILE_OUT.search(line):
        return [Event("file_out")]
    if _FAILED.search(line):
        return [Event("sync_error", "unreachable")]
    match = _REQ.search(line)
    return _request(match.group("url"), match.group("code")) if match else []


def _daemon(line: str) -> list[Event]:
    text = line[len("[Daemon] ") :]
    if text.startswith("Replicating with remote"):
        return [Event("phase", "initial_replication")]
    if text.startswith("Initial replication complete"):
        return [Event("sync_ok")]
    if text.startswith(("LiveSync active", "Polling mode")):
        return [Event("phase", "live")]
    if text.startswith("Initial replication failed"):
        return [Event("sync_error", "initial_replication_failed")]
    if text.startswith(("Poll error", "Warning:")) and "consecutive" in text:
        return [Event("sync_error", "poll_failed")]
    if text.startswith("Replication recovered"):
        return [Event("sync_ok")]
    return []


def _request(url: str, code: str) -> list[Event]:
    try:
        path = urlsplit(url).path
    except ValueError:
        return []
    found: list[Event] = []
    if "/_local/" in path:
        local_id = unquote(path.rsplit("/_local/", 1)[1])
        if local_id and local_id not in _NOT_CHECKPOINTS and len(local_id) <= 128:
            found.append(Event("checkpoint", local_id))
    endpoint = path.rstrip("/").rsplit("/", 1)[-1]
    if code == "failed":
        found.append(Event("sync_error", "unreachable"))
    elif code in ("401", "403"):
        found.append(Event("sync_error", "unauthorized" if code == "401" else "forbidden"))
    elif code.startswith("5"):
        found.append(Event("sync_error", "server_error"))
    elif code.startswith("2") and endpoint in _REPLICATION:
        found.append(Event("sync_ok"))
    return found


def forwarded(line: str, secrets: Iterable[str], *, verbose: bool = False) -> str | None:
    """The line as the container's log shows it, or None to drop it: only what the CLI prints
    without `--verbose` (everything with `verbose`), with secret values masked."""
    stripped = line.rstrip("\r\n")
    if not stripped.strip():
        return None
    if not verbose and (
        stripped.startswith(_VERBOSE) or stripped[:1].isspace() or stripped in ("{", "}")
    ):
        return None
    for secret in secrets:
        if secret:
            stripped = stripped.replace(secret, "***")
    return stripped
