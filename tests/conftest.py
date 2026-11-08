# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a synthetic `livesync.yaml` and its secret files (never real values)."""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "synthetic-couch-password"
PASSPHRASE = "synthetic-e2ee-passphrase"


def config_document(secrets_dir: Path, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "couchdb": {
            "url": "http://couchdb:5984",
            "database": "notes",
            "username": "horae",
            "password_secret": "LIVESYNC_COUCHDB_PASSWORD",
        },
        "e2ee": {"passphrase_secret": "LIVESYNC_E2EE_PASSPHRASE", "path_obfuscation": True},
        "secrets": {"dir": str(secrets_dir)},
    }
    document.update(overrides)
    return document


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "LIVESYNC_COUCHDB_PASSWORD").write_text(PASSWORD + "\n", "utf-8")
    (directory / "LIVESYNC_E2EE_PASSPHRASE").write_text(PASSPHRASE, "utf-8")
    return directory


@pytest.fixture
def cli_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = json.loads((FIXTURES / "cli-defaults.json").read_text("utf-8"))
    return defaults
