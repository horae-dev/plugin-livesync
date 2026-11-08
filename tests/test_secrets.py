# SPDX-License-Identifier: Apache-2.0
"""Secrets follow the core's rules (schemas decision 25) and never show their value.

`CORE_CONTRACT` mirrors the outcomes of the core's `horae.secrets` (`tests/test_secrets.py`
and `contracts.types.SecretName` of horae-dev/horae `main`): the same input must give the same
outcome here, with the same reason in the message. When the core changes a rule, change the
row and `horae_livesync.secrets` together.
"""

import copy
import os
import pickle
import re
import traceback
from pathlib import Path

import pytest

from horae_livesync.secrets import (
    MAX_SECRET_BYTES,
    EnvRef,
    FileRef,
    Secret,
    SecretError,
    SecretResolver,
    safe_excerpt,
    valid_name,
)

VALUE = "s3cr3t-sentinel-0b5d2e7a"  # synthetic, as the core's tests


def _leak_free(exc: BaseException) -> str:
    """The exception with every frame's locals, from the raising code on (the core's)."""
    assert exc.__traceback__ is not None
    rendered = traceback.TracebackException(
        type(exc), exc, exc.__traceback__.tb_next, capture_locals=True
    )
    return "".join(rendered.format())


# (secret file content, None when accepted / the reason of the core's message)
CORE_CONTRACT: list[tuple[bytes, str | None]] = [
    (VALUE.encode(), None),
    (VALUE.encode() + b"\n", None),
    (VALUE.encode() + b"\r\n", None),
    ("pässwörd-é".encode(), None),
    (b"12345678", None),
    (b"", "is empty"),
    (b"\n", "is empty"),
    (b"\r\n", "is empty"),
    (b"a\x00" + VALUE.encode(), "contains a NUL character"),
    (b"\xff" + VALUE.encode(), "is not valid UTF-8"),
    (VALUE.encode() + b"\nsecond-line", "contains a line break"),
    (VALUE.encode() + b"\n\n", "contains a line break"),
    (VALUE.encode() + b"\r", "contains a line break"),
    (b"1234567", "is too short for reliable redaction (min 8 bytes)"),
    (b"1234567\n", "is too short for reliable redaction (min 8 bytes)"),
    (VALUE.encode() * (MAX_SECRET_BYTES // len(VALUE) + 1), "is larger than"),
]

# (name, valid in the core)
CORE_NAMES: list[tuple[str, bool]] = [
    ("API_KEY", True),
    ("LIVESYNC_COUCHDB_PASSWORD", True),
    ("A", True),
    ("A" * 128, True),
    ("A" * 129, False),
    ("HORAE", False),
    ("HORAE_X", False),
    ("HORAEX", True),
    ("LD_PRELOAD", False),
    ("DYLD_INSERT_LIBRARIES", False),
    ("PYTHONPATH", False),
    ("PYTHON", False),
    ("PATH", False),
    ("PATHS", True),
    ("HOME", False),
    ("HOMEDIR", True),
    ("api_key", False),
    ("_KEY", False),
    ("1KEY", False),
    ("", False),
    ("/etc/hosts", False),
    ("../x", False),
]


@pytest.mark.parametrize(("content", "reason"), CORE_CONTRACT)
def test_file_contract(tmp_path: Path, content: bytes, reason: str | None) -> None:
    (tmp_path / "API_KEY").write_bytes(content)
    resolver = SecretResolver(tmp_path)
    if reason is None:
        value = resolver.resolve("API_KEY").reveal()
        assert value == content.decode().removesuffix("\n").removesuffix("\r")
        return
    with pytest.raises(SecretError, match=re.escape(reason)) as excinfo:
        resolver.resolve("API_KEY")
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)


@pytest.mark.parametrize(("content", "reason"), CORE_CONTRACT)
def test_env_contract(tmp_path: Path, content: bytes, reason: str | None) -> None:
    try:
        text = content.decode()
    except UnicodeDecodeError:
        pytest.skip("an environment value is text")
    if len(content) > MAX_SECRET_BYTES:
        pytest.skip("the size limit is for files")
    resolver = SecretResolver(tmp_path, {"API_KEY": EnvRef("VAR")}, environ={"VAR": text})
    if reason is None:
        assert resolver.resolve("API_KEY").reveal() == text.removesuffix("\n").removesuffix("\r")
        return
    with pytest.raises(
        SecretError, match=re.escape(f"environment variable VAR {reason}")
    ) as excinfo:
        resolver.resolve("API_KEY")
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)


@pytest.mark.parametrize(("name", "valid"), CORE_NAMES)
def test_name_contract(tmp_path: Path, name: str, valid: bool) -> None:
    assert valid_name(name) is valid
    if valid:
        return
    (tmp_path / "x").write_text(VALUE)
    with pytest.raises(SecretError, match="invalid secret name") as excinfo:
        SecretResolver(tmp_path, environ={name: VALUE}).resolve(name)
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)


