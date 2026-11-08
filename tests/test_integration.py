# SPDX-License-Identifier: Apache-2.0
"""Two-way sync, end to end: a real CouchDB, the sidecar image, and a second LiveSync client.

    docker build -t horae-livesync:test . && HORAE_LIVESYNC_IMAGE=horae-livesync:test \\
        uv run pytest -m integration

Skipped unless `-m integration` is given; fails (does not skip) when Docker is missing then,
so CI cannot pass by skipping. Everything is synthetic: credentials, passphrase, notes.

The sidecar runs as in production (non-root 10001, read-only root, tmpfs /tmp, no
capabilities). The "other device" is the same official CLI run once (`put`, `sync`, `cat`),
as an Obsidian client would write through CouchDB, with the same E2EE passphrase.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae_livesync.config import parse, render_settings

from .conftest import FIXTURES, config_document

pytestmark = pytest.mark.integration

COUCHDB_IMAGE = (
    "couchdb:3.5@sha256:907eab5b7e55ab7cfd241d39b90b2a335ea40bef82acee5a37388c08bf201dda"
)
ADMIN, ADMIN_PASSWORD = "admin", "synthetic-admin-password"
PASSPHRASE = "synthetic-e2ee-passphrase-for-tests"
DATABASE = "notes"


def docker(*args: str, input: str | None = None, check: bool = True) -> str:
    done = subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        input=input,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if check and done.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed ({done.returncode}): {done.stderr[-2000:]}")
    return done.stdout


def eventually(check: Callable[[], Any], timeout: float = 120, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except AssertionError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(2)


class Stack:
    def __init__(self, image: str) -> None:
        self.image = image
        self.id = uuid.uuid4().hex[:8]
        self.network = f"livesync-it-{self.id}"
        self.couch = f"livesync-it-couch-{self.id}"
        self.sidecar = f"livesync-it-sidecar-{self.id}"
        self.volumes = [
            f"livesync-it-{name}-{self.id}" for name in ("vault", "db", "status", "dev")
        ]
        self.files = Path(tempfile.mkdtemp(prefix="livesync-it-"))

    def exec(self, *args: str, input: str | None = None) -> str:
        return docker("exec", "-i", self.sidecar, *args, input=input)

    def curl(self, method: str, path: str) -> str:
        """A request to CouchDB from inside its own container (it has curl)."""
        return docker(
            "exec", self.couch, "curl", "-sS", "-X", method, "-u", f"{ADMIN}:{ADMIN_PASSWORD}",
            f"http://127.0.0.1:5984{path}",
        )  # fmt: skip

    def device(self, *args: str, input: str | None = None) -> str:
        """The other LiveSync client: the official CLI once, on its own local database."""
        return docker(
            "run", "--rm", "-i", "--network", self.network, "--user", "10001:10001",
            "--read-only", "--tmpfs", "/tmp", "--entrypoint", "node",
            "-v", f"{self.volumes[3]}:/livesync",
            "-v", f"{self.files / 'device'}:/device:ro",
            self.image, "/app/dist/index.cjs", "/livesync",
            "--settings", "/device/settings.json", *args,
            input=input,
        )  # fmt: skip

    def health(self) -> dict[str, Any]:
        out = docker("exec", self.sidecar, "horae-livesync", "health", "--json", check=False)
        health: dict[str, Any] = json.loads(out)
        return health


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    image = os.environ.get("HORAE_LIVESYNC_IMAGE", "horae-livesync:test")
    if shutil.which("docker") is None:
        pytest.fail("the integration tests need Docker")
    s = Stack(image)
    try:
        docker("network", "create", s.network)
        for volume in s.volumes:
            docker("volume", "create", volume)
        docker(
            "run", "-d", "--name", s.couch, "--network", s.network, "--network-alias", "couchdb",
            "-e", f"COUCHDB_USER={ADMIN}", "-e", f"COUCHDB_PASSWORD={ADMIN_PASSWORD}",
            COUCHDB_IMAGE,
        )  # fmt: skip
        write_files(s)
        # CouchDB first: wait for it, create the database, seed one note in the vault volume.
        eventually(lambda: '"ok"' in s.curl("GET", "/_up"), what="CouchDB")
        assert '"ok":true' in s.curl("PUT", f"/{DATABASE}")
        docker(
            "run", "--rm", "--user", "10001:10001", "--entrypoint", "sh",
            "-v", f"{s.volumes[0]}:/vault", s.image,
            "-c", "printf 'first note\\n' > /vault/welcome.md",
        )  # fmt: skip
        docker(
            "run", "-d", "--name", s.sidecar, "--network", s.network,
            "--read-only", "--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{s.files / 'config'}:/config:ro",
            "-v", f"{s.files / 'secrets'}:/run/secrets:ro",
            "-v", f"{s.volumes[0]}:/vault", "-v", f"{s.volumes[1]}:/livesync",
            "-v", f"{s.volumes[2]}:/status", "-e", "TZ=UTC", s.image,
        )  # fmt: skip
        yield s
    finally:
        print(docker("logs", s.sidecar, check=False)[-5000:])
        docker("rm", "-f", "-v", s.sidecar, s.couch, check=False)
        for volume in s.volumes:
            docker("volume", "rm", "-f", volume, check=False)
        docker("network", "rm", s.network, check=False)
        shutil.rmtree(s.files, ignore_errors=True)


def write_files(s: Stack) -> None:
    """livesync.yaml, the two secret files, and the other device's settings (all synthetic,
    world-readable so that uid 10001 in the containers can read the bind mounts)."""
    for name in ("config", "secrets", "device"):
        (s.files / name).mkdir()
    document = config_document(Path("/run/secrets"))
    document["couchdb"].update(
        username=ADMIN, database=DATABASE, password_secret="LIVESYNC_COUCHDB_PASSWORD"
    )
    document["health"] = {"probe_interval_s": 5}
    (s.files / "config" / "livesync.yaml").write_text(yaml.safe_dump(document))
    (s.files / "secrets" / "LIVESYNC_COUCHDB_PASSWORD").write_text(ADMIN_PASSWORD + "\n")
    (s.files / "secrets" / "LIVESYNC_E2EE_PASSPHRASE").write_text(PASSPHRASE + "\n")
    defaults = json.loads((FIXTURES / "cli-defaults.json").read_text())
    settings = render_settings(defaults, parse(document), ADMIN_PASSWORD, PASSPHRASE)
    (s.files / "device" / "settings.json").write_text(json.dumps(settings))
    s.files.chmod(0o755)
    for path in s.files.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def test_sidecar_becomes_healthy(stack: Stack) -> None:
    health = eventually(lambda: stack.health()["healthy"] and stack.health(), what="healthy")
    assert health["state"] == "live"
    assert health["remote_ok"] is True
    assert health["cli_version"] == "1.0.46-patch2-cli"


def test_a_file_written_in_the_folder_reaches_couchdb(stack: Stack) -> None:
    stack.exec("sh", "-c", "printf 'written by horae\\n' > /vault/from-folder.md")

    def arrived() -> bool:
        stack.device("sync")
        return "from-folder.md" in stack.device("ls")

    eventually(arrived, what="from-folder.md in CouchDB")
    assert stack.device("cat", "from-folder.md") == "written by horae\n"
    assert stack.device("cat", "welcome.md") == "first note\n"
    # E2EE and path obfuscation: neither the name nor the text is readable in CouchDB.
    raw = stack.curl("GET", f"/{DATABASE}/_all_docs?include_docs=true")
    assert '"rows":[' in raw
    assert "from-folder" not in raw
    assert "written by horae" not in raw


def test_a_document_written_in_couchdb_reaches_the_folder(stack: Stack) -> None:
    stack.device("put", "inbox/from-couch.md", input="written on another device\n")
    stack.device("sync")
    text = eventually(
        lambda: stack.exec("sh", "-c", "cat /vault/inbox/from-couch.md 2>/dev/null || true"),
        what="inbox/from-couch.md in the folder",
    )
    assert text == "written on another device\n"


def test_deletions_travel_both_ways(stack: Stack) -> None:
    stack.exec("rm", "/vault/from-folder.md")

    def gone_remotely() -> bool:
        stack.device("sync")
        return "from-folder.md" not in stack.device("ls")

    eventually(gone_remotely, what="from-folder.md deleted in CouchDB")
    stack.device("rm", "inbox/from-couch.md")
    stack.device("sync")
    eventually(
        lambda: stack.exec("sh", "-c", "test -e /vault/inbox/from-couch.md || echo gone"),
        what="inbox/from-couch.md deleted in the folder",
    )


def test_health_reports_the_traffic_and_no_lag(stack: Stack) -> None:
    def settled() -> dict[str, Any] | None:
        health = stack.health()
        return health if health["healthy"] and health["pending_changes"] == 0 else None

    health = eventually(settled, what="no pending changes")
    assert health["files_out"] >= 2
    assert health["files_in"] >= 1
    assert health["lag_s"] == 0
    assert health["last_sync_at"] is not None
    status = stack.exec("cat", "/status/status.json")
    assert ADMIN_PASSWORD not in status
    assert PASSPHRASE not in status
    log = docker("logs", stack.sidecar)
    assert ADMIN_PASSWORD not in log
    assert PASSPHRASE not in log
    mode = stack.exec("sh", "-c", "stat -c %a /tmp/livesync-*/settings.json")
    assert mode.strip() == "600"
