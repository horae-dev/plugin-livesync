# SPDX-License-Identifier: Apache-2.0
"""`livesync.yaml`: the sidecar's configuration, and the LiveSync CLI settings rendered from it.

The core's `horae.yaml` (instance schema v1) has no room for a plugin's own settings
(`plugins.<name>` takes only timeouts, and unknown keys are refused), so the sidecar reads a
small file of its own, kept next to `horae.yaml` in the instance repository. Its `secrets`
block follows `horae.yaml`'s exactly (`dir`, `refs`; schemas decision 25), so the CouchDB
password and the E2EE passphrase are provisioned like every other Horae secret. The file
never holds a secret value.

```yaml
schema_version: 1
couchdb:
  url: https://couchdb.example.org     # https; plain http only to a private or service host
  database: obsidian
  username: horae
  password_secret: LIVESYNC_COUCHDB_PASSWORD
e2ee:
  passphrase_secret: LIVESYNC_E2EE_PASSPHRASE
  path_obfuscation: true               # must match the Obsidian clients
sync: { mode: live }                   # or { mode: poll, poll_interval_s: 60 }
health: { probe_interval_s: 30, request_timeout_s: 10 }
settings: {}                           # other LiveSync settings to match the clients
secrets: { dir: /run/secrets, refs: {} }
```

The rendered settings start from the CLI's own defaults (`init-settings` of the pinned CLI),
so a newer CLI brings its defaults along; the sidecar sets only what it owns.
"""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from horae_livesync.secrets import (
    SECRET_NAME,
    EnvRef,
    FileRef,
    SecretRef,
    SecretResolver,
    valid_file_name,
)

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024

# Database names CouchDB accepts.
_DATABASE = re.compile(r"[a-z][a-z0-9_$()+/-]{0,237}")
_ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

# The settings the sidecar sets itself; `settings` may not touch them.
OWNED_SETTINGS = frozenset(
    {
        "remoteType",
        "couchDB_URI",
        "couchDB_USER",
        "couchDB_PASSWORD",
        "couchDB_DBNAME",
        "encrypt",
        "passphrase",
        "usePathObfuscation",
        "liveSync",
        "syncOnStart",
        "periodicReplication",
        "isConfigured",
    }
)
# Credentials, other remotes and anything that could hold a secret: never through `settings`.
_FORBIDDEN_SETTING = re.compile(
    r"(?i)(pass|secret|key|token|credential|jwt|encrypted|header|uri|endpoint|bucket|region"
    r"|user|remote|^p2p_|configuration)"
)


class ConfigError(ValueError):
    """`livesync.yaml` is not valid. Messages name the key, never a value that may be secret."""


@dataclass(frozen=True, slots=True)
class CouchDB:
    url: str
    database: str
    username: str
    password_secret: str


@dataclass(frozen=True, slots=True)
class Config:
    couchdb: CouchDB
    passphrase_secret: str
    path_obfuscation: bool = True
    mode: str = "live"
    poll_interval_s: int = 60
    probe_interval_s: int = 30
    request_timeout_s: int = 10
    settings: Mapping[str, Any] = field(default_factory=dict)
    secrets_dir: Path = Path("/run/secrets")
    secret_refs: Mapping[str, SecretRef] = field(default_factory=dict)

    def resolver(self, environ: Mapping[str, str] | None = None) -> SecretResolver:
        return SecretResolver(self.secrets_dir, self.secret_refs, environ)


def load(path: Path) -> Config:
    """Read and validate `livesync.yaml`."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({exc.strerror})") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{path}: larger than {MAX_CONFIG_BYTES} bytes")
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError:
        raise ConfigError(f"{path}: not valid YAML") from None
    return parse(document)


def parse(document: object) -> Config:
    root = _mapping(
        document,
        "the file",
        {"schema_version", "couchdb", "e2ee", "sync", "health", "settings", "secrets"},
    )
    if root.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION or isinstance(
        root.get("schema_version"), bool
    ):
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")
    couch = _mapping(
        root.get("couchdb"), "couchdb", {"url", "database", "username", "password_secret"}
    )
    e2ee = _mapping(root.get("e2ee"), "e2ee", {"passphrase_secret", "path_obfuscation"})
    sync = _mapping(root.get("sync", {}), "sync", {"mode", "poll_interval_s"})
    health = _mapping(root.get("health", {}), "health", {"probe_interval_s", "request_timeout_s"})
    secrets = _mapping(root.get("secrets", {}), "secrets", {"dir", "refs"})

    mode = sync.get("mode", "live")
    if mode not in ("live", "poll"):
        raise ConfigError("sync.mode must be live or poll")
    obfuscation = e2ee.get("path_obfuscation", True)
    if not isinstance(obfuscation, bool):
        raise ConfigError("e2ee.path_obfuscation must be true or false")
    return Config(
        couchdb=CouchDB(
            url=_url(couch.get("url")),
            database=_text(couch.get("database"), "couchdb.database", _DATABASE),
            username=_username(couch.get("username")),
            password_secret=_secret_name(couch.get("password_secret"), "couchdb.password_secret"),
        ),
        passphrase_secret=_secret_name(e2ee.get("passphrase_secret"), "e2ee.passphrase_secret"),
        path_obfuscation=obfuscation,
        mode=mode,
        poll_interval_s=_int(sync.get("poll_interval_s", 60), "sync.poll_interval_s", 10, 3600),
        probe_interval_s=_int(
            health.get("probe_interval_s", 30), "health.probe_interval_s", 5, 600
        ),
        request_timeout_s=_int(
            health.get("request_timeout_s", 10), "health.request_timeout_s", 1, 60
        ),
        settings=_settings(root.get("settings", {})),
        secrets_dir=_secrets_dir(secrets.get("dir", "/run/secrets")),
        secret_refs=_refs(secrets.get("refs", {})),
    )


def render_settings(
    defaults: Mapping[str, Any], config: Config, password: str, passphrase: str
) -> dict[str, Any]:
    """The CLI's settings: its own defaults, the checked `settings`, then what the sidecar owns."""
    if "couchDB_URI" not in defaults:
        raise ConfigError("the CLI's default settings look wrong (no couchDB_URI)")
    rendered = dict(defaults)
    for key, value in config.settings.items():
        if key not in defaults:
            raise ConfigError(f"settings.{key} is not a setting of this LiveSync CLI")
        default = defaults[key]
        if type(value) is not type(default) and not (
            isinstance(default, float) and type(value) is int
        ):
            raise ConfigError(f"settings.{key} must be a {type(default).__name__}")
        rendered[key] = value
    rendered.update(
        remoteType="",
        couchDB_URI=config.couchdb.url,
        couchDB_USER=config.couchdb.username,
        couchDB_PASSWORD=password,
        couchDB_DBNAME=config.couchdb.database,
        encrypt=True,
        passphrase=passphrase,
        usePathObfuscation=config.path_obfuscation,
        liveSync=config.mode == "live",
        syncOnStart=True,
        periodicReplication=False,
        isConfigured=True,
    )
    return rendered