def test_an_invalid_name_is_shown_as_a_safe_excerpt(tmp_path: Path) -> None:
    pasted = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"  # synthetic
    with pytest.raises(SecretError) as excinfo:
        SecretResolver(tmp_path).resolve(pasted)
    assert excinfo.value.name == "sk-ant-a…"
    assert str(excinfo.value) == "secret sk-ant-a…: invalid secret name"
    assert pasted[9:] not in _leak_free(excinfo.value)
    assert safe_excerpt("a b\nc") == "a?b?c"


def test_a_name_without_reference_is_read_from_its_file_only(tmp_path: Path) -> None:
    (tmp_path / "TOKEN").write_text("from-the-file\r\n")
    resolver = SecretResolver(tmp_path, environ={"TOKEN": "from-the-env", "OTHER": "from-env"})
    assert resolver.resolve("TOKEN").reveal() == "from-the-file"
    with pytest.raises(SecretError) as excinfo:
        resolver.resolve("OTHER")
    assert str(excinfo.value) == f"secret OTHER: file {tmp_path / 'OTHER'} does not exist"


def test_references(tmp_path: Path) -> None:
    (tmp_path / "token.txt").write_text("file-value-1\n")
    (tmp_path / "-dash.txt").write_text("file-value-2\n")
    resolver = SecretResolver(
        tmp_path,
        {
            "A_TOKEN": EnvRef("MY_VAR"),
            "B_TOKEN": FileRef("token.txt"),
            "C_TOKEN": FileRef("-dash.txt"),
            "E_TOKEN": EnvRef("UNSET"),
        },
        environ={"MY_VAR": "env-value-1\n"},
    )
    assert resolver.resolve("A_TOKEN") == Secret("A_TOKEN", "env-value-1")
    assert resolver.resolve("B_TOKEN").reveal() == "file-value-1"
    assert resolver.resolve("C_TOKEN").reveal() == "file-value-2"
    with pytest.raises(SecretError, match="UNSET is not set"):
        resolver.resolve("E_TOKEN")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "TOKEN"
    path.write_text("a-long-enough-value")
    path.chmod(0)
    with pytest.raises(SecretError, match="cannot be read") as excinfo:
        SecretResolver(tmp_path).resolve("TOKEN")
    assert excinfo.value.__context__ is None


def test_odd_files(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "FIFO_TOKEN")
    (tmp_path / "DIR_TOKEN").mkdir()
    resolver = SecretResolver(tmp_path)
    with pytest.raises(SecretError, match="not a regular file"):
        resolver.resolve("FIFO_TOKEN")  # never blocks
    with pytest.raises(SecretError, match="not a regular file"):
        resolver.resolve("DIR_TOKEN")


def test_a_file_growing_after_fstat_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "API_KEY").write_bytes(VALUE.encode() * (MAX_SECRET_BYTES // len(VALUE) + 1))
    real_fstat = os.fstat

    class Small:
        def __init__(self, info: os.stat_result) -> None:
            self.st_mode = info.st_mode
            self.st_size = 1

    monkeypatch.setattr(os, "fstat", lambda fd: Small(real_fstat(fd)))
    with pytest.raises(SecretError, match="larger than") as excinfo:
        SecretResolver(tmp_path).resolve("API_KEY")
    assert VALUE not in _leak_free(excinfo.value)


def test_a_read_failure_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "API_KEY").write_text(VALUE)

    def refuse(fd: int, size: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", refuse)
    with pytest.raises(SecretError, match=r"cannot be read \(Input/output error\)") as excinfo:
        SecretResolver(tmp_path).resolve("API_KEY")
    assert excinfo.value.__context__ is None


def test_secret_hides_its_value() -> None:
    secret = Secret("TOKEN", VALUE)
    shown = f"{secret} {secret!r} {secret:>20} {[secret]} { ({'k': secret}) }"
    assert VALUE not in shown
    assert secret.reveal() == VALUE
    assert secret.name == "TOKEN"


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_secret_cannot_be_pickled(protocol: int) -> None:
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(Secret("TOKEN", VALUE), protocol=protocol)


def test_secret_is_an_immutable_final_value() -> None:
    secret = Secret("TOKEN", VALUE)
    with pytest.raises(AttributeError):
        secret._value = "other"
    with pytest.raises(AttributeError):
        del secret._value
    with pytest.raises(TypeError, match="re-initialised"):
        secret.__init__("TOKEN", "other")
    with pytest.raises(TypeError, match="cannot be pickled"):
        secret.__getstate__()
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Leaky(Secret):  # type: ignore[misc]
            pass

    assert copy.copy(secret) is secret
    assert copy.deepcopy(secret) is secret
    assert secret == Secret("TOKEN", VALUE)
    assert secret != Secret("TOKEN", "other-value")
    assert secret != Secret("OTHER", VALUE)
    assert secret != VALUE
    assert len({secret, Secret("TOKEN", VALUE)}) == 1
