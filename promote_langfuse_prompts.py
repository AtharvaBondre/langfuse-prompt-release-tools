#!/usr/bin/env python3
"""Preview or move one explicit label onto verified staged Langfuse versions.

Promotion is not atomic, does not evaluate model quality, and does not hold back
consumers of latest: Langfuse assigns latest when the staged version is created.
"""

import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langfuse_release_common import Client, ReleaseError, fingerprint, inside, make_payload, verify_exact
from publish_langfuse_prompts import load_snapshot, write_report
from evaluate_langfuse_release import validate_evaluation
from langfuse_release_policy import snapshot_policy


def _positive_int(value):
    return type(value) is int and value > 0


def _load_stage(snapshot, stage_path, label):
    stage = json.loads(stage_path.read_text(encoding="utf-8"))
    if (not isinstance(stage, dict) or stage.get("format_version") != 2 or
            stage.get("mode") != "stage" or stage.get("status") != "verified"):
        raise ReleaseError("Promotion requires a completed format_version=2 stage report.")
    if stage.get("release_strategy", "stage") != "stage":
        raise ReleaseError("Stage report has a conflicting release strategy.")
    if stage.get("host") != snapshot.get("host"):
        raise ReleaseError("Stage report host does not match the snapshot.")
    if stage.get("snapshot_sha256") != fingerprint(snapshot):
        raise ReleaseError("Snapshot or local prompt content changed since staging; promotion is blocked.")
    results = stage.get("results")
    if not isinstance(results, list) or not results:
        raise ReleaseError("Stage report has no results.")
    entries = {entry["name"]: entry for entry in snapshot["prompts"]}
    seen, plans = set(), []
    for result in results:
        if not isinstance(result, dict):
            raise ReleaseError("Stage report entries must be objects.")
        name = result.get("name")
        if not isinstance(name, str) or name not in entries or name in seen:
            raise ReleaseError("Stage report names must be unique and present in the snapshot.")
        seen.add(name)
        baseline = entries[name]["baseline"]
        if (not _positive_int(result.get("baseline_version")) or
                result["baseline_version"] != baseline["version"]):
            raise ReleaseError("Stage baseline version does not match the snapshot.")
        if type(result.get("changed")) is not bool:
            raise ReleaseError("Stage report changed flags must be booleans.")
        if not result["changed"]:
            if result.get("status") != "unchanged":
                raise ReleaseError("Unchanged stage items must have unchanged status.")
            continue
        version = result.get("created_version")
        if result.get("status") != "verified" or not _positive_int(version) or version <= baseline["version"]:
            raise ReleaseError("Changed stage items require a verified newer created version.")
        if label not in baseline["labels"]:
            raise ReleaseError("Promotion label was not on every original baseline; new labels are not invented.")
        payload = make_payload(baseline, entries[name]["local_content"], None)
        payload["labels"] = []
        plans.append({"name": name, "baseline": baseline, "payload": payload,
                      "target_version": version,
                      "result": {"name": name, "baseline_version": baseline["version"],
                                 "target_version": version, "status": "pending_preflight"}})
    if not plans:
        raise ReleaseError("Stage report has no changed verified versions to promote.")
    return stage, plans


def _inspect(client, plan, label):
    """Return pending or already_promoted after checking both version and label."""
    name, version = plan["name"], plan["target_version"]
    target = client.get(name, version=version)
    target_labels = set(target["labels"]) - {"latest"}
    if target_labels not in (set(), {label}):
        raise ReleaseError("Staged target has conflicting non-latest labels.")
    intended = {**plan["payload"], "labels": sorted(target_labels)}
    verify_exact(target, intended, version)
    selected = client.get(name, label=label)
    if target_labels == {label}:
        # A prior successful or uncertain attempt may already have moved this label.
        verify_exact(selected, {**plan["payload"], "labels": [label]}, version)
        return "already_promoted"
    if selected["version"] != plan["baseline"]["version"]:
        raise ReleaseError("Promotion label no longer points to the original baseline.")
    # Staging moves latest. Every other baseline label and all data must still match.
    verify_exact(selected, plan["baseline"], plan["baseline"]["version"])
    return "pending_promotion"


