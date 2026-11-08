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
  folder). Its startup scan, on every start, depends on the file's history (checked, see
  [Known limitation](#known-limitation-deletions-while-the-sidecar-is-down)): a file that was
  in the folder at the previous start and is deleted while the daemon is down **is deleted in
  CouchDB**; a file created after the previous start and deleted while it is down **comes
  back** from CouchDB.
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
| `health.max_age_s`, `health.max_lag_s` | The health's thresholds (below): default 120 (at least 3 × `probe_interval_s`) and 300 seconds. |
| `health.auth_retry_s` | After CouchDB refuses the credentials, how long before they are tried again: default 900, at least 900 (below). |
| `settings` | Parity settings only, where the Obsidian clients differ from the CLI's defaults (e.g. `pathNormalizationVersion: 0` for a vault created before 1.0.38). See below. |
| `secrets.dir`, `secrets.refs` | As in `horae.yaml`; a secret name may not be reserved (`HORAE_*`, `LD_*`, `DYLD_*`, `PYTHON*`, `PATH`, `HOME`). |

**`settings` is an allowlist** ([`config.py`](src/horae_livesync/config.py)): only the
settings LiveSync itself requires every device to share (its `TweakCompatibilityRules`, the
ones its "configuration mismatch" dialog compares), each with its type and the pinned CLI's
values:

| Setting | Values |
|---|---|
| `pathNormalizationVersion` | `0` (legacy path identity) or `1` (Unicode NFC, the default) |
| `E2EEAlgorithm` | `""` (V1), `"v2"` (default), `"forceV1"` |
| `hashAlg` | `""` (legacy), `"xxhash32"`, `"xxhash64"` (default), `"mixed-purejs"`, `"sha1"` |
| `chunkSplitterVersion` | `"v1"`, `"v2"`, `"v2-segmenter"`, `"v3-rabin-karp"` (default) |
| `idDerivationVersion` | `0` only (`1` needs an ID derivation key, a secret: not supported yet) |
| `customChunkSize`, `minimumChunkSize`, `longLineThreshold` | integers |
| `handleFilenameCaseSensitive`, `useDynamicIterationCount`, `encryptInternalMetadata`, `enableCompression`, `enableChunkSplitterV2`, `useSegmenter`, `usePluginSyncV2` | `true` / `false` |
| `useEden`, `maxChunksInEden`, `maxTotalLengthInEden`, `maxAgeInEden` | as the clients (Eden is obsolete) |

Everything else is refused and stays at the CLI's default: credentials and other remotes,
P2P and JWT, plugin and internal-file sync, ignore and sync-only patterns, the suspend
switches, logging to a file, `disableCheckingConfigMismatch`, `trashInsteadDelete`, conflict
handling, UI and tuning. Each of the pinned CLI's 177 settings is classified as owned by the
sidecar, allowed or refused, and a test fails when a CLI bump brings an unclassified one.

The rendered settings start from the pinned CLI's own `init-settings` defaults, then
`settings`, then what the sidecar owns (connection, `encrypt: true`, passphrase,
obfuscation, live or poll, `isConfigured`). They are written **0600 into a private directory
on the container's tmpfs** (`/tmp/livesync-*/settings.json`): not in the image or a volume
and never logged, but in memory for the container's whole lifetime, and tmpfs pages can be
swapped out to the host's swap like any memory. `horae-livesync check` validates the file
(its `settings` against the pinned CLI's own defaults too) and resolves both secrets without
printing them.

## Health

The sidecar writes `/status/status.json` after every probe (schema in
[`status.py`](src/horae_livesync/status.py); no secret, no vault path, no file name in it):

- **process**: `starting`, `initial_replication`, `live`, `exited`, `config_error` or
  `auth_failed`; restarts and the last exit code. A CLI that exits is restarted (5 s, doubling
  to 5 min).
- **remote**: a read-only probe of CouchDB (`GET /<db>`) every `probe_interval_s`, the first
  one before the CLI starts: reachable or an error class (`unauthorized`, `forbidden`,
  `database_missing`, `timeout`, `tls_error`, `unreachable`, `http_<code>`, `bad_response`).
- **replication**: the last successful and failed replication request of the CLI (read from
  its `--verbose` output), and the **lag**: the database's `update_seq` minus the CLI's own
  pull checkpoint on the server (the `_local` document whose id the CLI logs). Exact on a
  single CouchDB node, an estimate on a cluster.
- **files**: files written to the vault and sent to CouchDB since the start.
- **thresholds**: `probe_interval_s`, `max_age_s`, `max_lag_s` from `livesync.yaml`.

**The lag** is the time since the CLI's checkpoint last moved while changes are pending: any
progress resets it, so a large backlog that is being worked through is not `lagging`, a stuck
one is. It carries over a CouchDB outage (the checkpoint cannot move while CouchDB is away, so
right after a long outage the lag includes it until the CLI catches up), and starts again
from zero when the sidecar restarts.

**Refused credentials.** On a 401 or 403, from the probe or in the CLI's output, the sidecar
stops the CLI and makes no request with those credentials for `health.auth_retry_s` (15 min by
default), then one probe tries again and the CLI starts only if it passes. Meanwhile the
state is `auth_failed` and the problems hold `unauthorized` or `forbidden`. CouchDB 3.4 and
later lock a user out of an address after repeated failures (`[chttpd_auth_lockout]`,
enforced by default): a wrong password retried every few seconds would keep the sidecar,
and anything sharing its user and address, locked out.

The file is rewritten on every probe, so it lags the CLI by up to `probe_interval_s`. The
container log shows what the CLI prints without `--verbose`; set
`HORAE_LIVESYNC_VERBOSE_LOG=1` (or `run --verbose-log`) to see every request and file path
while debugging. Both secrets are masked in everything forwarded, in every form a line could
carry them: the value, its percent-encoding, JSON escaping, hex, base64 (also misaligned
inside a longer stream) and the base64 of `user:password`.

`horae-livesync health` (the image's `HEALTHCHECK`) and the plugin's source turn it into a
health. **healthy** when none of these `problems` holds:

| Problem | When |
|---|---|
| `status_missing`, `status_invalid` | No status file, or not one this version reads. |
| `status_stale` | Older than `max_age_s`, or more than 60 s in the future (a skewed clock). |
| `config_error`, `not_live` | The configuration was refused; the CLI is not live (starting, exited, `auth_failed`). |
| the probe's error | `unauthorized`, `forbidden`, `timeout`, `unreachable`... (`remote_unreachable` before the first probe). |
| the CLI's error | `initial_replication_failed`, `unreachable`, `server_error`... when the CLI's last failure is newer than its last success and has lasted more than one probe interval (at once when the CLI is not live); before that, the warning `replication_retrying`. |
| `lagging` | Changes pending and the checkpoint has not moved for `max_lag_s`. |
| `lag_unknown` | The lag cannot be measured (the CLI's log changed) more than 3 probe intervals after the sidecar's start; a warning before. |
| `sync_stale` | The CLI's last successful request is older than `max_lag_s` while the copy is not known to be current (changes pending for more than a probe interval, or an unknown lag). A quiet live feed makes no request, so an idle copy that is current never goes stale. |
| `log_reader_failed` | The sidecar stopped reading the CLI's output (it is drained unread, so the CLI never blocks). |

`live` is true when the status is fresh and the CLI is live: a consumer that deletes or
undoes in the vault folder refuses when it is false (see the known limitation below).

### `livesync.status`

```yaml
- collect:
    - livesync.status: {}               # the thresholds of livesync.yaml
    - livesync.status: { max_lag_s: 600 }
```

Returns `healthy`, `live`, `problems`, `warnings`, `state`, `last_sync_at`, `lag_s`,
`pending_changes`, `remote_ok`, `remote_last_ok_at`, `remote_retry_at`, `last_error`,
`last_error_at`, `files_in`, `files_out`, `restarts`, `cli_version`, `status_age_s`,
`max_lag_s`, `max_age_s`. `max_lag_s` and `max_age_s` (1 to 86400) override `livesync.yaml`'s;
`max_age_s` may not be under 3 probe intervals. An unhealthy sync is data, not a tool error:
a pipeline can put it in the morning note or `notify` on it.

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
like the Horae image. It is not published yet. **linux/amd64 only**: that is the platform CI
builds and tests (`docker build --platform linux/amd64`); the upstream CLI image has an arm64
variant, but this image is not tested on it.

[`deploy/compose.livesync.yaml`](deploy/compose.livesync.yaml) is an override for the core's
`deploy/compose` stack: it adds the `livesync` service (read-only root, tmpfs `/tmp`, no
capabilities, `no-new-privileges`, an init process as PID 1, only its two secrets) and gives
`horae` the vault at `/vault` and the status read-only at `/livesync-status`. Then in
`horae.lock`, plugin-obsidian's `paths.write` is `/vault` and this plugin's `paths.read` is
`/livesync-status`. `HORAE_LIVESYNC_REF` (the plugin-livesync commit to build) has no default:
an update is always a deliberate pin.

```
cp livesync.example.yaml /srv/horae/livesync.yaml && $EDITOR /srv/horae/livesync.yaml
printf '%s\n' '<couchdb password>' > "$HORAE_SECRETS_DIR/LIVESYNC_COUCHDB_PASSWORD"
printf '%s\n' '<e2ee passphrase>'  > "$HORAE_SECRETS_DIR/LIVESYNC_E2EE_PASSPHRASE"
chown 10001:10001 "$HORAE_SECRETS_DIR"/LIVESYNC_* && chmod 0400 "$HORAE_SECRETS_DIR"/LIVESYNC_*
HORAE_LIVESYNC_CONFIG=/srv/horae/livesync.yaml HORAE_LIVESYNC_REF=<commit> \
  docker compose -f docker-compose.yaml -f compose.livesync.yaml up -d
docker compose exec livesync horae-livesync check
docker compose exec livesync horae-livesync health
```

On **Coolify** (PLAN §7), add the same service to Horae's compose resource; secrets can be
Coolify variables referenced with `secrets.refs: {LIVESYNC_COUCHDB_PASSWORD: {env: ...}}`
(the sidecar never passes its environment to the CLI). The full guide is #5.

Point the sidecar at an **empty** vault volume the first time: it then fetches everything. A
vault folder that already has files is merged both ways by the startup scan. The image's
`HEALTHCHECK` allows 5 minutes for that first fetch (`not_live` until it is done); for a
large vault raise it in the compose file, e.g. `healthcheck: { start_period: 1h }`.

### Known limitation: deletions while the sidecar is down

> [!WARNING]
> **Delete notes through Obsidian, or in the vault folder only while the sidecar is up.**
> The LiveSync CLI rescans the vault folder against its local database **every time it
> starts**: every container restart, and every restart of the CLI by the sidecar. For a file
> deleted from the folder while the sidecar was down, that scan does one of two things
> (checked by the integration test `test_deletions_while_the_sidecar_is_down`):
>
> - the file was in the folder **at the previous start**: it **is deleted in CouchDB**, and
>   so on every device, when the sidecar starts again;
> - the file was **created after the previous start**: it **comes back** from CouchDB.
>
> So a deletion while the sidecar is down is either propagated late or silently undone, and
> which one depends on when the file was created. Horae's consumers that delete or undo in
> the folder (plugin-obsidian, an undo) check `livesync.status` first and refuse while
> `live` is false.

### CouchDB credentials

Give the sidecar a **dedicated CouchDB user**, admin of the LiveSync database only (members
cannot write design documents), not the server admin and not the user of the Obsidian
clients: a wrong password then locks out only the sidecar, and the user can be revoked on
its own. See "Refused credentials" under [Health](#health) for what the sidecar does on a
401 or 403.

### TLS

`https` to CouchDB needs a certificate from a **public CA**. A private CA or a self-signed
certificate is not supported: the CLI (Node.js) verifies against Node's bundled CA store and
the probe (Python) against the image's system store, so both would need the private CA; Node.js
takes an extra CA only when told to (`NODE_EXTRA_CA_CERTS`, `--use-system-ca`), and the sidecar
runs the CLI with a fixed command and a minimal environment. On a private network, reach CouchDB over
plain `http` by its service name or private address instead (allowed by `couchdb.url`).

### Licences

The image's own code is Apache-2.0. It also carries Node.js and the LiveSync CLI (MIT), the
CLI's npm dependencies (one MPL-2.0 package, `mediabunny`, shipped unmodified), Python
(PSF-2.0) and Debian packages. The licence texts are in `/usr/share/licenses/` (Node.js,
obsidian-livesync at the pinned commit, this project) and
[`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES) lists the rest and where each text is.

## Design

Decisions taken in review of #2 (maintainer: "go with what's good for the project"):

- **Deletions while the sidecar is down** are a documented limitation of the CLI's startup
  scan, not worked around: `livesync.status` exposes `live` and `healthy` so that consumers
  (Horae's undo and delete) refuse while the sidecar is down. No core change in this story.
- **Refused credentials stop all requests** for `health.auth_retry_s` (at least 15 min) and
  are a hard `auth_failed` state, rather than a fast retry that keeps a CouchDB lockout
  alive. A dedicated CouchDB user is the documented setup.
- **The health fails closed**: a replication error newer than the last success, a lag that
  stays unknown past a grace period, and a stale last success while not current are
  problems, with the cause in `problems`. The lag counts from the checkpoint's last move.
- **`settings` is an allowlist** of LiveSync's own parity settings with their values; every
  setting of the pinned CLI is classified (owned, allowed, refused) and a test forces a
  decision on a CLI bump.
- **Secret names and values follow the core's rules** (reserved names, empty and short
  values, `Secret` guards), checked by a contract table mirroring the core's tests.
- **`HORAE_LIVESYNC_REF` is required**; the build uses no apt package (git comes with the
  pinned full Python image of the build stage).
- **Thresholds live in `livesync.yaml`** and travel in the status, so the image's
  `HEALTHCHECK` and the plugin agree; `max_age_s` is at least 3 probe intervals.
- **linux/amd64 only**, an init process as PID 1, licences in the image.
- **Private-CA CouchDB is unsupported** (two CA stores, minimal CLI environment).

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
docker build --platform linux/amd64 -t horae-livesync:test . && HORAE_LIVESYNC_IMAGE=horae-livesync:test uv run pytest -m integration
```

The unit tests run the supervisor against a fake CLI (`tests/fake_cli.py`) that replays a log
recorded from CLI 1.0.46 (`tests/fixtures/`). The integration tests (CI job **Integration**)
start a real CouchDB, the sidecar image and a second LiveSync client on a private Docker
network, all with synthetic credentials (secrets full of awkward characters), and check
two-way sync with E2EE and path obfuscation, deletions (also while the sidecar is down), the
health through a stop, a CouchDB outage and a wrong password (one request, no CLI), and that
no form of a secret is left in the log, the status, the volumes or `docker inspect`. To bump
the CLI, change `LIVESYNC_CLI` (tag and digest), `LIVESYNC_CLI_VERSION` and `LIVESYNC_COMMIT`
in the Dockerfile together, re-record the fixtures (`init-settings` into
`tests/fixtures/cli-defaults.json`, then classify any new setting in `config.py`), refresh
`licenses/obsidian-livesync.LICENSE` and `THIRD_PARTY_NOTICES`, and run both suites.

See [AGENTS.md](AGENTS.md) for every command.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE). The sidecar image also contains the Self-hosted LiveSync CLI and
Node.js (MIT) and their dependencies: [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
