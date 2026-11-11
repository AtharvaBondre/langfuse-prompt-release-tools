#!/usr/bin/env python3
"""Create private portable release evidence and verify its original bytes offline."""
import argparse
import copy
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from import_langfuse_experiment import validate_experiment_artifacts
from langfuse_release_common import ReleaseError, fingerprint, inside
from langfuse_release_policy import digest, object_fields, read_object, snapshot_policy
from publish_langfuse_prompts import load_snapshot

MAX_FILES = 10000
MAX_BYTES = 128 * 1024 * 1024
_REQUIRED = {"snapshot", "stage", "dependency_lock", "capture", "results", "evaluation"}
_OPTIONAL = {"remote_verification"}
_FIXED = {role: f"evidence/{role}.json" for role in _REQUIRED | _OPTIONAL if role != "snapshot"}
_SECRET_KEYS = {"password", "passwd", "api_key", "apikey", "secret_key", "client_secret", "access_token", "refresh_token", "authorization", "private_key"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_value(raw, description):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError(f"{description} contains duplicate JSON keys.")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as exc:
        raise ReleaseError(f"{description} is not valid UTF-8 JSON.") from exc
    fingerprint(value)
    return value


def _json(raw, description):
    value = _json_value(raw, description)
    if not isinstance(value, dict):
        raise ReleaseError(f"{description} must be a JSON object.")
    return value


def _safe_path(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value) or
            value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ReleaseError("Bundle paths must be safe relative ASCII paths without traversal.")
    for part in value.split("/"):
        if (part.endswith((".", " ")) or
                re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise ReleaseError("Bundle paths must be portable across supported platforms.")
    return value


def _regular_bytes(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file():
        raise ReleaseError("Evidence must be regular files without symlink ancestors.")
    if path.stat().st_size > MAX_BYTES:
        raise ReleaseError("Evidence exceeds the bundle size limit.")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ReleaseError("Evidence grew beyond the bundle size limit.")
    return raw


def _credentials(raw, *, value=None):
    # Never discover arbitrary workspace files. This additional guard rejects
    # recognizable credentials, not every possible secret hidden in prose.
    if re.search(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|sk-lf-[A-Za-z0-9_-]{8,}|(?:ghp_|github_pat_)[A-Za-z0-9_]{15,}", raw):
        raise ReleaseError("Evidence contains a recognizable credential; remove it before bundling.")
    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower().replace("-", "_") in _SECRET_KEYS and child not in (None, ""):
                    raise ReleaseError("Evidence contains a credential field; remove it before bundling.")
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    if value is not None:
        walk(value)


def release_identity(snapshot, stage, lock):
    binding = snapshot_policy(snapshot, required=True)
    return {"host": snapshot["host"], "snapshot_sha256": fingerprint(snapshot),
            "stage_sha256": fingerprint(stage), "policy_sha256": binding["sha256"],
            "dependency_lock_sha256": fingerprint(lock)}


def _reference_manifest(rows, values):
    roles = {row["role"]: row["path"] for row in rows if row["role"] != "prompt"}
    prompts = {row["snapshot_file"]: row["path"] for row in rows if row["role"] == "prompt"}
    refs = [{"from": roles["evaluation"], "pointer": f"/{field}/file", "to": roles[role]}
            for field, role in (("stage_report", "stage"), ("source_results", "results"), ("execution_capture", "capture"))]
    refs.append({"from": roles["capture"], "pointer": "/dependency_lock/file", "to": roles["dependency_lock"]})
    refs.extend({"from": roles["snapshot"], "pointer": f"/prompts/{index}/file", "to": prompts[entry["file"]]}
                for index, entry in enumerate(values["snapshot"]["prompts"]))
    if "remote_verification" in roles:
        refs.append({"from": roles["evaluation"], "pointer": "/remote_verification/file", "to": roles["remote_verification"]})
    return refs


def _snapshot_from_bytes(rows, payloads):
    snapshot_row = next(row for row in rows if row["role"] == "snapshot")
    with tempfile.TemporaryDirectory(prefix="langfuse-bundle-") as directory:
        root = Path(directory)
        for row in rows:
            if row["role"] not in ("snapshot", "prompt"):
                continue
            path = inside(root, row["path"])
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payloads[row["path"]])
        _, snapshot = load_snapshot(inside(root, snapshot_row["path"]))
    return snapshot


def _semantics(rows, payloads):
    values = {row["role"]: _json(payloads[row["path"]], row["role"])
              for row in rows if row["role"] != "prompt"}
    raw_snapshot = values["snapshot"]
    if not isinstance(raw_snapshot.get("prompts"), list):
        raise ReleaseError("Bundle snapshot lacks its prompt inventory.")
    snapshots = [row for row in rows if row["role"] == "snapshot"]
    snapshot_parent = str(PurePosixPath(snapshots[0]["path"]).parent)
    prompt_rows = [row for row in rows if row["role"] == "prompt"]
    required_paths = [entry.get("file") for entry in raw_snapshot["prompts"] if isinstance(entry, dict)]
    if (len(required_paths) != len(raw_snapshot["prompts"]) or any(not isinstance(path, str) for path in required_paths) or
            len(set(required_paths)) != len(required_paths)):
        raise ReleaseError("Snapshot prompt paths are missing or duplicated.")
    supplied = [row["snapshot_file"] for row in prompt_rows]
    if len(set(supplied)) != len(supplied) or set(required_paths) != set(supplied):
        raise ReleaseError("Bundle must contain exactly the snapshot's prompt files.")
    for row in prompt_rows:
        relative = _safe_path(row["snapshot_file"])
        if row["path"] != f"{snapshot_parent}/{relative}":
            raise ReleaseError("Prompt mapping does not preserve the snapshot-relative path.")
    chat_files = {entry["file"] for entry in raw_snapshot["prompts"] if entry.get("baseline", {}).get("type") == "chat"}
    chat_values = {row["path"]: _json_value(payloads[row["path"]], "chat prompt")
                   for row in prompt_rows if row["snapshot_file"] in chat_files}
    snapshot = _snapshot_from_bytes(rows, payloads)
    capture, report = values["capture"], values["evaluation"]
    if "dependency_lock" not in capture:
        raise ReleaseError("Portable release bundles require a task-bound dependency lock.")
    has_remote = "remote_verification" in report
    if has_remote != ("remote_verification" in values):
        raise ReleaseError("Remote verification evidence must match the evaluation reference.")
    kwargs = {"dependency_lock": values["dependency_lock"]}
    if has_remote:
        reference = object_fields(report["remote_verification"], ("file", "sha256"), where="remote verification reference")
        if reference["sha256"] != fingerprint(values["remote_verification"]):
            raise ReleaseError("Remote verification report changed after evaluation.")
        kwargs["remote_receipt"] = values["remote_verification"]
    validate_experiment_artifacts(report, snapshot, values["stage"], values["results"], capture, **kwargs)
    for row in rows:
        value = values.get(row["role"])
        if row["role"] == "prompt":
            value = chat_values.get(row["path"])
        _credentials(payloads[row["path"]], value=value)
    return release_identity(snapshot, values["stage"], values["dependency_lock"]), values


def _inventory(manifest):
    return {"privacy": "private", "contains": ["prompt content and configuration", "dataset inputs and expected outputs",
            "actual outputs and scores", "original local file locators", "release and execution identities"],
            "files": copy.deepcopy(manifest["files"]), "file_count": len(manifest["files"]),
            "total_bytes": sum(row["size"] for row in manifest["files"]),
            "upload_performed": False,
            "review_required": "Review this inventory and actual content before sharing; secret scanning is not exhaustive."}


def create_bundle(manifest_path, stage_report_path, evaluation_report_path, output_path, *, dependency_lock_path=None):
    manifest_path = Path(manifest_path)
    _, raw_snapshot = read_object(manifest_path, "snapshot")
    _, evaluation = read_object(evaluation_report_path, "evaluation")
    capture_path = evaluation.get("execution_capture", {}).get("file")
    result_path = evaluation.get("source_results", {}).get("file")
    if not isinstance(capture_path, str) or not isinstance(result_path, str):
        raise ReleaseError("Evaluation must reference its source result and execution capture.")
    _, capture = read_object(capture_path, "capture")
    lock_path = dependency_lock_path or capture.get("dependency_lock", {}).get("file")
    if not isinstance(lock_path, (str, Path)):
        raise ReleaseError("A task-bound dependency lock is required for a portable bundle.")
    sources = {"snapshot": manifest_path, "stage": stage_report_path, "evaluation": evaluation_report_path,
               "capture": capture_path, "results": result_path, "dependency_lock": lock_path}
    if "remote_verification" in evaluation:
        sources["remote_verification"] = evaluation["remote_verification"].get("file")
    rows, payloads = [], {}
    for role, source_path in sources.items():
        if not isinstance(source_path, (str, Path)):
            raise ReleaseError("Evidence reference lacks its source file.")
        relative = f"snapshot/{manifest_path.name}" if role == "snapshot" else _FIXED[role]
        raw = _regular_bytes(source_path)
        _safe_path(relative)
        payloads[relative] = raw
        rows.append({"path": relative, "role": role, "sha256": _sha(raw), "size": len(raw)})
    for entry in raw_snapshot.get("prompts", []):
        relative = _safe_path(entry["file"])
        destination = f"snapshot/{relative}"
        if destination.casefold() in {path.casefold() for path in payloads}:
            raise ReleaseError("Bundle contains colliding prompt paths.")
        raw = _regular_bytes(inside(manifest_path.parent, relative))
        payloads[destination] = raw
        rows.append({"path": destination, "role": "prompt", "snapshot_file": relative, "sha256": _sha(raw), "size": len(raw)})
    rows.sort(key=lambda row: row["path"])
    if len(rows) > MAX_FILES or sum(row["size"] for row in rows) > MAX_BYTES:
        raise ReleaseError("Bundle inventory exceeds the file/size limit.")
    release, values = _semantics(rows, payloads)
    manifest = {"format_version": 1, "mode": "langfuse_release_bundle", "privacy": "private",
                "release": release, "release_id": fingerprint(release), "files": rows,
                "references": _reference_manifest(rows, values)}
    manifest["manifest_sha256"] = fingerprint(manifest)
    payloads["bundle.json"] = (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    if sum(map(len, payloads.values())) > MAX_BYTES:
        raise ReleaseError("Bundle plus inventory exceeds the size limit.")
    output = Path(output_path)
    if output.suffix.lower() != ".zip" or output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ReleaseError("Bundle output must be a new .zip file without symlink ancestors.")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ReleaseError("Bundle output already exists; old evidence is never overwritten.") from None
    try:
        with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
            for relative, raw in sorted(payloads.items()):
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, raw)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"status": "created", "bundle_file": str(output.resolve()), "artifact_sha256": _sha(_regular_bytes(output)),
            "release_id": manifest["release_id"], "inventory": _inventory(manifest)}


def verify_bundle(bundle_path, *, expected_release_id=None, trust_policy_path=None, attestation_path=None, trusted_root_path=None):
    raw = _regular_bytes(bundle_path)
    payloads = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1 or sum(info.file_size for info in infos) > MAX_BYTES:
                raise ReleaseError("Bundle exceeds the file/size limit.")
            seen = set()
            for info in infos:
                path = _safe_path(info.filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if (info.orig_filename != info.filename or path.casefold() in seen or info.is_dir() or kind not in (0, stat.S_IFREG) or
                        info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ReleaseError("Bundle contains duplicate, encrypted, linked or nonregular entries.")
                seen.add(path.casefold())
                payloads[path] = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError("Bundle archive is malformed or unsupported.") from exc
    if "bundle.json" not in payloads:
        raise ReleaseError("Bundle inventory is missing.")
    manifest = _json(payloads.pop("bundle.json"), "bundle inventory")
    object_fields(manifest, ("format_version", "mode", "privacy", "release", "release_id", "files", "references", "manifest_sha256"), where="bundle inventory")
    if type(manifest["format_version"]) is not int or manifest["format_version"] != 1 or manifest["mode"] != "langfuse_release_bundle" or manifest["privacy"] != "private":
        raise ReleaseError("Unsupported bundle format or privacy classification.")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if fingerprint(unsigned) != manifest["manifest_sha256"]:
        raise ReleaseError("Bundle inventory digest mismatch.")
    rows = manifest["files"]
    if not isinstance(rows, list):
        raise ReleaseError("Bundle inventory files must be a list.")
    roles, seen = [], set()
    for row in rows:
        object_fields(row, ("path", "role", "sha256", "size"), ("snapshot_file",), where="file inventory")
        path = _safe_path(row["path"])
        if path.casefold() in seen:
            raise ReleaseError("Bundle inventory contains duplicate paths.")
        seen.add(path.casefold())
        role = row["role"]
        if not isinstance(role, str) or role not in _REQUIRED | _OPTIONAL | {"prompt"} or ("snapshot_file" in row) != (role == "prompt"):
            raise ReleaseError("Unknown inventory role or prompt mapping.")
        roles.append(role)
        if role in _FIXED and path != _FIXED[role]:
            raise ReleaseError("Evidence role does not use its canonical relative path.")
        if role == "snapshot" and (not path.startswith("snapshot/") or len(path.split("/")) != 2):
            raise ReleaseError("Snapshot must have one portable filename under snapshot/.")
        if type(row["size"]) is not int or row["size"] < 0:
            raise ReleaseError("Inventory size must be a nonnegative integer.")
        digest(row["sha256"], "file digest")
        if path not in payloads or len(payloads[path]) != row["size"] or _sha(payloads[path]) != row["sha256"]:
            raise ReleaseError("Bundle has missing or modified evidence bytes.")
    if set(payloads) != {row["path"] for row in rows}:
        raise ReleaseError("Bundle has unlisted evidence files.")
    if any(roles.count(role) != 1 for role in _REQUIRED) or any(roles.count(role) > 1 for role in _OPTIONAL):
        raise ReleaseError("Bundle requires exactly one of each release evidence role.")
    release, values = _semantics(rows, payloads)
    if fingerprint(release) != manifest["release_id"] or fingerprint(release) != fingerprint(manifest["release"]):
        raise ReleaseError("Bundle release identity is inconsistent with its evidence.")
    if fingerprint(_reference_manifest(rows, values)) != fingerprint(manifest["references"]):
        raise ReleaseError("Bundle reference mapping differs from its evidence graph.")
    if expected_release_id is not None and digest(expected_release_id, "expected release id") != manifest["release_id"]:
        raise ReleaseError("Bundle belongs to a different expected release; replay is blocked.")
    if _regular_bytes(bundle_path) != raw:
        raise ReleaseError("Bundle changed while it was being verified.")
    trust = {"classification": "unsigned_local", "artifact_sha256": _sha(raw)}
    if trust_policy_path is not None:
        from langfuse_release_provenance import verify_provenance
        trust = verify_provenance(bundle_path, trust_policy_path, attestation_path=attestation_path, trusted_root_path=trusted_root_path)
        if trust.get("artifact_sha256") != _sha(raw) or _regular_bytes(bundle_path) != raw:
            raise ReleaseError("Signed artifact differs from the bundle whose semantics were verified.")
    elif attestation_path is not None or trusted_root_path is not None:
        raise ReleaseError("Signed verification requires a separately supplied trust policy.")
    return {"status": "verified", "release_id": manifest["release_id"], "artifact_sha256": _sha(raw),
            "release_binding": "expected_release" if expected_release_id is not None else "self_consistent",
            "trust": trust, "inventory": _inventory(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    create = sub.add_parser("create")
    create.add_argument("--manifest", required=True)
    create.add_argument("--stage-report", required=True)
    create.add_argument("--evaluation-report", required=True)
    create.add_argument("--dependency-lock")
    create.add_argument("--out", required=True)
    for name in ("verify", "inventory"):
        command = sub.add_parser(name)
        command.add_argument("--bundle", required=True)
        command.add_argument("--expected-release-id")
        command.add_argument("--trust-policy")
        command.add_argument("--attestation")
        command.add_argument("--trusted-root")
    args = parser.parse_args()
    try:
        if args.operation == "create":
            report = create_bundle(args.manifest, args.stage_report, args.evaluation_report, args.out, dependency_lock_path=args.dependency_lock)
        else:
            report = verify_bundle(args.bundle, expected_release_id=args.expected_release_id, trust_policy_path=args.trust_policy,
                                   attestation_path=args.attestation, trusted_root_path=args.trusted_root)
            if args.operation == "inventory":
                report = report["inventory"]
        print(json.dumps(report, indent=2))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Bundle stopped: {exc}\n")


if __name__ == "__main__":
    main()
