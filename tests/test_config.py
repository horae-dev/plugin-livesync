# SPDX-License-Identifier: Apache-2.0
"""`livesync.yaml` validation and the CLI settings rendered from it."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from horae_livesync import config
from horae_livesync.config import ConfigError, parse, render_settings
from horae_livesync.secrets import EnvRef, FileRef

from .conftest import PASSPHRASE, PASSWORD, config_document


def test_a_minimal_file_gets_the_defaults(secrets_dir: Path) -> None:
    loaded = parse(config_document(secrets_dir))
    assert loaded.couchdb.url == "http://couchdb:5984"
    assert loaded.path_obfuscation is True
    assert (loaded.mode, loaded.probe_interval_s, loaded.request_timeout_s) == ("live", 30, 10)
    assert (loaded.max_age_s, loaded.max_lag_s, loaded.auth_retry_s) == (120, 300, 900)


def test_max_age_defaults_to_three_probe_intervals(secrets_dir: Path) -> None:
    loaded = parse(config_document(secrets_dir, health={"probe_interval_s": 60}))
    assert loaded.max_age_s == 180
    assert loaded.secrets_dir == secrets_dir
    assert loaded.resolver().resolve("LIVESYNC_COUCHDB_PASSWORD").reveal() == PASSWORD


def test_load_reads_yaml(tmp_path: Path, secrets_dir: Path) -> None:
    path = tmp_path / "livesync.yaml"
    path.write_text(yaml.safe_dump(config_document(secrets_dir, sync={"mode": "poll"})))
    assert config.load(path).mode == "poll"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot be read"),
        (b"a: [", "not valid YAML"),
        (b"x" * (config.MAX_CONFIG_BYTES + 1), "larger than"),
    ],
)
def test_load_refuses_unreadable_files(tmp_path: Path, content: bytes | None, message: str) -> None:
    path = tmp_path / "livesync.yaml"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ConfigError, match=message):
        config.load(path)


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://couch.example.org/", True),
        ("https://couch.example.org/prefix/couchdb", True),
        ("http://couchdb:5984", True),
        ("http://localhost:5984", True),
        ("http://127.0.0.1:5984", True),
        ("http://192.168.1.20:5984", True),
        ("http://couch.example.org", False),
        ("http://8.8.8.8", False),
        ("ftp://couchdb", False),
        ("https://user:pw@couch.example.org", False),
        ("https://couch.example.org/?a=1", False),
        ("https://couch.example.org/#f", False),
        ("https://couch.example.org:99999", False),
        ("https:///nohost", False),
        ("https://couch.example.org/\n", False),
        (42, False),
    ],
)
def test_couchdb_url(secrets_dir: Path, url: object, accepted: bool) -> None:
    couch = {**config_document(secrets_dir)["couchdb"], "url": url}
    document = config_document(secrets_dir, couchdb=couch)
    if accepted:
        assert not parse(document).couchdb.url.endswith("/")
    else:
        with pytest.raises(ConfigError, match=r"couchdb\.url"):
            parse(document)


def test_errors_never_echo_a_value_pasted_as_a_secret_name(secrets_dir: Path) -> None:
    couch = {**config_document(secrets_dir)["couchdb"], "password_secret": "hunter2-real"}
    with pytest.raises(ConfigError) as error:
        parse(config_document(secrets_dir, couchdb=couch))
    assert "hunter2" not in str(error.value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": True}, "schema_version"),
        ({"couchdb": None}, "couchdb is required"),
        ({"couchdb": []}, "couchdb must be a mapping"),
        ({"e2ee": None}, "e2ee is required"),
        ({"surprise": 1}, "unknown key surprise"),
        ({"sync": {"mode": "push"}}, "sync.mode"),
        ({"sync": {"poll_interval_s": 1}}, "poll_interval_s"),
        ({"health": {"probe_interval_s": True}}, "probe_interval_s"),
        ({"e2ee": {"passphrase_secret": "P_SECRET", "path_obfuscation": "yes"}}, "obfuscation"),
        ({"settings": []}, "settings must be a mapping"),
        ({"settings": {"couchDB_PASSWORD": "x"}}, "set by the sidecar"),
        ({"settings": {"usePathObfuscation": False}}, "set by the sidecar"),
        ({"settings": {"customHeaderSecret": "x"}}, "not a parity setting"),
        ({"settings": {"P2P_Enabled": True}}, "not a parity setting"),
        ({"settings": {"suspendFileWatching": True}}, "not a parity setting"),
        ({"settings": {"syncInternalFiles": True}}, "not a parity setting"),
        ({"settings": {"usePluginSync": True}}, "not a parity setting"),
        ({"settings": {"syncIgnoreRegEx": ".*"}}, "not a parity setting"),
        ({"settings": {"writeLogToTheFile": True}}, "not a parity setting"),
        ({"settings": {"disableCheckingConfigMismatch": True}}, "not a parity setting"),
        ({"settings": {"trashInsteadDelete": False}}, "not a parity setting"),
        ({"settings": {"not a name": 1}}, "setting names"),
        ({"settings": {"hashAlg": ["x"]}}, "must be a str"),
        ({"settings": {"hashAlg": "md5"}}, "must be one of"),
        ({"settings": {"E2EEAlgorithm": "v3"}}, "must be one of"),
        ({"settings": {"chunkSplitterVersion": ""}}, "must be one of"),
        ({"settings": {"pathNormalizationVersion": True}}, "must be a int"),
        ({"settings": {"pathNormalizationVersion": 2}}, "must be one of"),
        ({"settings": {"idDerivationVersion": 1}}, "must be one of"),
        ({"settings": {"customChunkSize": -1}}, "from 0 to 1000"),
        ({"settings": {"useSegmenter": 1}}, "must be a bool"),
        ({"health": {"probe_interval_s": 60, "max_age_s": 120}}, "at least 3 x"),
        ({"health": {"max_lag_s": 10}}, "max_lag_s"),
        ({"health": {"auth_retry_s": 60}}, "auth_retry_s must be an integer from 900"),
        ({"e2ee": {"passphrase_secret": "HORAE_PASSPHRASE"}}, "not HORAE_"),
        ({"e2ee": {"passphrase_secret": "PYTHONPATH"}}, "not HORAE_"),
        ({"secrets": {"dir": "relative"}}, "absolute"),
        ({"secrets": {"dir": "/s", "refs": []}}, "refs must be a mapping"),
        ({"secrets": {"dir": "/s", "refs": {"lower": {"env": "X"}}}}, "secret names"),
        ({"secrets": {"dir": "/s", "refs": {"LD_PRELOAD": {"env": "X"}}}}, "secret names"),
        ({"secrets": {"dir": "/s", "refs": {"A_B": {"env": "1X"}}}}, "variable name"),
        ({"secrets": {"dir": "/s", "refs": {"A_B": {"file": "../x"}}}}, "one file name"),
        ({"secrets": {"dir": "/s", "refs": {"A_B": {"url": "x"}}}}, "must be"),
    ],
)
def test_invalid_files_are_refused(secrets_dir: Path, change: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(config_document(secrets_dir, **change))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database", "Notes"),
        ("database", "_users"),
        ("username", ""),
        ("username", "a:b"),
        ("username", "bad\nname"),
        ("password_secret", None),
    ],
)
def test_invalid_couchdb_fields(secrets_dir: Path, field: str, value: object) -> None:
    couch = {**config_document(secrets_dir)["couchdb"], field: value}
    with pytest.raises(ConfigError, match=f"couchdb.{field}"):
        parse(config_document(secrets_dir, couchdb=couch))


def test_unknown_non_identifier_keys_are_not_echoed(secrets_dir: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key <key>"):
        parse(config_document(secrets_dir, **{"pa ss": 1}))
    with pytest.raises(ConfigError, match="the file must be a mapping"):
        parse(["not", "a", "mapping"])


def test_refs(secrets_dir: Path) -> None:
    refs = {"A_B": {"env": "MY_VAR"}, "C_D": {"file": "c_d.txt"}}
    loaded = parse(config_document(secrets_dir, secrets={"dir": "/run/secrets", "refs": refs}))
    assert loaded.secret_refs == {"A_B": EnvRef("MY_VAR"), "C_D": FileRef("c_d.txt")}


def test_render_settings_overlays_what_the_sidecar_owns(
    secrets_dir: Path, cli_defaults: dict[str, Any]
) -> None:
    loaded = parse(
        config_document(
            secrets_dir,
            settings={"pathNormalizationVersion": 0, "hashAlg": "xxhash32"},
            e2ee={"passphrase_secret": "LIVESYNC_E2EE_PASSPHRASE", "path_obfuscation": False},
        )
    )
    settings = render_settings(cli_defaults, loaded, PASSWORD, PASSPHRASE)
    assert settings["couchDB_URI"] == "http://couchdb:5984"
    assert settings["couchDB_DBNAME"] == "notes"
    assert settings["couchDB_USER"] == "horae"
    assert settings["couchDB_PASSWORD"] == PASSWORD
    assert settings["passphrase"] == PASSPHRASE
    assert settings["encrypt"] is True
    assert settings["usePathObfuscation"] is False
    assert settings["liveSync"] is True
    assert settings["isConfigured"] is True
    assert settings["pathNormalizationVersion"] == 0
    assert settings["hashAlg"] == "xxhash32"
    # Everything else is the CLI's own default.
    assert settings["E2EEAlgorithm"] == cli_defaults["E2EEAlgorithm"]
    assert set(settings) == set(cli_defaults)


def test_poll_mode_turns_live_sync_off(secrets_dir: Path, cli_defaults: dict[str, Any]) -> None:
    loaded = parse(config_document(secrets_dir, sync={"mode": "poll", "poll_interval_s": 30}))
    assert render_settings(cli_defaults, loaded, PASSWORD, PASSPHRASE)["liveSync"] is False


def test_render_settings_checks_names_and_types_against_the_cli(
    secrets_dir: Path, cli_defaults: dict[str, Any]
) -> None:
    loaded = parse(config_document(secrets_dir, settings={"useSegmenter": True}))
    older = {key: value for key, value in cli_defaults.items() if key != "useSegmenter"}
    with pytest.raises(ConfigError, match="not a setting of this LiveSync CLI"):
        render_settings(older, loaded, PASSWORD, PASSPHRASE)
    changed = {**cli_defaults, "useSegmenter": "auto"}
    with pytest.raises(ConfigError, match="another type in this LiveSync CLI"):
        render_settings(changed, loaded, PASSWORD, PASSPHRASE)


def test_every_setting_of_the_pinned_cli_is_classified(cli_defaults: dict[str, Any]) -> None:
    """A CLI bump that adds, renames or removes a setting fails here: decide whether the
    sidecar owns it, allows it (a parity setting) or refuses it, in `config`."""
    owned, allowed = config.OWNED_SETTINGS, set(config.ALLOWED_SETTINGS)
    refused = config.REFUSED_SETTINGS
    assert not owned & allowed
    assert not owned & refused
    assert not allowed & refused
    assert owned | allowed | refused == set(cli_defaults)


def test_allowed_settings_accept_the_cli_defaults(cli_defaults: dict[str, Any]) -> None:
    for key, allowed in config.ALLOWED_SETTINGS.items():
        allowed.check(key, cli_defaults[key])


def test_render_settings_refuses_odd_defaults(secrets_dir: Path) -> None:
    with pytest.raises(ConfigError, match="default settings"):
        render_settings({}, parse(config_document(secrets_dir)), PASSWORD, PASSPHRASE)


def test_snapshot_defaults_to_off_and_is_validated(secrets_dir: Path) -> None:
    from horae_livesync.config import Snapshot, parse

    assert parse(config_document(secrets_dir)).snapshot == Snapshot()
    on = parse(
        config_document(
            secrets_dir,
            snapshot={"enabled": True, "time": "23:30", "exclude": ["a/"], "max_file_mb": 5},
        )
    ).snapshot
    assert (on.enabled, on.time, on.exclude, on.max_file_mb) == (True, "23:30", ("a/",), 5)


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": "yes"},
        {"time": "3am"},
        {"time": "24:00"},
        {"path": "relative"},
        {"path": "/a/../b"},
        {"exclude": "x"},
        {"exclude": [""]},
        {"exclude": ["/abs"]},
        {"exclude": ["../x"]},
        {"max_file_mb": 0},
        {"unknown": 1},
    ],
)
def test_snapshot_refuses_bad_values(secrets_dir: Path, block: dict[str, object]) -> None:
    from horae_livesync.config import ConfigError, parse

    with pytest.raises(ConfigError):
        parse(config_document(secrets_dir, snapshot=block))
