# SPDX-License-Identifier: Apache-2.0
"""The image and the deployment files agree with the code."""

import re
from pathlib import Path
from typing import Any

import yaml

from horae_livesync import config

ROOT = Path(__file__).parent.parent


def test_the_example_config_is_valid() -> None:
    loaded = config.load(ROOT / "deploy" / "livesync.example.yaml")
    assert loaded.path_obfuscation is True
    assert loaded.secrets_dir == Path("/run/secrets")


def test_the_dockerfile_pins_the_cli_by_digest_and_names_its_version() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    image = re.search(r"^ARG LIVESYNC_CLI=(\S+)$", dockerfile, re.MULTILINE)
    version = re.search(r"^ARG LIVESYNC_CLI_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert image is not None
    assert version is not None
    assert re.fullmatch(r"ghcr\.io/vrtmrz/livesync-cli:(\S+)@sha256:[0-9a-f]{64}", image[1])
    assert image[1].split(":")[1].split("@")[0] == version[1]
    pins = {}
    for name in ("PYTHON", "PYTHON_BUILD", "UV"):
        pinned = re.search(rf"^ARG {name}=(\S+)@sha256:[0-9a-f]{{64}}$", dockerfile, re.MULTILINE)
        assert pinned is not None, name
        pins[name] = pinned[1]
    # The venv is built on the full image and run on the slim one: the same Python minor.
    assert (
        pins["PYTHON"].split(":")[1].split("-")[0]
        == pins["PYTHON_BUILD"].split(":")[1].split("-")[0]
    )
    assert "apt-get" not in dockerfile  # nothing unpinned from apt


def test_the_image_carries_the_licences() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    commit = re.search(r"^ARG LIVESYNC_COMMIT=([0-9a-f]{40})$", dockerfile, re.MULTILINE)
    assert commit is not None
    notices = (ROOT / "THIRD_PARTY_NOTICES").read_text("utf-8")
    assert commit[1] in notices
    assert "MPL-2.0" in notices
    assert "COPY --from=cli /usr/local/LICENSE /usr/share/licenses/node/LICENSE" in dockerfile
    assert "/usr/share/licenses/obsidian-livesync/LICENSE" in dockerfile
    assert "MIT License" in (ROOT / "licenses" / "obsidian-livesync.LICENSE").read_text("utf-8")
    label = re.search(r'org\.opencontainers\.image\.licenses="([^"]+)"', dockerfile)
    assert label is not None
    for licence in ("Apache-2.0", "MIT", "MPL-2.0", "PSF-2.0"):
        assert licence in label[1].split(" AND ")
    ignored = (ROOT / ".dockerignore").read_text("utf-8").split()
    assert {"!THIRD_PARTY_NOTICES", "!licenses/", "!LICENSE"} <= set(ignored)


def test_the_compose_override_keeps_the_sidecar_locked_down() -> None:
    document: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy" / "compose.livesync.yaml").read_text("utf-8")
    )
    sidecar = document["services"]["livesync"]
    assert sidecar["user"] == "10001:10001"
    assert sidecar["read_only"] is True
    assert sidecar["cap_drop"] == ["ALL"]
    assert sidecar["security_opt"] == ["no-new-privileges:true"]
    assert sidecar["tmpfs"][0].startswith("/tmp:")
    assert "noexec" in sidecar["tmpfs"][0]
    assert sidecar["secrets"] == ["LIVESYNC_COUCHDB_PASSWORD", "LIVESYNC_E2EE_PASSPHRASE"]
    assert "ports" not in sidecar
    assert sidecar["init"] is True
    assert sidecar["platform"] == "linux/amd64"
    # HORAE_LIVESYNC_REF has no default: an update is always a deliberate pin.
    assert ":?" in sidecar["image"]
    assert ":?" in sidecar["build"]["context"]
    assert ":-" not in sidecar["image"] + sidecar["build"]["context"]
    assert "livesync-status:/livesync-status:ro" in document["services"]["horae"]["volumes"]


def test_the_snapshots_have_their_own_volume_and_the_example_enables_them() -> None:
    document: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy" / "compose.livesync.yaml").read_text("utf-8")
    )
    assert "livesync-snapshots:/snapshots" in document["services"]["livesync"]["volumes"]
    assert "livesync-snapshots" in document["volumes"]
    # Never mounted in the vault or shared with Horae.
    assert not any("snapshots" in v for v in document["services"]["horae"]["volumes"])
    snapshot = config.load(ROOT / "deploy" / "livesync.example.yaml").snapshot
    assert snapshot.enabled is True
    assert str(snapshot.path).startswith("/snapshots/")
