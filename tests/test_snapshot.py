# SPDX-License-Identifier: Apache-2.0
"""The nightly git snapshot of the vault and its restore (synthetic notes only)."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from horae_livesync import cli, snapshot, status
from horae_livesync.snapshot import SnapshotBusy, SnapshotError, excluded
from horae_livesync.supervisor import EX_CONFIG

from .conftest import config_document

T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def take(vault: Path, git_dir: Path, **options: Any) -> snapshot.Result:
    """`snapshot.take` for files that were just written: no settle time unless asked."""
    options.setdefault("settle_s", 0)
    return snapshot.take(vault, git_dir, **options)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Notes").mkdir(parents=True)
    (root / "Notes" / "todo.md").write_text("- buy milk\n")
    (root / "Notes" / "ideas.md").write_text("an idea\n")
    (root / "daily.md").write_text("today\n")
    for path in root.rglob("*.md"):  # quiet for a while, as notes are at night
        os.utime(path, (1.7e9, 1.7e9))
    return root


@pytest.fixture
def git_dir(tmp_path: Path) -> Path:
    return tmp_path / "snapshots" / "vault.git"


def head(repo: Repo) -> Commit:
    commit = repo[repo.refs[snapshot.BRANCH]]
    assert isinstance(commit, Commit)
    return commit


def tree_of(repo: Repo, tree_id: bytes) -> Tree:
    tree = repo[tree_id]
    assert isinstance(tree, Tree)
    return tree


def data_of(repo: Repo, sha: bytes) -> bytes:
    blob = repo[sha]
    assert isinstance(blob, Blob)
    return blob.data


def files_of(git_dir: Path) -> dict[str, bytes]:
    repo = Repo(str(git_dir))
    found: dict[str, bytes] = {}

    def walk(tree_id: bytes, prefix: str) -> None:
        for item in tree_of(repo, tree_id).items():
            if item.mode == 0o040000:
                walk(item.sha, prefix + item.path.decode() + "/")
            else:
                found[prefix + item.path.decode()] = data_of(repo, item.sha)

    walk(head(repo).tree, "")
    repo.close()
    return found


def test_commits_the_vault_with_a_fixed_author_and_counts(vault: Path, git_dir: Path) -> None:
    result = take(vault, git_dir, now=T0)
    assert (result.files, result.added, result.modified, result.deleted) == (3, 3, 0, 0)
    assert files_of(git_dir) == {
        "Notes/todo.md": b"- buy milk\n",
        "Notes/ideas.md": b"an idea\n",
        "daily.md": b"today\n",
    }
    repo = Repo(str(git_dir))
    commit = head(repo)
    assert commit.author == commit.committer == b"Horae LiveSync <livesync@horae.invalid>"
    assert commit.message.startswith(b"Vault snapshot 2026-11-01T03:00:00Z\n")
    assert b"3 files: 3 added, 0 modified, 0 deleted; 0 skipped" in commit.message
    assert result.commit == commit.id.decode()
    repo.close()


def test_commits_changes_and_not_when_nothing_changed(vault: Path, git_dir: Path) -> None:
    first = take(vault, git_dir, now=T0)
    again = take(vault, git_dir, now=T0 + timedelta(days=1))
    assert again.commit is None
    assert again.head == first.commit
    assert len(snapshot.list_snapshots(git_dir)) == 1

    (vault / "daily.md").write_text("tomorrow\n")
    (vault / "Notes" / "ideas.md").unlink()
    (vault / "new.md").write_text("new\n")
    third = take(vault, git_dir, now=T0 + timedelta(days=2))
    assert third.commit not in (None, first.commit)
    assert (third.added, third.modified, third.deleted) == (1, 1, 1)
    entries = snapshot.list_snapshots(git_dir)
    assert [e.time for e in entries] == [T0 + timedelta(days=2), T0]
    assert "2 files" not in entries[0].message
    assert "3 files: 1 added, 1 modified, 1 deleted" in repo_message(git_dir)


def repo_message(git_dir: Path) -> str:
    repo = Repo(str(git_dir))
    message: bytes = head(repo).message
    repo.close()
    return message.decode()


def test_an_empty_vault_makes_no_first_commit(tmp_path: Path, git_dir: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert take(empty, git_dir).commit is None
    assert snapshot.list_snapshots(git_dir) == []


def test_emptying_the_vault_is_a_snapshot(vault: Path, git_dir: Path) -> None:
    take(vault, git_dir, now=T0)
    for path in [*vault.rglob("*.md")]:
        path.unlink()
    result = take(vault, git_dir, now=T0 + timedelta(days=1))
    assert (result.files, result.deleted) == (0, 3)


def test_excludes_are_respected(vault: Path, git_dir: Path) -> None:
    for name in (".obsidian/workspace.json", ".obsidian/workspaces.json", ".obsidian/cache"):
        (vault / name).parent.mkdir(exist_ok=True)
        (vault / name).write_text("churn")
    (vault / ".obsidian" / "app.json").write_text("{}")
    (vault / ".trash").mkdir()
    (vault / ".trash" / "old.md").write_text("trash")
    (vault / "Notes" / "scratch.tmp").write_text("x")
    take(vault, git_dir, exclude=[*snapshot.DEFAULT_EXCLUDE, "*.tmp"])
    assert sorted(files_of(git_dir)) == [
        ".obsidian/app.json",
        "Notes/ideas.md",
        "Notes/todo.md",
        "daily.md",
    ]


@pytest.mark.parametrize(
    ("path", "patterns", "is_dir", "expected"),
    [
        ("a/b.md", ["b.md"], False, True),  # no slash: any depth
        ("a/b.md", ["a/b.md"], False, True),
        ("a/b.md", ["x/b.md"], False, False),
        ("a/b.md", ["a/"], False, True),  # below an excluded folder
        ("a", ["a/"], True, True),
        ("a", ["a/"], False, False),  # a file named like a folder pattern
        (".obsidian/workspace-mobile.json", [".obsidian/workspace*"], False, True),
        ("notes/x.md", ["*.tmp"], False, False),
        ("notes/x.md", ["/"], False, False),
    ],
)
def test_exclude_patterns(path: str, patterns: list[str], is_dir: bool, expected: bool) -> None:
    assert excluded(path, patterns, is_dir=is_dir) is expected


def test_nothing_is_written_inside_the_vault(vault: Path, git_dir: Path) -> None:
    def tree(root: Path) -> dict[str, tuple[int, int]]:
        return {
            str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(root.rglob("*"))
        }

    before = tree(vault)
    take(vault, git_dir, gc=True)
    take(vault, git_dir)
    assert tree(vault) == before
    assert not (vault / ".git").exists()
    assert (git_dir / "HEAD").exists()


def test_the_repository_and_the_vault_must_not_contain_each_other(vault: Path) -> None:
    with pytest.raises(SnapshotError, match="contain each other"):
        take(vault, vault / ".snapshots")
    with pytest.raises(SnapshotError, match="contain each other"):
        take(vault, vault.parent)


def test_a_missing_vault_is_an_error(tmp_path: Path, git_dir: Path) -> None:
    with pytest.raises(SnapshotError, match="vault folder"):
        take(tmp_path / "nope", git_dir)


def test_binary_large_links_and_modes(vault: Path, git_dir: Path, tmp_path: Path) -> None:
    binary = bytes(range(256)) * 4096  # 1 MiB with NULs
    (vault / "pic.png").write_bytes(binary)
    (vault / "big.bin").write_bytes(b"x" * (2 << 20))
    (vault / "run.sh").write_text("#!/bin/sh\n")
    (vault / "run.sh").chmod(0o755)
    (vault / "link.md").symlink_to(tmp_path / "outside.md")
    (vault / "empty.md").write_bytes(b"")
    (tmp_path / "outside.md").write_text("secret")
    result = take(vault, git_dir, max_file_mb=1)
    found = files_of(git_dir)
    assert found["pic.png"] == binary
    assert found["empty.md"] == b""
    assert "big.bin" not in found  # over max_file_mb
    assert "link.md" not in found  # never followed
    assert result.skipped == 2
    repo = Repo(str(git_dir))
    root = tree_of(repo, head(repo).tree)
    assert root[b"run.sh"][0] == 0o100755
    assert root[b"pic.png"][0] == 0o100644
    repo.close()


def test_a_file_that_cannot_be_read_keeps_its_last_version(vault: Path, git_dir: Path) -> None:
    take(vault, git_dir, now=T0)
    target = vault / "daily.md"
    target.write_text("changed\n")
    target.chmod(0)
    try:
        if os.access(target, os.R_OK):  # pragma: no cover (running as root)
            pytest.skip("permissions are not enforced")
        result = take(vault, git_dir, now=T0 + timedelta(days=1))
    finally:
        target.chmod(0o644)
    assert result.commit is None  # unchanged: the old version is kept, not deleted
    assert result.skipped == 1
    assert files_of(git_dir)["daily.md"] == b"today\n"


def test_a_second_snapshot_at_the_same_time_is_refused(vault: Path, git_dir: Path) -> None:
    take(vault, git_dir)
    with snapshot._locked(git_dir), pytest.raises(SnapshotBusy):
        take(vault, git_dir)


def test_snapshots_taken_while_files_are_being_written_hold_whole_states(
    vault: Path, git_dir: Path
) -> None:
    """The sidecar's CLI writes in place while a snapshot runs: a stored file is always one
    whole state, never the empty or half-written file a reader can catch."""
    hot = [f"hot{i}.md" for i in range(10)]
    cold = [f"cold{i}.md" for i in range(10)]
    states = (b"A" * 200_000, b"B" * 200_000)
    for name in hot + cold:
        (vault / name).write_bytes(states[0])
    first = take(vault, git_dir, now=T0)  # the hot files were written a moment ago: all quiet now
    assert first.skipped == 0
    for name in cold:  # cold files have been quiet for a while
        os.utime(vault / name, (T0.timestamp(), T0.timestamp()))
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        flip = 1
        while not stop.is_set():
            for name in hot:
                try:
                    with open(vault / name, "r+b") as handle:
                        handle.truncate(0)
                        handle.write(states[flip])
                except OSError as exc:  # pragma: no cover
                    errors.append(exc)
            flip ^= 1

    thread = threading.Thread(target=writer)
    thread.start()
    skipped = 0
    try:
        for i in range(8):
            skipped += take(vault, git_dir, settle_s=0.5, now=T0 + timedelta(minutes=i + 1)).skipped
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert skipped == 8 * len(hot)  # in flux: left at the last snapshot's version
    repo = Repo(str(git_dir))
    for item in repo.get_walker():
        for entry in tree_of(repo, item.commit.tree).items():
            if entry.path.decode() in hot + cold:
                assert data_of(repo, entry.sha) in states
    repo.close()


def test_a_file_written_just_now_is_not_trusted(vault: Path, git_dir: Path) -> None:
    for path in vault.rglob("*.md"):
        os.utime(path)  # now
    result = take(vault, git_dir, settle_s=3600)
    assert result.commit is None
    assert result.skipped == 3  # no earlier version to keep: caught the next night


def test_restore_a_deleted_note_and_the_whole_vault(vault: Path, git_dir: Path) -> None:
    take(vault, git_dir, now=T0)
    (vault / "Notes" / "todo.md").unlink()
    (vault / "daily.md").write_text("rewritten\n")
    (vault / "later.md").write_text("made after\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.json").write_text("{}")
    take(vault, git_dir, now=T0 + timedelta(days=1))

    # a dry run changes nothing
    plan = snapshot.restore(vault, git_dir, vault, at="2026-11-01", dry_run=True)
    assert plan["written"] == ["Notes/todo.md", "daily.md"]
    assert not (vault / "Notes" / "todo.md").exists()

    # one deleted note, from the snapshot at a point in time
    done = snapshot.restore(vault, git_dir, vault, at="2026-11-01", paths=["Notes/todo.md"])
    assert done["written"] == ["Notes/todo.md"]
    assert (vault / "Notes" / "todo.md").read_text() == "- buy milk\n"
    assert (vault / "daily.md").read_text() == "rewritten\n"  # the others are left alone

    # the whole vault, mirrored: later files go, excluded ones stay
    done = snapshot.restore(vault, git_dir, vault, at="2026-11-01T03:00", mirror=True)
    assert done["removed"] == ["later.md"]
    assert (vault / "daily.md").read_text() == "today\n"
    assert not (vault / "later.md").exists()
    assert (vault / ".obsidian" / "workspace.json").exists()


def test_restore_by_commit_into_another_folder(vault: Path, git_dir: Path, tmp_path: Path) -> None:
    first = take(vault, git_dir, now=T0)
    (vault / "daily.md").write_text("rewritten\n")
    take(vault, git_dir, now=T0 + timedelta(days=1))
    assert first.commit is not None
    target = tmp_path / "look"
    snapshot.restore(vault, git_dir, target, commit=first.commit[:8], paths=["daily.md"])
    assert (target / "daily.md").read_text() == "today\n"
    assert (vault / "daily.md").read_text() == "rewritten\n"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"commit": "zz"}, "7 to 40 hex"),
        ({"commit": "0123456"}, "no snapshot has that id"),
        ({"at": "yesterday"}, "YYYY-MM-DD"),
        ({"at": "2020-01-01"}, "no snapshot at or before"),
        ({"commit": "0123456", "at": "2026-11-01"}, "not both"),
        ({"paths": ["../x"]}, "inside the vault"),
    ],
)
def test_restore_refuses_bad_requests(
    vault: Path, git_dir: Path, kwargs: dict[str, object], message: str
) -> None:
    take(vault, git_dir, now=T0)
    with pytest.raises(SnapshotError, match=message):
        snapshot.restore(vault, git_dir, vault, dry_run=True, **kwargs)  # type: ignore[arg-type]


def test_restore_without_a_repository_or_a_snapshot(vault: Path, git_dir: Path) -> None:
    with pytest.raises(SnapshotError, match="no snapshot repository"):
        snapshot.restore(vault, git_dir, vault)
    with pytest.raises(SnapshotError, match="no snapshot repository"):
        snapshot.list_snapshots(git_dir)
    empty = vault.parent / "empty"
    empty.mkdir()
    take(empty, git_dir)  # creates the repository, no commit
    with pytest.raises(SnapshotError, match="no snapshot yet"):
        snapshot.restore(vault, git_dir, vault)


def test_restore_never_writes_through_a_symbolic_link(
    vault: Path, git_dir: Path, tmp_path: Path
) -> None:
    take(vault, git_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "Notes").symlink_to(outside)
    with pytest.raises(SnapshotError, match="symbolic link"):
        snapshot.restore(vault, git_dir, target)
    assert list(outside.iterdir()) == []


# --- the commands ---------------------------------------------------------------------------


@pytest.fixture
def conf(tmp_path: Path, secrets_dir: Path, git_dir: Path) -> Path:
    path = tmp_path / "livesync.yaml"
    path.write_text(
        yaml.safe_dump(
            config_document(secrets_dir, snapshot={"enabled": True, "path": str(git_dir)})
        )
    )
    return path


def test_commands_snapshot_list_and_restore(
    vault: Path, conf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--config", str(conf), "--vault", str(vault)]
    assert cli.main(["snapshot", "--json", *base]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["added"] == 3
    assert cli.main(["snapshot", *base]) == 0
    assert "unchanged" in capsys.readouterr().out

    assert cli.main(["snapshots", "--config", str(conf)]) == 0
    assert result["commit"][:12] in capsys.readouterr().out

    (vault / "daily.md").unlink()
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    assert cli.main(["restore", *base, "--status-dir", str(status_dir), "daily.md"]) == 0
    assert "write  daily.md" in capsys.readouterr().out
    assert (vault / "daily.md").read_text() == "today\n"


def test_restore_refuses_while_the_sidecar_is_live(
    vault: Path, conf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--config", str(conf), "--vault", str(vault)]
    cli.main(["snapshot", *base])
    (vault / "daily.md").unlink()
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    doc = status.new_status("x")
    doc["process"]["state"] = "live"
    status.write(status_dir, doc)
    capsys.readouterr()
    assert cli.main(["restore", *base, "--status-dir", str(status_dir)]) == 1
    assert "sidecar is live" in capsys.readouterr().err
    assert not (vault / "daily.md").exists()
    assert cli.main(["restore", *base, "--status-dir", str(status_dir), "--force"]) == 0
    assert (vault / "daily.md").exists()


def test_command_errors(
    vault: Path, conf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["snapshots", "--config", str(conf)]) == 1  # no repository yet
    assert "no snapshot repository" in capsys.readouterr().err
    assert cli.main(["snapshot", "--config", str(tmp_path / "missing.yaml")]) == EX_CONFIG
    cli.main(["snapshot", "--config", str(conf), "--vault", str(vault)])
    with snapshot._locked(Path(yaml.safe_load(conf.read_text())["snapshot"]["path"])):
        code = cli.main(["snapshot", "--config", str(conf), "--vault", str(vault)])
    assert code == snapshot.EX_BUSY
    assert "another snapshot" in capsys.readouterr().err
