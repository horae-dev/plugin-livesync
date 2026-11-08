# SPDX-License-Identifier: Apache-2.0
"""Secrets follow the core's rules (schemas decision 25) and never show their value."""

import os
from pathlib import Path

import pytest

from horae_livesync.secrets import (
    MAX_SECRET_BYTES,
    EnvRef,
    FileRef,
    Secret,
    SecretError,
    SecretResolver,
)


def test_a_name_without_reference_is_read_from_its_file_only(tmp_path: Path) -> None:
    (tmp_path / "TOKEN").write_text("from-the-file\r\n")
    resolver = SecretResolver(tmp_path, environ={"TOKEN": "from-the-env"})
    assert resolver.resolve("TOKEN").reveal() == "from-the-file"


def test_references(tmp_path: Path) -> None:
    (tmp_path / "token.txt").write_text("file-value-1\n")
    resolver = SecretResolver(
        tmp_path,
        {"A_TOKEN": EnvRef("MY_VAR"), "B_TOKEN": FileRef("token.txt")},
        environ={"MY_VAR": "env-value-1\n"},
    )
    assert resolver.resolve("A_TOKEN").reveal() == "env-value-1"
    assert resolver.resolve("B_TOKEN").reveal() == "file-value-1"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"short", "shorter than 8 bytes"),
        (b"two\nlines-here", "line break"),
        (b"nul\x00inside-it", "NUL"),
        (b"\xff\xfe-not-utf8", "not valid UTF-8"),
        (b"x" * (MAX_SECRET_BYTES + 1), "larger than"),
    ],
)
def test_bad_values_are_refused_without_showing_them(
    tmp_path: Path, content: bytes, message: str
) -> None:
    (tmp_path / "TOKEN").write_bytes(content)
    with pytest.raises(SecretError, match=message) as error:
        SecretResolver(tmp_path).resolve("TOKEN")
    assert "inside-it" not in str(error.value)
    assert "lines-here" not in str(error.value)


def test_missing_and_odd_sources(tmp_path: Path) -> None:
    resolver = SecretResolver(tmp_path, {"E_TOKEN": EnvRef("UNSET")}, environ={})
    with pytest.raises(SecretError, match="does not exist"):
        resolver.resolve("TOKEN")
    with pytest.raises(SecretError, match="UNSET is not set"):
        resolver.resolve("E_TOKEN")
    with pytest.raises(SecretError, match="invalid secret name"):
        resolver.resolve("not a name")
    (tmp_path / "DIR_TOKEN").mkdir()
    with pytest.raises(SecretError, match="not a regular file"):
        resolver.resolve("DIR_TOKEN")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "TOKEN"
    path.write_text("a-long-enough-value")
    path.chmod(0)
    with pytest.raises(SecretError, match="cannot be read"):
        SecretResolver(tmp_path).resolve("TOKEN")


def test_a_fifo_never_blocks(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "TOKEN")
    with pytest.raises(SecretError, match="not a regular file"):
        SecretResolver(tmp_path).resolve("TOKEN")


def test_secret_hides_its_value() -> None:
    secret = Secret("TOKEN", "very-secret-value")
    shown = f"{secret} {secret!r} {secret:>20}"
    assert "very-secret-value" not in shown
    assert secret.reveal() == "very-secret-value"
