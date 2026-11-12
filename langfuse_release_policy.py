#!/usr/bin/env python3
"""Pin an independently reviewed experiment policy into a snapshot before staging."""
import argparse
import copy
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from langfuse_release_common import ReleaseError, fingerprint
from publish_langfuse_prompts import load_snapshot, write_report


def object_fields(value, required, optional=(), *, where="object"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ReleaseError(f"{where} has missing or unsupported fields.")
    return value


def text(value, where):
    if (not isinstance(value, str) or not value or value != value.strip() or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ReleaseError(f"{where} must be a nonempty string without surrounding whitespace or controls.")
    return value


def digest(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReleaseError(f"{where} must be a lowercase SHA-256 digest.")
    return value


def timestamp(value, where):
    text(value, where)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReleaseError(f"{where} must be an ISO-8601 timestamp.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseError(f"{where} must include a timezone.")
    return parsed


def _unique_ids(values, where):
    if not isinstance(values, list) or not values:
        raise ReleaseError(f"{where} must be a nonempty list.")
    for value in values:
        text(value, where)
    if len(set(values)) != len(values):
        raise ReleaseError(f"{where} contains duplicates.")
    return set(values)


def read_object(path, description):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ReleaseError(f"{description} must not use symlinks.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError(f"{description} contains a duplicate JSON key.")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ReleaseError(f"{description} must be a JSON object.")
    fingerprint(value)  # Reject non-finite JSON numbers, including nested values.
    return path.resolve(), value


def validate_execution(value):
    object_fields(value, ("task_revision", "evaluator_revision", "model"), where="execution")
    for key in ("task_revision", "evaluator_revision"):
        text(value[key], key)
    model = object_fields(value["model"], ("provider", "name", "config_sha256"), where="model")
    for key in ("provider", "name"):
        text(model[key], f"model.{key}")
    digest(model["config_sha256"], "model.config_sha256")


def validate_policy(policy):
    object_fields(policy, ("format_version", "id", "producer", "dataset", "execution", "targets", "checks", "remote_export"), ("remote_verification",), where="policy")
    if type(policy["format_version"]) is not int or policy["format_version"] not in (1, 2):
        raise ReleaseError("Expected policy format_version=1 or 2.")
    if "remote_verification" in policy and policy["format_version"] != 2:
        raise ReleaseError("Built-in remote verification requires policy format_version=2.")
    if policy["format_version"] == 2:
        if (policy["remote_export"] == "required") != ("remote_verification" in policy):
            raise ReleaseError("Policy v2 remote-required releases need an independent remote verification policy.")
        if "remote_verification" in policy:
            remote = object_fields(policy["remote_verification"], ("project_id", "dataset_kind", "max_age_seconds"), where="remote verification policy")
            text(remote["project_id"], "remote project identity")
            if remote["dataset_kind"] not in ("local", "langfuse"):
                raise ReleaseError("Remote dataset kind must be local or langfuse.")
            if type(remote["max_age_seconds"]) is not int or not 0 < remote["max_age_seconds"] <= 3600:
                raise ReleaseError("Remote verification age must be 1–3600 seconds.")
    text(policy["id"], "policy.id")
    producer = object_fields(policy["producer"], ("kind", "schema_version", "script_path"), where="producer")
    if producer["kind"] not in ("langfuse-experiment-action", "langfuse-python-sdk", "langfuse-javascript-sdk") or producer["schema_version"] != "v1":
        raise ReleaseError("Only Action/SDK producers using Langfuse result_json v1 are supported.")
    text(producer["script_path"], "producer.script_path")
    dataset = object_fields(policy["dataset"], ("id", "name", "version", "cases"), ("schema_sha256",), where="dataset")
    for key in ("id", "name", "version"):
        text(dataset[key], f"dataset.{key}")
    if "schema_sha256" in dataset:
        digest(dataset["schema_sha256"], "dataset.schema_sha256")
    if not isinstance(dataset["cases"], list) or not dataset["cases"]:
        raise ReleaseError("Policy needs explicit dataset cases; an aggregate count is insufficient.")
    case_ids = []
    for case in dataset["cases"]:
        object_fields(case, ("id", "input_sha256", "expected_output_sha256"), where="dataset case")
        case_ids.append(text(case["id"], "case.id"))
        digest(case["input_sha256"], "case.input_sha256")
        digest(case["expected_output_sha256"], "case.expected_output_sha256")
    cases = _unique_ids(case_ids, "dataset case ids")
    validate_execution(policy["execution"])
    if not isinstance(policy["targets"], list) or not policy["targets"]:
        raise ReleaseError("Policy requires prompt targets.")
    names, covered = [], set()
    for target in policy["targets"]:
        object_fields(target, ("name", "case_ids"), where="policy target")
        names.append(text(target["name"], "target.name"))
        required = _unique_ids(target["case_ids"], "target.case_ids")
        if required - cases:
            raise ReleaseError("Target refers to an unpinned dataset case.")
        covered.update(required)
    _unique_ids(names, "policy target names")
    if covered != cases:
        raise ReleaseError("Every pinned dataset case must belong to a target.")
    if not isinstance(policy["checks"], list) or not policy["checks"]:
        raise ReleaseError("Policy requires independently selected checks.")
    checks = []
    for check in policy["checks"]:
        object_fields(check, ("id", "kind", "operator", "threshold"), where="policy check")
        checks.append(text(check["id"], "check.id"))
        value = check["threshold"]
        if check["kind"] == "boolean":
            if check["operator"] != "eq" or type(value) is not bool:
                raise ReleaseError("Boolean checks require operator=eq and a boolean threshold.")
        elif check["kind"] == "numeric":
            if check["operator"] not in ("eq", "gte", "lte") or type(value) not in (int, float) or not math.isfinite(value):
                raise ReleaseError("Numeric checks require a finite numeric threshold and eq/gte/lte.")
        else:
            raise ReleaseError("Checks support explicit boolean or numeric scores only.")
    _unique_ids(checks, "policy check ids")
    if policy["remote_export"] not in ("required", "optional"):
        raise ReleaseError("remote_export policy must be required or optional.")
    fingerprint(policy)
    return copy.deepcopy(policy)


def snapshot_policy(snapshot, *, required=False):
    if "evaluation_policy" not in snapshot:
        if required:
            raise ReleaseError("Pin an evaluation policy into the snapshot before staging.")
        return None
    binding = object_fields(snapshot["evaluation_policy"], ("format_version", "sha256", "policy", "pinned_at_utc"), where="snapshot evaluation policy")
    if type(binding["format_version"]) is not int or binding["format_version"] != 1:
        raise ReleaseError("Unsupported policy binding version.")
    policy = validate_policy(binding["policy"])
    if digest(binding["sha256"], "policy digest") != fingerprint(policy):
        raise ReleaseError("Pinned policy digest mismatch.")
    timestamp(binding["pinned_at_utc"], "policy pin time")
    names = {entry["name"] for entry in snapshot["prompts"]}
    if {target["name"] for target in policy["targets"]} - names:
        raise ReleaseError("Policy target is absent from the snapshot.")
    return copy.deepcopy(binding)


from langfuse_release_operations import locked_manifest


@locked_manifest
def pin_policy(manifest_path, policy_path):
    """Bind once, before staging. Replacing a policy requires a fresh export."""
    manifest_path, raw = read_object(manifest_path, "snapshot")
    _, snapshot = load_snapshot(manifest_path)
    _, source = read_object(policy_path, "policy")
    policy = validate_policy(source)
    if "evaluation_policy" in raw:
        existing = snapshot_policy(snapshot, required=True)
        if existing["sha256"] == fingerprint(policy):
            return existing
        raise ReleaseError("Snapshot already has a pinned policy; start from a fresh export to change it.")
    binding = {"format_version": 1, "sha256": fingerprint(policy), "policy": policy,
               "pinned_at_utc": datetime.now(timezone.utc).isoformat()}
    snapshot_policy({**snapshot, "evaluation_policy": binding}, required=True)
    # Persist raw exporter data, never the load_snapshot-only local_content cache.
    raw["evaluation_policy"] = binding
    write_report(manifest_path, raw)
    return copy.deepcopy(binding)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    try:
        binding = pin_policy(args.manifest, args.policy)
        print(f"Policy pinned: {binding['policy']['id']} ({binding['sha256']})")
        print("Stage this snapshot after pinning; any older stage report is invalid.")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Policy stopped: {exc}\n")


if __name__ == "__main__":
    main()