# --- validation helpers -------------------------------------------------------------------


def _mapping(value: object, where: str, keys: set[str]) -> dict[str, Any]:
    if value is None:
        raise ConfigError(f"{where} is required")
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    for key in value:
        if key not in keys:
            shown = key if isinstance(key, str) and key.isidentifier() else "<key>"
            raise ConfigError(f"{where}: unknown key {shown}")
    return value


def _text(value: object, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigError(f"{where} is missing or not valid")
    return value


def _username(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(not ch.isprintable() or ch == ":" for ch in value)
    ):
        raise ConfigError("couchdb.username is missing or not valid")
    return value


def _secret_name(value: object, where: str) -> str:
    if not isinstance(value, str) or not SECRET_NAME.fullmatch(value):
        raise ConfigError(f"{where} must be a secret name (A-Z, 0-9, _), not a value")
    return value


def _int(value: object, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}")
    return value


def _url(value: object) -> str:
    """https to any host; plain http only to loopback, a private address or a single-label
    service name (a Compose or Coolify service), as the core's `BackendUrl`."""
    if not isinstance(value, str) or len(value) > 2048 or any(not c.isprintable() for c in value):
        raise ConfigError("couchdb.url is missing or not valid")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 (raises on a bad port)
    except ValueError:
        raise ConfigError("couchdb.url is not a valid URL") from None
    if parts.scheme not in ("https", "http") or not host:
        raise ConfigError("couchdb.url must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("couchdb.url must not hold credentials (use password_secret)")
    if parts.query or parts.fragment:
        raise ConfigError("couchdb.url must not have a query or fragment")
    if parts.scheme == "http" and not _private_host(host):
        raise ConfigError("couchdb.url: plain http only to a private address or a service name")
    return value.rstrip("/")


def _private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host
    return address.is_loopback or address.is_private


def _settings(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("settings must be a mapping")
    for key, item in value.items():
        if not isinstance(key, str) or not key.isidentifier():
            raise ConfigError("settings: keys must be LiveSync setting names")
        if key in OWNED_SETTINGS or _FORBIDDEN_SETTING.search(key):
            raise ConfigError(f"settings.{key} is set by the sidecar or may hold a secret")
        if not isinstance(item, str | int | float | bool) or (
            isinstance(item, str) and len(item) > 4096
        ):
            raise ConfigError(f"settings.{key} must be a string, number or boolean")
    return value


def _secrets_dir(value: object) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ConfigError("secrets.dir must be an absolute path")
    return Path(value)


def _refs(value: object) -> dict[str, SecretRef]:
    if not isinstance(value, dict):
        raise ConfigError("secrets.refs must be a mapping")
    refs: dict[str, SecretRef] = {}
    for name, ref in value.items():
        if not isinstance(name, str) or not SECRET_NAME.fullmatch(name):
            raise ConfigError("secrets.refs: keys must be secret names")
        if isinstance(ref, dict) and set(ref) == {"env"} and isinstance(ref["env"], str):
            if not _ENV_VAR.fullmatch(ref["env"]):
                raise ConfigError(f"secrets.refs.{name}.env is not a variable name")
            refs[name] = EnvRef(ref["env"])
        elif isinstance(ref, dict) and set(ref) == {"file"}:
            if not valid_file_name(ref["file"]):
                raise ConfigError(f"secrets.refs.{name}.file must be one file name in secrets.dir")
            refs[name] = FileRef(ref["file"])
        else:
            raise ConfigError(f"secrets.refs.{name} must be {{env: VAR}} or {{file: name}}")
    return refs
