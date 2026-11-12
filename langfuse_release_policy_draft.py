#!/usr/bin/env python3
"""Build or validate a reviewable experiment policy without contacting Langfuse."""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path, PureWindowsPath

from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_policy import text, timestamp, validate_policy
from publish_langfuse_prompts import write_report


def _fields(value, required, optional=(), *, where):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ReleaseError(f"{where} must be a JSON object.")
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing:
        raise ReleaseError(f"{where} is missing fields: {', '.join(missing)}.")
    if extra:
        raise ReleaseError(f"{where} has unsupported fields: {', '.join(extra)}.")
    return value


def _no_symlinks(path, where):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ReleaseError(f"{where} must not use symlinks.")


def read_json(path, *, where):
    """Read selected inputs only; reject duplicate keys and non-JSON numbers."""
    path = Path(path)
    _no_symlinks(path, where)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError(f"{where} contains duplicate JSON key {key!r}.")
            result[key] = value
        return result

    def constant(_value):
        raise ReleaseError(f"{where} contains a non-finite JSON number.")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{where} is not valid JSON at line {exc.lineno}, column {exc.colno}.") from None
    fingerprint(value)
    return value


def _cases(value):
    if isinstance(value, dict):
        _fields(value, ("cases",), where="cases file")
        value = value["cases"]
    if not isinstance(value, list) or not value:
        raise ReleaseError("cases must be a nonempty list of selected input/expected_output cases.")
    found = set()
    for index, case in enumerate(value):
        where = f"cases[{index}]"
        _fields(case, ("id", "input", "expected_output"), where=where)
        case_id = text(case["id"], f"{where}.id")
        if case_id in found:
            raise ReleaseError(f"{where}.id duplicates case {case_id!r}.")
        found.add(case_id)
        for field in ("input", "expected_output"):
            try:
                fingerprint(case[field])
            except ReleaseError:
                raise ReleaseError(f"{where}.{field} must contain finite JSON data.") from None
    return copy.deepcopy(value)


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _checks(value, *, where="spec.checks"):
    if not isinstance(value, list) or not value:
        raise ReleaseError(f"{where} must contain independently selected score checks.")
    ids = set()
    for index, check in enumerate(value):
        item_where = f"{where}[{index}]"
        _fields(check, ("id", "kind", "operator", "threshold"), where=item_where)
        check_id = text(check["id"], f"{item_where}.id")
        if check_id in ids:
            raise ReleaseError(f"{item_where}.id duplicates check {check_id!r}.")
        ids.add(check_id)
        if check["kind"] == "boolean":
            if check["operator"] != "eq":
                raise ReleaseError(f"{item_where}.operator must be eq for a boolean check.")
            if type(check["threshold"]) is not bool:
                raise ReleaseError(f"{item_where}.threshold must be true or false for a boolean check.")
        elif check["kind"] == "numeric":
            if check["operator"] not in ("eq", "gte", "lte"):
                raise ReleaseError(f"{item_where}.operator must be eq, gte or lte for a numeric check.")
            if not _finite_number(check["threshold"]):
                raise ReleaseError(f"{item_where}.threshold must be a finite number for a numeric check.")
        else:
            raise ReleaseError(f"{item_where}.kind must be boolean or numeric.")
    return copy.deepcopy(value)


def _targets(value, cases):
    if not isinstance(value, list) or not value:
        raise ReleaseError("spec.targets must contain explicit prompt names and case_ids.")
    available = {case["id"] for case in cases}
    covered, names = set(), set()
    for index, target in enumerate(value):
        where = f"spec.targets[{index}]"
        _fields(target, ("name", "case_ids"), where=where)
        name = text(target["name"], f"{where}.name")
        if name in names:
            raise ReleaseError(f"{where}.name duplicates target {name!r}.")
        names.add(name)
        values = target["case_ids"]
        if not isinstance(values, list) or not values:
            raise ReleaseError(f"{where}.case_ids must be a nonempty list.")
        seen = set()
        for case_index, case_id in enumerate(values):
            text(case_id, f"{where}.case_ids[{case_index}]")
            if case_id in seen:
                raise ReleaseError(f"{where}.case_ids duplicates case {case_id!r}.")
            if case_id not in available:
                raise ReleaseError(f"{where}.case_ids refers to unknown case {case_id!r}.")
            seen.add(case_id)
        covered.update(seen)
    if covered != available:
        raise ReleaseError(f"spec.targets omits selected cases: {', '.join(sorted(available - covered))}.")
    return copy.deepcopy(value)


