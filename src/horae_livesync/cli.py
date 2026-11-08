# SPDX-License-Identifier: Apache-2.0
"""`horae-livesync`: the sidecar's command (the container's entrypoint).

    horae-livesync run     [--config F]   render the CLI settings, run the daemon, report
    horae-livesync check   [--config F]   validate livesync.yaml and resolve its secrets
    horae-livesync health  [--status-dir D] [--max-lag-s N] [--max-age-s N] [--json]
                                          exit 0 when healthy, 1 otherwise (HEALTHCHECK)

Exit codes: 0 ok, 1 unhealthy, 2 usage error, 78 configuration refused (EX_CONFIG).
Nothing it prints holds a secret value.
"""

import argparse
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from horae_livesync import config, status
from horae_livesync.secrets import SecretError
from horae_livesync.supervisor import EX_CONFIG, Layout, Supervisor

DEFAULT_CONFIG = "/config/livesync.yaml"


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
    health = commands.add_parser("health", help="exit 0 when the last status is healthy")
    health.add_argument("--status-dir", default="/status")
    health.add_argument("--max-lag-s", type=int, default=300)
    health.add_argument("--max-age-s", type=int, default=120)
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
    )
    supervisor = Supervisor(loaded, layout)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


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
