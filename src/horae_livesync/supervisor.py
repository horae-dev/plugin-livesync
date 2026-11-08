# SPDX-License-Identifier: Apache-2.0
"""The sidecar's main process: render the CLI's settings, run `livesync-cli daemon`, report.

1. Read `livesync.yaml` and resolve its two secrets (`config`, `secrets`).
2. Ask the pinned CLI for its default settings (`init-settings`), overlay ours, and write the
   result **0600 into a private directory on tmpfs** (`/tmp` in the container); it is never
   logged and disappears with the container.
3. Run `livesync-cli <database> --settings <file> --vault <vault> --verbose daemon` with a
   minimal environment, forward what it prints without `--verbose` (secrets masked) and read
   the rest for replication events (`logwatch`).
4. Every `health.probe_interval_s`, probe CouchDB (`couch`) and write `status.json`
   (`status`). The first probe comes before the CLI starts. If the CLI exits it is restarted
   with a backoff (5 s doubling to 5 min).
5. **Refused credentials.** When the probe or the CLI gets a 401 or 403, the sidecar stops the
   CLI and makes no request with the credentials for `health.auth_retry_s` (15 min by
   default): CouchDB locks a user out after repeated failures, and a wrong password retried
   every few seconds would keep it locked (and lock out the Obsidian clients sharing that
   user). The state is `auth_failed`, with `remote.error` and `remote.retry_at`; then one
   probe tries again, and the CLI starts only if it passes.
6. On SIGTERM or SIGINT: stop the CLI (SIGTERM, then SIGKILL after 20 s), write the final
   status and exit 0.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from horae_livesync import logwatch, status
from horae_livesync.config import Config, ConfigError, render_settings
from horae_livesync.couch import AUTH_ERRORS, CouchProbe, Probe
from horae_livesync.secrets import SecretError

EX_CONFIG = 78
STOP_GRACE_S = 20
BACKOFF_S = (5, 10, 20, 40, 80, 160, 300)
DEFAULT_CLI = ("node", "/app/dist/index.cjs")


class Prober(Protocol):
    def probe(self, checkpoint_ids: Sequence[str]) -> Probe: ...


@dataclass
class Layout:
    """Where things are in the container."""

    vault: Path = Path("/vault")
    database: Path = Path("/livesync")
    status_dir: Path = Path("/status")
    private_dir: Path = Path("/tmp")  # noqa: S108 (a tmpfs in the container)
    cli: Sequence[str] = DEFAULT_CLI
    cli_version: str = "unknown"
    verbose_log: bool = False
    """Forward the CLI's whole `--verbose` output (file paths, requests) to the container log."""


@dataclass
class Tracker:
    """The status document and the state behind it, updated from events, probes and exits."""

    cli_version: str
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    thresholds: Mapping[str, int] | None = None
    doc: dict[str, Any] = field(init=False)
    checkpoints: OrderedDict[str, None] = field(default_factory=OrderedDict)
    auth_error: str | None = field(default=None, init=False)
    """Set when the CLI's output shows refused credentials; the supervisor takes it."""

    def __post_init__(self) -> None:
        self.doc = status.new_status(self.cli_version, self.thresholds, status.now_iso(self.now()))

    def started(self) -> None:
        process = self.doc["process"]
        process.update(
            state="starting", started_at=status.now_iso(self.now()), log_reader_error=None
        )

    def line(self, text: str) -> None:
        for event in logwatch.events(text):
            self.event(event)

    def event(self, event: logwatch.Event) -> None:
        at = status.now_iso(self.now())
        replication, files = self.doc["replication"], self.doc["files"]
        if event.kind == "phase":
            self.doc["process"]["state"] = event.value
        elif event.kind == "sync_ok":
            replication.update(last_ok_at=at, failing_since=None)
        elif event.kind == "sync_error":
            replication.update(last_error_at=at, last_error=event.value)
            if replication["failing_since"] is None:
                replication["failing_since"] = at
            if event.value in AUTH_ERRORS:
                self.auth_error = event.value
        elif event.kind == "checkpoint":
            self.checkpoints[event.value] = None
            self.checkpoints.move_to_end(event.value)
            while len(self.checkpoints) > logwatch.MAX_CHECKPOINTS:
                self.checkpoints.popitem(last=False)
        elif event.kind == "file_in":
            files["in"] += 1
        elif event.kind == "file_out":
            files["out"] += 1

    def probed(self, probe: Probe) -> None:
        at = status.now_iso(self.now())
        remote, replication = self.doc["remote"], self.doc["replication"]
        remote.update(ok=probe.ok, checked_at=at, error=probe.error)
        if not probe.ok:
            return
        remote.update(last_ok_at=at, update_seq=probe.update_seq, retry_at=None)
        previous = replication["checkpoint_seq"]
        replication["checkpoint_seq"] = probe.checkpoint_seq
        if probe.update_seq is None or probe.checkpoint_seq is None:
            pending = None
        else:
            pending = max(0, probe.update_seq - probe.checkpoint_seq)
        moved = previous is None or probe.checkpoint_seq != previous
        if not pending:
            replication["pending_since"] = None
        elif not replication["pending"] or moved:
            # Behind: the lag counts from the checkpoint's last move (progress resets it).
            replication["pending_since"] = at
        replication["pending"] = pending

    def auth_failed(self, error: str, retry_at: datetime) -> None:
        self.doc["process"]["state"] = "auth_failed"
        self.doc["remote"].update(ok=False, error=error, retry_at=status.now_iso(retry_at))

    def log_reader_failed(self, error: str) -> None:
        self.doc["process"]["log_reader_error"] = error[:64]

    def exited(self, code: int | None, *, restarting: bool) -> None:
        process = self.doc["process"]
        process.update(state="exited", last_exit_code=code, last_exit_at=status.now_iso(self.now()))
        if restarting:
            process["restarts"] += 1

    def config_error(self, message: str) -> None:
        self.doc["process"]["state"] = "config_error"
        self.doc["config_error"] = message[:300]

    def snapshot(self) -> dict[str, Any]:
        self.doc["updated_at"] = status.now_iso(self.now())
        snapshot: dict[str, Any] = json.loads(json.dumps(self.doc))
        return snapshot


