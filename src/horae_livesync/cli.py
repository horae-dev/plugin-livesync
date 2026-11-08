# SPDX-License-Identifier: Apache-2.0
"""`horae-livesync`: the sidecar's command (the container's entrypoint).

    horae-livesync run     [--config F] [--verbose-log]
                                          render the CLI settings, run the daemon, report
    horae-livesync check   [--config F]   validate livesync.yaml (its `settings` against the
                                          pinned CLI too) and resolve its secrets
    horae-livesync health  [--status-dir D] [--max-lag-s N] [--max-age-s N] [--json]
                                          exit 0 when healthy, 1 otherwise (HEALTHCHECK); the
                                          thresholds default to livesync.yaml's (in the status)

    horae-livesync snapshot  [--config F] [--vault D] [--json]
                                          commit the vault into the snapshot repository now
                                          (75 when another snapshot is running)
    horae-livesync snapshots [--config F] [--limit N]
                                          list the snapshots, newest first
    horae-livesync restore   [--config F] [--commit ID | --at TIME] [--target D] [--mirror]
                             [--dry-run] [--force] [PATH ...]
                                          write a snapshot's files back (README, "Snapshots")

Exit codes: 0 ok, 1 unhealthy, 2 usage error, 78 configuration refused (EX_CONFIG).
Nothing it prints holds a secret value.
"""

import argparse
import contextlib
import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from horae_livesync import config, snapshot, status
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
    take = commands.add_parser("snapshot", help="commit the vault into the snapshot repository")
    listing = commands.add_parser("snapshots", help="list the snapshots, newest first")
    restoring = commands.add_parser("restore", help="write a snapshot's files back")
    for sub in (take, listing, restoring):
        sub.add_argument(
            "--config", default=os.environ.get("HORAE_LIVESYNC_CONFIG", DEFAULT_CONFIG)
        )
    for sub in (take, restoring):
        sub.add_argument("--vault", default="/vault")
    take.add_argument("--json", action="store_true", help="print the result as JSON")
    listing.add_argument("--limit", type=int, default=30)
    restoring.add_argument("--commit", help="a snapshot's id (7 hex digits or more)")
    restoring.add_argument(
        "--at", help="the last snapshot at or before YYYY-MM-DD[THH:MM[:SS]] UTC"
    )
    restoring.add_argument("--target", help="where to write (default: the vault)")
    restoring.add_argument("--mirror", action="store_true", help="also delete what is not in it")
    restoring.add_argument("--dry-run", action="store_true", help="only list what would change")
    restoring.add_argument("--force", action="store_true", help="even if the sidecar is live")
    restoring.add_argument("--status-dir", default="/status")
    restoring.add_argument("paths", nargs="*", help="files or folders (default: everything)")
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
    if args.command in ("snapshot", "snapshots", "restore"):
        return _snapshot_command(args)
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


def _snapshot_command(args: argparse.Namespace) -> int:
    try:
        options = config.load(Path(args.config)).snapshot
        if args.command == "snapshots":
            for entry in snapshot.list_snapshots(options.path, args.limit):
                stamp = entry.time.strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"{entry.commit[:12]} {stamp} {entry.message}")
            return 0
        vault = Path(args.vault)
        if args.command == "snapshot":
            result = snapshot.take(
                vault,
                options.path,
                exclude=options.exclude,
                max_file_mb=options.max_file_mb,
                gc=options.gc,
            )
            if args.json:
                print(json.dumps(dataclasses.asdict(result), sort_keys=True))
            else:
                print(
                    f"snapshot {result.commit[:12] if result.commit else 'unchanged'}: "
                    f"{result.files} files, {result.added} added, {result.modified} modified, "
                    f"{result.deleted} deleted, {result.skipped} skipped"
                )
            return 0
        target = Path(args.target) if args.target else vault
        if target == vault and not args.force and not args.dry_run:
            live = status.evaluate(_read_status(args.status_dir), datetime.now(UTC))["live"]
            if live:
                print(
                    'refused: the sidecar is live; stop it first (README, "Snapshots")',
                    file=sys.stderr,
                )
                return 1
        done = snapshot.restore(
            vault,
            options.path,
            target,
            commit=args.commit,
            at=args.at,
            paths=args.paths,
            mirror=args.mirror,
            dry_run=args.dry_run,
            exclude=options.exclude,
        )
        verb = "would " if args.dry_run else ""
        print(
            f"snapshot {str(done['snapshot'])[:12]}: {verb}write {len(done['written'])} "
            f"files, {verb}delete {len(done['removed'])}"
        )
        for name in done["written"]:
            print(f"  write  {name}")
        for name in done["removed"]:
            print(f"  delete {name}")
        return 0
    except snapshot.SnapshotBusy as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return snapshot.EX_BUSY
    except (config.ConfigError, snapshot.SnapshotError, OSError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return EX_CONFIG if isinstance(exc, config.ConfigError) else 1


def _read_status(directory: str) -> dict[str, object] | None:
    try:
        return status.read(directory)
    except ValueError:
        return None


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
