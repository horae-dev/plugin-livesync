# Setup and recovery guide

One end-to-end path, from nothing to a phone that syncs with a vault folder Horae can read,
then what to do when something goes wrong. Reference material (every `livesync.yaml` key, the
health problems, snapshots) stays in the [README](../README.md); this page links to it
instead of repeating it. The core's own Coolify guide
([`docs/operations/coolify.md`](https://github.com/horae-dev/horae/blob/main/docs/operations/coolify.md))
covers deploying Horae itself and lists `vault-sync` (the CouchDB below) as one of the
resources around it.

Verification legend: statements about this repository were checked against its code and
tests. Statements about the Obsidian plug-in, CouchDB and Coolify come from the upstream
documentation cited next to them, and anything I could not confirm is marked **(unverified)**.
Upstream pages relied on:

- [Self-hosted LiveSync, `docs/setup_own_server.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/setup_own_server.md)
  (CORS, credentials, setup URI generator),
- [`README.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/README.md) ("Copy settings as a new Setup URI"),
- [`docs/settings.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/settings.md),
  [`docs/troubleshooting.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/troubleshooting.md) and
  [`docs/recovery.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/recovery.md)
  (listed in the upstream README; consult them for the Obsidian-side screens),
- [the CLI README at the pinned tag](https://github.com/vrtmrz/obsidian-livesync/blob/1.0.46-patch2-cli/src/apps/cli/README.md).

Placeholders used below: `couchdb.example.org` (CouchDB's host name), `obsidian` (database),
`horae` (the sidecar's CouchDB user). Replace them; use your own values everywhere.

## 0. What you end up with

```
phone / desktop Obsidian ──E2EE──▶ CouchDB (own domain, no SSO) ◀── sidecar ──▶ /vault ◀── Horae
```

CouchDB only ever holds encrypted data. The sidecar holds the passphrase, decrypts, and keeps
the plain files in `/vault`, which Horae's other plugins read and write.

## 1. CouchDB on Coolify

1. In Coolify, add a resource from the one-click service **Obsidian LiveSync** (a CouchDB
   configured for the plug-in). Name it `vault-sync`, as in the core's reference deployment.
   The exact template contents depend on your Coolify version **(unverified: I did not deploy
   it; check what it sets, below)**.
2. Give it **its own domain** (for example `couchdb.example.org`) over HTTPS with a certificate
   from a public CA (Coolify's Let's Encrypt is one). The sidecar cannot use a private CA or a
   self-signed certificate ([README, TLS](../README.md#tls)); neither is a good idea for a
   phone either.
3. **No SSO on that domain.** The Obsidian plug-in speaks CouchDB's own HTTP API with basic
   credentials and cannot go through a forward-auth or OIDC login; the core's deployment keeps
   `vault-sync` outside the SSO for this reason. CouchDB's own authentication is the protection,
   so use a long random admin password.
4. Check the settings the plug-in needs. Upstream's
   [server guide](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/setup_own_server.md)
   names CORS: `credentials = true`, `origins = app://obsidian.md,capacitor://localhost,http://localhost`
   and `headers = accept, authorization, content-type, origin, referer` (section `[cors]`;
   `enable_cors = true` under `[httpd]` is the usual companion, **unverified** in the page I read),
   and, behind a reverse proxy, no response buffering and a request-size limit large enough for
   chunks (its Nginx example uses `proxy_buffering off` and `client_max_body_size 50M`).
   Coolify's template normally sets CORS already; open CouchDB's Fauxton
   (`https://couchdb.example.org/_utils`, Configuration) and compare. If a proxy sits in front,
   check its limits too. Upstream also lists `require_valid_user` and size limits in its
   own CouchDB config; I did not find them in the page I fetched **(unverified)**.
5. Create the database (`obsidian`), either in Fauxton or by letting the first Obsidian client
   do it (step 2).
6. Create a **dedicated CouchDB user for the sidecar** (Fauxton, `_users`, or
   `PUT /_users/org.couchdb.user:horae` with the admin credentials), and make it an **admin of
   the `obsidian` database only** (Fauxton, database, Permissions, Admins), not a server admin:
   members cannot write design documents, which LiveSync needs. Why a separate user: CouchDB
   3.4 and later lock a user out of an address after repeated failed logins. If the sidecar's
   password is ever wrong, only `horae` is locked out and the phones keep working; the user can
   also be revoked or rotated alone. The sidecar additionally stops using refused credentials for
   15 minutes ([README, Health](../README.md#health)).

Check: `curl -u horae:<password> https://couchdb.example.org/obsidian` returns the database's
JSON once the database exists (do not paste the password in shell history you keep; use
`--netrc` or an env var).

## 2. Obsidian: Self-hosted LiveSync on desktop and phone

Install **Self-hosted LiveSync** from Obsidian's Community plugins on the **desktop first**.
Use the plug-in's own setup wizard or its settings pane
(upstream's screens change between versions, so follow its on-screen text and
[`docs/settings.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/settings.md) **(unverified: not re-read for this guide)**).

Enter, on the desktop:

| Setting | Value |
|---|---|
| Remote type | CouchDB |
| URI | `https://couchdb.example.org` |
| Username / password | the **clients'** user (not `horae`): a separate account, so the sidecar and the devices cannot lock each other out |
| Database name | `obsidian` (the same as `couchdb.database`) |
| End-to-end encryption | **on**, with a long passphrase you store in your password manager |
| Obfuscate paths | **on** (the sidecar's default `e2ee.path_obfuscation: true`; the two must agree) |
| Sync mode | LiveSync (or periodic; the sidecar's `sync.mode` is independent) |

The passphrase and path obfuscation must be **identical** on every device and in the sidecar
(`LIVESYNC_E2EE_PASSPHRASE`, `e2ee.path_obfuscation`). A mismatch does not error loudly: the
sidecar sees documents it cannot decrypt or paths it does not recognise.

### Parity settings

LiveSync requires some tweak settings to be the same on all devices (it shows a
"configuration mismatch" dialog when they differ). The sidecar uses the CLI's defaults for
them, and `settings:` in `livesync.yaml` lets you match your clients when they differ
(the full allowlist and values are in the [README](../README.md#configuration-livesyncyaml)):

- `pathNormalizationVersion`: a vault created **before LiveSync 1.0.38** keeps the legacy path
  identity: set `pathNormalizationVersion: 0`. A newer vault uses `1` (the default).
- `E2EEAlgorithm`, `hashAlg`, `chunkSplitterVersion`, `customChunkSize`, `minimumChunkSize`,
  `handleFilenameCaseSensitive`, `useDynamicIterationCount`, `encryptInternalMetadata`,
  `enableCompression`: set each of them to the value your Obsidian plug-in shows if it is not
  the CLI default listed in the README. `idDerivationVersion` can only be `0` for now.

How to read the client's values: the plug-in's settings pane and its mismatch dialog. Anything
the sidecar refuses to take is reported by `horae-livesync check` (step 3). The issue tracking
a real two-client parity check against the Obsidian plug-in is
[#3](https://github.com/horae-dev/plugin-livesync/issues/3); until it lands, treat a mismatch
as a suspect when the first sync misbehaves.

### Setup URI flow (phone)

Typing a long URL, user, password and passphrase on a phone is error-prone. Instead, on the
working desktop run the command palette entry **Copy settings as a new Setup URI** (upstream
README), give it a one-off URI passphrase (different from the E2EE passphrase), and move the
resulting `obsidian://setuplivesync?...` string to the phone through a channel you trust (a
password manager's note, not a chat with the passphrase in the same message). On the phone,
install the plug-in, open the URI (or paste it into the plug-in's "Use the Setup URI" wizard),
enter the URI passphrase, and accept. For a **new device with a non-empty vault**, let it fetch
from the server rather than upload, or you create duplicates: keep the phone's vault empty
before the first sync. The generator script described in the server guide
(`LIVESYNC_*` variables) is an alternative to produce the first URI without the desktop
**(unverified for this setup)**.

## 3. The sidecar

The image is published by this repository's CI as `ghcr.io/horae-dev/horae-livesync`,
linux/amd64 only, signed with cosign ([README, Deploying](../README.md#deploying)). Pin a tag
with `HORAE_LIVESYNC_TAG` (no default): `sha-<short>` of a main commit, a release `X.Y.Z`, or
`<tag>@sha256:<digest>` to pin the digest too. With plain Docker Compose follow the steps
below; on Coolify, the next section.

1. Copy [`deploy/livesync.example.yaml`](../deploy/livesync.example.yaml) next to `horae.yaml`
   and set `couchdb.url`, `couchdb.database`, `couchdb.username: horae`, the E2EE and obfuscation
   values from step 2, and `settings:` if needed. Keep `snapshot.enabled: true` (step 5).
2. Create the two secrets as **files** in the secrets directory (names are the ones in
   `livesync.yaml`), readable by uid `10001` only:

   ```sh
   printf '%s\n' '<horae user password>'  > "$HORAE_SECRETS_DIR/LIVESYNC_COUCHDB_PASSWORD"
   printf '%s\n' '<e2ee passphrase>'      > "$HORAE_SECRETS_DIR/LIVESYNC_E2EE_PASSPHRASE"
   chown 10001:10001 "$HORAE_SECRETS_DIR"/LIVESYNC_* && chmod 0400 "$HORAE_SECRETS_DIR"/LIVESYNC_*
   ```

3. Add [`deploy/compose.livesync.yaml`](../deploy/compose.livesync.yaml) to Horae's stack and
   set `HORAE_LIVESYNC_TAG=<image tag>`, `HORAE_LIVESYNC_CONFIG=<path to livesync.yaml>` and
   `HORAE_SECRETS_DIR`:

   ```sh
   docker compose -f docker-compose.yaml -f compose.livesync.yaml up -d
   ```

   To build the image from source instead, add
   [`deploy/compose.livesync.build.yaml`](../deploy/compose.livesync.build.yaml) as a third
   `-f` and set `HORAE_LIVESYNC_TAG` to a plugin-livesync **commit** (`up -d --build`).

   Volumes it creates: `livesync-vault` mounted at `/vault` in **both** the sidecar and
   `horae` (the plain-file vault), `livesync-db` at `/livesync` (the CLI's local PouchDB; keep
   it, losing it means a full re-fetch), `livesync-status` at `/status` (and `/livesync-status`
   read-only in `horae`) and `livesync-snapshots` at `/snapshots`. Back up `livesync-vault` and
   `livesync-snapshots`.

### On Coolify

The core's Coolify guide
([`docs/operations/coolify.md`](https://github.com/horae-dev/horae/blob/main/docs/operations/coolify.md))
deploys Horae as a "Docker Compose Empty" resource from its template
(`deploy/coolify/docker-compose.yaml`: services `horae-init` and `horae`, volume
`horae-instance`). Such a resource cannot build from a git context, so the sidecar runs the
published image. Coolify has no compose `secrets:` files either: the two secrets are Coolify
variables, and `livesync.yaml` is a Coolify file mount. **(unverified on Coolify: the steps
below follow the core's guide and Coolify's documented compose features, not a test.)**

1. **Edit Compose** of Horae's resource and merge
   [`deploy/coolify.livesync.yaml`](../deploy/coolify.livesync.yaml) into it: the whole
   `livesync` service; in `horae`, the `livesync` entry of `depends_on` and the two
   `livesync-*` volumes (keep everything else of the core's `horae`); the four `livesync-*`
   volumes next to `horae-instance`. No `networks` and no `ports`. The sidecar keeps the same
   hardening as with plain Compose (user 10001, read-only root, tmpfs `/tmp`, no capabilities,
   `no-new-privileges`, an init process), and nothing of the core's service changes.
2. **Environment Variables** of the resource: `HORAE_LIVESYNC_TAG` (as above),
   `LIVESYNC_COUCHDB_PASSWORD` and `LIVESYNC_E2EE_PASSPHRASE` (the values), and `TZ` if not
   UTC. The fragment hands the two secrets to `livesync` only; never add them to `horae`.
3. **Storages**: after the first save, Coolify lists the file mount `/config/livesync.yaml`
   of `livesync`. Put the file's content there: the example with `secrets.refs` pointing at
   the two variables, for example

   ```yaml
   # livesync.yaml on Coolify
   schema_version: 1
   couchdb:
     url: https://couchdb.example.org
     database: obsidian
     username: horae
     password_secret: LIVESYNC_COUCHDB_PASSWORD
   e2ee:
     passphrase_secret: LIVESYNC_E2EE_PASSPHRASE
     path_obfuscation: true
   sync: { mode: live }
   settings: {}
   snapshot:
     enabled: true
     time: "03:00"
   # Both secrets from the environment (Coolify variables), not from files.
   secrets:
     refs:
       LIVESYNC_COUCHDB_PASSWORD: { env: LIVESYNC_COUCHDB_PASSWORD }
       LIVESYNC_E2EE_PASSPHRASE: { env: LIVESYNC_E2EE_PASSPHRASE }
   ```

   The file holds no secret value, and the sidecar only needs to read it (Coolify creates it
   on the host; it must be readable by uid 10001). Every other key of
   [`livesync.example.yaml`](../deploy/livesync.example.yaml) works the same way.
4. Deploy. `horae-livesync check` in the `livesync` container's terminal (Coolify's
   **Terminal** tab) validates the file and resolves both secrets without printing them.

How the variables are used: `horae-livesync` resolves each secret through its `{env: VAR}`
reference (the same rules as `horae.yaml`, schemas decision 25), renders the CLI's settings
into its private tmpfs, and starts the LiveSync CLI with a minimal environment that does
**not** contain the variables (checked by the integration test
`test_secrets_from_environment_variables`). Unlike secret files, environment variables are
visible to whoever can run `docker inspect` on the server or read the resource's variables
in Coolify: the same exposure as the core's own `HORAE_ANTHROPIC_API_KEY` there.

### Then, with either layout

4. In `horae.lock`: give plugin-obsidian `paths.write` = `/vault` and this plugin
   `paths.read` = `/livesync-status`. Add `livesync.status` to a pipeline if you want the health
   in a note or a notification.
5. Verify:

   ```sh
   docker compose exec livesync horae-livesync check    # config and both secrets, no secret printed
   docker compose exec livesync horae-livesync health   # exit 0 when healthy (also the HEALTHCHECK)
   docker compose logs livesync
   ```

   `check` fails with the setting name when `settings:` has a key outside the allowlist.
   Set `HORAE_LIVESYNC_VERBOSE_LOG=1` temporarily to see requests while debugging.

`livesync.status` returns `healthy`, `live`, `state`, `lag_s`, `problems` and the rest
([README](../README.md#livesyncstatus)). `state` goes `starting`, `initial_replication`, `live`.
The image's HEALTHCHECK allows 5 minutes for the first fetch; for a large vault set
`healthcheck: { start_period: 1h }` in the compose override.

## 4. First sync from an empty vault

1. Start with an **empty** `livesync-vault` volume (the compose file's default for a new
   volume). The sidecar then fetches everything from CouchDB; a pre-filled folder is merged
   in both directions by the startup scan, which is fine but harder to reason about.
2. Order: CouchDB up and database created, the desktop synced once (so the database holds your
   vault), then the sidecar, then the phone.
3. Watch `state` move to `live` and `files_in` grow (`horae-livesync health --json` in the
   container, or the `livesync.status` source).

Checklist:

- [ ] `https://couchdb.example.org/_utils` answers and is **not** behind SSO.
- [ ] A note created on the desktop shows up in `/vault` (plain Markdown, the real file name)
      within seconds: `docker compose exec horae ls /vault`.
- [ ] A file created in `/vault` appears in the desktop Obsidian and then on the phone.
- [ ] A note created on the phone appears in `/vault`.
- [ ] `horae-livesync health` exits 0 and `livesync.status` says `healthy: true`, `live: true`.
- [ ] `horae-livesync check` is clean and the log has no line about decryption or "mismatch".
- [ ] CouchDB itself shows no readable titles or contents (Fauxton, a document: encrypted
      payload, obfuscated path) which proves E2EE and obfuscation are active.
- [ ] After the first night (or `horae-livesync snapshot --json` once), `horae-livesync snapshots`
      lists a commit.
- [ ] Read [the deletion limitation](../README.md#known-limitation-deletions-while-the-sidecar-is-down)
      before you delete notes in the folder rather than in Obsidian.

If a check fails, see the troubleshooting table at the end of this page.

## 5. Conflicts

**How they arise.** Two devices edit the same note before they have synced; CouchDB keeps
both revisions as a conflict.

- **In the Obsidian plug-in**: simple conflicts are merged automatically and the rest are
  offered in a dialog to choose or merge (upstream README: "Automatically merge simple
  conflicts"; the dialog is its own UI **(unverified: not exercised here)**). On a phone this
  is where you normally resolve them.
- **In the CLI/sidecar**: conflicts are **not** resolved automatically
  (`resolveConflictsByNewerFile` is `false`, and the sidecar does not let you change it).
  The startup scan skips conflicted documents (the CLI README: "Conflicted documents ... are
  always skipped"), so the folder keeps the version it had. A conflicted note therefore stays
  as it is on disk until the conflict is resolved on some client; it does not make the sidecar
  unhealthy, and the sidecar does not report conflicts in `livesync.status`.
- The README states this behaviour for the pinned CLI but "not exercised in this story": no
  test here creates a conflict.

**Find and inspect.** The CLI's `ls` marks a conflicted file with a trailing `*`
(`path<TAB>size<TAB>mtime<TAB>revision[*]`), `info <path>` lists the current and past
revisions and a `conflicts` field (`N/A` when none), and `resolve <path> <rev>` keeps the
revision you name. Those commands read the CLI's local database. The image runs the CLI only
through the supervisor, with a settings file rendered on the container's tmpfs, so running them
by hand needs that file and exclusive use of `/livesync`. **No command in this repository does
that for you, and I did not test it (unverified).** A cautious procedure, if you must:

1. Prefer resolving it in Obsidian (the plug-in's conflict dialog), on whichever device you
   trust; the change then reaches the folder through the normal sync. This needs no CLI.
2. For the CLI route: `docker compose stop livesync`, then run a one-off container with the
   same volumes whose command is `node /app/dist/index.cjs /livesync --settings <file> --vault
   /vault info <path>` and `... resolve <path> <rev>`, where `<file>` is a settings JSON you
   write yourself with the connection values (it contains the passphrase and password: keep it
   0600 on a tmpfs and delete it). Then `docker compose start livesync`.
   Keep `--write-settings` off. Check the CLI README at the pinned tag for exact argument order.

**Deletions while the sidecar is down.** Do not delete notes in the vault folder while the
sidecar is down: depending on when the file was created, the deletion is either applied to
CouchDB (and every device) late, or silently undone. See the
[known limitation](../README.md#known-limitation-deletions-while-the-sidecar-is-down). Delete
through Obsidian; Horae's consumers refuse to delete or undo while `livesync.status` says
`live: false`.

## 6. Recovery

Pick the smallest tool that works.

**A note was deleted or overwritten everywhere (LiveSync syncs the mistake).** Restore from the
nightly snapshot ([README, Snapshots and Restore](../README.md#restore)):

```sh
docker compose stop livesync
R="docker compose run --rm --no-deps livesync"
$R snapshots --limit 20
$R restore --at 2026-11-01 --dry-run Notes/todo.md
$R restore --at 2026-11-01 Notes/todo.md
docker compose start livesync
```

On start the scan pushes the restored file to CouchDB and so to every device. Prefer
single notes; `--mirror` deletes files and follows the deletion limitation above. Check in
Obsidian that the note came back. Snapshots are nightly: anything younger than the last one
is not in it, and the sidecar must have been up at snapshot time.

**The vault folder is lost or wrong, CouchDB is fine (re-seed from CouchDB).**

1. `docker compose stop livesync`
2. Remove the vault **and** the CLI database volumes (`livesync-vault`, `livesync-db`; a vault
   without its database would be read as "everything was deleted"; remove both together).
   Keep `livesync-snapshots`. Take a copy of the volumes first if unsure.
3. `docker compose up -d livesync`: an empty vault is fetched from CouchDB in full. Watch
   `state` reach `live`. This is also the recovery when `livesync-db` alone is lost.

**CouchDB is lost or damaged, the vault folder is fine.** Create a new database and user as in
step 1, point the devices at it by a fresh Setup URI (a fresh remote needs a **new** upload from
one device; do not mix with the old one), and re-seed: with the sidecar stopped, remove
`livesync-db` only, start it, and the startup scan uploads the folder. **(unverified: the
README documents the scan; this exact full-rebuild sequence has not been exercised by tests.)**
Upstream's [`docs/recovery.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/recovery.md)
has the Obsidian-side "rebuild everything" options.

**Rotate the CouchDB password** (the sidecar's or the clients'; one at a time):

1. Change the password in CouchDB (Fauxton, the user document, or `PUT /_users/...` with the
   admin credentials).
2. Update the secret (file or Coolify variable) and restart the sidecar:
   `docker compose restart livesync`. If the sidecar was started with the old value and
   CouchDB refused it, it waits 15 minutes before trying again (`auth_failed`); a restart
   re-reads the secret and probes at once **(unverified: the retry wait is in the status, a
   restart resets it by creating a new process; not tested)**.
3. For client devices, edit the password in the plug-in settings. Because the user is separate,
   the sidecar and the devices do not interfere.

**Rotate the E2EE passphrase** (this re-encrypts everything: do it deliberately, with a
current snapshot and a copy of the vault):

1. Make sure all devices are synced; take a snapshot
   (`docker compose exec livesync horae-livesync snapshot --json`) and a volume copy.
2. Stop the sidecar. Turn sync off on every device.
3. On one device, change the passphrase in the plug-in and perform the plug-in's
   **overwrite the remote database** (rebuild) operation, which uploads the vault encrypted
   with the new passphrase (see upstream
   [`docs/recovery.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/recovery.md) and
   [`troubleshooting.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/docs/troubleshooting.md)
   for the current wording of the screens **(unverified)**).
4. Update `LIVESYNC_E2EE_PASSPHRASE`, remove the `livesync-db` volume (it holds data for the old
   remote), keep `livesync-vault`, and start the sidecar; it must reach `live`.
5. Re-join the other devices with a fresh Setup URI, **fetching** from the remote; keep the old
   passphrase until all devices have joined.

Passphrase rotation across the sidecar has no automated test here; treat the order above as a
plan to rehearse on a copy first.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `state: auth_failed`, problem `unauthorized` or `forbidden` | Wrong password or user lacks admin on the database. Fix, then wait the retry window or restart. |
| `database_missing` | The database `obsidian` does not exist yet: create it (step 1.5). |
| `tls_error` | The certificate is not from a public CA ([TLS](../README.md#tls)). |
| `initial_replication_failed` on a big vault | Wait; raise `start_period`; check proxy size limits and buffering. |
| Files appear garbled, or never appear | Passphrase, path obfuscation or a parity setting differs from the clients (step 2). |
| `check` refuses `settings` | A key outside the allowlist; remove it. |
| A note keeps its old text after editing on two devices | An unresolved conflict (step 5). |
| A deleted note came back | A deletion while the sidecar was down (known limitation). |
| `lagging` or `sync_stale` | CouchDB or proxy slow or stuck; see the [health table](../README.md#health). |