def write_private(directory: Path, name: str, data: Mapping[str, Any]) -> Path:
    """Write `data` as JSON to a new file `directory/name`, mode 0600, never through a link."""
    path = directory / name
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    return path


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The CLI's environment: nothing of ours (no secret reference variable), only basics."""
    keep = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}  # noqa: S108
    if "TZ" in environ:
        keep["TZ"] = environ["TZ"]
    return keep


def cli_defaults(cli: Sequence[str], private: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """The default settings of the pinned CLI (`init-settings`)."""
    target = private / "defaults.json"
    target.unlink(missing_ok=True)
    subprocess.run(  # noqa: S603 (fixed command)
        [*cli, "init-settings", str(target)],
        env=child_environment(environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
        check=True,
    )
    defaults = json.loads(target.read_text("utf-8"))
    target.unlink()
    if not isinstance(defaults, dict):
        raise ConfigError("the CLI's init-settings did not write a JSON object")
    return defaults


class Supervisor:
    def __init__(
        self,
        config: Config,
        layout: Layout,
        *,
        environ: Mapping[str, str] | None = None,
        out: TextIO | None = None,
        prober: Prober | None = None,
    ) -> None:
        self.config = config
        self.layout = layout
        self.environ: Mapping[str, str] = os.environ if environ is None else environ
        self.out = out or sys.stdout
        self.tracker = Tracker(
            layout.cli_version,
            thresholds={
                "probe_interval_s": config.probe_interval_s,
                "max_age_s": config.max_age_s,
                "max_lag_s": config.max_lag_s,
            },
        )
        self.prober = prober
        self.masks: tuple[str, ...] = ()
        """Every form of the two secrets to mask in what is forwarded (`logwatch`)."""
        self.stopping = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        with self._lock:
            self.out.write(f"[horae-livesync] {message}\n")
            self.out.flush()

    def write_status(self) -> None:
        with self._lock:
            snapshot = self.tracker.snapshot()
        try:
            status.write(self.layout.status_dir, snapshot)
        except OSError as exc:
            self.log(f"cannot write the status file ({exc.strerror})")

    def prepare(self) -> list[str]:
        """Resolve the secrets, write the settings file and return the daemon's command."""
        resolver = self.config.resolver(self.environ)
        password = resolver.resolve(self.config.couchdb.password_secret).reveal()
        passphrase = resolver.resolve(self.config.passphrase_secret).reveal()
        self.masks = logwatch.mask_forms((password, passphrase), self.config.couchdb.username)
        if self.prober is None:
            couch = self.config.couchdb
            self.prober = CouchProbe(
                couch.url, couch.database, couch.username, password, self.config.request_timeout_s
            )
        private = Path(tempfile.mkdtemp(prefix="livesync-", dir=self.layout.private_dir))
        defaults = cli_defaults(self.layout.cli, private, self.environ)
        settings = render_settings(defaults, self.config, password, passphrase)
        path = write_private(private, "settings.json", settings)
        del settings, password, passphrase
        command = [
            *self.layout.cli,
            str(self.layout.database),
            "--settings",
            str(path),
            "--vault",
            str(self.layout.vault),
            "--verbose",
            "daemon",
        ]
        if self.config.mode == "poll":
            command[-1:-1] = ["--interval", str(self.config.poll_interval_s)]
        return command

    def _pump(self, stream: TextIO) -> None:
        """Read the CLI's output until it closes. A failure marks the status (the health then
        says `log_reader_failed`) and the rest is drained unread, so the CLI never blocks on
        a full pipe."""
        with stream:
            try:
                for raw in stream:
                    with self._lock:
                        self.tracker.line(raw)
                        auth = self.tracker.auth_error is not None
                    if auth:
                        self._wake.set()
                    shown = logwatch.forwarded(raw, self.masks, verbose=self.layout.verbose_log)
                    if shown is not None:
                        with self._lock:
                            self.out.write(shown + "\n")
                            self.out.flush()
            except Exception as exc:
                with self._lock:
                    self.tracker.log_reader_failed(type(exc).__name__)
                self.log(f"stopped reading the CLI's output ({type(exc).__name__})")
                self._wake.set()
                with contextlib.suppress(Exception):
                    for _ in stream:
                        pass
        self._wake.set()  # the CLI closed its output: it is exiting

    def _start(self, command: list[str]) -> subprocess.Popen[str]:
        child = subprocess.Popen(  # noqa: S603 (fixed command)
            command,
            env=child_environment(self.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if child.stdout is None:  # pragma: no cover (stdout=PIPE always gives one)
            raise RuntimeError("the CLI has no output pipe")
        threading.Thread(target=self._pump, args=(child.stdout,), daemon=True).start()
        with self._lock:
            self.tracker.started()
        return child

    def _probe(self) -> Probe:
        if self.prober is None:  # pragma: no cover (prepare() sets it)
            raise RuntimeError("no prober")
        with self._lock:
            ids = list(self.tracker.checkpoints)
        result = self.prober.probe(ids)
        with self._lock:
            self.tracker.probed(result)
        return result

    def stop(self, *_: object) -> None:
        self.stopping.set()
        self._wake.set()

    def run(self) -> int:
        try:
            command = self.prepare()
        except (ConfigError, SecretError) as exc:
            self.log(f"configuration refused: {exc}")
            with self._lock:
                self.tracker.config_error(str(exc))
            self.write_status()
            return EX_CONFIG
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.log(f"cannot prepare the CLI settings ({type(exc).__name__})")
            with self._lock:
                self.tracker.config_error(f"cannot prepare the CLI settings ({type(exc).__name__})")
            self.write_status()
            return EX_CONFIG
        child: subprocess.Popen[str] | None = None
        attempt = 0
        restart_at = 0.0  # monotonic; start at once
        hold_until: float | None = None
        while not self.stopping.is_set():
            now = time.monotonic()
            if hold_until is not None and now >= hold_until:
                hold_until = None
                self.log("trying the CouchDB credentials again")
            refused = self._take_auth_error()
            if refused is None and hold_until is None:
                result = self._probe()
                refused = result.error if result.error in AUTH_ERRORS else None
            if refused is not None:
                hold_until = now + self.config.auth_retry_s
                if child is not None:
                    self._terminate(child)
                    child = None
                retry_at = datetime.now(UTC) + timedelta(seconds=self.config.auth_retry_s)
                with self._lock:
                    self.tracker.auth_error = None
                    self.tracker.auth_failed(refused, retry_at)
                self.log(
                    f"CouchDB refused the credentials ({refused}); the CLI is stopped and they "
                    f"are tried again in {self.config.auth_retry_s} s"
                )
            if hold_until is None:
                if child is not None and (code := child.poll()) is not None:
                    delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                    self.log(f"the LiveSync CLI exited with code {code}; restarting in {delay} s")
                    with self._lock:
                        self.tracker.exited(code, restarting=True)
                    child, restart_at = None, now + delay
                    attempt += 1
                if child is None and now >= restart_at:
                    child = self._start(command)
            self.write_status()
            wait = float(self.config.probe_interval_s)
            if hold_until is not None:
                wait = min(wait, max(0.0, hold_until - now))
            elif child is None:
                wait = min(wait, max(0.0, restart_at - now))
            self._wake.wait(wait)
            self._wake.clear()
            if self._live():
                attempt = 0
        if child is not None:
            self._terminate(child)
        else:
            self.write_status()
        return 0

    def _take_auth_error(self) -> str | None:
        with self._lock:
            refused, self.tracker.auth_error = self.tracker.auth_error, None
        return refused

    def _live(self) -> bool:
        with self._lock:
            return bool(self.tracker.doc["process"]["state"] == "live")

    def _terminate(self, child: subprocess.Popen[str]) -> None:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        with self._lock:
            self.tracker.exited(child.returncode, restarting=False)
        self.write_status()
