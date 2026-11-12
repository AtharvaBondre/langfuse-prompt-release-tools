"""Cooperative local locks and crash-durable JSON journals; never retry HTTP writes."""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import tempfile

from langfuse_release_common import ReleaseError


def sync_directory(path):
    """POSIX directory persistence. Windows has no portable directory fsync API."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def snapshot_lock(root):
    """One cooperating operation per local snapshot; OS releases lock on process exit.

    The lock file is retained. Never unlink or force-unlock it: another process
    could still hold its inode. This is not a distributed or remote writer lock.
    """
    root = Path(root)
    if root.is_symlink():
        raise ReleaseError("Snapshot directory must not be a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ".release.lock"
    if path.is_symlink():
        raise ReleaseError("Snapshot lock must not be a symlink.")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        # Windows locks a byte range; ensure the first byte exists before locking.
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\n")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise ReleaseError("Another local operation holds this snapshot lock; let it finish. Do not delete the lock file.") from None
        yield
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def locked_snapshot(operation):
    @wraps(operation)
    def wrapped(client, manifest_path, *args, **kwargs):
        source = Path(manifest_path)
        if source.is_symlink() or source.parent.is_symlink():
            raise ReleaseError("Snapshot file and directory must not be symlinks.")
        with snapshot_lock(source.resolve().parent):
            return operation(client, manifest_path, *args, **kwargs)
    return wrapped


def locked_manifest(operation):
    """Lock a local manifest mutator that has no remote client argument."""
    @wraps(operation)
    def wrapped(manifest_path, *args, **kwargs):
        source = Path(manifest_path)
        if source.is_symlink() or source.parent.is_symlink():
            raise ReleaseError("Snapshot file and directory must not be symlinks.")
        with snapshot_lock(source.resolve().parent):
            return operation(manifest_path, *args, **kwargs)
    return wrapped


def durable_json(path, value, *, create=False):
    """Publish complete fsynced bytes atomically, reserving new evidence exclusively."""
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ReleaseError("Journal file and directory must not be symlinks.")
    missing = []
    ancestor = path.parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in reversed(missing):
        sync_directory(directory.parent)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".release-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            try:
                # Unlike rename, link cannot replace an earlier attempt.
                os.link(temporary, path)
            except FileExistsError:
                raise ReleaseError("Report already exists; choose a new --report filename.") from None
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resume_proposal(report):
    """Advice bound to fresh observations, never an executable retry command."""
    actions = []
    for row in report["results"]:
        classification = row["classification"]
        if classification == "completed":
            action = "retain_verified_result"
            reason = "Keep this observed result; do not resubmit its write."
        elif classification == "pending":
            action = "review_new_attempt"
            reason = "No completion was observed. Review scope and fresh evidence before a separately approved attempt."
        else:
            action = "stop_and_inspect"
            reason = "Outcome is conflicting or unknown. Do not retry; inspect exact remote versions and labels."
        actions.append({"name": row["name"], "classification": classification,
                        "action": action, "reason": reason})
    return {"format_version": 1, "automatic_execution": False,
            "snapshot_sha256": report["snapshot_sha256"],
            "source_report_sha256": report["release_report"]["sha256"],
            "fresh_observations_at": report["finished_at"], "actions": actions,
            "requires_new_preflight": True,
            "limits": "A proposal is not permission to replay a POST or proof that another writer cannot race."}
