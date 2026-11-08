# SPDX-License-Identifier: Apache-2.0
"""Secrets for the sidecar, resolved with the rules of the core's `horae.yaml`.

Schemas decision 25 of the core: a secret with no entry in `secrets.refs` is read from
`<secrets.dir>/<NAME>` **only**; an environment variable is read only through an explicit
`{env: VAR}` reference; `{file: name}` names a single file inside `secrets.dir`. One trailing
newline is stripped; a value that still holds `\\r`, `\\n` or NUL, is shorter than 8 bytes, or a
file that is not a regular UTF-8 file of at most 64 KiB, is an error. Files are opened once,
non-blocking, and checked with `fstat` on that descriptor.

Errors name the secret and where it was looked for, never its value.
"""

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_SECRET_BYTES = 64 * 1024
MIN_SECRET_BYTES = 8
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class SecretError(Exception):
    """A secret cannot be resolved. The message never holds the value."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"secret {name}: {reason}")


@dataclass(frozen=True, slots=True)
class EnvRef:
    env: str


@dataclass(frozen=True, slots=True)
class FileRef:
    file: str


SecretRef = EnvRef | FileRef


def valid_file_name(name: object) -> bool:
    """A single path segment inside the secrets directory (no `/`, no leading dot)."""
    return isinstance(name, str) and bool(_FILE_NAME.fullmatch(name))


class Secret:
    """A resolved value that never shows itself in `repr`, `str` or formatting."""

    __slots__ = ("_value", "name")

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self.name}, ***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self) -> str:  # pragma: no cover (refusal only)
        raise TypeError("a Secret cannot be pickled")


class SecretResolver:
    """Resolves secret names from `directory`, or through an explicit reference."""

    def __init__(
        self,
        directory: Path,
        refs: Mapping[str, SecretRef] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.directory = directory
        self._refs = dict(refs or {})
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def resolve(self, name: str) -> Secret:
        if not SECRET_NAME.fullmatch(name):
            raise SecretError("<invalid name>", "invalid secret name")
        ref = self._refs.get(name)
        if isinstance(ref, EnvRef):
            if ref.env not in self._environ:
                raise SecretError(name, f"environment variable {ref.env} is not set")
            return Secret(
                name, _checked(name, f"environment variable {ref.env}", self._environ[ref.env])
            )
        path = self.directory / (ref.file if isinstance(ref, FileRef) else name)
        data = _read(name, path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretError(name, f"file {path} is not valid UTF-8") from None
        return Secret(name, _checked(name, f"file {path}", text))


def _checked(name: str, where: str, value: str) -> str:
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if "\x00" in value:
        raise SecretError(name, f"{where} contains a NUL character")
    if "\r" in value or "\n" in value:
        raise SecretError(name, f"{where} contains a line break")
    if len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        raise SecretError(name, f"{where} is shorter than {MIN_SECRET_BYTES} bytes")
    return value


def _read(name: str, path: Path) -> bytes:
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        raise SecretError(name, f"file {path} does not exist") from None
    except OSError as exc:
        raise SecretError(name, f"file {path} cannot be read ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretError(name, f"file {path} is not a regular file")
        data = b""
        while len(data) <= MAX_SECRET_BYTES:
            chunk = os.read(fd, MAX_SECRET_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise SecretError(name, f"file {path} cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_SECRET_BYTES:
        raise SecretError(name, f"file {path} is larger than {MAX_SECRET_BYTES} bytes")
    return data
