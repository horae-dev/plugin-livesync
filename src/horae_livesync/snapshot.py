# SPDX-License-Identifier: Apache-2.0
"""Nightly git snapshot of the vault, and its restore.

The vault folder is committed into a **bare git repository on its own volume**
(`snapshot.path`, `/snapshots/vault.git`): nothing is written into the vault, which stays free
of any `.git`, and the repository is a plain git repository (`git --git-dir ... log`) you can
also read on the host. The image has no `git` (it is in the build stage only), so this is pure
Python on `dulwich`; `list_snapshots` and `restore` give the recovery commands the image needs.

How a snapshot is taken (`take`):

- Every included file is read once into memory (at most `max_file_mb`) and hashed from that
  read, so each stored file is exactly one state of the file, never a mix of two writes. The
  size and mtime are compared before and after the read, and a file modified in the last
  `SETTLE_S` seconds (the LiveSync CLI is writing it, possibly in place) is not trusted: it is
  read again, and if it is still in flux the previous snapshot's version is kept (counted in
  `skipped`). A vault changing under the snapshot is therefore captured file by file, and the
  next night catches what moved. No lock is taken on the vault: the CLI does not know of one.
- A tree is built from the files; when it equals the last snapshot's tree nothing is
  committed. The commit has a fixed author and a message with the time and the counts.
- A lock file in the repository keeps two snapshots (the supervisor's and a manual one) from
  running at once.
- Symbolic links are never followed or stored; files over the size limit are skipped.
"""

import contextlib
import errno
import fcntl
import fnmatch
import os
import re
import stat
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from dulwich import porcelain
from dulwich.diff_tree import tree_changes
from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, Commit, ObjectID, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo

BRANCH = Ref(b"refs/heads/main")
AUTHOR = b"Horae LiveSync <livesync@horae.invalid>"
LOCK_FILE = "horae-snapshot.lock"
READ_ATTEMPTS = 3
SETTLE_S = 2.0
"""A file modified more recently than this is being written: its last snapshot's version stays."""
EX_BUSY = 75
DEFAULT_EXCLUDE: tuple[str, ...] = (
    ".obsidian/workspace*",
    ".obsidian/cache",
    ".trash/",
    ".git/",
)


class SnapshotError(Exception):
    """A snapshot or restore that cannot be done. The message holds no file name."""


class SnapshotBusy(SnapshotError):
    """Another snapshot is running."""


@dataclass(frozen=True, slots=True)
class Result:
    commit: str | None
    """The new commit (hex), or None when nothing changed since the last snapshot."""
    head: str | None
    files: int
    added: int
    modified: int
    deleted: int
    skipped: int
    """Files left out: over the size limit, symbolic links, unreadable or still changing."""


@dataclass(frozen=True, slots=True)
class Entry:
    commit: str
    time: datetime
    message: str


def excluded(relative: str, patterns: Sequence[str], *, is_dir: bool = False) -> bool:
    """gitignore-like, simplified. `a/b*` matches that path; no `/` in the pattern: any name at
    any depth; a trailing `/`: directories only (and so everything below)."""
    parts = relative.split("/")
    for pattern in patterns:
        only_dirs = pattern.endswith("/")
        pattern = pattern.strip("/")
        if not pattern:
            continue
        anchored = "/" in pattern
        for depth in range(1, len(parts) + 1):
            last = depth == len(parts)
            if last and only_dirs and not is_dir:
                continue
            sub = "/".join(parts[:depth])
            target = sub if anchored else parts[depth - 1]
            if fnmatch.fnmatchcase(target, pattern):
                return True
    return False


def _walk(vault: Path, patterns: Sequence[str]) -> Iterator[tuple[str, Path | None, int]]:
    """(relative path, path, flag) for each candidate file: flag 1 a file, 0 a skipped
    symbolic link or special file (path is then None)."""
    stack = [(vault, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda e: e.name)
        except OSError:
            continue
        for child in children:
            relative = prefix + child.name
            try:
                if child.is_symlink():
                    if not excluded(relative, patterns):
                        yield relative, None, 0
                elif child.is_dir(follow_symlinks=False):
                    if not excluded(relative, patterns, is_dir=True):
                        stack.append((Path(child.path), relative + "/"))
                elif child.is_file(follow_symlinks=False):
                    if not excluded(relative, patterns):
                        yield relative, Path(child.path), 1
                elif not excluded(relative, patterns):
                    yield relative, None, 0
            except OSError:
                continue


def _read(path: Path, limit: int, settle_ns: int) -> tuple[bytes, int] | bool | None:
    """The content and mode of a file as one consistent read; False when it is too big or not
    a regular file (leave it out); None when it is gone, unreadable or still changing (the
    previous snapshot's version is kept)."""
    for _ in range(READ_ATTEMPTS):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                return False
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, 1 << 20):
                size += len(chunk)
                if size > limit:
                    return False
                chunks.append(chunk)
            after = os.fstat(fd)
        except OSError:
            return None
        finally:
            os.close(fd)
        # A file written in place (truncate, then write) can be read half-written with an
        # unchanged size and mtime, so a file touched in the last `settle_s` is not trusted.
        quiet = not 0 <= time.time_ns() - after.st_mtime_ns < settle_ns
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and (
            size == after.st_size and quiet
        ):
            return b"".join(chunks), 0o100755 if before.st_mode & 0o111 else 0o100644
    return None


