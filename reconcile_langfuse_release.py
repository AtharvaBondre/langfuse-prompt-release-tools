#!/usr/bin/env python3
"""Inspect an interrupted prompt release without retrying any write."""
import argparse
import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langfuse_release_common import (Client, ReleaseError, fingerprint, inside,
                                     make_payload, validate_prompt, verify_exact)
from publish_langfuse_prompts import load_snapshot, observed_state, write_report


class ReadOnlyClient(Client):
    """Restrict even inherited client methods to GET before a request is opened."""
    def _request(self, method, path, query=None, payload=None):
        if method != "GET" or payload is not None:
            raise ReleaseError("Reconciliation permits GET requests only.")
        return super()._request(method, path, query, payload)

    def list_versions(self, name, max_pages=100):
        # The name filter is deliberately not combined with label/version filters:
        # those filters apply before version aggregation in the official API.
        if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
            raise ReleaseError("A nonempty prompt name is required.")
        if type(max_pages) is not int or max_pages < 1:
            raise ReleaseError("max_pages must be a positive integer.")
        totals, found, seen = None, None, set()
        for page in range(1, max_pages + 1):
            response = self._request("GET", "/api/public/v2/prompts",
                                     {"name": name, "page": page, "limit": 100})
            if (not isinstance(response, dict) or not isinstance(response.get("data"), list)
                    or not isinstance(response.get("meta"), dict)):
                raise ReleaseError("Prompt listing is missing data or pagination metadata.")
            meta, rows = response["meta"], response["data"]
            if any(type(meta.get(key)) is not int for key in ("page", "limit", "totalPages", "totalItems")):
                raise ReleaseError("Prompt listing pagination must contain integers.")
            pages, count = meta["totalPages"], meta["totalItems"]
            if (meta["page"] != page or meta["limit"] != 100 or count < 0
                    or pages != (count + 99) // 100 or pages > max_pages
                    or len(rows) != min(100, max(0, count - (page - 1) * 100))):
                raise ReleaseError("Prompt listing is incomplete or exceeds max_pages.")
            if totals is not None and totals != (pages, count):
                raise ReleaseError("Prompt listing changed during pagination.")
            totals = pages, count
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str) or row["name"] in seen:
                    raise ReleaseError("Prompt listing contains an invalid or repeated name.")
                seen.add(row["name"])
                if row["name"] != name:
                    continue
                versions = row.get("versions")
                if (not isinstance(versions, list) or not versions
                        or any(type(v) is not int or v < 1 for v in versions)
                        or len(set(versions)) != len(versions)):
                    raise ReleaseError("Prompt listing contains invalid version identifiers.")
                found = sorted(versions)
            if page >= pages:
                return found or []
        raise ReleaseError("Prompt listing reached max_pages without completion.")


