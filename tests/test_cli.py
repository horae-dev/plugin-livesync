# SPDX-License-Identifier: Apache-2.0
"""`horae-livesync` commands."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae_livesync import cli, status
from horae_livesync.supervisor import Supervisor

from .conftest import PASSWORD, config_document

FAKE_CLI = Path(__file__).parent / "fake_cli.py"


@pytest.fixture(autouse=True)
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "CLI_COMMAND", (sys.executable, str(FAKE_CLI), "ok"))


def write_config(tmp_path: Path, secrets_dir: Path, **change: Any) -> Path:
    path = tmp_path / "livesync.yaml"
    path.write_text(yaml.safe_dump(config_document(secrets_dir, **change)))
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


def test_check_validates_settings(
    tmp_path: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = write_config(tmp_path, secrets_dir, settings={"pathNormalizationVersion": 0})
    assert cli.main(["check", "--config", str(good)]) == 0
    refused = write_config(tmp_path, secrets_dir, settings={"syncIgnoreRegEx": ".*"})
    assert cli.main(["check", "--config", str(refused)]) == 78
    assert "not a parity setting" in capsys.readouterr().err


def test_check_compares_settings_with_the_pinned_cli(
    tmp_path: Path,
    secrets_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write_config(tmp_path, secrets_dir, settings={"useSegmenter": True})
    monkeypatch.setattr(cli, "CLI_COMMAND", (sys.executable, str(FAKE_CLI), "oldcli"))
    assert cli.main(["check", "--config", str(path)]) == 78
    assert "not a setting of this LiveSync CLI" in capsys.readouterr().err
    monkeypatch.setattr(cli, "CLI_COMMAND", (sys.executable, str(FAKE_CLI), "badinit"))
    assert cli.main(["check", "--config", str(path)]) == 78
    assert "cannot ask the LiveSync CLI" in capsys.readouterr().err


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


def test_health_takes_its_thresholds_from_the_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = status.new_status("v", {"probe_interval_s": 10, "max_age_s": 30, "max_lag_s": 60})
    doc["process"]["state"] = "live"
    doc["remote"]["ok"] = True
    doc["replication"]["pending"] = 0
    status.write(tmp_path, doc)
    assert cli.main(["health", "--status-dir", str(tmp_path), "--json"]) == 0
    health = json.loads(capsys.readouterr().out)
    assert (health["max_age_s"], health["max_lag_s"]) == (30, 60)
    doc["updated_at"] = status.now_iso(datetime.now(UTC) - timedelta(seconds=45))
    status.write(tmp_path, doc)
    assert cli.main(["health", "--status-dir", str(tmp_path)]) == 1
    assert "status_stale" in capsys.readouterr().out
    assert cli.main(["health", "--status-dir", str(tmp_path), "--max-age-s", "60"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--max-age-s", "29"], "at least 3 probe intervals (30 s)"),
        (["--max-lag-s", "0"], "1 to 86400"),
        (["--max-age-s", "100000"], "1 to 86400"),
    ],
)
def test_health_refuses_bad_thresholds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    status.write(tmp_path, status.new_status("v", {"probe_interval_s": 10}))
    assert cli.main(["health", "--status-dir", str(tmp_path), *args]) == 2
    assert message in capsys.readouterr().err
