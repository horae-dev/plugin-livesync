# SPDX-License-Identifier: Apache-2.0
"""Read the LiveSync CLI's output: what it says about replication, and what to forward.

The CLI runs with `--verbose` so that every request to CouchDB is logged
(`(debug) [RemoteService]  [REQ] (POST) http://host:5984/db/_changes -> 200`, CLI 1.0.46).
Those lines tell when replication last succeeded or failed and which `_local` checkpoint
documents are the CLI's own. The parsing is best effort: a line it does not know is ignored,
so a CLI that changes its wording makes the health `lag_unknown`, never wrong.

Only what the CLI prints without `--verbose` is forwarded to the container's log (the
`[Daemon]`, `[Ready]`, `(Notice)` lines), with every secret replaced by `***` in the forms
`mask_forms` lists (as the core's redactor): the value, its percent-encoding, its JSON string
escaping, its hex, its base64 (both alphabets, padded or not, and the misaligned base64 of the
value inside a longer stream), and the base64 of `user:password` (an HTTP Basic header). The
longest form is replaced first, so a secret that contains another is masked whole.
"""

import base64
import binascii
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

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


def _base64_forms(raw: bytes) -> set[str]:
    """The base64 of `raw`, alone and at each alignment inside a longer stream: for offset
    `k` and `n` bytes, the characters `ceil(8k/6)` to `floor((8k + 8n)/6)` of the encoding
    depend on `raw` alone (the core's `horae.secrets.redact`)."""
    forms: set[str] = set()
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        aligned = encode(raw).decode("ascii")
        forms |= {aligned, aligned.rstrip("=")}
        for offset in (0, 1, 2):
            encoded = encode(b"\0" * offset + raw + b"\0\0").decode("ascii")
            forms.add(encoded[-(-8 * offset // 6) : (8 * offset + 8 * len(raw)) // 6])
    return forms


def _forms(value: str) -> set[str]:
    raw = value.encode("utf-8")
    hexed = binascii.hexlify(raw).decode("ascii")
    percent = quote(value, safe="")
    forms = {value, hexed, hexed.upper(), percent} | _base64_forms(raw)
    forms.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), percent))
    for ascii_only in (True, False):
        forms.add(json.dumps(value, ensure_ascii=ascii_only)[1:-1])
    return forms


def mask_forms(secrets: Iterable[str], username: str | None = None) -> tuple[str, ...]:
    """Every form of `secrets` to mask, longest first. With `username`, also the base64 of
    `username:secret` (what an HTTP Basic header carries)."""
    forms: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        forms |= _forms(secret)
        if username is not None:
            forms |= _base64_forms(f"{username}:{secret}".encode())
    forms.discard("")
    return tuple(sorted(forms, key=lambda form: (-len(form), form)))


def forwarded(line: str, masks: Sequence[str], *, verbose: bool = False) -> str | None:
    """The line as the container's log shows it, or None to drop it: only what the CLI prints
    without `--verbose` (everything with `verbose`), with every form in `masks` (from
    `mask_forms`, longest first) replaced by `***`."""
    stripped = line.rstrip("\r\n")
    if not stripped.strip():
        return None
    if not verbose and (
        stripped.startswith(_VERBOSE) or stripped[:1].isspace() or stripped in ("{", "}")
    ):
        return None
    for form in masks:
        if form:
            stripped = stripped.replace(form, "***")
    return stripped
