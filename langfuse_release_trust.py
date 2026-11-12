"""Authenticate portable evidence before previewing or applying a promotion.

The verifier runs for each context entry. A saved verification report is never
accepted as authorization, and original artifact locators are never followed.
"""
import copy
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from import_langfuse_experiment import validate_experiment_artifacts
from langfuse_release_bundle import MAX_BYTES, _json, verify_bundle_contents
from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_operations import sync_directory
from langfuse_release_policy import digest, text
from langfuse_release_provenance import validate_trust_policy
from publish_langfuse_prompts import load_snapshot

_CONSTRUCTION_TOKEN = object()


def _no_links(path):
    path = Path(path).absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ReleaseError("Promotion evidence and trust inputs must not use symlinks.")
    return path


def _read_input(path):
    """Bound reads and detect path replacement during a read, including links."""
    path = _no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
        raise ReleaseError("Promotion inputs must be bounded regular files.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ReleaseError("Promotion input was replaced during its read.")
        raw = handle.read(MAX_BYTES + 1)
        closed = os.fstat(handle.fileno())
    after = _no_links(path).stat()
    attributes = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if len(raw) > MAX_BYTES or not attributes(before) == attributes(opened) == attributes(closed) == attributes(after):
        raise ReleaseError("Promotion input changed during its read.")
    return raw


def _write_private(path, raw):
    path = _no_links(path)
    missing = []
    ancestor = path.parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        # Persist bytes and the read-only mode before any promotion journal
        # can claim a write backed by this materialization.
        path.chmod(0o400)
        os.fsync(handle.fileno())
    # Persist the file entry and every new ancestor, deepest directory first.
    # sync_directory documents the portable Windows directory-sync limit.
    sync_directory(path.parent)
    for directory in missing:
        sync_directory(directory.parent)


def validate_trust_mode(*, require_trusted_ci, allow_unsigned_local):
    for field, value in (("require_trusted_ci", require_trusted_ci), ("allow_unsigned_local", allow_unsigned_local)):
        if type(value) is not bool:
            raise ReleaseError(f"{field} must be a boolean.")
    if require_trusted_ci == allow_unsigned_local:
        raise ReleaseError("Choose required trusted CI or explicit allow_unsigned_local compatibility.")


def _destination(snapshot, *, host, project_id, label):
    if type(snapshot.get("format_version")) is not int or snapshot["format_version"] != 2:
        raise ReleaseError("Promotion requires a project-bound format_version=2 snapshot; re-export and restage legacy evidence.")
    if snapshot.get("host") != host:
        raise ReleaseError("Trusted release host differs from the independently selected destination.")
    if snapshot.get("project_id") != project_id:
        raise ReleaseError("Trusted release project differs from the independently selected destination.")
    if snapshot.get("selector", {}).get("label") != label:
        raise ReleaseError("Trusted release label differs from the independently selected destination.")


class PromotionEvidence:
    """One verified materialization, valid only within its owning context.

    Integrators retain the normal live destination, dependency and version/label
    checks. Call check_integrity before each PATCH and on the final/no-op path.
    """
    def __init__(self, root, report, manifest, payloads, source_bytes, copied_inputs, *, _token=None):
        if _token is not _CONSTRUCTION_TOKEN:
            raise ReleaseError("Use trusted_promotion to authenticate evidence; saved reports cannot construct a trusted context.")
        self._active = True
        self._attempt_started = False
        self._root = root
        self._report = copy.deepcopy(report)
        self._paths = {row["role"]: root / row["path"] for row in manifest["files"] if row["role"] != "prompt"}
        self._files = {root / path: raw for path, raw in payloads.items()}
        self._sources = {**source_bytes, **copied_inputs}

    @property
    def manifest_path(self):
        return self._paths["snapshot"]

    @property
    def stage_report_path(self):
        return self._paths["stage"]

    @property
    def evaluation_report_path(self):
        return self._paths["evaluation"]

    @property
    def dependency_lock_path(self):
        return self._paths["dependency_lock"]

    @property
    def report(self):
        return copy.deepcopy(self._report)

    def check_integrity(self):
        if not self._active:
            raise ReleaseError("Trusted promotion context has ended; verify provenance again for a new attempt.")
        for path, expected in {**self._sources, **self._files}.items():
            if _read_input(path) != expected:
                raise ReleaseError("Promotion evidence or independent trust inputs changed after verification.")

    def authorize_attempt(self, *, require_trusted_ci, allow_unsigned_local):
        """Bind one promoter call to this context's independently chosen mode."""
        validate_trust_mode(require_trusted_ci=require_trusted_ci, allow_unsigned_local=allow_unsigned_local)
        self.check_integrity()
        if (self._report["required_trusted_ci"] != require_trusted_ci or
                self._report["allow_unsigned_local"] != allow_unsigned_local or
                (require_trusted_ci and self._report["trust"]["classification"] != "verified_ci")):
            raise ReleaseError("Promotion trust mode differs from its verified evidence context.")
        if self._attempt_started:
            raise ReleaseError("This context already authorized a promotion attempt; verify provenance again.")
        self._attempt_started = True

    def assert_paths(self, manifest_path, stage_report_path, evaluation_report_path, dependency_lock_path):
        """Reject substituting local files for the authenticated evidence roles."""
        for provided, role in ((manifest_path, "snapshot"), (stage_report_path, "stage"),
                               (evaluation_report_path, "evaluation"), (dependency_lock_path, "dependency_lock")):
            if provided is None or _no_links(provided) != self._paths[role]:
                raise ReleaseError("Promotion inputs must be the exact materialized bundle role paths.")
        self.check_integrity()

    def validate_evaluation(self, snapshot, stage):
        """Recompute evaluation with current freshness rules and no old locators."""
        self.check_integrity()
        _, current_snapshot = load_snapshot(self.manifest_path)
        values = {role: _json(self._files[path], role) for role, path in self._paths.items() if role != "snapshot"}
        if fingerprint(snapshot) != fingerprint(current_snapshot) or fingerprint(stage) != fingerprint(values["stage"]):
            raise ReleaseError("Promotion snapshot or stage differs from the authenticated bundle.")
        evidence = validate_experiment_artifacts(values["evaluation"], current_snapshot, values["stage"],
                                                values["results"], values["capture"],
                                                dependency_lock=values["dependency_lock"],
                                                remote_receipt=values.get("remote_verification"), historical=False)
        self.check_integrity()
        return evidence


@contextmanager
def trusted_promotion(bundle_path, *, expected_host, expected_project_id, expected_label, expected_release_id,
                      trust_policy_path=None, attestation_path=None, trusted_root_path=None,
                      require_trusted_ci=True, allow_unsigned_local=False, materialize_dir=None):
    """Authenticate once per attempt and materialize only those verified bytes.

    Destination/release/signer expectations must come from deployment-owner
    configuration, never from bundle fields. With materialize_dir, use a new
    absent directory under an existing private parent; files and promotion
    reports persist after exit. Otherwise all materialization is temporary.
    """
    validate_trust_mode(require_trusted_ci=require_trusted_ci, allow_unsigned_local=allow_unsigned_local)
    text(expected_host, "expected host")
    text(expected_project_id, "expected project ID")
    if not isinstance(expected_label, str) or not re.fullmatch(r"[a-z0-9_.-]{1,36}", expected_label) or expected_label == "latest":
        raise ReleaseError("Trusted promotion requires one explicit non-latest deployment label.")
    digest(expected_release_id, "independently selected expected release ID")
    if bundle_path is None:
        raise ReleaseError("Trusted promotion requires a portable bundle for this attempt.")
    if require_trusted_ci and (trust_policy_path is None or attestation_path is None):
        raise ReleaseError("Required trusted CI promotion needs an independent trust policy and a signed attestation.")
    if trust_policy_path is None and (attestation_path is not None or trusted_root_path is not None):
        raise ReleaseError("Attestation and trusted root require an independent trust policy.")
    sources = {"bundle.zip": bundle_path, "trust-policy.json": trust_policy_path,
               "attestation.jsonl": attestation_path, "trusted-root.json": trusted_root_path}
    sources = {name: _no_links(path) for name, path in sources.items() if path is not None}
    identities = [(path.stat().st_dev, path.stat().st_ino) for path in sources.values()]
    if len(set(identities)) != len(identities):
        raise ReleaseError("Bundle, independent policy, attestation and trusted root must be distinct files.")
    source_bytes = {path: _read_input(path) for path in sources.values()}
    if trust_policy_path is not None:
        policy = validate_trust_policy(_json(source_bytes[sources["trust-policy.json"]], "independent provenance policy"))
        if require_trusted_ci and policy["allow_unsigned"]:
            raise ReleaseError("Required trusted CI promotion cannot use a policy that permits unsigned artifacts.")
    with tempfile.TemporaryDirectory(prefix="langfuse-promotion-") as temporary:
        private = Path(temporary).resolve()
        sync_directory(private.parent)
        copied_inputs = {}
        for name, source in sources.items():
            copied = private / "inputs" / name
            _write_private(copied, source_bytes[source])
            copied_inputs[copied] = source_bytes[source]
        copied = lambda name: private / "inputs" / name if name in sources else None
        report, manifest, payloads = verify_bundle_contents(copied("bundle.zip"), expected_release_id=expected_release_id,
                                                           trust_policy_path=copied("trust-policy.json"),
                                                           attestation_path=copied("attestation.jsonl"),
                                                           trusted_root_path=copied("trusted-root.json"))
        if require_trusted_ci and report["trust"]["classification"] != "verified_ci":
            raise ReleaseError("Required trusted CI promotion did not authenticate the artifact signer.")
        snapshot_row = next(row for row in manifest["files"] if row["role"] == "snapshot")
        snapshot = _json(payloads[snapshot_row["path"]], "snapshot")
        _destination(snapshot, host=expected_host, project_id=expected_project_id, label=expected_label)
        report["destination"] = {"host": expected_host, "project_id": expected_project_id, "label": expected_label}
        report["required_trusted_ci"] = require_trusted_ci
        report["allow_unsigned_local"] = allow_unsigned_local
        root = private / "materialized" if materialize_dir is None else _no_links(materialize_dir)
        # Never merge into an existing tree, including an empty tree or a symlink.
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise ReleaseError("Promotion materialization requires a new, absent directory.") from None
        sync_directory(root.parent)
        for path, raw in payloads.items():
            _write_private(root / path, raw)
        sync_directory(root)
        evidence = PromotionEvidence(root, report, manifest, payloads, source_bytes, copied_inputs,
                                     _token=_CONSTRUCTION_TOKEN)
        try:
            evidence.check_integrity()
            yield evidence
        finally:
            evidence._active = False
