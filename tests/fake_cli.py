# SPDX-License-Identifier: Apache-2.0
"""A stand-in for the LiveSync CLI (`node /app/dist/index.cjs`), for the supervisor's tests.

    fake_cli.py <behaviour> init-settings <path>
    fake_cli.py <behaviour> <database> --settings <file> --vault <dir> --verbose daemon

`init-settings` writes the recorded defaults of CLI 1.0.46. The daemon writes its argv and the
settings it was given to `<database>/seen.json`, prints the recorded log of a real run
(`fixtures/cli-daemon.log`) plus one line holding the password (the supervisor must mask it),
then waits for SIGTERM. Behaviours: `ok`, `exit3` (the daemon exits 3 at once), `badinit`
(init-settings fails).
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def main(argv: list[str]) -> int:
    behaviour, args = argv[0], argv[1:]
    if args[0] == "init-settings":
        if behaviour == "badinit":
            return 1
        Path(args[1]).write_text((FIXTURES / "cli-defaults.json").read_text("utf-8"), "utf-8")
        return 0
    database = Path(args[0])
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text("utf-8"))
    mode = os.stat(args[args.index("--settings") + 1]).st_mode & 0o777
    record = {"argv": args, "settings": settings, "mode": mode, "env": sorted(os.environ)}
    (database / "seen.json").write_text(json.dumps(record), "utf-8")
    if behaviour == "exit3":
        print("[Daemon] Initial replication failed, cannot continue", flush=True)
        return 3
    stopped = False

    def stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    for line in (FIXTURES / "cli-daemon.log").read_text("utf-8").splitlines():
        print(line, flush=True)
    print(f"[Info] leaked {settings['couchDB_PASSWORD']}", flush=True)
    while not stopped:
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
