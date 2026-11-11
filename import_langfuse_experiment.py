#!/usr/bin/env python3
"""Import official Experiment Action v1 per-item results under a pinned policy.

The execution capture is a trusted producer attestation, not proof against a
producer that fabricates data. Hashes bind preserved artifacts; no models run.
"""
import argparse
import copy
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from langfuse_release_common import ReleaseError, fingerprint, inside
from langfuse_release_policy import (object_fields, read_object, snapshot_policy,
                                    text, timestamp, validate_execution)
from publish_langfuse_prompts import load_snapshot, write_report


def _targets(snapshot, stage, policy):
    from evaluate_langfuse_release import _stage_targets
    versions = _stage_targets(snapshot, stage)
    if set(versions) != {target["name"] for target in policy["targets"]}:
        raise ReleaseError("Pinned policy targets must exactly cover all changed staged prompts.")
    return sorted(({"name": target["name"], "version": versions[target["name"]],
                    "case_ids": sorted(target["case_ids"])} for target in policy["targets"]),
                  key=lambda target: target["name"])


def _dataset_identity(policy):
    return {key: policy["dataset"][key] for key in ("id", "name", "version", "schema_sha256") if key in policy["dataset"]}


def _capture_lock(reference, snapshot=None, stage=None, *, started=None):
    from langfuse_release_dependencies import validate_lock_integrity
    object_fields(reference, ("file", "sha256"), where="dependency lock reference")
    text(reference["file"], "dependency lock file")
    _, lock = read_object(reference["file"], "dependency lock")
    validate_lock_integrity(lock)
    if reference["sha256"] != fingerprint(lock):
        raise ReleaseError("Dependency lock changed after execution capture began.")
    if snapshot is not None and (lock.get("snapshot_sha256") != fingerprint(snapshot) or lock.get("host") != snapshot["host"]):
        raise ReleaseError("Dependency lock belongs to another snapshot or host.")
    if stage is not None and lock.get("stage_report_sha256") != fingerprint(stage):
        raise ReleaseError("Dependency lock belongs to another stage report.")
    if started is not None and timestamp(lock.get("recorded_at_utc"), "dependency lock time") > started:
        raise ReleaseError("Dependency lock must exist before experiment execution starts.")
    return lock


def start_capture(manifest_path, stage_report_path, *, dataset, execution, dependency_lock_path=None):
    """Call before evaluating, with actual dataset and task/model identities.

`dataset` contains id/name/version, not cases. `execution` is the producer's
actual task revision, evaluator revision and model configuration identity.
"""
    _, snapshot = load_snapshot(manifest_path)
    _, stage = read_object(stage_report_path, "stage report")
    binding = snapshot_policy(snapshot, required=True)
    policy = binding["policy"]
    expected_dataset = _dataset_identity(policy)
    if dataset != expected_dataset:
        raise ReleaseError("Actual dataset identity/version differs from the pinned policy.")
    validate_execution(execution)
    if execution != policy["execution"]:
        raise ReleaseError("Actual task, evaluator or model configuration differs from policy.")
    capture = {"format_version": 1, "mode": "langfuse_execution_capture",
            "snapshot_sha256": fingerprint(snapshot), "stage_sha256": fingerprint(stage),
            "policy_sha256": binding["sha256"], "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "completed_at_utc": None, "dataset": copy.deepcopy(dataset),
            "execution": copy.deepcopy(execution), "targets": _targets(snapshot, stage, policy),
            "completion": {"local": "running", "remote_export": "not_attempted"},
            "result_sha256": None, "items": []}
    if dependency_lock_path is not None:
        lock_path, lock = read_object(dependency_lock_path, "dependency lock")
        reference = {"file": str(lock_path), "sha256": fingerprint(lock)}
        checked = _capture_lock(reference, snapshot, stage, started=timestamp(capture["started_at_utc"], "execution start"))
        expected = {(target["name"], target["version"]) for target in capture["targets"]}
        if {(root["name"], root["version"]) for root in checked["roots"]} != expected:
            raise ReleaseError("Dependency lock must cover exactly the staged prompt targets.")
        capture["dependency_lock"] = reference
    return capture


