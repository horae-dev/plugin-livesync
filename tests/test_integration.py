# SPDX-License-Identifier: Apache-2.0
"""Two-way sync, end to end: a real CouchDB, the sidecar image, and a second LiveSync client.

    docker build -t horae-livesync:test . && HORAE_LIVESYNC_IMAGE=horae-livesync:test \\
        uv run pytest -m integration

Skipped unless `-m integration` is given; fails (does not skip) when Docker is missing then,
so CI cannot pass by skipping. Everything is synthetic: credentials, passphrase, notes.

The sidecar runs as in production (non-root 10001, read-only root, tmpfs /tmp, no
capabilities, an init process) with a dedicated CouchDB user (admin of the LiveSync database
only) whose password and the E2EE passphrase hold every awkward character (`: / @ % " \\ & +
#`, a space, `é`). The "other device" is the same official CLI run once (`put`, `sync`,
`cat`) as the CouchDB admin, as an Obsidian client would write through CouchDB, with the same
E2EE passphrase.

The tests share one stack and run in order: sync both ways, then a restart with deletions
while the sidecar is down, a CouchDB outage, a second sidecar with a wrong password, and last
a scan of everything the sidecar leaves behind for its secrets.
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
from horae_livesync.logwatch import mask_forms

from .conftest import FIXTURES, config_document

pytestmark = pytest.mark.integration

COUCHDB_IMAGE = (
    "couchdb:3.5@sha256:907eab5b7e55ab7cfd241d39b90b2a335ea40bef82acee5a37388c08bf201dda"
)
ADMIN, ADMIN_PASSWORD = "admin", "synthetic-admin-password"
USER = "horae"
# Synthetic secrets with every character a careless encoder breaks on.
PASSWORD = 'synthetic pw:/@%"\\&+#é-couch'
PASSPHRASE = 'synthetic e2ee:/@%"\\&+#é-phrase'
DATABASE = "notes"
ENV_SECRETS = ("LIVESYNC_COUCHDB_PASSWORD", "LIVESYNC_E2EE_PASSPHRASE")
PROBE_INTERVAL_S, MAX_AGE_S = 5, 15


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


def logs(container: str) -> str:
    """A container's whole log, both streams (CouchDB logs to stderr)."""
    done = subprocess.run(  # noqa: S603
        ["docker", "logs", container],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return done.stdout + done.stderr


def eventually(check: Callable[[], Any], timeout: float = 120, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except (AssertionError, json.JSONDecodeError):
            pass  # not there yet, or a status read while the health printed nothing
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
        self.bad_sidecar = f"livesync-it-bad-{self.id}"
        self.env_sidecar = f"livesync-it-env-{self.id}"
        self.volumes = [
            f"livesync-it-{name}-{self.id}" for name in ("vault", "db", "status", "dev")
        ]
        self.files = Path(tempfile.mkdtemp(prefix="livesync-it-"))

    def exec(self, *args: str, input: str | None = None) -> str:
        return docker("exec", "-i", self.sidecar, *args, input=input)

    def curl(self, method: str, path: str, body: Any = None) -> str:
        """A request to CouchDB as its admin, from inside its own container (it has curl)."""
        data = (
            [] if body is None else ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        )
        return docker(
            "exec", "-i", self.couch, "curl", "-sS", "-X", method,
            "-u", f"{ADMIN}:{ADMIN_PASSWORD}", *data, f"http://127.0.0.1:5984{path}",
            input=None if body is None else json.dumps(body),
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

    def health(self, container: str | None = None) -> dict[str, Any]:
        out = docker(
            "exec", container or self.sidecar, "horae-livesync", "health", "--json", check=False
        )  # fmt: skip
        health: dict[str, Any] = json.loads(out)
        return health

    def offline_health(self) -> dict[str, Any]:
        """The health from the status volume while the sidecar is stopped."""
        out = docker(
            "run", "--rm", "--read-only", "-v", f"{self.volumes[2]}:/status:ro", self.image,
            "health", "--json", check=False,
        )  # fmt: skip
        health: dict[str, Any] = json.loads(out)
        return health

    def in_vault(self, *command: str) -> str:
        """A shell command on the vault volume, in a throwaway container (sidecar up or not)."""
        return docker(
            "run", "--rm", "--entrypoint", "sh", "-v", f"{self.volumes[0]}:/vault", self.image,
            "-c", " ".join(command),
        )  # fmt: skip

    def synced_names(self) -> str:
        self.device("sync")
        return self.device("ls")


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
        # The sidecar's own CouchDB user: admin of the LiveSync database only. (A fresh
        # single-node CouchDB has no `_users` database yet.)
        assert '"ok":true' in s.curl("PUT", "/_users")
        user = {"name": USER, "password": PASSWORD, "roles": [], "type": "user"}
        assert '"ok":true' in s.curl("PUT", f"/_users/org.couchdb.user:{USER}", user)
        security = {"admins": {"names": [USER]}, "members": {"names": [USER]}}
        assert '"ok":true' in s.curl("PUT", f"/{DATABASE}/_security", security)
        docker(
            "run", "--rm", "--user", "10001:10001", "--entrypoint", "sh",
            "-v", f"{s.volumes[0]}:/vault", s.image,
            "-c", "printf 'first note\\n' > /vault/welcome.md",
        )  # fmt: skip
        docker(
            "run", "-d", "--name", s.sidecar, "--network", s.network, "--init",
            "--read-only", "--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{s.files / 'config'}:/config:ro",
            "-v", f"{s.files / 'secrets'}:/run/secrets:ro",
            "-v", f"{s.volumes[0]}:/vault", "-v", f"{s.volumes[1]}:/livesync",
            "-v", f"{s.volumes[2]}:/status", "-e", "TZ=UTC",
            "-e", "HORAE_LIVESYNC_VERBOSE_LOG=1", s.image,
        )  # fmt: skip
        yield s
    finally:
        print(docker("logs", s.sidecar, check=False)[-30000:])
        print(docker("exec", s.sidecar, "cat", "/status/status.json", check=False))
        print(docker("logs", s.bad_sidecar, check=False)[-5000:])
        print(docker("logs", s.env_sidecar, check=False)[-5000:])
        docker("rm", "-f", "-v", s.sidecar, s.bad_sidecar, s.env_sidecar, s.couch, check=False)
        for volume in s.volumes:
            docker("volume", "rm", "-f", volume, check=False)
        docker("network", "rm", s.network, check=False)
        shutil.rmtree(s.files, ignore_errors=True)


def write_files(s: Stack) -> None:
    """livesync.yaml, the two secret files, and the other device's settings (all synthetic,
    world-readable so that uid 10001 in the containers can read the bind mounts)."""
    for name in ("config", "config-env", "secrets", "secrets-bad", "device"):
        (s.files / name).mkdir()
    document = config_document(Path("/run/secrets"))
    document["couchdb"].update(
        username=USER, database=DATABASE, password_secret="LIVESYNC_COUCHDB_PASSWORD"
    )
    document["health"] = {"probe_interval_s": PROBE_INTERVAL_S, "max_age_s": MAX_AGE_S}
    (s.files / "config" / "livesync.yaml").write_text(yaml.safe_dump(document))
    # The Coolify layout: both secrets from environment variables, no secrets directory.
    from_env = {**document, "secrets": {"refs": {name: {"env": name} for name in ENV_SECRETS}}}
    (s.files / "config-env" / "livesync.yaml").write_text(yaml.safe_dump(from_env))
    for directory, password in (("secrets", PASSWORD), ("secrets-bad", "synthetic-wrong-pw")):
        (s.files / directory / "LIVESYNC_COUCHDB_PASSWORD").write_text(password + "\n", "utf-8")
        (s.files / directory / "LIVESYNC_E2EE_PASSPHRASE").write_text(PASSPHRASE + "\n", "utf-8")
    # The other device writes as the CouchDB admin, with the same passphrase.
    defaults = json.loads((FIXTURES / "cli-defaults.json").read_text())
    settings = render_settings(defaults, parse(document), ADMIN_PASSWORD, PASSPHRASE)
    settings["couchDB_USER"] = ADMIN
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
    # The status is rewritten every probe (5 s here): wait for one after the traffic above.
    def settled() -> dict[str, Any] | None:
        health = stack.health()
        done = health["files_out"] >= 2 and health["files_in"] >= 2  # a note, then a deletion
        return health if done and health["healthy"] and health["pending_changes"] == 0 else None

    health = eventually(settled, what="the traffic counted and no pending changes")
    assert health["lag_s"] == 0
    assert health["live"] is True
    assert health["last_sync_at"] is not None
    assert (health["max_age_s"], health["max_lag_s"]) == (MAX_AGE_S, 300)
    mode = stack.exec("sh", "-c", "stat -c %a /tmp/livesync-*/settings.json")
    assert mode.strip() == "600"
    # The init process is PID 1, horae-livesync its child.
    assert stack.exec("cat", "/proc/1/comm").strip() in ("docker-init", "tini")


def test_deletions_while_the_sidecar_is_down(stack: Stack) -> None:
    """README, "Known limitation: deletions while the sidecar is down". `welcome.md` was in the
    folder at the sidecar's (only) start; `during-run.md` is created while it runs."""
    stack.exec("sh", "-c", "printf 'created while running\\n' > /vault/during-run.md")
    eventually(lambda: "during-run.md" in stack.synced_names(), what="during-run.md in CouchDB")

    docker("stop", stack.sidecar)
    stopped = stack.offline_health()
    assert stopped["state"] == "exited"
    assert stopped["live"] is False
    assert "not_live" in stopped["problems"]
    eventually(
        lambda: "status_stale" in stack.offline_health()["problems"],
        timeout=MAX_AGE_S + 30,
        what="a stale status",
    )
    stack.in_vault("rm", "/vault/welcome.md", "/vault/during-run.md")
    docker("start", stack.sidecar)
    eventually(lambda: stack.health()["healthy"], what="healthy after the restart")

    # Present at the previous start, deleted while down: deleted in CouchDB on the next start.
    eventually(
        lambda: "welcome.md" not in stack.synced_names(), what="welcome.md deleted in CouchDB"
    )
    assert stack.in_vault("test -e /vault/welcome.md || echo gone").strip() == "gone"
    # Created during the previous run, deleted while down: restored from CouchDB.
    restored = eventually(
        lambda: stack.in_vault("cat /vault/during-run.md 2>/dev/null || true"),
        what="during-run.md restored in the folder",
    )
    assert restored == "created while running\n"
    assert "during-run.md" in stack.synced_names()


def test_a_couchdb_outage_is_reported_then_recovers(stack: Stack) -> None:
    eventually(lambda: stack.health()["healthy"], what="healthy before the outage")
    docker("stop", stack.couch)
    try:
        down = eventually(
            lambda: (h := stack.health())["remote_ok"] is False and not h["healthy"] and h,
            what="the outage reported",
        )
        assert set(down["problems"]) & {"unreachable", "timeout"}
    finally:
        docker("start", stack.couch)
    eventually(lambda: '"ok"' in stack.curl("GET", "/_up"), what="CouchDB back")
    eventually(lambda: stack.health()["healthy"], timeout=300, what="healthy after the outage")
    stack.exec("sh", "-c", "printf 'after the outage\\n' > /vault/after-outage.md")
    eventually(lambda: "after-outage.md" in stack.synced_names(), what="sync after the outage")


def test_a_wrong_password_is_not_retried(stack: Stack) -> None:
    """A second sidecar with a wrong password: `unauthorized`, the CLI never started, and one
    request with the refused credentials (CouchDB locks a user out after repeated failures)."""
    tmpfs = "uid=10001,gid=10001,mode=0700"
    docker(
        "run", "-d", "--name", stack.bad_sidecar, "--network", stack.network, "--init",
        "--read-only", "--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
        "--tmpfs", f"/vault:{tmpfs}", "--tmpfs", f"/livesync:{tmpfs}",
        "--tmpfs", f"/status:{tmpfs}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{stack.files / 'config'}:/config:ro",
        "-v", f"{stack.files / 'secrets-bad'}:/run/secrets:ro", stack.image,
    )  # fmt: skip
    health = eventually(
        lambda: (h := stack.health(stack.bad_sidecar)).get("state") == "auth_failed" and h,
        what="auth_failed",
    )
    assert "unauthorized" in health["problems"]
    assert health["remote_retry_at"] is not None
    time.sleep(4 * PROBE_INTERVAL_S)  # four probe intervals: no probe, no CLI
    health = stack.health(stack.bad_sidecar)
    assert health["state"] == "auth_failed"
    assert health["restarts"] == 0
    # CouchDB logs each request with the client's address and user. The address was used
    # before by the other device's throwaway containers (the admin): leave those out.
    address = docker(
        "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        stack.bad_sidecar,
    ).strip()  # fmt: skip
    requests = [
        line
        for line in logs(stack.couch).splitlines()
        if f" {address} " in line and f" {ADMIN} " not in line
    ]
    assert len(requests) == 1, requests  # the first probe, nothing since
    assert " 401 " in requests[0]
    log = logs(stack.bad_sidecar)
    assert "refused the credentials (unauthorized)" in log
    assert "[Daemon]" not in log  # the CLI never started


def test_secrets_from_environment_variables(stack: Stack) -> None:
    """The Coolify layout: `secrets.refs: {NAME: {env: NAME}}` and the two secrets as
    environment variables, no /run/secrets. The sidecar syncs, and the LiveSync CLI it starts
    does not get them in its environment."""
    tmpfs = "uid=10001,gid=10001,mode=0700"
    docker(
        "run", "-d", "--name", stack.env_sidecar, "--network", stack.network, "--init",
        "--read-only", "--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev",
        "--tmpfs", f"/vault:{tmpfs}", "--tmpfs", f"/livesync:{tmpfs}",
        "--tmpfs", f"/status:{tmpfs}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{stack.files / 'config-env'}:/config:ro",
        "-e", f"LIVESYNC_COUCHDB_PASSWORD={PASSWORD}",
        "-e", f"LIVESYNC_E2EE_PASSPHRASE={PASSPHRASE}", stack.image,
    )  # fmt: skip
    try:
        eventually(
            lambda: stack.health(stack.env_sidecar)["healthy"],
            timeout=240,
            what="the sidecar with secrets from the environment healthy",
        )
        # It fetched the vault from CouchDB: the password and the passphrase both worked.
        eventually(
            lambda: "after-outage.md" in docker("exec", stack.env_sidecar, "ls", "/vault"),
            what="the vault fetched with secrets from the environment",
        )
        # The environment of every LiveSync CLI process (argv[0] is node; the whole command
        # line would match this shell's own too): no variable of ours.
        environ = docker(
            "exec", stack.env_sidecar, "sh", "-c",
            "for p in /proc/[0-9]*; do"
            " first=$(tr '\\0' '\\n' < $p/cmdline 2>/dev/null | head -n 1);"
            " case \"$first\" in node|*/node) echo \"== $p\";"
            " tr '\\0' '\\n' < $p/environ;; esac; done",
        )  # fmt: skip
        assert "== /proc/" in environ, "no LiveSync CLI process found"
        for name in ENV_SECRETS:
            assert name not in environ
        forms = [form.encode() for form in mask_forms([PASSWORD, PASSPHRASE], USER)]
        log = logs(stack.env_sidecar).encode() + environ.encode()
        for form in forms:
            assert form not in log, "a form of a secret in the log or the CLI's environment"
    finally:
        docker("rm", "-f", "-v", stack.env_sidecar, check=False)


def test_no_secret_is_left_anywhere(stack: Stack) -> None:
    """Every encoded form of both secrets, in everything the sidecar writes or shows: its log,
    its status, its local database, the vault, and `docker inspect`."""
    forms = [form.encode() for form in mask_forms([PASSWORD, PASSPHRASE], USER)]
    places = {
        "log": logs(stack.sidecar).encode(),
        "status.json": stack.exec("cat", "/status/status.json").encode(),
        "inspect": docker("inspect", stack.sidecar).encode(),
        "volumes": subprocess.run(  # noqa: S603
            ["docker", "exec", stack.sidecar, "tar", "-C", "/", "-cf", "-",  # noqa: S607
             "livesync", "vault", "status"],
            capture_output=True, timeout=300, check=True,
        ).stdout,
    }  # fmt: skip
    assert len(places["volumes"]) > 10_000
    for place, data in places.items():
        for form in forms:
            assert form not in data, f"a form of a secret in {place}"
