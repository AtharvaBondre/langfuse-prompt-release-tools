#!/usr/bin/env python3
"""Record trusted local application-check results for exact staged versions.

This command records supplied results; it does not run tests, shell commands or
models. Hashes detect changed files, not whether an author told the truth.
"""

import argparse
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langfuse_release_common import ReleaseError, fingerprint, inside
from publish_langfuse_prompts import load_snapshot, write_report
from langfuse_release_policy import snapshot_policy


def _read_object(path, description):
    path = Path(path)
    if path.is_symlink():
        raise ReleaseError(f"{description} must not be a symlink.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReleaseError(f"Cannot read {description} as a JSON object.") from None
    if not isinstance(value, dict):
        raise ReleaseError(f"{description} must be a JSON object.")
    fingerprint(value)
    return path.resolve(), value


def _required_check_ids(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ReleaseError("Supply a non-empty list of required check IDs.")
    if any(not isinstance(value, str) or not value.strip() or value != value.strip() or
           any(ord(char) < 32 for char in value) for value in values):
        raise ReleaseError("Required check IDs must be non-empty strings without surrounding whitespace or controls.")
    if len(set(values)) != len(values):
        raise ReleaseError("Required check IDs must be unique.")
    return sorted(values)


def _stage_targets(snapshot, stage):
    if (not isinstance(stage, dict) or stage.get("format_version") != 2 or
            stage.get("mode") != "stage" or stage.get("status") != "verified" or
            stage.get("release_strategy", "stage") != "stage"):
        raise ReleaseError("Evaluation requires a completed format_version=2 stage report.")
    if stage.get("host") != snapshot.get("host") or stage.get("snapshot_sha256") != fingerprint(snapshot):
        raise ReleaseError("Stage report does not match the current host, snapshot and local prompt content.")
    rows = stage.get("results")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("Stage report has no results.")
    entries = {entry["name"]: entry for entry in snapshot["prompts"]}
    seen, targets = set(), {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseError("Stage result entries must be objects.")
        name = row.get("name")
        if not isinstance(name, str) or name not in entries or name in seen:
            raise ReleaseError("Stage names must be unique and present in the snapshot.")
        seen.add(name)
        baseline = entries[name]["baseline"]["version"]
        if type(row.get("baseline_version")) is not int or row["baseline_version"] != baseline:
            raise ReleaseError("Stage baseline version differs from the snapshot.")
        if type(row.get("changed")) is not bool:
            raise ReleaseError("Stage changed flags must be booleans.")
        if not row["changed"]:
            if row.get("status") != "unchanged":
                raise ReleaseError("Unchanged stage entries must have unchanged status.")
            continue
        version = row.get("created_version")
        if row.get("status") != "verified" or type(version) is not int or version <= baseline:
            raise ReleaseError("Changed stage entries require a verified newer version.")
        targets[name] = version
    if not targets:
        raise ReleaseError("There are no changed verified staged versions to evaluate.")
    return targets


def _parse_results(source, snapshot, stage, required_checks):
    targets = _stage_targets(snapshot, stage)
    if type(source.get("format_version")) is not int or source["format_version"] != 1:
        raise ReleaseError("Application results require format_version=1.")
    if (source.get("snapshot_sha256") != fingerprint(snapshot) or
            source.get("stage_report_sha256") != fingerprint(stage)):
        raise ReleaseError("Application results are bound to a different snapshot or stage report.")
    rows = source.get("results")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("Application results contain no case results.")
    normalized, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseError("Application result entries must be objects.")
        name = row.get("name")
        if not isinstance(name, str) or name not in targets or name in seen:
            raise ReleaseError("Application result names must match the staged targets without duplicates.")
        seen.add(name)
        if type(row.get("version")) is not int or row["version"] != targets[name]:
            raise ReleaseError("Application results refer to a different staged version.")
        checks = row.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ReleaseError("Each staged target requires non-empty check results.")
        check_ids, clean_checks = set(), []
        for check in checks:
            if not isinstance(check, dict):
                raise ReleaseError("Check results must be objects.")
            check_id = _required_check_ids([check.get("id")])[0]
            if check_id in check_ids:
                raise ReleaseError("Check IDs must be unique within a target.")
            check_ids.add(check_id)
            if type(check.get("passed")) is not bool:
                raise ReleaseError("Check passed values must be JSON booleans, not strings or numbers.")
            clean = {"id": check_id, "passed": check["passed"]}
            if "detail" in check:
                if not isinstance(check["detail"], str):
                    raise ReleaseError("Optional check detail must be a string.")
                clean["detail"] = check["detail"]
            clean_checks.append(clean)
        if set(required_checks) - check_ids:
            raise ReleaseError("Application results are missing a required check for a staged target.")
        normalized[name] = {"name": name, "version": targets[name],
                            "checks": sorted(clean_checks, key=lambda check: check["id"])}
    if seen != set(targets):
        raise ReleaseError("Application results are missing one or more staged targets.")
    return [normalized[name] for name in sorted(targets)]


def _status(results):
    return "passed" if all(check["passed"] for row in results for check in row["checks"]) else "failed"


def record_evaluation(manifest_path, stage_report_path, results_path, *, required_checks, report_name=None):
    required = _required_check_ids(required_checks)
    manifest_path, snapshot = load_snapshot(manifest_path)
    if snapshot_policy(snapshot) is not None:
        raise ReleaseError("This snapshot pins an experiment policy; import execution-time experiment evidence instead of legacy checks.")
    stage_path, stage = _read_object(stage_report_path, "stage report")
    source_path, source = _read_object(results_path, "application results")
    results = _parse_results(source, snapshot, stage, required)
    root = manifest_path.parent
    if report_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_name = f"evaluation-{stamp}-{uuid.uuid4().hex[:8]}.json"
    report_path = inside(root / "reports", report_name)
    if report_path.suffix.lower() != ".json":
        raise ReleaseError("Evaluation report filename must end in .json.")
    if report_path in (manifest_path, stage_path, source_path) or any(
            report_path == inside(root, entry["file"]) for entry in snapshot["prompts"]):
        raise ReleaseError("Evaluation report would overwrite its input data.")
    report = {"format_version": 1, "mode": "application_evaluation", "status": _status(results),
              "host": snapshot["host"], "snapshot_sha256": fingerprint(snapshot),
              "stage_report": {"file": str(stage_path), "sha256": fingerprint(stage)},
              "source_results": {"file": str(source_path), "sha256": fingerprint(source)},
              "required_checks": required, "results": results, "report_file": report_name,
              "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
    report["evidence_sha256"] = fingerprint(report)
    write_report(report_path, report, create=True)
    return report


def validate_evaluation(report_path, snapshot, stage):
    """Re-read evidence and its source results; return passing evidence or fail closed."""
    _, report = _read_object(report_path, "evaluation report")
    policy = snapshot_policy(snapshot)
    if policy is not None or (type(report.get("format_version")) is int and report["format_version"] == 2):
        if report.get("mode") != "langfuse_experiment_evaluation" or type(report.get("format_version")) is not int or report["format_version"] != 2:
            raise ReleaseError("Pinned policy requires imported experiment evidence; legacy receipt downgrade is blocked.")
        from import_langfuse_experiment import validate_experiment
        return validate_experiment(report_path, snapshot, stage)
    if (type(report.get("format_version")) is not int or report["format_version"] != 1 or
            report.get("mode") != "application_evaluation"):
        raise ReleaseError("Expected a format_version=1 application-evaluation report.")
    unsigned = {key: value for key, value in report.items() if key != "evidence_sha256"}
    if report.get("evidence_sha256") != fingerprint(unsigned):
        raise ReleaseError("Evaluation report changed after it was recorded.")
    required = _required_check_ids(report.get("required_checks"))
    if report.get("host") != snapshot.get("host") or report.get("snapshot_sha256") != fingerprint(snapshot):
        raise ReleaseError("Evaluation report does not match the current snapshot and host.")
    binding = report.get("stage_report")
    if not isinstance(binding, dict) or binding.get("sha256") != fingerprint(stage):
        raise ReleaseError("Evaluation report belongs to a different stage report.")
    source_info = report.get("source_results")
    if not isinstance(source_info, dict) or not isinstance(source_info.get("file"), str) or not source_info["file"]:
        raise ReleaseError("Evaluation report lacks its source application-results file.")
    _, source = _read_object(source_info["file"], "source application results")
    if source_info.get("sha256") != fingerprint(source):
        raise ReleaseError("Source application results changed after evaluation was recorded.")
    results = _parse_results(source, snapshot, stage, required)
    if fingerprint(report.get("results")) != fingerprint(results) or report.get("status") != _status(results):
        raise ReleaseError("Evaluation report no longer matches its application results.")
    if report["status"] != "passed":
        raise ReleaseError("Application evaluation contains failed checks; promotion is blocked.")
    return copy.deepcopy(report)


evaluate = record_evaluation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage-report", required=True)
    parser.add_argument("--results", required=True, help="Trusted local application-check results JSON; nothing is executed.")
    parser.add_argument("--require-check", action="append", required=True, help="Required case ID, repeated for each check.")
    parser.add_argument("--report", help="New filename inside the snapshot's reports directory.")
    args = parser.parse_args()
    try:
        report = record_evaluation(args.manifest, args.stage_report, args.results,
                                   required_checks=args.require_check, report_name=args.report)
        print(f"Application evaluation: {report['status']}")
        print(f"Report: {Path(args.manifest).resolve().parent / 'reports' / report['report_file']}")
        return 0 if report["status"] == "passed" else 1
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Evaluation record stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