def _source_revision(root, value, where):
    text(value, where)
    relative = Path(value)
    windows = PureWindowsPath(value)
    if relative.is_absolute() or windows.is_absolute() or windows.drive or ".." in relative.parts or "\\" in value:
        raise ReleaseError(f"{where} must be a relative path within --source-root, without parent traversal.")
    candidate = root / relative
    _no_symlinks(candidate, where)
    if not candidate.is_file():
        raise ReleaseError(f"{where} does not name a regular file within --source-root: {value}.")
    try:
        # Match sdk_experiment.identities(): canonical JSON fingerprint of text,
        # not the SHA-256 of raw file bytes or a claim about a Git commit.
        source = candidate.read_text(encoding="utf-8")
    except UnicodeError:
        raise ReleaseError(f"{where} must name a UTF-8 source file.") from None
    return fingerprint(source)


def build_policy(spec, cases, *, source_root="."):
    """Build an existing v1/v2 policy; never execute sources or inspect results.

    The caller chooses thresholds, targets, dataset identity and model config.
    Source paths resolve within source_root. The result can be reviewed and
    passed to the existing explicit policy-pin command without conversion.
    """
    _fields(spec, ("format_version", "id", "producer", "dataset", "execution", "targets", "checks", "remote_export"),
            ("remote_verification",), where="spec")
    if type(spec["format_version"]) is not int or spec["format_version"] not in (1, 2):
        raise ReleaseError("spec.format_version must be 1 or 2 (the output policy version).")
    text(spec["id"], "spec.id")
    cases = _cases(cases)
    checks = _checks(spec["checks"])
    targets = _targets(spec["targets"], cases)
    producer = _fields(spec["producer"], ("kind", "schema_version", "script_path"), where="spec.producer")
    if producer["kind"] not in ("langfuse-experiment-action", "langfuse-python-sdk", "langfuse-javascript-sdk"):
        raise ReleaseError("spec.producer.kind must select a supported Action/Python/JavaScript producer.")
    if producer["schema_version"] != "v1":
        raise ReleaseError("spec.producer.schema_version must be v1.")
    text(producer["script_path"], "spec.producer.script_path")
    dataset = _fields(spec["dataset"], ("id", "name"), ("version", "schema"), where="spec.dataset")
    for key in ("id", "name"):
        text(dataset[key], f"spec.dataset.{key}")
    if "version" in dataset:
        text(dataset["version"], "spec.dataset.version")
    if "schema" in dataset and not isinstance(dataset["schema"], dict):
        raise ReleaseError("spec.dataset.schema must be a JSON object.")
    execution = _fields(spec["execution"], ("task_file", "evaluator_file", "model"), where="spec.execution")
    model = _fields(execution["model"], ("provider", "name", "config"), where="spec.execution.model")
    for key in ("provider", "name"):
        text(model[key], f"spec.execution.model.{key}")
    if not isinstance(model["config"], dict):
        raise ReleaseError("spec.execution.model.config must be a JSON object.")
    try:
        model_digest = fingerprint(model["config"])
    except ReleaseError:
        raise ReleaseError("spec.execution.model.config must contain finite JSON data.") from None
    if spec["remote_export"] not in ("required", "optional"):
        raise ReleaseError("spec.remote_export must be required or optional.")
    if "remote_verification" in spec:
        remote = _fields(spec["remote_verification"], ("project_id", "dataset_kind", "max_age_seconds"), where="spec.remote_verification")
        text(remote["project_id"], "spec.remote_verification.project_id")
        if remote["dataset_kind"] not in ("local", "langfuse"):
            raise ReleaseError("spec.remote_verification.dataset_kind must be local or langfuse.")
        if type(remote["max_age_seconds"]) is not int or not 0 < remote["max_age_seconds"] <= 3600:
            raise ReleaseError("spec.remote_verification.max_age_seconds must be an integer from 1 to 3600.")
        if spec["format_version"] != 2 or spec["remote_export"] != "required":
            raise ReleaseError("spec.remote_verification requires format_version=2 and remote_export=required.")
        if remote["dataset_kind"] == "langfuse":
            if "version" not in dataset:
                raise ReleaseError("spec.dataset.version is required for a hosted Langfuse dataset; use its captured timestamp, not a local case hash.")
            timestamp(dataset["version"], "spec.dataset.version")
            if "schema" in dataset:
                _fields(dataset["schema"], ("inputSchema", "expectedOutputSchema"), where="spec.dataset.schema")
    elif spec["format_version"] == 2 and spec["remote_export"] == "required":
        raise ReleaseError("spec.remote_verification is required for a version 2 remote-required policy.")
    root = Path(source_root)
    _no_symlinks(root, "--source-root")
    if not root.is_dir():
        raise ReleaseError("--source-root must be an existing directory.")
    root = root.resolve()
    policy = {
        "format_version": spec["format_version"], "id": spec["id"],
        "producer": copy.deepcopy(producer),
        "dataset": {
            "id": dataset["id"], "name": dataset["name"],
            "version": dataset.get("version", fingerprint(cases)),
            "cases": [{"id": case["id"], "input_sha256": fingerprint(case["input"]),
                       "expected_output_sha256": fingerprint(case["expected_output"])} for case in cases],
        },
        "execution": {
            "task_revision": _source_revision(root, execution["task_file"], "spec.execution.task_file"),
            "evaluator_revision": _source_revision(root, execution["evaluator_file"], "spec.execution.evaluator_file"),
            "model": {"provider": model["provider"], "name": model["name"], "config_sha256": model_digest},
        },
        "targets": targets, "checks": checks, "remote_export": spec["remote_export"],
    }
    if "schema" in dataset:
        policy["dataset"]["schema_sha256"] = fingerprint(dataset["schema"])
    if "remote_verification" in spec:
        policy["remote_verification"] = copy.deepcopy(spec["remote_verification"])
    return validate_policy(policy)


