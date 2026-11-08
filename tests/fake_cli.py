# SPDX-License-Identifier: Apache-2.0
"""A stand-in for the LiveSync CLI (`node /app/dist/index.cjs`), for the supervisor's tests.

    fake_cli.py <behaviour> init-settings <path>
    fake_cli.py <behaviour> <database> --settings <file> --vault <dir> --verbose daemon

`init-settings` writes the recorded defaults of CLI 1.0.46. The daemon writes its argv and the
settings it was given to `<database>/seen.json`, prints the recorded log of a real run
(`fixtures/cli-daemon.log`) plus lines holding the password and its encodings (the supervisor
must mask them), then waits for SIGTERM. Behaviours: `ok`, `exit3` (the daemon exits 3 at
once), `badinit` (init-settings fails), `oldcli` (a CLI without `useSegmenter`), `unauth`
(CouchDB answers 401: the daemon logs it and keeps retrying every 0.1 s, as a careless client
would), `badoutput` (prints bytes that are not UTF-8 then a line the reader chokes on).
"""

import base64
import json
import os
import signal
import sys
import time
from pathlib import Path
from urllib.parse import quote

FIXTURES = Path(__file__).parent / "fixtures"


def main(argv: list[str]) -> int:
    behaviour, args = argv[0], argv[1:]
    if args[0] == "init-settings":
        if behaviour == "badinit":
            return 1
        defaults = json.loads((FIXTURES / "cli-defaults.json").read_text("utf-8"))
        if behaviour == "oldcli":
            del defaults["useSegmenter"]
        Path(args[1]).write_text(json.dumps(defaults), "utf-8")
        return 0
    database = Path(args[0])
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text("utf-8"))
    mode = os.stat(args[args.index("--settings") + 1]).st_mode & 0o777
    record = {"argv": args, "settings": settings, "mode": mode, "env": sorted(os.environ)}
    (database / "seen.json").write_text(json.dumps(record), "utf-8")
    if behaviour == "exit3":
        print("[Daemon] Initial replication failed, cannot continue", flush=True)
        return 3
    if behaviour == "unauth":
        with (database / "requests.log").open("a", encoding="utf-8") as requests:
            while True:
                requests.write("GET\n")
                requests.flush()
                print("(debug) [RemoteService]  [REQ] (GET) http://couchdb:5984/notes/ -> 401")
                sys.stdout.flush()
                time.sleep(0.1)
    stopped = False

    def stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    for line in (FIXTURES / "cli-daemon.log").read_text("utf-8").splitlines():
        print(line, flush=True)
    password = settings["couchDB_PASSWORD"]
    basic = base64.b64encode(f"{settings['couchDB_USER']}:{password}".encode()).decode()
    print(f"[Info] leaked {password}", flush=True)
    print(f"[Info] leaked {quote(password, safe='')} {json.dumps(password)}", flush=True)
    print(f"[Info] leaked Authorization: Basic {basic}", flush=True)
    if behaviour == "badoutput":
        print("[Info] LOG-READER-BOOM", flush=True)
    while not stopped:
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