def promote(client, manifest_path, stage_report_path, *, label, apply=False, report_name=None,
            evaluation_report=None, require_evaluation=True, allow_structural_only=False):
    for field, value in (("apply", apply), ("require_evaluation", require_evaluation),
                         ("allow_structural_only", allow_structural_only)):
        if type(value) is not bool:
            raise ReleaseError(f"{field} must be a boolean.")
    if require_evaluation and allow_structural_only:
        raise ReleaseError("Required evaluation and allow_structural_only are contradictory; choose one promotion policy.")
    if not require_evaluation and not allow_structural_only:
        raise ReleaseError("Structural-only promotion requires explicit allow_structural_only=True.")
    if require_evaluation and evaluation_report is None:
        raise ReleaseError("This promotion requires --evaluation-report before any label change.")
    if (not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_.-]{1,36}", label) or
            label == "latest"):
        raise ReleaseError("Choose one explicit non-latest label using 1–36 lowercase letters, digits, _, . or -.")
    manifest_path, snapshot = load_snapshot(manifest_path)
    pinned_policy = snapshot_policy(snapshot)
    if pinned_policy is not None and (allow_structural_only or not require_evaluation or evaluation_report is None):
        raise ReleaseError("This snapshot pins an experiment policy; structural-only promotion cannot bypass it.")
    if snapshot.get("host") != client.host:
        raise ReleaseError("Snapshot host does not match the requested target.")
    stage_path = Path(stage_report_path)
    if stage_path.is_symlink():
        raise ReleaseError("Stage report must not be a symlink.")
    stage_path = stage_path.resolve()
    stage, plans = _load_stage(snapshot, stage_path, label)
    evaluation_path = None
    if evaluation_report is not None:
        evaluation_path = Path(evaluation_report)
        if evaluation_path.is_symlink():
            raise ReleaseError("Evaluation report must not be a symlink.")
        evaluation_path = evaluation_path.resolve()
    root = manifest_path.parent
    if report_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_name = f"promotion-{'apply' if apply else 'preview'}-{stamp}-{uuid.uuid4().hex[:8]}.json"
    report_path = inside(root / "reports", report_name)
    if report_path.suffix.lower() != ".json":
        raise ReleaseError("Report filename must end in .json.")
    if report_path in (manifest_path, stage_path, evaluation_path) or any(
            report_path == inside(root, entry["file"]) for entry in snapshot["prompts"]):
        raise ReleaseError("Promotion report would overwrite its source data.")
    report = {"format_version": 2, "host": client.host, "mode": "promote", "apply": apply,
              "status": "preflight", "label": label, "report_file": report_name,
              "snapshot_sha256": fingerprint(snapshot),
              "stage_report": {"file": str(stage_path), "sha256": fingerprint(stage)},
              "evaluation": {"required": bool(require_evaluation or evaluation_path is not None),
                             "allow_structural_only": allow_structural_only,
                             "policy_sha256": pinned_policy["sha256"] if pinned_policy else None,
                             "file": str(evaluation_path) if evaluation_path is not None else None,
                             "status": "pending" if evaluation_path is not None else "not_requested"},
              "results": [plan["result"] for plan in plans]}
    # Reserve evidence before any HTTP request; do not erase an earlier attempt.
    write_report(report_path, report, create=True)
    evidence_binding = None

    def check_evaluation():
        nonlocal evidence_binding
        if evaluation_path is None:
            return
        # Re-read local evidence before each PATCH; no commands from reports run.
        if evaluation_path.is_symlink() or stage_path.is_symlink():
            raise ReleaseError("Evaluation or stage report changed to a symlink.")
        _, current_snapshot = load_snapshot(manifest_path)
        current_stage = json.loads(stage_path.read_text(encoding="utf-8"))
        if (fingerprint(current_snapshot) != fingerprint(snapshot) or
                fingerprint(current_stage) != fingerprint(stage)):
            raise ReleaseError("Stage report or snapshot changed during promotion.")
        evidence = validate_evaluation(evaluation_path, current_snapshot, current_stage)
        current_binding = fingerprint(evidence)
        if evidence_binding is not None and current_binding != evidence_binding:
            raise ReleaseError("Evaluation evidence changed after promotion preflight.")
        evidence_binding = current_binding
        report["evaluation"].update({"status": "passed", "sha256": current_binding,
                                     "evidence_sha256": evidence["evidence_sha256"],
                                     "required_checks": evidence["required_checks"]})

    phase, active = "evaluation" if evaluation_path is not None else "preflight", None
    try:
        check_evaluation()
        phase = "preflight"
        for plan in plans:
            active = plan["result"]
            active["status"] = _inspect(client, plan, label)
            active = None
        report["status"] = "preflight_passed"
        write_report(report_path, report)
        if not apply:
            report["status"] = "dry_run_complete"
            write_report(report_path, report)
            return report
        for plan in plans:
            result = plan["result"]
            if result["status"] == "already_promoted":
                continue
            active, phase = result, "recheck"
            result["status"] = _inspect(client, plan, label)
            if result["status"] == "already_promoted":
                write_report(report_path, report)
                active = None
                continue
            intended = {**plan["payload"], "labels": [label]}
            phase = "evaluation"
            check_evaluation()
            phase = "patch"
            result["status"] = "submitting"
            write_report(report_path, report)
            returned = client.add_labels(plan["name"], plan["target_version"], [label])
            phase = "readback"
            result["status"] = "patch_returned_pending_verification"
            write_report(report_path, report)
            verify_exact(returned, intended, plan["target_version"])
            exact = client.get(plan["name"], version=plan["target_version"])
            verify_exact(exact, intended, plan["target_version"])
            selected = client.get(plan["name"], label=label)
            verify_exact(selected, intended, plan["target_version"])
            result["status"] = "verified"
            write_report(report_path, report)
            active = None
        report["status"] = "verified"
        write_report(report_path, report)
        return report
    except (ReleaseError, OSError, ValueError) as exc:
        report["status"], report["phase"], report["error"] = "stopped", phase, str(exc)
        if phase == "evaluation":
            report["evaluation"]["status"] = "blocked"
            report["evaluation"]["error"] = str(exc)
        if active is not None:
            active["error"] = str(exc)
            if phase == "patch" and getattr(exc, "unknown_write", False):
                active["status"] = "promotion_outcome_unknown_do_not_retry"
            elif phase == "readback":
                active["status"] = "patch_returned_but_verification_failed"
            elif phase == "patch":
                active["status"] = "promotion_failed"
            else:
                active["status"] = "stopped_before_patch"
        write_report(report_path, report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage-report", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--evaluation-report", help="Recorded application checks; validated whenever supplied.")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--require-evaluation", action="store_true", help="Require passing application evidence (the default).")
    policy.add_argument("--allow-structural-only", action="store_true", help="Explicitly allow legacy promotion without application evidence; any supplied evidence is still validated.")
    parser.add_argument("--report", help="New filename inside the snapshot's reports directory.")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        report = promote(Client(args.host, args.timeout), args.manifest, args.stage_report,
                         label=args.label, apply=args.apply, report_name=args.report,
                         evaluation_report=args.evaluation_report,
                         require_evaluation=not args.allow_structural_only,
                         allow_structural_only=args.allow_structural_only)
        print(f"Promotion status: {report['status']}")
        print(f"Report: {Path(args.manifest).resolve().parent / 'reports' / report['report_file']}")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Promotion stopped: {exc}\n")


if __name__ == "__main__":
    main()