def capture_prompt(capture, *, item_id, requested_name, requested_version, prompt):
    """Record returned SDK prompt identity immediately after a versioned get.

Accepts a Langfuse prompt client or a plain mapping for offline adapters. The
caller must pass the arguments actually used for get_prompt, never a label.
"""
    if capture.get("completed_at_utc") is not None or capture.get("completion", {}).get("local") != "running":
        raise ReleaseError("Capture is finalized or is not running.")
    expected = next((target for target in capture["targets"] if target["name"] == requested_name), None)
    if (expected is None or item_id not in expected["case_ids"] or type(requested_version) is not int or
            requested_version != expected["version"]):
        raise ReleaseError("Prompt request is outside the pinned target/case/version plan.")
    returned_name = prompt.get("name") if isinstance(prompt, dict) else getattr(prompt, "name", None)
    returned_version = prompt.get("version") if isinstance(prompt, dict) else getattr(prompt, "version", None)
    if returned_name != requested_name or type(returned_version) is not int or returned_version != requested_version:
        raise ReleaseError("Langfuse returned a different prompt identity/version than requested.")
    item = next((entry for entry in capture["items"] if entry["id"] == item_id), None)
    if item is None:
        item = {"id": item_id, "prompts": []}
        capture["items"].append(item)
    if any(observed["requested_name"] == requested_name for observed in item["prompts"]):
        raise ReleaseError("Prompt capture already exists for this case; duplicate observations are ambiguous.")
    observation = {"requested_name": requested_name, "requested_version": requested_version,
                   "returned_name": returned_name, "returned_version": returned_version}
    if "dependency_lock" in capture:
        from langfuse_release_dependencies import prompt_sha256
        lock = _capture_lock(capture["dependency_lock"])
        root = next((root for root in lock["roots"] if root["name"] == requested_name and root["version"] == requested_version), None)
        content = prompt.get("prompt") if isinstance(prompt, dict) else getattr(prompt, "prompt", None)
        config = prompt.get("config") if isinstance(prompt, dict) else getattr(prompt, "config", None)
        kind = prompt.get("type") if isinstance(prompt, dict) else getattr(prompt, "type", None)
        if kind not in ("text", "chat"):
            kind = "text" if isinstance(content, str) else "chat" if isinstance(content, list) else None
        if root is None or kind is None:
            raise ReleaseError("Runtime prompt content/type is missing from the dependency capture.")
        actual_hash = prompt_sha256({"name": returned_name, "version": returned_version,
                                     "type": kind, "prompt": content, "config": config})
        if actual_hash != root["resolved_sha256"]:
            raise ReleaseError("SDK resolved prompt content/config differs from the pre-execution dependency lock.")
        observation["resolved_sha256"] = actual_hash
    item["prompts"].append(observation)
    return copy.deepcopy(observation)