type _Node = dict[str, "_Node | tuple[int, ObjectID]"]


def _tree(repo: Repo, files: dict[str, tuple[int, ObjectID]]) -> ObjectID:
    """Store the nested trees for `files` (relative path -> mode, blob id); the root's id."""
    root: _Node = {}
    for relative, leaf in files.items():
        node = root
        *folders, name = relative.split("/")
        for folder in folders:
            child = node.setdefault(folder, {})
            if not isinstance(child, dict):  # pragma: no cover (a file is not a folder)
                raise SnapshotError("a file and a folder have the same name")
            node = child
        node[name] = leaf

    def store(node: _Node) -> ObjectID:
        tree = Tree()
        for name, value in node.items():
            if isinstance(value, dict):
                tree.add(os.fsencode(name), 0o040000, store(value))
            else:
                tree.add(os.fsencode(name), value[0], value[1])
        repo.object_store.add_object(tree)
        return tree.id

    return store(root)


@contextlib.contextmanager
def _locked(git_dir: Path) -> Iterator[None]:
    fd = os.open(git_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise SnapshotBusy("another snapshot is running") from None
            raise
        yield
    finally:
        os.close(fd)


def _open(git_dir: Path, vault: Path, *, create: bool) -> Repo:
    g, v = git_dir.resolve(), vault.resolve()
    if g == v or g.is_relative_to(v) or v.is_relative_to(g):
        raise SnapshotError("the snapshot repository and the vault must not contain each other")
    try:
        return Repo(str(git_dir))
    except NotGitRepository:
        if not create:
            raise SnapshotError("there is no snapshot repository yet") from None
    git_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Repo.init_bare(str(git_dir), mkdir=False, default_branch=b"main")


def take(
    vault: Path,
    git_dir: Path,
    *,
    exclude: Sequence[str] = DEFAULT_EXCLUDE,
    max_file_mb: int = 64,
    gc: bool = False,
    settle_s: float = SETTLE_S,
    now: datetime | None = None,
) -> Result:
    """Commit the vault into `git_dir` unless nothing changed since the last snapshot."""
    when = (now or datetime.now(UTC)).replace(microsecond=0)
    if not vault.is_dir():
        raise SnapshotError("the vault folder does not exist")
    try:
        repo = _open(git_dir, vault, create=True)
    except OSError as exc:
        raise SnapshotError(f"cannot create the snapshot repository ({exc.strerror})") from None
    try:
        with _locked(git_dir):
            return _take(
                repo, vault, list(exclude), max_file_mb << 20, gc, when, int(settle_s * 1e9)
            )
    finally:
        repo.close()


def _take(
    repo: Repo, vault: Path, exclude: list[str], limit: int, gc: bool, when: datetime, settle: int
) -> Result:
    try:
        head: ObjectID | None = repo.refs[BRANCH]
    except KeyError:
        head = None
    commit_before = repo[head] if head else None
    previous = commit_before.tree if isinstance(commit_before, Commit) else None
    before = Tree() if previous is None else repo[previous]
    assert isinstance(before, Tree)  # noqa: S101
    files: dict[str, tuple[int, ObjectID]] = {}
    skipped = 0
    for relative, path, flag in _walk(vault, exclude):
        read = _read(path, limit, settle) if path is not None and flag else False
        if read is None:  # changing or unreadable now: keep the last snapshot's version
            skipped += 1
            with contextlib.suppress(KeyError):
                mode, sha = before.lookup_path(repo.object_store.__getitem__, os.fsencode(relative))
                files[relative] = (mode, sha)
        elif read is False or read is True:
            skipped += 1
        else:
            blob = Blob.from_string(read[0])
            if blob.id not in repo.object_store:
                repo.object_store.add_object(blob)
            files[relative] = (read[1], blob.id)
    tree_id = _tree(repo, files)
    if previous == tree_id:
        return Result(None, head.decode() if head else None, len(files), 0, 0, 0, skipped)
    if previous is None and not files:
        return Result(None, None, 0, 0, 0, 0, skipped)  # an empty vault: no first commit
    added = modified = deleted = 0
    for change in tree_changes(repo.object_store, previous, tree_id):
        if change.type == "add":
            added += 1
        elif change.type == "delete":
            deleted += 1
        else:
            modified += 1
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    message = (
        f"Vault snapshot {stamp}\n\n{len(files)} files: {added} added, {modified} modified, "
        f"{deleted} deleted; {skipped} skipped\n"
    ).encode()
    commit = Commit()
    commit.tree = tree_id
    commit.parents = [head] if head else []
    commit.author = commit.committer = AUTHOR
    commit.author_time = commit.commit_time = int(when.timestamp())
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = message
    repo.object_store.add_object(commit)
    repo.refs.set_symbolic_ref(Ref(b"HEAD"), BRANCH)
    repo.refs[BRANCH] = commit.id
    if gc:
        porcelain.gc(repo)
    return Result(
        commit.id.decode(), commit.id.decode(), len(files), added, modified, deleted, skipped
    )


def list_snapshots(git_dir: Path, limit: int = 30) -> list[Entry]:
    """The newest snapshots first."""
    try:
        repo = Repo(str(git_dir))
    except NotGitRepository:
        raise SnapshotError("there is no snapshot repository yet") from None
    entries: list[Entry] = []
    with contextlib.closing(repo):
        if BRANCH not in repo.refs:
            return entries
        for item in repo.get_walker(include=[repo.refs[BRANCH]], max_entries=limit):
            c = item.commit
            entries.append(
                Entry(
                    c.id.decode(),
                    datetime.fromtimestamp(c.commit_time, UTC),
                    c.message.decode("utf-8", "replace").splitlines()[0],
                )
            )
    return entries


_AT = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?Z?")


def resolve(repo: Repo, commit: str | None, at: str | None) -> Commit:
    """A snapshot by commit id (or unique prefix of at least 7 hex digits), or the last one at
    or before `at` (UTC; a bare date means the end of that day); the last one by default."""
    if BRANCH not in repo.refs:
        raise SnapshotError("there is no snapshot yet")
    if commit and at:
        raise SnapshotError("give a commit or a time, not both")
    head = repo.refs[BRANCH]
    if commit:
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise SnapshotError("a commit is 7 to 40 hex digits")
        found = [
            w.commit
            for w in repo.get_walker(include=[head])
            if w.commit.id.decode().startswith(commit)
        ]
        if len(found) != 1:
            raise SnapshotError("no snapshot has that id" if not found else "that id is ambiguous")
        return found[0]
    if at:
        if not _AT.fullmatch(at):
            raise SnapshotError("a time is YYYY-MM-DD[THH:MM[:SS]] in UTC")
        text = at.rstrip("Z").replace("T", " ")
        stamp = datetime.fromisoformat(text + (" 23:59:59" if " " not in text else ""))
        limit = stamp.replace(tzinfo=UTC).timestamp()
        for walked in repo.get_walker(include=[head]):
            if walked.commit.commit_time <= limit:
                return walked.commit
        raise SnapshotError("no snapshot at or before that time")
    commit_object = repo[head]
    assert isinstance(commit_object, Commit)  # noqa: S101
    return commit_object


def _tree_files(
    repo: Repo, tree_id: ObjectID, prefix: str = ""
) -> Iterator[tuple[str, int, ObjectID]]:
    tree = repo[tree_id]
    assert isinstance(tree, Tree)  # noqa: S101
    for item in tree.items():
        name = prefix + os.fsdecode(item.path)
        if stat.S_ISDIR(item.mode):
            yield from _tree_files(repo, item.sha, name + "/")
        else:
            yield name, item.mode, item.sha


def _selected(relative: str, paths: Sequence[str]) -> bool:
    return not paths or any(relative == p or relative.startswith(p + "/") for p in paths)


def _safe_path(target: Path, relative: str) -> Path:
    """`target/relative`, refusing a path with `..` and any symbolic link on the way."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise SnapshotError("a path in the snapshot leaves the target")
    path = target
    for part in pure.parts:
        path = path / part
        if path.is_symlink():
            raise SnapshotError("a symbolic link is in the way in the target")
    return path


def restore(
    vault: Path,
    git_dir: Path,
    target: Path,
    *,
    commit: str | None = None,
    at: str | None = None,
    paths: Sequence[str] = (),
    mirror: bool = False,
    dry_run: bool = False,
    exclude: Sequence[str] = DEFAULT_EXCLUDE,
) -> dict[str, list[str] | str]:
    """Write the files of a snapshot (all, or those under `paths`) into `target`, replacing
    what differs. `mirror` also deletes the files under `paths` that the snapshot does not
    have (never excluded ones). Returns the snapshot and the lists of files, by action."""
    repo = _open(git_dir, vault, create=False)
    with contextlib.closing(repo):
        chosen = resolve(repo, commit, at)
        wanted = [p.strip("/") for p in paths]
        if any(".." in PurePosixPath(p).parts for p in wanted):
            raise SnapshotError("a path must stay inside the vault")
        snapshot = {n: (m, s) for n, m, s in _tree_files(repo, chosen.tree) if _selected(n, wanted)}
        written: list[str] = []
        removed: list[str] = []
        for relative, (mode, sha) in sorted(snapshot.items()):
            blob = repo[sha]
            assert isinstance(blob, Blob)  # noqa: S101
            destination = _safe_path(target, relative)
            if destination.is_file() and destination.read_bytes() == blob.data:
                continue
            written.append(relative)
            if dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.restore.{os.getpid()}")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob.data)
                os.fchmod(handle.fileno(), mode & 0o777)
            os.replace(temporary, destination)
        if mirror:
            for relative, _, flag in _walk(target, list(exclude)):
                if flag and _selected(relative, wanted) and relative not in snapshot:
                    removed.append(relative)
                    if not dry_run:
                        _safe_path(target, relative).unlink()
        return {"snapshot": chosen.id.decode(), "written": written, "removed": removed}
