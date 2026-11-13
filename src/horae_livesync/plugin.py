# SPDX-License-Identifier: Apache-2.0
"""The Horae plugin: one source, `livesync.status`, the health of the LiveSync sidecar.

The sidecar (`horae-livesync run`, its own container) mirrors the CouchDB database into the
vault folder and writes `status.json` into a directory it shares with Horae. This source reads
that file and returns the health (`horae_livesync.status.evaluate`): whether the CLI is live,
CouchDB answers, when replication last succeeded and how far behind it is. It proposes no
intent and connects to nothing: a pipeline uses its data (a line in the morning note, a
`notify` when unhealthy), and a consumer that deletes or undoes in the vault folder checks
`live` first (a deletion made while the sidecar is down can be undone by its next start).

**Paths.** The status directory is the plugin's single `paths.read` entry as approved in
`horae.lock` (the manifest asks for none, as plugin-obsidian does for the vault). The file is
opened without following a symbolic link, at most 64 KiB, and only typed fields of it are
returned (it is written by another container: treated as untrusted).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from horae_sdk import Call, Output, Paths, Permissions, Plugin, PluginError

from horae_livesync import status

NAME = "livesync"
"""The plugin's name, also the namespace of its refs (`livesync.status`)."""
VERSION = "0.1.1"
COMMAND = ["python3", "-m", "horae_livesync"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions()
"""No intent, host or secret; the status directory is approved as `paths.read` in the lock."""


def status_dir(paths: Paths) -> str:
    """The status directory: the single approved `paths.read` entry."""
    if len(paths.read) != 1:
        raise PluginError(
            "the livesync plugin needs exactly one paths.read entry (the status directory)"
        )
    return paths.read[0]


def build() -> Plugin:
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Mirrors an E2EE LiveSync CouchDB into the vault folder; reports its health.",
    )

    @plugin.source(f"{NAME}.status")
    async def status_source(
        call: Call, max_lag_s: int | None = None, max_age_s: int | None = None
    ) -> Output:
        """The sidecar's health. `max_lag_s` and `max_age_s` default to the thresholds of
        `livesync.yaml` (written into the status); `max_age_s` may not be under 3 probe
        intervals. `live` is false while the sidecar is down: refuse to delete or undo in the
        vault then (README, "Known limitation: deletions while the sidecar is down")."""
        for value in (max_lag_s, max_age_s):
            if value is not None and not 0 < value <= 86400:
                raise PluginError("max_lag_s and max_age_s must be 1 to 86400 seconds")
        directory = status_dir(call.paths)
        try:
            document: dict[str, Any] | None = await asyncio.to_thread(status.read, directory)
        except ValueError:
            document = {"schema_version": None}
        minimum = status.GRACE_PROBES * status.probe_interval(document)
        if max_age_s is not None and max_age_s < minimum:
            raise PluginError(f"max_age_s must be at least 3 probe intervals ({minimum} s)")
        health = status.evaluate(
            document, datetime.now(UTC), max_lag_s=max_lag_s, max_age_s=max_age_s
        )
        return Output(health)

    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_livesync --manifest`)."""
    return build().manifest(command=COMMAND, horae=">=0,<0.2", license="Apache-2.0")