def _read_source(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ReleaseError("Release report must not be a symlink or use a symlink directory.")
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
        fingerprint(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError("Release report is not valid finite JSON.") from None
    return path.resolve(), data, hashlib.sha256(raw).hexdigest()


def _plans(snapshot, source):
    """Reject inconsistent local evidence before allowing any remote inspection."""
    if (not isinstance(source, dict) or source.get("format_version") != 2
            or source.get("mode") not in {"stage", "apply", "dry_run", "promote"}
            or source.get("host") != snapshot.get("host")
            or source.get("snapshot_sha256") != fingerprint(snapshot)
            or source.get("status") not in {"preflight", "preflight_passed", "verified", "dry_run_complete", "stopped"}
            or not isinstance(source.get("results"), list)):
        raise ReleaseError("Release report does not bind this snapshot or has an unsupported format.")
    label = snapshot["selector"].get("label")
    if not isinstance(label, str) or not label:
        raise ReleaseError("Reconciliation requires a label-based release snapshot.")
    entries = {entry["name"]: entry for entry in snapshot["prompts"]}
    seen, plans = set(), []
    promote = source["mode"] == "promote"
    if promote:
        label = source.get("label")
        if (not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_.-]{1,36}", label)
                or label == "latest" or type(source.get("apply")) is not bool):
            raise ReleaseError("Promotion report must have an explicit deployment label and apply boolean.")
        strategy = "promote"
    else:
        strategy = source.get("release_strategy")
        if (strategy not in {"stage", "direct"}
                or (source["mode"] == "stage" and strategy != "stage")
                or (source["mode"] == "apply" and strategy != "direct")):
            raise ReleaseError("Release report has an inconsistent release strategy.")
    for row in source["results"]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ReleaseError("Release report contains an invalid result.")
        name = row["name"]
        if name not in entries or name in seen:
            raise ReleaseError("Release report contains an unknown or duplicate prompt.")
        seen.add(name)
        entry = entries[name]
        baseline = entry["baseline"]
        if type(row.get("baseline_version")) is not int or row["baseline_version"] != baseline["version"]:
            raise ReleaseError("Release report baseline does not match the snapshot.")
        changed = entry["local_content"] != baseline["prompt"]
        payload = make_payload(baseline, entry["local_content"])
        if strategy in {"stage", "promote"}:
            payload["labels"] = []
        if promote:
            allowed = {"pending_preflight", "already_promoted", "pending_promotion", "submitting",
                       "patch_returned_pending_verification", "verified", "promotion_outcome_unknown_do_not_retry",
                       "patch_returned_but_verification_failed", "promotion_failed", "stopped_before_patch"}
            target = row.get("target_version")
            if not changed or label not in baseline["labels"]:
                raise ReleaseError("Promotion result must refer to a changed prompt with the deployment label.")
        else:
            allowed = {"planned", "unchanged", "submitting", "verified", "created_pending_verification",
                       "created_but_verification_failed", "write_outcome_unknown_do_not_retry", "stopped"}
            if type(row.get("changed")) is not bool or row["changed"] != changed:
                raise ReleaseError("Release report changed flag does not match the snapshot.")
            if row.get("labels_to_move") != (payload["labels"] if changed else []):
                raise ReleaseError("Release report intended labels do not match the snapshot.")
            target = row.get("created_version")
            if not changed and (row.get("status") != "unchanged" or target is not None):
                raise ReleaseError("Unchanged release result contains write evidence.")
        if row.get("status") not in allowed:
            raise ReleaseError("Release report contains an unsupported result status.")
        if target is not None and (type(target) is not int or target <= baseline["version"]):
            raise ReleaseError("Release report contains an invalid target version.")
        if (promote or (changed and row["status"] in {"verified", "created_pending_verification",
                                                      "created_but_verification_failed"})) and target is None:
            raise ReleaseError("Release report is missing its acknowledged target version.")
        plans.append({"name": name, "baseline": baseline, "payload": payload, "source": row,
                      "target": target, "changed": changed, "strategy": strategy, "label": label})
    # Publication persists all selected plans before the first write. Only an
    # interrupted preflight leaves selection scope uncertain. A completed plan
    # may legitimately omit snapshot prompts excluded by --name.
    incomplete_preflight = not promote and (source["status"] == "preflight" or
                                           (source["status"] == "stopped" and source.get("phase") == "preflight"))
    if incomplete_preflight:
        for name in sorted(entries.keys() - seen):
            plans.append({"name": name, "unrecorded": True})
    elif not promote and not plans:
        raise ReleaseError("Release report has no selected results after preflight.")
    return plans


def _metadata(value):
    return {"version": value["version"], "labels": sorted(value["labels"]),
            "tags": sorted(value["tags"]), "state_sha256": fingerprint(observed_state(value))}


def _observe(client, name, version, labels):
    exact = validate_prompt(client.get(name, version=version), name, version)
    selected = {}
    for label in sorted(set(labels)):
        value = validate_prompt(client.get(name, label=label), name)
        if label not in value["labels"]:
            raise ReleaseError("Label observation does not carry the requested label.")
        selected[label] = value
    return exact, selected


def _stable_observation(client, name, version, labels):
    before = _observe(client, name, version, labels)
    after = _observe(client, name, version, labels)
    def states(observation):
        exact, selected = observation
        return {"exact": observed_state(exact),
                "labels": {label: observed_state(value) for label, value in selected.items()}}
    if fingerprint(states(before)) != fingerprint(states(after)):
        raise ReleaseError("Remote observations changed during reconciliation; inspect again before deciding.")
    return after


def _matches_content(actual, payload):
    expected = copy.deepcopy(payload)
    # Labels and tags are mutable metadata, not proof of who created a version.
    expected["labels"], expected["tags"] = actual["labels"], actual["tags"]
    try:
        verify_exact(actual, expected, actual["version"])
        return True
    except ReleaseError:
        return False


def _known(client, plan):
    baseline, payload, strategy = plan["baseline"], plan["payload"], plan["strategy"]
    target = plan["target"]
    labels = [plan["label"]] if strategy == "promote" else [v for v in baseline["labels"] if v != "latest"]
    actual, selected = _stable_observation(client, plan["name"], target, labels)
    observations = {"target": _metadata(actual), "labels": {k: _metadata(v) for k, v in selected.items()}}
    intended = copy.deepcopy(payload)
    if strategy == "promote":
        intended["labels"] = [plan["label"]]
    conflicts = []
    try:
        verify_exact(actual, intended, target)
    except ReleaseError as exc:
        conflicts.append(str(exc))
    expected_label_version = baseline["version"] if strategy == "stage" else target
    for label, value in selected.items():
        if value["version"] != expected_label_version:
            conflicts.append(f"Label {label} selects version {value['version']}, expected {expected_label_version}.")
        else:
            label_payload = copy.deepcopy(intended)
            if strategy == "stage":
                label_payload = make_payload(baseline, baseline["prompt"])
                label_payload["config"] = baseline["config"]
            try:
                verify_exact(value, label_payload, expected_label_version)
            except ReleaseError as exc:
                conflicts.append(f"Label {label} content differs from the intended state: {exc}")
    if strategy == "promote" and not conflicts:
        return "completed", "The intended target and deployment label agree in two reads; this does not identify the actor who moved the label.", observations
    if strategy == "promote" and selected[plan["label"]]["version"] == baseline["version"]:
        # A known PATCH target makes a baseline label distinguishable from an
        # unknown POST. It is pending now, even if it was moved and later reverted.
        unpromoted = copy.deepcopy(payload)
        try:
            verify_exact(actual, unpromoted, target)
            base_payload = make_payload(baseline, baseline["prompt"])
            base_payload["config"] = baseline["config"]
            verify_exact(selected[plan["label"]], base_payload, baseline["version"])
            return "pending", "The staged target matches and the deployment label still selects the baseline. No retry was made.", observations
        except ReleaseError:
            pass
    if conflicts:
        return "conflicting", " ".join(conflicts), observations
    return "completed", "The acknowledged version and expected deployment-label state match in two reads.", observations


def _pending(client, plan):
    baseline = plan["baseline"]
    labels = [v for v in baseline["labels"] if v != "latest"]
    actual, selected = _stable_observation(client, plan["name"], baseline["version"], labels)
    observations = {"baseline": _metadata(actual), "labels": {k: _metadata(v) for k, v in selected.items()}}
    intended = make_payload(baseline, baseline["prompt"])
    intended["config"] = baseline["config"]  # No create normalization took place.
    try:
        verify_exact(actual, intended, baseline["version"])
        for value in selected.values():
            if value["version"] != baseline["version"]:
                raise ReleaseError("A deployment label moved away from the recorded baseline.")
            verify_exact(value, intended, baseline["version"])
    except ReleaseError as exc:
        return "conflicting", str(exc), observations
    if not plan["changed"]:
        return "completed", "No content change was required and the recorded baseline still matches.", observations
    return "pending", "The report records no submitted create for this item and the baseline still matches. This is not permission to retry another item.", observations


def _unknown_create(client, plan, max_versions):
    versions = client.list_versions(plan["name"])
    if (not isinstance(versions, list) or any(type(v) is not int or v < 1 for v in versions)
            or len(set(versions)) != len(versions)):
        raise ReleaseError("Version listing is invalid.")
    newer = sorted((v for v in versions if v > plan["baseline"]["version"]), reverse=True)
    candidates, inspected = [], []
    for version in newer[:max_versions]:
        actual = validate_prompt(client.get(plan["name"], version=version), plan["name"], version)
        inspected.append(version)
        if _matches_content(actual, plan["payload"]):
            candidate = _metadata(actual)
            candidate["labels_match_intent"] = set(actual["labels"]) - {"latest"} == set(plan["payload"]["labels"])
            candidate["tags_match_intent"] = set(actual["tags"]) == set(plan["payload"]["tags"])
            candidates.append(candidate)
    stable = sorted(client.list_versions(plan["name"])) == sorted(versions)
    observations = {"candidate_versions": candidates, "inspected_versions": inspected,
                    "candidate_scan_complete": len(newer) <= max_versions and stable,
                    "version_listing_stable": stable,
                    "candidate_identity_proven": False}
    return "unknown", ("No acknowledged created version was saved. Matching content/config is only a candidate: "
                       "another caller may have created it. Absence of a candidate does not prove the write failed. Do not retry this POST automatically."), observations


def reconcile(client, manifest_path, release_report_path, *, report_name=None, max_versions=100):
    """Write separate observations; never submit/resume/roll back a release.

    Client-like objects must implement get(name, version=.../label=...) and
    list_versions(name). Production CLI uses ReadOnlyClient's enforced GET fence.
    """
    if type(max_versions) is not int or max_versions < 1 or max_versions > 10000:
        raise ReleaseError("max_versions must be an integer between 1 and 10000.")
    manifest_path, snapshot = load_snapshot(manifest_path)
    source_path, source, source_sha = _read_source(release_report_path)
    if snapshot.get("host") != client.host:
        raise ReleaseError("Snapshot host does not match the requested target.")
    plans = _plans(snapshot, source)
    if report_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_name = f"reconcile-{stamp}-{uuid.uuid4().hex[:8]}.json"
    report_path = inside(manifest_path.parent / "reports", report_name)
    protected = {manifest_path, source_path}
    protected.update(inside(manifest_path.parent, entry["file"]) for entry in snapshot["prompts"])
    if report_path.suffix.lower() != ".json" or report_path in protected:
        raise ReleaseError("Choose a new .json reconciliation report within the reports directory.")
    report = {"format_version": 1, "mode": "reconcile", "read_only": True,
              "host": client.host, "status": "inspecting", "report_file": report_name,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "snapshot_sha256": fingerprint(snapshot),
              "release_report": {"file": str(source_path), "sha256": source_sha,
                                 "mode": source["mode"], "status": source["status"]},
              "max_versions": max_versions, "results": [],
              "limits": ["Observations are not an atomic snapshot or proof of the original writer.",
                         "No automatic resume, retry, rollback or application-adoption claim.",
                         "Unsigned local reports are evidence bindings, not authenticated attestations."]}
    write_report(report_path, report, create=True)
    for plan in plans:
        result = {"name": plan["name"], "classification": "unknown"}
        if plan.get("unrecorded"):
            result["reason"] = "No result was recorded for this snapshot prompt; it may have been outside the selected release scope. No request was inferred."
        else:
            row = plan["source"]
            result.update({"original_status": row["status"], "baseline_version": plan["baseline"]["version"],
                           "acknowledged_target_version": plan["target"], "strategy": plan["strategy"]})
            try:
                if plan["target"] is not None:
                    classification, reason, observations = _known(client, plan)
                elif not plan["changed"] or row["status"] == "planned":
                    classification, reason, observations = _pending(client, plan)
                else:
                    classification, reason, observations = _unknown_create(client, plan, max_versions)
                result.update(classification=classification, reason=reason, observations=observations)
            except (ReleaseError, OSError, ValueError) as exc:
                result["reason"] = f"Remote inspection could not establish the outcome: {exc}"
        report["results"].append(result)
        write_report(report_path, report)
    try:
        _, _, final_source_sha = _read_source(source_path)
        _, final_snapshot = load_snapshot(manifest_path)
        unchanged = final_source_sha == source_sha and fingerprint(final_snapshot) == report["snapshot_sha256"]
    except (ReleaseError, OSError, ValueError):
        unchanged = False
    if not unchanged:
        for result in report["results"]:
            result["classification"] = "unknown"
            result["reason"] = "Local source evidence changed or became unavailable during inspection; use a preserved matching snapshot and report."
    report["source_evidence_unchanged"] = unchanged
    report["counts"] = {key: sum(row["classification"] == key for row in report["results"])
                        for key in ("completed", "pending", "conflicting", "unknown")}
    report["status"] = "completed" if report["counts"]["completed"] == len(report["results"]) else "needs_review"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--release-report", required=True)
    parser.add_argument("--report", help="New private report filename within the snapshot reports directory.")
    parser.add_argument("--max-versions", type=int, default=100,
                        help="Maximum newer versions inspected per unknown POST (default: 100).")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        result = reconcile(ReadOnlyClient(args.host, args.timeout), args.manifest,
                           args.release_report, report_name=args.report, max_versions=args.max_versions)
        print("Reconciliation: " + ", ".join(f"{count} {name}" for name, count in result["counts"].items()))
        print(f"Report: {Path(args.manifest).resolve().parent / 'reports' / result['report_file']}")
        if result["status"] != "completed":
            parser.exit(2, "Reconciliation needs review; no release writes were sent.\n")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Reconciliation stopped: {exc}\n")


if __name__ == "__main__":
    main()
