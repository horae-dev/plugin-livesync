# SPDX-License-Identifier: Apache-2.0
"""Secrets for the sidecar, resolved with the rules of the core's `horae.yaml`.

Schemas decision 25 of the core: a secret with no entry in `secrets.refs` is read from
`<secrets.dir>/<NAME>` **only**; an environment variable is read only through an explicit
`{env: VAR}` reference; `{file: name}` names a single file inside `secrets.dir`.

The rules mirror the core's `horae.secrets` (and `tests/test_secrets.py` checks them against a
table of the core's outcomes):

- a name is `[A-Z][A-Z0-9_]*`, at most 128 characters, and not reserved: `HORAE_*` belongs to
  the core, and `LD_*`, `DYLD_*`, `PYTHON*`, `PATH` and `HOME` change how a process loads code;
  an invalid name is shown only as a safe excerpt (it may be a value pasted in the wrong place);
- one trailing newline (`\\n` or `\\r\\n`) is stripped; then a value that is empty, holds a NUL
  or a line break, or is shorter than 8 bytes of UTF-8 (too short to be masked reliably) is
  refused;
- a file must be a regular UTF-8 file of at most 64 KiB, opened once, non-blocking, and
  checked with `fstat` on that descriptor (a FIFO never blocks, a swap is not followed).

Errors name the secret and where it was looked for, never its value, and never chain the
exception that held it.
"""

import hmac
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, final

MAX_SECRET_BYTES = 64 * 1024
MIN_SECRET_BYTES = 8
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
RESERVED_NAME = re.compile(r"HORAE(_|$)|LD_|DYLD_|PYTHON|PATH$|HOME$")
"""Names a secret may not have (the core's `_UNSAFE_ENV_NAME`, matched at the start)."""
_FILE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,254}")
_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-./")
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class SecretError(Exception):
    """A secret cannot be resolved. The message never holds the value."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"secret {name}: {reason}")
        self.name = name
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EnvRef:
    env: str


@dataclass(frozen=True, slots=True)
class FileRef:
    file: str


SecretRef = EnvRef | FileRef


def valid_name(name: object) -> bool:
    """A secret name: `[A-Z][A-Z0-9_]*`, at most 128 characters, not reserved."""
    return (
        isinstance(name, str)
        and bool(SECRET_NAME.fullmatch(name))
        and not RESERVED_NAME.match(name)
    )


def valid_file_name(name: object) -> bool:
    """A single path segment inside the secrets directory (no `/`, no leading dot)."""
    return isinstance(name, str) and bool(_FILE_NAME.fullmatch(name))


def safe_excerpt(text: str, limit: int = 8) -> str:
    """At most `limit` characters of `text`, safe to show (as the core's `safe_excerpt`)."""
    shown = "".join(char if char in _SAFE_CHARS else "?" for char in text[:limit])
    return shown + ("…" if len(text) > limit else "")


@final
class Secret:
    """A resolved value that never shows itself in `repr`, `str` or formatting, cannot be
    pickled, subclassed or changed, and compares in constant time (as the core's)."""

    __slots__ = ("_name", "_value")

    _name: str
    _value: str

    def __init__(self, name: str, value: str) -> None:
        try:
            object.__getattribute__(self, "_value")
        except AttributeError:
            pass
        else:
            raise TypeError("a Secret cannot be re-initialised")
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_value", value)

    def __init_subclass__(cls, **kwargs: Any) -> NoReturn:
        raise TypeError("Secret cannot be subclassed")

    @property
    def name(self) -> str:
        return self._name

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self._name}, ***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._name == other._name and hmac.compare_digest(
            self._value.encode(), other._value.encode()
        )

    def __hash__(self) -> int:
        return hash((Secret, self._name))

    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "Secret":
        return self

    def __getstate__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
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
        if not valid_name(name):
            shown = safe_excerpt(str(name))
            del name  # it may be a value pasted in the wrong place: keep it out of tracebacks
            raise SecretError(shown, "invalid secret name")
        ref = self._refs.get(name)
        if isinstance(ref, EnvRef):
            if ref.env not in self._environ:
                raise SecretError(name, f"environment variable {ref.env} is not set")
            return _checked(name, f"environment variable {ref.env}", self._environ[ref.env])
        return _from_file(name, self.directory / (ref.file if isinstance(ref, FileRef) else name))


def _from_file(name: str, path: Path) -> Secret:
    data = _read(name, path)
    text = _decode(data)
    del data
    if text is None:
        raise SecretError(name, f"file {path} is not valid UTF-8")
    text = _strip_newline(text)
    problem = _problem(text)
    if problem is not None:
        del text  # keep the value out of this frame's locals
        raise SecretError(name, f"file {path} {problem}")
    return Secret(name, text)


def _problem(value: str) -> str | None:
    """Why `value` (one trailing newline already stripped) is not a usable secret, if so."""
    if not value:
        return "is empty"
    if "\x00" in value:
        return "contains a NUL character"
    if "\r" in value or "\n" in value:
        return "contains a line break (only one trailing newline is allowed)"
    if len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        return f"is too short for reliable redaction (min {MIN_SECRET_BYTES} bytes)"
    return None


def _strip_newline(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def _checked(name: str, where: str, value: str) -> Secret:
    value = _strip_newline(value)
    problem = _problem(value)
    if problem is not None:
        del value
        raise SecretError(name, f"{where} {problem}")
    return Secret(name, value)


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None  # the error holds the bytes: never let it escape or chain


def _read(name: str, path: Path) -> bytes:
    reason: str | None = None
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        reason = "does not exist"
    except OSError as exc:
        reason = f"cannot be read ({exc.strerror})"
    if reason is not None:
        raise SecretError(name, f"file {path} {reason}")
    chunks: list[bytes] = []
    chunk = b""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reason = "is not a regular file"
        elif info.st_size > MAX_SECRET_BYTES:
            reason = f"is larger than {MAX_SECRET_BYTES} bytes"
        else:
            size = 0
            while size <= MAX_SECRET_BYTES:
                chunk = os.read(fd, MAX_SECRET_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_SECRET_BYTES:
                reason = f"is larger than {MAX_SECRET_BYTES} bytes"
    except OSError as exc:
        reason = f"cannot be read ({exc.strerror})"
    finally:
        os.close(fd)
    if reason is not None:
        chunks.clear()
        chunk = b""  # drop the last chunk from this frame's locals
        raise SecretError(name, f"file {path} {reason}")
    return b"".join(chunks)
