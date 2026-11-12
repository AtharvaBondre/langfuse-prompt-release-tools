#!/usr/bin/env python3
"""Inspect saved release evidence without changing files or contacting Langfuse.

This is a local explanation, not a promotion gate or a provenance verifier.
Reports describe historical observations. A label update never proves app use.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_policy import read_object, snapshot_policy
from publish_langfuse_prompts import load_snapshot
from evaluate_langfuse_release import _parse_results, _required_check_ids, _stage_targets, _status
from langfuse_release_dependencies import validate_lock_integrity


MODES = {
    "stage": {"stage"},
    "evaluation": {"application_evaluation", "langfuse_experiment_evaluation"},
    "dependencies": {"dependency_lock"},
    "promotion": {"promote"},
    "remote": {"remote_experiment_persistence"},
    "adoption": {"consumer_adoption"},
    "rollout": {"rollout_observation"},
    "rollout_window": {"rollout_window"},
}
DISPLAY = ("stage", "evaluation", "remote", "dependencies", "promotion", "adoption", "rollout")


def _time(value, field):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ReleaseError(f"{field} must be a timezone-aware timestamp.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReleaseError(f"{field} must be a timezone-aware timestamp.")
    return value.astimezone(timezone.utc)


def _bound(path, root):
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
        raise ReleaseError("Inspection inputs must not use symlinks.")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ReleaseError("Referenced evidence is outside this workspace; restore it inside the release directory.")
    return resolved


def _read(path, root):
    path = _bound(path, root)
    _, value = read_object(path, "status input")
    return path, value


def _seal(value):
    if value.get("evidence_sha256") != fingerprint({k: v for k, v in value.items() if k != "evidence_sha256"}):
        raise ReleaseError("Saved evidence digest does not match its contents.")


def _binding(value, snapshot, stage=None):
    if value.get("host") != snapshot.get("host") or value.get("snapshot_sha256") != fingerprint(snapshot):
        raise ReleaseError("Report belongs to a different host, snapshot or local prompt content.")
    if stage is not None:
        ref = value.get("stage_report")
        if not isinstance(ref, dict) or ref.get("sha256") != fingerprint(stage):
            raise ReleaseError("Report belongs to a different stage attempt.")


def _reference(value, key, root):
    ref = value.get(key)
    if not isinstance(ref, dict) or not isinstance(ref.get("file"), str) or not ref["file"]:
        raise ReleaseError(f"Missing {key} source reference.")
    path, source = _read(ref["file"], root)
    if ref.get("sha256") != fingerprint(source):
        raise ReleaseError(f"The {key} source changed after the report was recorded.")
    return path, source


def _host(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    # Hosts should not contain credentials; do not echo them if a malformed
    # snapshot does. Query/fragment are not needed to identify the destination.
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _section(path=None, value=None):
    return {"state": "missing" if value is None else "unverified", "recorded_status": None if value is None else value.get("status"),
            "source": None if path is None else str(path), "canonical_sha256": fingerprint(value) if value is not None else None,
            "validation": "not_checked", "messages": []}


def _selected(kind, supplied, candidates, root):
    if supplied is not None:
        try:
            path, value = _read(supplied, root)
            matches = (isinstance(value.get("mode"), str) and value["mode"] in MODES.get(kind, set()) if kind != "rollout_policy"
                       else "required_consumers" in value and "window_seconds" in value)
            if not matches:
                raise ReleaseError(f"Selected file is not a {kind} report.")
            return path, value, _section(path, value)
        except (ReleaseError, OSError, ValueError) as exc:
            result = _section(str(supplied)); result.update(state="invalid", messages=[str(exc)])
            return None, None, result
    matches = [(path, value) for path, value in candidates if
               (isinstance(value.get("mode"), str) and value["mode"] in MODES.get(kind, set()) if kind != "rollout_policy" else
                "required_consumers" in value and "window_seconds" in value and "provenance" in value)]
    if len(matches) > 1:
        result = _section(); result.update(state="ambiguous", candidates=[str(path) for path, _ in matches],
                                          messages=[f"Several {kind} artifacts exist; select the intended attempt explicitly."])
        return None, None, result
    if not matches:
        return None, None, _section()
    path, value = matches[0]
    return path, value, _section(path, value)


def _age(section, value, field, now, max_age=None):
    observed = _time(value.get(field), field)
    age = (now - observed).total_seconds()
    section.update(recorded_at_utc=observed.isoformat(), age_seconds=round(age, 3))
    if age < 0:
        raise ReleaseError("Saved evidence is dated in the future.")
    if max_age is not None:
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age <= 0:
            raise ReleaseError("Evidence age limit must be a positive finite number.")
        section.update(max_age_seconds=max_age, freshness="stale" if age > max_age else "within_recorded_limit")
        if age > max_age:
            section["state"] = "stale"
    else:
        section["freshness"] = "no_freshness_policy"


def inspect_release(manifest_path=None, *, workspace=None, stage_report=None, evaluation_report=None,
                    dependency_lock=None, promotion_report=None, remote_report=None, adoption_report=None,
                    rollout_report=None, rollout_window=None, rollout_policy=None, now=None):
    """Build a JSON-compatible local explanation. No writes, subprocesses or HTTP.

    All selected and referenced artifacts must remain inside the release folder.
    Missing artifacts are visible. Multiple attempts require explicit selection.
    """
    now = datetime.now(timezone.utc) if now is None else _time(now, "now")
    if workspace is None and manifest_path is None:
        raise ReleaseError("Choose --workspace or --manifest.")
    if workspace is None:
        manifest_path = Path(manifest_path).absolute()
    root_source = Path(workspace) if workspace is not None else Path(manifest_path).parent
    if root_source.is_symlink() or any(p.is_symlink() for p in root_source.parents):
        raise ReleaseError("Workspace must not use symlinks.")
    root = root_source.resolve()
    manifest = _bound(manifest_path if manifest_path is not None else "prompt-index.json", root)
    _, raw_snapshot = _read(manifest, root)  # strict JSON/duplicate-key checks before loader
    if type(raw_snapshot.get("format_version")) is not int or raw_snapshot["format_version"] != 1:
        raise ReleaseError("Snapshot requires format_version=1.")
    manifest, snapshot = load_snapshot(manifest)
    binding = snapshot_policy(snapshot)
    policy = binding["policy"] if binding else None
    warnings, candidates = [], []
    for directory in (root, root / "reports"):
        if not directory.exists():
            continue
        if directory.is_symlink():
            warnings.append("Skipped a symlinked reports directory.")
            continue
        for path in sorted(directory.glob("*.json")):
            if path == manifest:
                continue
            try:
                _, value = _read(path, root)
                if "mode" in value and not isinstance(value["mode"], str):
                    warnings.append(f"Cannot inspect candidate {path.name}: mode must be a string.")
                    continue
                candidates.append((path, value))
            except (ReleaseError, OSError, ValueError) as exc:
                # Auxiliary files may be arrays. They are not report candidates.
                if str(exc) != "status input must be a JSON object.":
                    warnings.append(f"Cannot inspect candidate {path.name}: {exc}")
    supplied = {"stage": stage_report, "evaluation": evaluation_report, "dependencies": dependency_lock,
                "promotion": promotion_report, "remote": remote_report, "adoption": adoption_report,
                "rollout": rollout_report, "rollout_window": rollout_window, "rollout_policy": rollout_policy}
    chosen = {kind: _selected(kind, path, candidates, root) for kind, path in supplied.items()}
    objects = {key: row[1] for key, row in chosen.items()}
    sections = {key: row[2] for key, row in chosen.items()}
    sections["remote"]["required"] = bool(policy and "remote_verification" in policy)
    stage, lock, promotion = objects["stage"], objects["dependencies"], objects["promotion"]
    result = {"format_version": 1, "mode": "release_status", "observed_at_utc": now.isoformat(),
              "scope": "local_saved_evidence_only", "remote_checked": False, "workspace": str(root),
              "manifest": str(manifest), "host": _host(snapshot.get("host")),
              "project_id": policy.get("remote_verification", {}).get("project_id") if policy else None,
              "project_identity_source": "pinned_remote_verification_policy" if policy and "remote_verification" in policy else "not_recorded",
              "label": snapshot.get("selector", {}).get("label"), "snapshot_sha256": fingerprint(snapshot),
              "policy": {"state": "pinned" if policy else "missing", "id": policy.get("id") if policy else None,
                         "sha256": binding["sha256"] if binding else None},
              "prompts": [{"name": entry["name"], "baseline_version": entry["baseline"]["version"]} for entry in snapshot["prompts"]],
              "sections": sections, "warnings": warnings,
              "limits": ["Local digests and recomputation do not authenticate the author or builder.",
                         "No current Langfuse state, signature or application traffic was fetched.",
                         "Promotion and app adoption are separate outcomes; observations cover only supplied instrumented accesses."]}

    def check(kind, action):
        if objects[kind] is None:
            return
        try:
            action(sections[kind], objects[kind])
        except (ReleaseError, OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            sections[kind].update(state="invalid", validation="failed", messages=[str(exc)])

    def stage_check(section, value):
        _binding(value, snapshot)
        if (type(value.get("format_version")) is not int or value["format_version"] != 2 or
                value.get("mode") != "stage" or value.get("release_strategy", "stage") != "stage"):
            raise ReleaseError("Expected a format_version=2 staging report.")
        if value.get("status") != "verified":
            section.update(state="incomplete", validation="identity_only", messages=["Saved staging attempt did not finish verification."])
            return
        rows = value.get("results")
        unchanged = (isinstance(rows, list) and bool(rows) and
                     all(isinstance(row, dict) and row.get("changed") is False for row in rows))
        if unchanged:
            entries = {entry["name"]: entry for entry in snapshot["prompts"]}
            seen = set()
            for row in rows:
                name = row.get("name")
                if not isinstance(name, str) or name not in entries or name in seen:
                    raise ReleaseError("Unchanged stage targets must be unique snapshot prompt names.")
                seen.add(name)
                if (type(row.get("baseline_version")) is not int or row["baseline_version"] != entries[name]["baseline"]["version"] or
                        row.get("status") != "unchanged" or entries[name]["local_content"] != entries[name]["baseline"]["prompt"]):
                    raise ReleaseError("Unchanged stage outcome disagrees with the snapshot baseline and local content.")
            targets = {}
        else:
            targets = _stage_targets(snapshot, value)
        selected = {row["name"] for row in value["results"]}
        # The release command supports --name subsets of a larger snapshot.
        # Its result rows define the staged selection, not every exported prompt.
        section.update(state="recorded_unchanged" if unchanged else "recorded_verified", validation="local_bindings_checked", targets=targets,
                       selected_prompts=sorted(selected),
                       unselected_snapshot_prompts=sorted({row["name"] for row in snapshot["prompts"]} - selected))
    check("stage", stage_check)

    def dependencies_check(section, value):
        validate_lock_integrity(value)
        _binding(value, snapshot)
        if stage is None or value.get("stage_report_sha256") != fingerprint(stage):
            raise ReleaseError("Dependency lock's stage artifact is absent or different.")
        if sections["stage"]["state"] != "recorded_verified":
            raise ReleaseError("Dependency lock needs a valid selected stage.")
        if {r["name"]: r["version"] for r in value["roots"]} != sections["stage"]["targets"]:
            raise ReleaseError("Dependency roots differ from staged targets.")
        section.update(state="recorded_locked", validation="local_bindings_checked", roots=len(value["roots"]),
                       consumer=value.get("consumer"), current_remote_drift="not_checked")
        _age(section, value, "recorded_at_utc", now)
    check("dependencies", dependencies_check)

    loaded_eval = {}
    def evaluation_check(section, value):
        _seal(value); _binding(value, snapshot, stage)
        expected_version = 2 if value.get("mode") == "langfuse_experiment_evaluation" else 1
        if type(value.get("format_version")) is not int or value["format_version"] != expected_version:
            raise ReleaseError("Unsupported evaluation report version.")
        if stage is None or sections["stage"]["state"] != "recorded_verified":
            raise ReleaseError("Evaluation needs a valid selected stage.")
        _, source = _reference(value, "source_results", root)
        loaded_eval["source"] = source
        if value.get("mode") == "langfuse_experiment_evaluation":
            from import_langfuse_experiment import _derive
            _, capture = _reference(value, "execution_capture", root)
            loaded_eval["capture"] = capture
            receipt = None
            if "remote_verification" in value:
                _, receipt = _reference(value, "remote_verification", root)
                if objects["remote"] is None or fingerprint(objects["remote"]) != fingerprint(receipt):
                    raise ReleaseError("Select the remote receipt bound to this evaluation.")
            if "dependency_lock" in capture:
                if lock is None or sections["dependencies"]["state"] != "recorded_locked":
                    raise ReleaseError("Evaluation needs its valid dependency lock.")
            expected = _derive(snapshot, stage, source, capture, dependency_lock=lock, remote_receipt=receipt,
                               verification_time=_time(value.get("recorded_at_utc"), "evaluation time").isoformat())
            for field, actual in expected.items():
                if fingerprint(value.get(field)) != fingerprint(actual):
                    raise ReleaseError(f"Evaluation {field} disagrees with source evidence and pinned policy.")
        else:
            if policy is not None:
                raise ReleaseError("Pinned experiment policy cannot use legacy application evidence.")
            checks = _required_check_ids(value.get("required_checks"))
            rows = _parse_results(source, snapshot, stage, checks)
            if value.get("results") != rows or value.get("status") != _status(rows):
                raise ReleaseError("Evaluation does not match its original application results.")
        section.update(state="passed" if value["status"] == "passed" else "failed",
                       validation="historical_results_recomputed", required_checks=value["required_checks"],
                       failed_checks=[{"name": row["name"], "check": check["id"]} for row in value["results"]
                                      for check in row["checks"] if not check["passed"]])
        _age(section, value, "recorded_at_utc", now)
    check("evaluation", evaluation_check)

    def remote_check(section, value):
        _seal(value)
        if value.get("status") not in {"confirmed", "pending", "failed"}:
            raise ReleaseError("Unknown remote persistence outcome.")
        if not policy or "remote_verification" not in policy:
            raise ReleaseError("No pinned remote-verification policy binds this receipt.")
        rp = policy["remote_verification"]
        if value.get("host") != snapshot.get("host") or value.get("project_id") != rp["project_id"] or value.get("policy_sha256") != fingerprint(policy):
            raise ReleaseError("Remote receipt belongs to a different host, project or policy.")
        section.update(state="reported_" + str(value.get("status")), validation="local_bindings_checked",
                       project_id=rp["project_id"], cases=len(value.get("observed_cases", [])), issues=value.get("issues", []))
        _age(section, value, "checked_at_utc", now, rp["max_age_seconds"])
        if value.get("status") != "confirmed":
            return
        if set(loaded_eval) != {"source", "capture"}:
            section.update(state="unverified", validation="incomplete_sources",
                           messages=["Need the selected evaluation's original results and capture to reconstruct remote expectations."])
            return
        from langfuse_release_remote import make_remote_expectation, validate_remote_receipt
        expected = make_remote_expectation(loaded_eval["source"], loaded_eval["capture"], policy,
                                          host=snapshot["host"], project_id=rp["project_id"], dataset_kind=rp["dataset_kind"])
        # Reconstruct what the saved readback proved then; freshness is reported
        # separately against the current inspection time, without contacting it.
        validate_remote_receipt(value, expected, max_age_seconds=rp["max_age_seconds"], now=value["checked_at_utc"])
        section.update(validation="historical_receipt_recomputed")
        if section["state"] != "stale":
            section["state"] = "recorded_confirmed"
    check("remote", remote_check)

    def promotion_check(section, value):
        if type(value.get("format_version")) is not int or value["format_version"] != 2:
            raise ReleaseError("Unsupported promotion report version.")
        evaluation_meta = value.get("evaluation", {})
        if not isinstance(evaluation_meta, dict):
            raise ReleaseError("Promotion evaluation metadata must be an object.")
        if not isinstance(value.get("dependency_lock", {}), dict):
            raise ReleaseError("Promotion dependency-lock metadata must be an object.")
        _binding(value, snapshot, stage)
        if stage is None or sections["stage"]["state"] != "recorded_verified":
            raise ReleaseError("Promotion needs a valid selected stage.")
        if not isinstance(value.get("label"), str) or value["label"] != result["label"]:
            raise ReleaseError("Promotion label differs from the snapshot selector.")
        section["label"] = value["label"]
        rows = value.get("results")
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ReleaseError("Promotion has no target outcomes.")
        targets = sections["stage"]["targets"]
        if len(rows) != len(targets) or {r["name"]: r["target_version"] for r in rows} != targets:
            raise ReleaseError("Promotion targets differ from staged versions.")
        section["targets"] = [{"name": r["name"], "version": r["target_version"], "recorded_status": r["status"]} for r in rows]
        if value.get("apply") is not True:
            section.update(state="preview_only", validation="local_bindings_checked")
            return
        if value.get("status") != "verified" or any(r.get("status") not in {"verified", "already_promoted"} for r in rows):
            unknown = any("unknown" in str(r.get("status")) for r in rows)
            section.update(state="uncertain_do_not_retry" if unknown else "incomplete", validation="local_bindings_checked",
                           messages=["Inspect/reconcile the existing attempt before considering another write."])
            return
        if policy or evaluation_meta.get("required"):
            evaluation = objects["evaluation"]
            if evaluation is None or sections["evaluation"]["state"] != "passed":
                raise ReleaseError("Completed promotion's passing evaluation is absent or invalid.")
            if evaluation_meta.get("sha256") != fingerprint(evaluation):
                raise ReleaseError("Promotion references a different evaluation receipt.")
        if value.get("dependency_lock_sha256") is not None:
            if lock is None or sections["dependencies"]["state"] != "recorded_locked" or value["dependency_lock_sha256"] != fingerprint(lock):
                raise ReleaseError("Promotion references a different or invalid dependency lock.")
        section.update(state="recorded_promoted", validation="local_bindings_checked", current_label="not_checked")
        _age(section, value, "completed_at_utc", now)
    check("promotion", promotion_check)

    def adoption_check(section, value):
        _seal(value)
        if promotion is None or lock is None or sections["promotion"]["state"] != "recorded_promoted" or sections["dependencies"]["state"] != "recorded_locked":
            raise ReleaseError("Adoption needs the valid bound promotion and dependency lock.")
        from langfuse_release_adoption import build_adoption_receipt
        recorded = _time(value.get("recorded_at_utc"), "adoption time")
        original = build_adoption_receipt(value.get("observations"), promotion, lock, required_consumers=value.get("required_consumers"),
                                          max_age_seconds=value.get("max_age_seconds"), now=recorded)
        if fingerprint(original) != fingerprint(value):
            raise ReleaseError("Adoption report differs from recomputed observations and release bindings.")
        fresh = build_adoption_receipt(value["observations"], promotion, lock, required_consumers=value["required_consumers"],
                                      max_age_seconds=value["max_age_seconds"], now=now)
        section.update(state="observed_adopted" if fresh["status"] == "verified" else "not_adopted",
                       validation="observations_recomputed", scope=value["scope"],
                       required_consumers=value["required_consumers"], observation_count=len(value["observations"]),
                       issues=fresh["issues"])
        _age(section, value, "recorded_at_utc", now, value["max_age_seconds"])
    check("adoption", adoption_check)

    def rollout_check(section, value):
        _seal(value)
        provenance = value.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ReleaseError("Rollout provenance metadata must be an object.")
        window, rollout_rules = objects["rollout_window"], objects["rollout_policy"]
        if promotion is None or lock is None or sections["promotion"]["state"] != "recorded_promoted" or sections["dependencies"]["state"] != "recorded_locked":
            raise ReleaseError("Rollout needs the valid bound promotion and dependency lock.")
        if value.get("promotion_sha256") != fingerprint(promotion) or value.get("dependency_lock_sha256") != fingerprint(lock):
            raise ReleaseError("Rollout belongs to a different release.")
        section.update(counts=value.get("counts"), consumers=value.get("consumers"), scope=value.get("scope"))
        _age(section, value, "recorded_at_utc", now)
        if window is None or rollout_rules is None:
            section.update(state="unverified", validation="incomplete_sources", messages=["Select the original rollout window and policy to recompute coverage."])
            return
        from langfuse_release_rollout import build_rollout_report, validate_rollout_policy
        rollout_rules = validate_rollout_policy(rollout_rules)
        if provenance.get("classification") != "unsigned_local" or rollout_rules["provenance"]["required"]:
            section.update(state="unverified", validation="signature_not_checked", messages=["This local status command does not run the external provenance verifier. Use the rollout verification command with its trust policy."])
            return
        original = build_rollout_report(value.get("observations"), window, rollout_rules, promotion, lock, now=value["recorded_at_utc"])
        if fingerprint(original) != fingerprint(value):
            raise ReleaseError("Rollout report differs from its samples, window or policy.")
        sections["rollout_window"].update(state="bound", validation="local_bindings_checked")
        sections["rollout_policy"].update(state="validated", validation="local_bindings_checked")
        fresh = build_rollout_report(value["observations"], window, rollout_rules, promotion, lock, now=now)
        section.update(state=fresh["status"], validation="coverage_recomputed", counts=fresh["counts"],
                       consumers=fresh["consumers"], issues=fresh["issues"], pending_requirements=fresh["pending_requirements"],
                       window={"start_at_utc": window["start_at_utc"], "end_at_utc": window["end_at_utc"]})
        if value["status"] == "complete" and fresh["status"] != "complete":
            section["state"] = "stale"
    check("rollout", rollout_check)

    result["next_steps"] = _next_steps(sections, warnings, required_remote=sections["remote"]["required"])
    result["status"] = "attention" if result["next_steps"] else "inspected"
    return result


def _next_steps(sections, warnings, *, required_remote=False):
    actions = []
    for kind in sections:
        section = sections[kind]
        if section["state"] in {"invalid", "ambiguous"}:
            actions.append({"section": kind, "action": "inspect_evidence", "message": "; ".join(section["messages"])})
    if warnings:
        actions.append({"section": "inputs", "action": "inspect_unreadable_files", "message": "Some candidate files could not be inspected; review the warnings."})
    if actions:
        if required_remote and sections["remote"]["state"] == "missing":
            actions.append({"section": "remote", "action": "refresh_remote_verification", "message": "The pinned policy requires remote readback. Select or regenerate the receipt from the original execution before promotion."})
        return actions
    priorities = [
        ("promotion", {"uncertain_do_not_retry", "incomplete"}, "reconcile_existing_attempt", "Inspect the interrupted promotion; do not retry a write based on this local summary."),
        ("stage", {"recorded_unchanged"}, "no_changes_to_promote", "The selected prompts were unchanged, so this stage created no candidates. Stop here or review a new edit; no promotion is needed."),
        ("stage", {"missing", "incomplete"}, "review_staging", "Review the candidate and its staging attempt before evaluation or promotion."),
        ("evaluation", {"failed"}, "repair_and_rerun_evaluation", "Fix the failed checks and capture new evidence; keep the reviewed thresholds."),
        ("evaluation", {"missing", "unverified"}, "run_required_evaluation", "Run the selected application checks and import their original results."),
        ("remote", {"stale", "reported_pending", "reported_failed", "unverified"} | ({"missing"} if required_remote else set()), "refresh_remote_verification", "Run the separate remote readback verifier before relying on remote persistence again."),
        ("promotion", {"missing", "preview_only"}, "review_promotion", "Review the promotion preview and current server state; this inspection grants no write approval."),
        ("adoption", {"missing", "not_adopted", "stale", "unverified"}, "inspect_application_observations", "Collect or inspect the actual consuming-app accesses; a changed label alone does not prove adoption."),
    ]
    for key, states, action, message in priorities:
        if sections[key]["state"] in states:
            actions.append({"section": key, "action": action, "message": message})
            break
    if not actions and sections["rollout"]["state"] in {"failed", "collecting", "stale", "unverified"}:
        actions.append({"section": "rollout", "action": "review_rollout_coverage", "message": "Inspect the window, consumer coverage and reported issues before declaring rollout complete."})
    return actions


def _line(value):
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else f"\\u{ord(char):04x}" for char in str(value))


def render_text(report):
    lines = ["Release status (saved local evidence; no server check)",
             f"Workspace: {_line(report['workspace'])}", f"Host: {_line(report['host'])}",
             f"Project: {_line(report['project_id'] or 'not recorded')} ({report['project_identity_source']})",
             f"Label: {_line(report['label'] or 'not selected')}",
             f"Policy: {_line(report['policy']['id'] or 'not pinned')}"]
    for key in DISPLAY:
        section = report["sections"][key]
        line = f"{key.title()}: {section['state']}"
        if section.get("required"):
            line += " (required by pinned policy)"
        if section["recorded_status"] is not None:
            line += f" (recorded: {_line(section['recorded_status'])})"
        if "age_seconds" in section:
            line += f"; age {section['age_seconds']:g}s"
        lines.append(line)
        if section["source"]:
            lines.append(f"  Source: {_line(section['source'])}")
        for message in section["messages"]:
            lines.append("  " + _line(message))
        if key == "stage" and section.get("unselected_snapshot_prompts"):
            lines.append("  Outside this stage selection: " + ", ".join(_line(name) for name in section["unselected_snapshot_prompts"]))
        if key == "adoption" and "observation_count" in section:
            lines.append(f"  {section['observation_count']} instrumented observations; {len(section['required_consumers'])} required consumers")
        if key == "rollout" and isinstance(section.get("counts"), dict):
            counts = section["counts"]
            lines.append(f"  Consumer/prompt coverage: {counts.get('observed_consumer_prompt_pairs', '?')}/{counts.get('expected_consumer_prompt_pairs', '?')}")
    for warning in report["warnings"]:
        lines.append("Warning: " + _line(warning))
    for action in report["next_steps"]:
        lines.append("Next: " + _line(action["message"]))
    lines.append("Saved label movement and observed application use are separate. This is not a release approval or signature verification.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Release directory containing prompt-index.json and reports/.")
    parser.add_argument("--manifest", help="Snapshot path; defaults to WORKSPACE/prompt-index.json.")
    for flag in ("stage-report", "evaluation-report", "dependency-lock", "promotion-report", "remote-report", "adoption-report", "rollout-report", "rollout-window", "rollout-policy"):
        parser.add_argument("--" + flag, help="Select one artifact inside the release workspace; relative paths are workspace-relative.")
    parser.add_argument("--json", action="store_true", help="Print stable JSON to stdout instead of the terminal summary.")
    parser.add_argument("--format", choices=("text", "json"), help="Output format; default text. --json is an alias for --format json.")
    args = parser.parse_args(argv)
    if args.json and args.format == "text":
        parser.error("--json conflicts with --format text.")
    try:
        result = inspect_release(args.manifest, workspace=args.workspace, **{key: value for key, value in vars(args).items() if key not in {"workspace", "manifest", "json", "format"}})
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json or args.format == "json" else render_text(result))
        if any(s["state"] in {"invalid", "ambiguous"} for s in result["sections"].values()) or result["warnings"]:
            return 1
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Status inspection stopped: {_line(exc)}\n")


if __name__ == "__main__":
    raise SystemExit(main())
