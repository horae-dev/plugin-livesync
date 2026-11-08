# SPDX-License-Identifier: Apache-2.0
"""`horae-livesync`: the sidecar's command (the container's entrypoint).

    horae-livesync run     [--config F] [--verbose-log]
                                          render the CLI settings, run the daemon, report
    horae-livesync check   [--config F]   validate livesync.yaml (its `settings` against the
                                          pinned CLI too) and resolve its secrets
    horae-livesync health  [--status-dir D] [--max-lag-s N] [--max-age-s N] [--json]
                                          exit 0 when healthy, 1 otherwise (HEALTHCHECK); the
                                          thresholds default to livesync.yaml's (in the status)

Exit codes: 0 ok, 1 unhealthy, 2 usage error, 78 configuration refused (EX_CONFIG).
Nothing it prints holds a secret value.
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from horae_livesync import config, status
from horae_livesync.secrets import SecretError
from horae_livesync.supervisor import DEFAULT_CLI, EX_CONFIG, Layout, Supervisor, cli_defaults

DEFAULT_CONFIG = "/config/livesync.yaml"
CLI_COMMAND: Sequence[str] = DEFAULT_CLI
"""The LiveSync CLI `check` asks for its default settings (the image's; tests change it)."""
EX_USAGE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="horae-livesync", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the LiveSync CLI daemon and report its status")
    check = commands.add_parser("check", help="validate livesync.yaml and its secrets")
    for sub in (run, check):
        sub.add_argument(
            "--config", default=os.environ.get("HORAE_LIVESYNC_CONFIG", DEFAULT_CONFIG)
        )
    run.add_argument("--vault", default="/vault")
    run.add_argument("--database", default="/livesync")
    run.add_argument("--status-dir", default="/status")
    run.add_argument("--private-dir", default="/tmp")  # noqa: S108 (a tmpfs in the container)
    run.add_argument(
        "--verbose-log",
        action="store_true",
        default=os.environ.get("HORAE_LIVESYNC_VERBOSE_LOG") == "1",
        help="forward the CLI's verbose output (file paths, every request) to the log",
    )
    health = commands.add_parser("health", help="exit 0 when the last status is healthy")
    health.add_argument("--status-dir", default="/status")
    health.add_argument("--max-lag-s", type=int, default=None, help="default: livesync.yaml's")
    health.add_argument("--max-age-s", type=int, default=None, help="default: livesync.yaml's")
    health.add_argument("--json", action="store_true", help="print the health as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "health":
        return _health(args)
    try:
        loaded = config.load(Path(args.config))
        if args.command == "check":
            resolver = loaded.resolver()
            resolver.resolve(loaded.couchdb.password_secret)
            resolver.resolve(loaded.passphrase_secret)
            _check_settings(loaded)
            print("livesync.yaml and its secrets are valid")
            return 0
    except (config.ConfigError, SecretError) as exc:
        print(f"configuration refused: {exc}", file=sys.stderr)
        if args.command == "run":
            _config_error_status(Path(args.status_dir), str(exc))
        return EX_CONFIG
    layout = Layout(
        vault=Path(args.vault),
        database=Path(args.database),
        status_dir=Path(args.status_dir),
        private_dir=Path(args.private_dir),
        cli_version=os.environ.get("LIVESYNC_CLI_VERSION", "unknown"),
        verbose_log=args.verbose_log,
    )
    supervisor = Supervisor(loaded, layout)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


def _check_settings(loaded: config.Config) -> None:
    """`settings` against the pinned CLI's own defaults (names and types), as `run` does."""
    try:
        with tempfile.TemporaryDirectory(prefix="livesync-check-") as private:
            defaults = cli_defaults(CLI_COMMAND, Path(private), os.environ)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise config.ConfigError(
            f"cannot ask the LiveSync CLI for its settings ({type(exc).__name__})"
        ) from None
    config.render_settings(defaults, loaded, "check-only", "check-only")


def _config_error_status(directory: Path, message: str) -> None:
    document = status.new_status(os.environ.get("LIVESYNC_CLI_VERSION", "unknown"))
    document["process"]["state"] = "config_error"
    document["config_error"] = message[:300]
    with contextlib.suppress(OSError):
        status.write(directory, document)


def _health(args: argparse.Namespace) -> int:
    try:
        document = status.read(args.status_dir)
    except ValueError:
        document = {"schema_version": None}
    for name in ("max_lag_s", "max_age_s"):
        value = getattr(args, name)
        if value is not None and not 0 < value <= 86400:
            print(f"--{name.replace('_', '-')} must be 1 to 86400 seconds", file=sys.stderr)
            return EX_USAGE
    minimum = status.GRACE_PROBES * status.probe_interval(document)
    if args.max_age_s is not None and args.max_age_s < minimum:
        print(f"--max-age-s must be at least 3 probe intervals ({minimum} s)", file=sys.stderr)
        return EX_USAGE
    health = status.evaluate(
        document, datetime.now(UTC), max_lag_s=args.max_lag_s, max_age_s=args.max_age_s
    )
    if args.json:
        print(json.dumps(health, sort_keys=True))
    else:
        print("healthy" if health["healthy"] else "unhealthy: " + ", ".join(health["problems"]))
    return 0 if health["healthy"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
