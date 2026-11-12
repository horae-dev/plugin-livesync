# SPDX-License-Identifier: Apache-2.0
"""The image and the deployment files agree with the code."""

import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

from horae_livesync import config

ROOT = Path(__file__).parent.parent
IMAGE = "ghcr.io/horae-dev/horae-livesync"


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
    assert "livesync-status:/livesync-status:ro" in document["services"]["horae"]["volumes"]


def test_the_compose_override_runs_the_published_image_at_a_pinned_tag() -> None:
    sidecar = yaml.safe_load((ROOT / "deploy" / "compose.livesync.yaml").read_text("utf-8"))[
        "services"
    ]["livesync"]
    # HORAE_LIVESYNC_TAG has no default: an update is always a deliberate pin.
    assert sidecar["image"].startswith(f"{IMAGE}:${{HORAE_LIVESYNC_TAG:?")
    assert ":-" not in sidecar["image"]
    assert "build" not in sidecar


def test_building_from_source_is_a_separate_override() -> None:
    document: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy" / "compose.livesync.build.yaml").read_text("utf-8")
    )
    sidecar = document["services"]["livesync"]
    assert set(document) == {"services"}
    assert set(document["services"]) == {"livesync"}
    # A local name, never the published one, built from the commit HORAE_LIVESYNC_TAG names
    # (the variable compose.livesync.yaml requires anyway), with no default.
    assert sidecar["image"].startswith("horae-livesync:${HORAE_LIVESYNC_TAG:?")
    assert sidecar["build"]["context"].startswith(
        "https://github.com/horae-dev/plugin-livesync.git#${HORAE_LIVESYNC_TAG:?"
    )
    assert ":-" not in sidecar["image"] + sidecar["build"]["context"]
    assert sidecar["pull_policy"] == "build"


def coolify_fragment() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy" / "coolify.livesync.yaml").read_text("utf-8")
    )
    return document


def test_the_coolify_fragment_has_the_same_hardening() -> None:
    compose = yaml.safe_load((ROOT / "deploy" / "compose.livesync.yaml").read_text("utf-8"))
    document = coolify_fragment()
    # Merged into the core's Coolify compose: services and volumes only, no networks.
    assert set(document) == {"services", "volumes"}
    sidecar, local = document["services"]["livesync"], compose["services"]["livesync"]
    for key in (
        "platform", "restart", "init", "user", "read_only", "tmpfs", "pids_limit", "cap_drop",
        "security_opt", "stop_grace_period",
    ):  # fmt: skip
        assert sidecar[key] == local[key], key
    assert "ports" not in sidecar
    assert "networks" not in sidecar
    assert "secrets" not in sidecar
    assert "build" not in sidecar
    # Coolify stores the text after `:?` as the value: no message there.
    assert sidecar["image"] == f"{IMAGE}:${{HORAE_LIVESYNC_TAG:?}}"
    # The same named volumes as the compose override, and the config as a Coolify file mount.
    mounts = [v for v in sidecar["volumes"] if isinstance(v, str)]
    assert mounts == [v for v in local["volumes"] if not v.startswith("${")]
    (config_mount,) = [v for v in sidecar["volumes"] if isinstance(v, dict)]
    assert config_mount["type"] == "bind"
    assert config_mount["target"] == "/config/livesync.yaml"
    assert config_mount["read_only"] is True
    assert set(document["volumes"]) == set(compose["volumes"]) | {"horae-instance"}


def test_the_coolify_fragment_gives_the_secrets_as_environment_variables() -> None:
    sidecar = coolify_fragment()["services"]["livesync"]
    assert sidecar["environment"] == [
        "TZ=${TZ:-UTC}",
        "LIVESYNC_COUCHDB_PASSWORD=${LIVESYNC_COUCHDB_PASSWORD:?}",
        "LIVESYNC_E2EE_PASSPHRASE=${LIVESYNC_E2EE_PASSPHRASE:?}",
    ]


def test_the_coolify_fragment_shares_the_vault_and_status_with_horae() -> None:
    document = coolify_fragment()
    horae = document["services"]["horae"]
    assert set(document["services"]) == {"livesync", "horae"}
    assert horae["volumes"] == [
        "horae-instance:/instance",
        "livesync-vault:/vault",
        "livesync-status:/livesync-status:ro",
    ]
    # The core's own dependency is kept, and the sidecar starts first (volume owner 10001).
    assert horae["depends_on"] == {
        "horae-init": {"condition": "service_completed_successfully"},
        "livesync": {"condition": "service_started"},
    }
    assert not any("snapshots" in v for v in horae["volumes"])


def coolify_livesync_yaml() -> str:
    """The `livesync.yaml` for Coolify shown in docs/setup.md (the block after its marker)."""
    text = (ROOT / "docs" / "setup.md").read_text("utf-8")
    block = re.search(r"```yaml\n( *# livesync\.yaml on Coolify\n.*?\n) *```", text, re.DOTALL)
    assert block is not None
    return textwrap.dedent(block[1])


def test_the_coolify_livesync_yaml_in_the_docs_reads_both_secrets_from_the_environment() -> None:
    loaded = config.parse(yaml.safe_load(coolify_livesync_yaml()))
    environ = {
        "LIVESYNC_COUCHDB_PASSWORD": "synthetic-couch-password",
        "LIVESYNC_E2EE_PASSPHRASE": "synthetic-e2ee-passphrase",
    }
    resolver = loaded.resolver(environ)
    # No secrets directory at all: nothing is read from a file.
    assert loaded.secrets_dir == Path("/run/secrets")
    password = resolver.resolve(loaded.couchdb.password_secret)
    passphrase = resolver.resolve(loaded.passphrase_secret)
    assert password.reveal() == "synthetic-couch-password"
    assert passphrase.reveal() == "synthetic-e2ee-passphrase"
    # The variables the fragment sets are the ones this file reads.
    sidecar = coolify_fragment()["services"]["livesync"]
    names = {item.split("=", 1)[0] for item in sidecar["environment"]}
    assert set(environ) <= names


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


def workflow(name: str) -> dict[Any, Any]:
    document: dict[Any, Any] = yaml.safe_load(
        (ROOT / ".github" / "workflows" / name).read_text("utf-8")
    )
    return document


def test_the_image_is_published_only_from_main_after_every_check() -> None:
    ci = workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    publish = ci["jobs"]["publish"]
    assert publish["needs"] == ["ci-ok"]
    assert publish["if"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert publish["env"]["IMAGE"] == IMAGE
    # CI OK gates the publish job, and needs the integration test (the image against CouchDB).
    assert "integration" in ci["jobs"]["ci-ok"]["needs"]
    assert "publish" not in ci["jobs"]["ci-ok"]["needs"]
    writers = [n for n, job in ci["jobs"].items() if "write" in str(job.get("permissions", {}))]
    assert writers == ["publish"]


def test_a_release_tag_only_retags_the_digest_main_published() -> None:
    release = workflow("image.yml")
    # PyYAML reads the `on` key as True.
    assert release[True] == {"push": {"tags": ["v*"]}}
    assert release["permissions"] == {"contents": "read"}
    (job,) = release["jobs"].values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    steps = yaml.safe_dump(job["steps"])
    assert "docker/build-push-action" not in steps  # nothing is built here
    assert "cosign verify" in steps


def test_every_action_is_pinned_by_commit() -> None:
    for name in ("ci.yml", "image.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text("utf-8")
        for use in re.findall(r"uses:\s*(\S+)", text):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", use), use
