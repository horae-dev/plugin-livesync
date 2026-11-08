# SPDX-License-Identifier: Apache-2.0
"""`horae-livesync` commands."""

import json
from pathlib import Path

import pytest
import yaml

from horae_livesync import cli, status
from horae_livesync.supervisor import Supervisor

from .conftest import PASSWORD, config_document


def write_config(tmp_path: Path, secrets_dir: Path) -> Path:
    path = tmp_path / "livesync.yaml"
    path.write_text(yaml.safe_dump(config_document(secrets_dir)))
    return path


def test_check_accepts_a_valid_config(
    tmp_path: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check", "--config", str(write_config(tmp_path, secrets_dir))]) == 0
    out = capsys.readouterr()
    assert "valid" in out.out
    assert PASSWORD not in out.out + out.err


def test_check_refuses_a_missing_secret(
    tmp_path: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (secrets_dir / "LIVESYNC_COUCHDB_PASSWORD").unlink()
    assert cli.main(["check", "--config", str(write_config(tmp_path, secrets_dir))]) == 78
    assert "LIVESYNC_COUCHDB_PASSWORD" in capsys.readouterr().err


def test_run_with_a_bad_config_writes_the_status(tmp_path: Path) -> None:
    (tmp_path / "livesync.yaml").write_text("schema_version: 3\n")
    code = cli.main(
        ["run", "--config", str(tmp_path / "livesync.yaml"), "--status-dir", str(tmp_path)]
    )
    assert code == 78
    written = status.read(tmp_path)
    assert written is not None
    assert written["process"]["state"] == "config_error"


def test_run_starts_the_supervisor(
    tmp_path: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}

    def fake_run(self: Supervisor) -> int:
        seen["layout"] = self.layout
        return 0

    monkeypatch.setattr(Supervisor, "run", fake_run)
    monkeypatch.setattr("signal.signal", lambda *args: None)
    monkeypatch.setenv("LIVESYNC_CLI_VERSION", "1.0.46-patch2-cli")
    config = write_config(tmp_path, secrets_dir)
    assert cli.main(["run", "--config", str(config), "--vault", "/v", "--verbose-log"]) == 0
    assert seen["layout"].verbose_log is True
    assert seen["layout"].vault == Path("/v")
    assert seen["layout"].cli_version == "1.0.46-patch2-cli"


def test_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["health", "--status-dir", str(tmp_path)]) == 1
    assert "status_missing" in capsys.readouterr().out
    doc = status.new_status("v")
    doc["process"]["state"] = "live"
    doc["remote"]["ok"] = True
    doc["replication"]["pending"] = 0
    status.write(tmp_path, doc)
    assert cli.main(["health", "--status-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "healthy\n"
    assert cli.main(["health", "--status-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True
    (tmp_path / status.STATUS_FILE).write_text("[]")
    assert cli.main(["health", "--status-dir", str(tmp_path)]) == 1
    assert "status_invalid" in capsys.readouterr().out