def generate_policy(spec_path, cases_path, *, source_root=".", output_path=None):
    """Build from files, optionally reserving a new output file without overwrite."""
    policy = build_policy(read_json(spec_path, where="spec file"),
                          read_json(cases_path, where="cases file"), source_root=source_root)
    if output_path is not None:
        path = Path(output_path)
        _no_symlinks(path, "--out")
        if path.suffix.lower() != ".json":
            raise ReleaseError("--out must name a new .json file.")
        if path.exists():
            raise ReleaseError("--out already exists; choose a new filename and preserve the reviewed draft.")
        write_report(path, policy, create=True)
    return policy


def validate_policy_file(path):
    """Check the existing policy contract without pinning or changing files."""
    policy = read_json(path, where="policy file")
    if isinstance(policy, dict) and "checks" in policy:
        _checks(policy["checks"], where="policy.checks")
    return validate_policy(policy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--spec", help="Reviewed choices and source paths; does not accept evaluation results.")
    action.add_argument("--validate", metavar="POLICY", help="Validate an existing v1/v2 policy without writing or pinning.")
    parser.add_argument("--cases", help="Selected input/expected_output cases as a JSON list or {cases: [...]}.")
    parser.add_argument("--source-root", default=None, help="Root for task/evaluator files (default: current directory).")
    parser.add_argument("--out", help="New policy .json path; existing files are never overwritten.")
    args = parser.parse_args()
    if args.spec and (not args.cases or not args.out):
        parser.error("--spec requires --cases and --out")
    if args.validate and (args.cases is not None or args.out is not None or args.source_root is not None):
        parser.error("--validate does not accept --cases, --source-root or --out")
    try:
        if args.validate:
            policy = validate_policy_file(args.validate)
            print(f"Policy valid: {policy['id']} ({fingerprint(policy)})")
            print("Validation checks structure and policy rules; it does not verify sources or server state.")
        else:
            policy = generate_policy(args.spec, args.cases, source_root=args.source_root or ".", output_path=args.out)
            print(f"Policy draft written: {Path(args.out).resolve()}")
            print(f"Policy: {policy['id']}; cases: {len(policy['dataset']['cases'])}; checks: {len(policy['checks'])}")
            print("Review the cases, identities and thresholds before explicitly pinning with langfuse-release-policy.")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Policy draft stopped: {exc}\n")


if __name__ == "__main__":
    main()
