<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-livesync

Horae plugin: keep an Obsidian vault as plain files on the server with Self-hosted LiveSync (E2EE CouchDB).

> 🚧 **In progress.** The sidecar and its health source are in (#2). The E2EE parity check
> with the Obsidian plugin (#3), the nightly git snapshot (#4) and the setup and recovery
> guide (#5) are not. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What it does

Two pieces, one repository:

- **The sidecar** (`horae-livesync run`, its own container): runs the official
  **Self-hosted LiveSync CLI** in daemon mode, which mirrors the LiveSync CouchDB database
  into a plain-file vault folder and back. Horae's other plugins (plugin-obsidian) read and
  write that folder. The sidecar renders the CLI's settings from `livesync.yaml` and two
  secrets, supervises the CLI and writes a `status.json`.
- **The plugin** (`livesync`, an MCP server the core starts): one source,
  `livesync.status`, that reads `status.json` and returns the sync's health. It proposes no
  intent and connects to nothing.

```
 Obsidian (desktop, phone) ──E2EE──▶ CouchDB (vault-sync, PLAN §7)
                                        ▲  _changes feed / push
                                        │
 ┌─ livesync (sidecar) ─────────────────┴──┐      ┌─ horae ───────────────────────┐
 │ horae-livesync run                      │      │ plugin-obsidian  ─▶ /vault    │
 │  └─ livesync-cli daemon (official, MIT) │      │ livesync.status ◀─ /livesync- │
 │ /vault  /livesync (PouchDB)  /status    │      │                     status:ro │
 └────┬──────────────────────────────┬─────┘      └──────┬───────────────┬────────┘
      └────────── volume livesync-vault ─────────────────┘               │
                                     └── volume livesync-status ─────────┘
```

## The LiveSync CLI

What this plugin relies on, checked on 2026-11-08 against
[vrtmrz/obsidian-livesync](https://github.com/vrtmrz/obsidian-livesync) at tag
`1.0.46-patch2-cli` (commit `c2e4e91`):

- **Name and source.** "Self-hosted LiveSync CLI", package `self-hosted-livesync-cli`, in the
  plugin's own repository under
  [`src/apps/cli`](https://github.com/vrtmrz/obsidian-livesync/tree/main/src/apps/cli), built
  on the same core as the Obsidian plug-in. Its README calls the command `livesync-cli`.
- **Licence.** MIT (the repository's licence).
- **Distribution.** No npm package (the package is `private`) and no standalone binary:
  build from source (Node.js 22.22 or later, `npm run build`), the systemd installer in
  `src/apps/cli/deploy/`, or the **Docker image `ghcr.io/vrtmrz/livesync-cli`**, published
  by the repository's `cli-docker.yml` workflow with tags `<version>-cli` (here
  `1.0.46-patch2-cli`, digest
  `sha256:4a0675db1ebd6d891624daedb37b1467f60de7b1a090f0252b7a1769fb382701`), `latest` and
  major-minor. The sidecar copies the CLI out of that image, pinned by digest.
- **Daemon mode.** `livesync-cli <database-path> --settings <file> --vault <vault> daemon`
  (the default command): a startup scan between the local database and the vault, one
  replication, then continuous sync both ways, CouchDB to files through the `_changes` feed
  (or `--interval N` polling), files to CouchDB through a file watcher. It exits on SIGTERM.
  The *database path* (the CLI's local PouchDB) and the *vault* are separate directories.
- **Configuration.** A JSON settings file in the Obsidian plug-in's own format:
  `couchDB_URI`, `couchDB_USER`, `couchDB_PASSWORD`, `couchDB_DBNAME`, `encrypt` and
  `passphrase` (E2EE), `usePathObfuscation`, `liveSync`, `isConfigured`, plus every other
  plug-in setting (`init-settings` writes the defaults, 177 keys in 1.0.46). Commands do not
  write the file back unless `--write-settings` is given.
- **Deletions.** The daemon propagates them both ways (checked: a file removed from the
  folder is deleted in CouchDB, a document deleted by another client is removed from the
  folder). Its startup scan does **not**: a file deleted while the daemon was down comes back
  ([CLI README, `mirror`](https://github.com/vrtmrz/obsidian-livesync/blob/main/src/apps/cli/README.md#mirror)).
- **Conflicts.** Not resolved automatically by default (`resolveConflictsByNewerFile` is
  `false`): the scan skips conflicted documents, `ls` marks them with `*`, and `info` /
  `resolve <path> <rev>` handle them (CLI README). Not exercised in this story.
- **Health or status.** None: no status file, endpoint or exit signal while running. Without
  `--verbose` it prints nothing at all when CouchDB goes away (checked). Hence the sidecar's
  own status, below.

## Configuration: `livesync.yaml`

The core's `horae.yaml` (instance schema v1) has no room for a plugin's settings
(`plugins.<name>` takes only timeouts, unknown keys are refused), so the sidecar reads its own
small file, kept next to `horae.yaml` in the instance repository. Its `secrets` block works
exactly as `horae.yaml`'s (schemas decision 25): a secret is the file `<dir>/<NAME>` unless
`refs` points it at `{env: VAR}` or `{file: name}`. See
[`deploy/livesync.example.yaml`](deploy/livesync.example.yaml).

| Key | Meaning |
|---|---|
| `couchdb.url` | CouchDB base URL. `https` to any host; plain `http` only to a private address or a single-label service name. No credentials, query or fragment. |
| `couchdb.database`, `couchdb.username` | The LiveSync database and its user. |
| `couchdb.password_secret` | Name of the secret holding the CouchDB password. |
| `e2ee.passphrase_secret` | Name of the secret holding the E2EE passphrase. E2EE is always on. |
| `e2ee.path_obfuscation` | Default `true` (PLAN §7). Must match the Obsidian clients. |
| `sync.mode` | `live` (default, `_changes` feed) or `poll` with `poll_interval_s` (10 to 3600). |
| `health.probe_interval_s`, `health.request_timeout_s` | Default 30 and 10 seconds. |
| `settings` | Other LiveSync settings to match the clients (e.g. `pathNormalizationVersion: 0` for a vault created before 1.0.38). Only names the pinned CLI knows, with the default's type; credentials, remotes, P2P and anything that may hold a secret are refused. |
| `secrets.dir`, `secrets.refs` | As in `horae.yaml`. |

The rendered settings start from the pinned CLI's own `init-settings` defaults, then
`settings`, then what the sidecar owns (connection, `encrypt: true`, passphrase,
obfuscation, live or poll, `isConfigured`). They are written **0600 into a private directory
on the container's tmpfs** (`/tmp/livesync-*/settings.json`): never on disk, never logged,
gone when the container stops. `horae-livesync check` validates the file and resolves both
secrets without printing them.

## Health

The sidecar writes `/status/status.json` after every probe (schema in
[`status.py`](src/horae_livesync/status.py); no secret, no vault path, no file name in it):

- **process**: `starting`, `initial_replication`, `live`, `exited` or `config_error`; restarts
  and the last exit code. A CLI that exits is restarted (5 s, doubling to 5 min).
- **remote**: a read-only probe of CouchDB (`GET /<db>`) every `probe_interval_s`: reachable or
  an error class (`unauthorized`, `forbidden`, `database_missing`, `timeout`, `tls_error`,
  `unreachable`, `http_<code>`).
- **replication**: the last successful and failed replication request of the CLI (read from
  its `--verbose` output), and the **lag**: the database's `update_seq` minus the CLI's own
  pull checkpoint on the server (the `_local` document whose id the CLI logs), with the time
  it has been behind. Exact on a single CouchDB node, an estimate on a cluster.
- **files**: files written to the vault and sent to CouchDB since the start.

`horae-livesync health` (the image's `HEALTHCHECK`) and the plugin's source turn it into a
health: **healthy** when the status is fresh (`max_age_s`, default 120), the CLI is live,
CouchDB answers and the lag is under `max_lag_s` (default 300); otherwise `problems` lists
the reasons (`status_missing`, `status_stale`, `config_error`, `not_live`, the remote error,
`lagging`). An unknown lag (the CLI's log changed) is a warning, `lag_unknown`, never a wrong
number.

### `livesync.status`

```yaml
- collect:
    - livesync.status: { max_lag_s: 300, max_age_s: 120 }
```

Returns `healthy`, `problems`, `warnings`, `state`, `last_sync_at`, `lag_s`,
`pending_changes`, `remote_ok`, `remote_last_ok_at`, `last_error`, `last_error_at`,
`files_in`, `files_out`, `restarts`, `cli_version`, `status_age_s`. An unhealthy sync is
data, not a tool error: a pipeline can put it in the morning note or `notify` on it.

## Permissions

| Permission | Value | Why |
|---|---|---|
| intents | none | A pure health source. |
| network | none | The plugin reads a file; only the sidecar talks to CouchDB. |
| secrets | none | The plugin never sees the CouchDB password or the passphrase. |
| `paths.read` | the status directory (granted in `horae.lock`, e.g. `/livesync-status`; the manifest asks for none) | `status.json`, opened without following links, at most 64 KiB. |

The sidecar is not a Horae plugin process and has no manifest: what it may touch is set by its
container (below).

## Deploying

The image is built from this repository's [`Dockerfile`](Dockerfile): the official CLI image
pinned by tag and digest (its Node.js runtime and `/app` bundle, unchanged) on
`python:3.12-slim-bookworm` (pinned), with `horae-livesync` as the entrypoint, user `10001`
like the Horae image. It is not published yet.

[`deploy/compose.livesync.yaml`](deploy/compose.livesync.yaml) is an override for the core's
`deploy/compose` stack: it adds the `livesync` service (read-only root, tmpfs `/tmp`, no
capabilities, `no-new-privileges`, only its two secrets) and gives `horae` the vault at
`/vault` and the status read-only at `/livesync-status`. Then in `horae.lock`, plugin-obsidian's
`paths.write` is `/vault` and this plugin's `paths.read` is `/livesync-status`.

```
cp livesync.example.yaml /srv/horae/livesync.yaml && $EDITOR /srv/horae/livesync.yaml
printf '%s\n' '<couchdb password>' > "$HORAE_SECRETS_DIR/LIVESYNC_COUCHDB_PASSWORD"
printf '%s\n' '<e2ee passphrase>'  > "$HORAE_SECRETS_DIR/LIVESYNC_E2EE_PASSPHRASE"
chown 10001:10001 "$HORAE_SECRETS_DIR"/LIVESYNC_* && chmod 0400 "$HORAE_SECRETS_DIR"/LIVESYNC_*
HORAE_LIVESYNC_CONFIG=/srv/horae/livesync.yaml HORAE_LIVESYNC_REF=<commit> \
  docker compose -f docker-compose.yaml -f compose.livesync.yaml up -d
docker compose exec livesync horae-livesync health
```

On **Coolify** (PLAN §7), add the same service to Horae's compose resource; secrets can be
Coolify variables referenced with `secrets.refs: {LIVESYNC_COUCHDB_PASSWORD: {env: ...}}`
(the sidecar never passes its environment to the CLI). The full guide is #5.

Point the sidecar at an **empty** vault volume the first time: it then fetches everything. A
vault folder that already has files is merged both ways by the startup scan.

## Planned work

| Milestone | Epic |
|---|---|
| M3 Dogfood | **LiveSync bridge** — Mirror an E2EE LiveSync CouchDB to plain files on the server, with snapshots. |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Development

The repository was scaffolded from [`template-plugin`](https://github.com/horae-dev/template-plugin):
an MCP stdio server built on [`horae-sdk`](https://github.com/horae-dev/horae/tree/main/sdk), a
`plugin.yaml` generated from the code, and CI (lockfile, ruff, mypy strict, pytest with an 85%
coverage gate, gitleaks, and the aggregate **CI OK** check). `horae-sdk` is not on PyPI yet:
`pyproject.toml` takes it from the core repository's `main` and `uv.lock` pins the commit.

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov
docker build -t horae-livesync:test . && HORAE_LIVESYNC_IMAGE=horae-livesync:test uv run pytest -m integration
```

The unit tests run the supervisor against a fake CLI (`tests/fake_cli.py`) that replays a log
recorded from CLI 1.0.46 (`tests/fixtures/`). The integration tests (CI job **Integration**)
start a real CouchDB, the sidecar image and a second LiveSync client on a private Docker
network, all with synthetic credentials, and check two-way sync with E2EE and path
obfuscation, deletions, and the health. To bump the CLI, change `LIVESYNC_CLI` (tag and
digest) and `LIVESYNC_CLI_VERSION` in the Dockerfile together, re-record the fixtures, and run
both suites.

See [AGENTS.md](AGENTS.md) for every command.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE). The sidecar image also contains the Self-hosted LiveSync CLI (MIT).