def finalize_capture(capture, result_path, *, remote_export="not_attempted"):
    """Finish after local evaluation completes; record remote flush separately.

This does not infer remote export success from a URL or from a flush call that
only queued data. The producer must establish the stated completion status.
"""
    if capture.get("completed_at_utc") is not None or capture.get("completion", {}).get("local") != "running":
        raise ReleaseError("Capture cannot be finalized twice.")
    if remote_export not in ("confirmed", "failed", "not_attempted", "unconfirmed"):
        raise ReleaseError("Unknown remote export outcome.")
    _, result = read_object(result_path, "Action results")
    finished = copy.deepcopy(capture)
    finished["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    finished["completion"] = {"local": "completed", "remote_export": remote_export}
    finished["result_sha256"] = fingerprint(result)
    return finished


def _scores(values, where):
    if not isinstance(values, list):
        raise ReleaseError(f"{where} must be an array.")
    seen = set()
    for score in values:
        object_fields(score, ("name", "value"), ("comment", "metadata", "data_type", "config_id"), where=where)
        name = text(score["name"], f"{where}.name")
        if name in seen:
            raise ReleaseError(f"{where} contains duplicate score names.")
        seen.add(name)
        value = score["value"]
        if value is not None and type(value) not in (str, bool, int, float):
            raise ReleaseError(f"{where} contains an unsupported score value.")
        for field in ("comment", "data_type", "config_id"):
            if field in score and not isinstance(score[field], str):
                raise ReleaseError(f"{where}.{field} must be a string.")
        if "metadata" in score and not isinstance(score["metadata"], dict):
            raise ReleaseError(f"{where}.metadata must be an object.")
    return values


def _action_result(source, policy):
    """Validate the official v1 envelope, then require the supported subset."""
    object_fields(source, ("schema_version", "action_metadata", "results"), where="Action result")
    if source["schema_version"] != "v1":
        raise ReleaseError("Unknown Action result_json version; expected v1.")
    if not isinstance(source["action_metadata"], dict) or any(not isinstance(v, str) for v in source["action_metadata"].values()):
        raise ReleaseError("Action metadata must map names to strings.")
    results = source["results"]
    if not isinstance(results, list) or len(results) != 1:
        raise ReleaseError("This adapter requires exactly one independently pinned experiment script.")
    result = object_fields(results[0], ("script_path", "script_name", "runtime", "duration_ms", "status",
                                       "langfuse_experiment_url", "error", "experiment_result"), where="script result")
    if result["script_path"] != policy["producer"]["script_path"]:
        raise ReleaseError("Experiment script differs from the pinned producer.")
    text(result["script_name"], "script_name")
    if result["runtime"] not in ("python", "node") or result["status"] not in ("passed", "regression", "error"):
        raise ReleaseError("Unsupported script runtime/status.")
    if type(result["duration_ms"]) not in (int, float) or not math.isfinite(result["duration_ms"]) or result["duration_ms"] < 0:
        raise ReleaseError("Script duration must be a finite nonnegative number.")
    url = result["langfuse_experiment_url"]
    if url is not None and (not isinstance(url, str) or not urlsplit(url).scheme):
        raise ReleaseError("Experiment URL must be a URI or null.")
    error = result["error"]
    if error is not None:
        object_fields(error, ("name", "message", "is_regression"), ("details",), where="script error")
        for field in ("name", "message", "details"):
            if field in error and not isinstance(error[field], str):
                raise ReleaseError("Script error text must be strings.")
        if type(error["is_regression"]) is not bool:
            raise ReleaseError("Script error is_regression must be boolean.")
    if result["status"] == "passed" and error is not None:
        raise ReleaseError("Passing script result contains an error.")
    experiment = object_fields(result["experiment_result"], ("item_results", "run_evaluations"),
                               ("experiment_id", "run_name", "dataset_run_id"), where="experiment_result")
    for field in ("experiment_id", "run_name", "dataset_run_id"):
        if field in experiment and not isinstance(experiment[field], str):
            raise ReleaseError(f"{field} must be a string.")
    if not isinstance(experiment["item_results"], list) or not experiment["item_results"]:
        raise ReleaseError("Per-item evidence is required; aggregate scores cannot establish case coverage.")
    _scores(experiment["run_evaluations"], "run evaluations")
    # Run summaries are preserved for inspection, but do not replace pinned item checks.
    for item in experiment["item_results"]:
        object_fields(item, ("item", "evaluations"), ("input", "expected_output", "output", "trace_id", "dataset_run_id"), where="item result")
        if not isinstance(item["item"], dict):
            raise ReleaseError("Experiment item must be an object.")
        _scores(item["evaluations"], "item evaluations")
        for field in ("trace_id", "dataset_run_id"):
            if field in item and not isinstance(item[field], str):
                raise ReleaseError(f"{field} must be a string.")
    fingerprint(source)
    return result


def _validate_capture(capture, snapshot, stage, binding, result):
    object_fields(capture, ("format_version", "mode", "snapshot_sha256", "stage_sha256", "policy_sha256",
                           "started_at_utc", "completed_at_utc", "dataset", "execution", "targets",
                           "completion", "result_sha256", "items"), ("dependency_lock",), where="execution capture")
    if type(capture["format_version"]) is not int or capture["format_version"] != 1 or capture["mode"] != "langfuse_execution_capture":
        raise ReleaseError("Expected execution capture format_version=1.")
    expected_hashes = {"snapshot_sha256": fingerprint(snapshot), "stage_sha256": fingerprint(stage),
                       "policy_sha256": binding["sha256"], "result_sha256": fingerprint(result)}
    for key, expected in expected_hashes.items():
        if capture[key] != expected:
            raise ReleaseError(f"Execution capture {key} does not match independently loaded evidence.")
    started = timestamp(capture["started_at_utc"], "execution start")
    completed = timestamp(capture["completed_at_utc"], "execution completion")
    if started < timestamp(binding["pinned_at_utc"], "policy pin time") or completed < started:
        raise ReleaseError("Policy must be pinned before execution starts; completion must follow start.")
    policy = binding["policy"]
    if capture["dataset"] != _dataset_identity(policy):
        raise ReleaseError("Execution dataset identity/version differs from policy.")
    validate_execution(capture["execution"])
    if capture["execution"] != policy["execution"]:
        raise ReleaseError("Execution task, evaluator or model identity differs from policy.")
    targets = _targets(snapshot, stage, policy)
    lock = _capture_lock(capture["dependency_lock"], snapshot, stage, started=started) if "dependency_lock" in capture else None
    if lock is not None and {(root["name"], root["version"]) for root in lock["roots"]} != {(target["name"], target["version"]) for target in targets}:
        raise ReleaseError("Dependency lock must cover exactly the staged prompt targets.")
    if capture["targets"] != targets:
        raise ReleaseError("Execution target plan does not match the staged versions and policy cases.")
    completion = object_fields(capture["completion"], ("local", "remote_export"), where="completion")
    if completion["local"] != "completed" or completion["remote_export"] not in ("confirmed", "failed", "not_attempted", "unconfirmed"):
        raise ReleaseError("Local experiment did not complete, or export outcome is invalid.")
    if policy["remote_export"] == "required" and completion["remote_export"] != "confirmed":
        raise ReleaseError("Pinned policy requires confirmed remote export, separately from local completion.")
    if not isinstance(capture["items"], list):
        raise ReleaseError("Capture items must be an array.")
    expected_cases = {case["id"] for case in policy["dataset"]["cases"]}
    seen = set()
    for item in capture["items"]:
        object_fields(item, ("id", "prompts"), where="captured item")
        case = text(item["id"], "captured item id")
        if case in seen or case not in expected_cases:
            raise ReleaseError("Captured case is duplicate or outside policy.")
        seen.add(case)
        if not isinstance(item["prompts"], list):
            raise ReleaseError("Captured prompts must be an array.")
        expected = {target["name"]: target["version"] for target in targets if case in target["case_ids"]}
        observed = set()
        for prompt in item["prompts"]:
            object_fields(prompt, ("requested_name", "requested_version", "returned_name", "returned_version"), ("resolved_sha256",), where="prompt observation")
            name = text(prompt["requested_name"], "requested prompt name")
            if name in observed or name not in expected:
                raise ReleaseError("Captured prompt is duplicate or not required for this case.")
            observed.add(name)
            if (prompt["returned_name"] != name or type(prompt["requested_version"]) is not int or
                    type(prompt["returned_version"]) is not int or
                    prompt["requested_version"] != expected[name] or prompt["returned_version"] != expected[name]):
                raise ReleaseError("Runtime prompt identity/version does not match the staged target.")
            if lock is not None:
                root = next(root for root in lock["roots"] if root["name"] == name)
                if prompt.get("resolved_sha256") != root["resolved_sha256"]:
                    raise ReleaseError("Runtime resolved content/config is absent or differs from the dependency lock.")
            elif "resolved_sha256" in prompt:
                raise ReleaseError("Resolved content claim has no independently captured dependency lock.")
        if observed != set(expected):
            raise ReleaseError("Missing runtime prompt observation for a required target/case.")
    if seen != expected_cases:
        raise ReleaseError("Execution capture is missing required dataset cases.")


def _item_value(item_result, key):
    item = item_result["item"]
    if key not in item_result and key not in item:
        raise ReleaseError(f"Per-item evidence must include {key}, including explicit null if absent by design.")
    if key in item_result and key in item and fingerprint(item_result[key]) != fingerprint(item[key]):
        raise ReleaseError(f"Item and result disagree about {key}.")
    return item_result[key] if key in item_result else item[key]


def _check_score(score, check):
    value = score["value"]
    if check["kind"] == "boolean":
        if type(value) is not bool:
            raise ReleaseError("Boolean policy check received a nonboolean score; coercion is forbidden.")
    elif type(value) not in (int, float) or not math.isfinite(value):
        raise ReleaseError("Numeric policy check received a nonnumeric/nonfinite score; coercion is forbidden.")
    operator, threshold = check["operator"], check["threshold"]
    passed = value == threshold if operator == "eq" else value >= threshold if operator == "gte" else value <= threshold
    return {"id": check["id"], "value": value, "passed": passed}


def _derive(snapshot, stage, source, capture):
    binding = snapshot_policy(snapshot, required=True)
    policy = binding["policy"]
    _validate_capture(capture, snapshot, stage, binding, source)
    script = _action_result(source, policy)
    cases = {case["id"]: case for case in policy["dataset"]["cases"]}
    checks = {check["id"]: check for check in policy["checks"]}
    normalized = {}
    for result in script["experiment_result"]["item_results"]:
        case_id = text(result["item"].get("id"), "experiment item.id")
        if case_id in normalized or case_id not in cases:
            raise ReleaseError("Experiment contains duplicate or unexpected dataset cases.")
        case = cases[case_id]
        for field in ("input", "expected_output"):
            if fingerprint(_item_value(result, field)) != case[f"{field}_sha256"]:
                raise ReleaseError(f"Dataset case {field} changed since policy pinning.")
        if "output" not in result:
            raise ReleaseError("Each evaluated case must preserve its actual output.")
        scores = {score["name"]: score for score in result["evaluations"]}
        if set(scores) != set(checks):
            raise ReleaseError("Item scores must exactly match independently pinned check IDs.")
        normalized[case_id] = {"id": case_id, "output_sha256": fingerprint(result["output"]),
                               "checks": [_check_score(scores[name], checks[name]) for name in sorted(checks)]}
    if set(normalized) != set(cases):
        raise ReleaseError("Experiment is missing required dataset cases.")
    results = []
    for target in _targets(snapshot, stage, policy):
        results.append({"name": target["name"], "version": target["version"],
                        "checks": [{"id": name, "passed": all(
                            next(check["passed"] for check in normalized[case]["checks"] if check["id"] == name)
                            for case in target["case_ids"])} for name in sorted(checks)]})
    passed = script["status"] == "passed" and all(check["passed"] for item in results for check in item["checks"])
    return {"format_version": 2, "mode": "langfuse_experiment_evaluation", "host": snapshot["host"],
            "status": "passed" if passed else "failed", "snapshot_sha256": fingerprint(snapshot),
            "policy_sha256": binding["sha256"], "required_checks": sorted(checks), "results": results,
            "dependency_lock_sha256": capture.get("dependency_lock", {}).get("sha256"),
            "cases": [normalized[name] for name in sorted(normalized)],
            "producer": copy.deepcopy(policy["producer"]), "completion": copy.deepcopy(capture["completion"]),
            "script_status": script["status"], "run_evaluations": copy.deepcopy(script["experiment_result"]["run_evaluations"])}


def import_experiment(manifest_path, stage_report_path, result_path, capture_path, *, report_name=None):
    manifest_path, snapshot = load_snapshot(manifest_path)
    stage_path, stage = read_object(stage_report_path, "stage report")
    source_path, source = read_object(result_path, "Action results")
    capture_path, capture = read_object(capture_path, "execution capture")
    report = _derive(snapshot, stage, source, capture)
    report.update({"stage_report": {"file": str(stage_path), "sha256": fingerprint(stage)},
                   "source_results": {"file": str(source_path), "sha256": fingerprint(source)},
                   "execution_capture": {"file": str(capture_path), "sha256": fingerprint(capture)}})
    if report_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_name = f"experiment-{stamp}-{uuid.uuid4().hex[:8]}.json"
    path = inside(manifest_path.parent / "reports", report_name)
    if path.suffix.lower() != ".json" or path in (manifest_path, stage_path, source_path, capture_path) or any(
            path == inside(manifest_path.parent, entry["file"]) for entry in snapshot["prompts"]):
        raise ReleaseError("Experiment report must be a new JSON file, separate from its sources.")
    report.update({"report_file": report_name, "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
    report["evidence_sha256"] = fingerprint(report)
    write_report(path, report, create=True)
    return report


def validate_experiment(report_path, snapshot, stage):
    _, report = read_object(report_path, "experiment receipt")
    unsigned = copy.deepcopy(report)
    claimed = unsigned.pop("evidence_sha256", None)
    if claimed != fingerprint(unsigned):
        raise ReleaseError("Experiment receipt digest mismatch.")
    loaded = {}
    for field, description in (("source_results", "Action results"), ("execution_capture", "execution capture")):
        reference = object_fields(report.get(field), ("file", "sha256"), where=field)
        text(reference["file"], f"{field}.file")
        _, loaded[field] = read_object(reference["file"], description)
        if fingerprint(loaded[field]) != reference["sha256"]:
            raise ReleaseError(f"{description} changed after import.")
    expected = _derive(snapshot, stage, loaded["source_results"], loaded["execution_capture"])
    for field, value in expected.items():
        if fingerprint(report.get(field)) != fingerprint(value):
            raise ReleaseError(f"Experiment receipt {field} disagrees with source evidence and pinned policy.")
    stage_reference = object_fields(report.get("stage_report"), ("file", "sha256"), where="stage reference")
    if stage_reference["sha256"] != fingerprint(stage):
        raise ReleaseError("Experiment receipt is bound to a different stage report.")
    if expected["status"] != "passed":
        raise ReleaseError("Experiment checks or execution failed; promotion is blocked.")
    return copy.deepcopy(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage-report", required=True)
    parser.add_argument("--results", required=True, help="Official Action result_json v1 file.")
    parser.add_argument("--capture", required=True, help="Execution-time prompt/dataset/model capture.")
    parser.add_argument("--report", help="New receipt filename inside reports/.")
    args = parser.parse_args()
    try:
        report = import_experiment(args.manifest, args.stage_report, args.results, args.capture, report_name=args.report)
        print(f"Experiment evidence: {report['status']}")
        print(f"Report: {Path(args.manifest).resolve().parent / 'reports' / report['report_file']}")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Experiment import stopped: {exc}\n")


if __name__ == "__main__":
    main()
