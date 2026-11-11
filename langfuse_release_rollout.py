#!/usr/bin/env python3
"""Read-only rollout evidence over a predeclared window of SDK accesses.

Counts describe supplied instrumented samples, never all application traffic.
The reviewed policy and window are separate from the observed results.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from langfuse_release_adoption import _now, _promotion_targets, _seconds, _utc
from langfuse_release_common import ReleaseError, _label, _name, fingerprint, inside
from langfuse_release_policy import digest, object_fields, read_object, text
from publish_langfuse_prompts import write_report


CAPTURE_METHODS = {
    "langfuse-python": "langfuse_release_adoption.capture_sdk_observation/v1",
    "langfuse-js": "langfuse_release_adoption.capture_js_sdk_observation/v1",
}
POLICY_FIELDS = ("format_version", "id", "required_consumers", "window_seconds", "bucket_seconds",
                 "min_unique_requests_per_target", "min_requests_per_bucket", "min_covered_buckets",
                 "max_observation_age_seconds", "max_latest_observation_age_seconds", "max_capture_delay_seconds",
                 "max_fallback_rate", "max_mismatch_rate", "provenance")


def _positive_integer(value, field):
    if type(value) is not int or value <= 0:
        raise ReleaseError(f"{field} must be a positive integer.")
    return value


def _version(value):
    if type(value) is not int or value != 1:
        raise ReleaseError("Expected format_version=1.")


def _sealed(value):
    result = copy.deepcopy(value)
    result["evidence_sha256"] = fingerprint(result)
    return result


def _integrity(value, mode):
    if not isinstance(value, dict) or value.get("mode") != mode:
        raise ReleaseError(f"Expected {mode} evidence.")
    _version(value.get("format_version"))
    body = {key: item for key, item in value.items() if key != "evidence_sha256"}
    if value.get("evidence_sha256") != fingerprint(body):
        raise ReleaseError(f"{mode} evidence changed since capture.")


def validate_rollout_policy(policy):
    object_fields(policy, POLICY_FIELDS, where="rollout policy")
    _version(policy["format_version"])
    text(policy["id"], "policy.id")
    consumers = policy["required_consumers"]
    if not isinstance(consumers, dict) or not consumers:
        raise ReleaseError("Rollout policy requires explicit consumer/deployment/SDK identities.")
    for consumer_id, consumer in consumers.items():
        text(consumer_id, "consumer ID")
        object_fields(consumer, ("deployment_id", "sdk"), where="required consumer")
        text(consumer["deployment_id"], "consumer deployment_id")
        object_fields(consumer["sdk"], ("name", "version"), where="required SDK")
        if not isinstance(consumer["sdk"]["name"], str) or consumer["sdk"]["name"] not in CAPTURE_METHODS:
            raise ReleaseError("Rollout supports explicitly captured official Python or JS SDK observations.")
        text(consumer["sdk"]["version"], "SDK version")
    for field in ("window_seconds", "bucket_seconds", "min_unique_requests_per_target", "min_requests_per_bucket", "min_covered_buckets"):
        _positive_integer(policy[field], field)
    if policy["window_seconds"] % policy["bucket_seconds"]:
        raise ReleaseError("Rollout window must divide into complete equal time buckets.")
    if policy["min_covered_buckets"] > policy["window_seconds"] // policy["bucket_seconds"]:
        raise ReleaseError("Requested time-bucket coverage exceeds the window.")
    for field in ("max_observation_age_seconds", "max_latest_observation_age_seconds", "max_capture_delay_seconds"):
        _seconds(policy[field], field, positive=True)
    fallback_rate = _seconds(policy["max_fallback_rate"], "max_fallback_rate")
    if fallback_rate > 1:
        raise ReleaseError("Fallback rate must be between zero and one.")
    if type(policy["max_mismatch_rate"]) not in (int, float) or policy["max_mismatch_rate"] != 0:
        raise ReleaseError("Mixed old/new versions or content are never complete; max_mismatch_rate must be zero.")
    provenance = object_fields(policy["provenance"], ("required", "trust_policy_sha256"), where="provenance policy")
    if type(provenance["required"]) is not bool:
        raise ReleaseError("provenance.required must be a boolean.")
    if provenance["trust_policy_sha256"] is not None:
        digest(provenance["trust_policy_sha256"], "provenance trust policy digest")
    if provenance["required"] and provenance["trust_policy_sha256"] is None:
        raise ReleaseError("Required provenance must pin its independent trust-policy digest.")
    fingerprint(policy)
    return copy.deepcopy(policy)


def _targets(roots):
    return [{"name": name, "version": root["version"], "resolved_sha256": root["resolved_sha256"],
             "type": next(node["type"] for node in root["nodes"] if node["name"] == name and node["version"] == root["version"])}
            for name, root in sorted(roots.items())]


def pin_rollout_window(policy, promotion, dependency_lock, *, window_id, start_at_utc=None, now=None):
    """Pin policy/release/window before the first access; performs no remote calls."""
    policy, now = validate_rollout_policy(policy), _now(now)
    _, roots = _promotion_targets(promotion, dependency_lock, now)
    start = now if start_at_utc is None else _utc(start_at_utc, "start_at_utc")
    if start < now:
        raise ReleaseError("Pin the rollout window before it starts; retrospective windows are not allowed.")
    return _sealed({"format_version": 1, "mode": "rollout_window", "window_id": text(window_id, "window_id"),
                    "created_at_utc": now.isoformat(), "start_at_utc": start.isoformat(),
                    "end_at_utc": (start + timedelta(seconds=policy["window_seconds"])).isoformat(),
                    "policy_sha256": fingerprint(policy), "promotion_sha256": fingerprint(promotion),
                    "dependency_lock_sha256": fingerprint(dependency_lock), "host": promotion["host"],
                    "label": promotion["label"], "targets": _targets(roots)})


def _window(window):
    _integrity(window, "rollout_window")
    object_fields(window, ("format_version", "mode", "window_id", "created_at_utc", "start_at_utc", "end_at_utc",
                          "policy_sha256", "promotion_sha256", "dependency_lock_sha256", "host", "label", "targets", "evidence_sha256"), where="rollout window")
    text(window["window_id"], "window_id")
    for field in ("policy_sha256", "promotion_sha256", "dependency_lock_sha256"):
        digest(window[field], field)
    start, end = _utc(window["start_at_utc"], "window start"), _utc(window["end_at_utc"], "window end")
    created = _utc(window["created_at_utc"], "window created")
    if created > start or start >= end:
        raise ReleaseError("Rollout window must be pinned before its non-empty time range.")
    return start, end, created


def _validate_window_bindings(window, policy, promotion, dependency_lock, now):
    start, end, created = _window(window)
    completed, roots = _promotion_targets(promotion, dependency_lock, now)
    if (window["policy_sha256"] != fingerprint(policy) or window["promotion_sha256"] != fingerprint(promotion) or
            window["dependency_lock_sha256"] != fingerprint(dependency_lock) or window["host"] != promotion["host"] or
            window["label"] != promotion["label"] or window["targets"] != _targets(roots)):
        raise ReleaseError("Rollout window was substituted across policies, releases or targets.")
    if completed > created or created > now or (end - start).total_seconds() != policy["window_seconds"]:
        raise ReleaseError("Rollout window timing differs from the reviewed release/policy.")
    return start, end


def normalize_rollout_observation(observation):
    """Validate the normalized actual-SDK capture contract; never infer object identity."""
    object_fields(observation, ("format_version", "capture_method", "consumer_id", "deployment_id", "request_id", "sdk",
                               "requested_label", "request_started_at_utc", "observed_at_utc", "cache_ttl_seconds",
                               "cache_status", "is_fallback", "returned"), where="SDK observation")
    _version(observation["format_version"])
    for field in ("consumer_id", "deployment_id", "request_id"):
        text(observation[field], field)
    sdk = object_fields(observation["sdk"], ("name", "version"), where="SDK identity")
    if not isinstance(sdk["name"], str) or sdk["name"] not in CAPTURE_METHODS or observation["capture_method"] != CAPTURE_METHODS[sdk["name"]]:
        raise ReleaseError("Unknown or inconsistent SDK capture method.")
    text(sdk["version"], "SDK version")
    _label(observation["requested_label"])
    _utc(observation["request_started_at_utc"], "request start")
    _utc(observation["observed_at_utc"], "observation time")
    ttl = _seconds(observation["cache_ttl_seconds"], "cache TTL")
    if observation["cache_status"] != ("disabled" if ttl == 0 else "sdk_cache_possible"):
        raise ReleaseError("SDK cache metadata is inconsistent.")
    if type(observation["is_fallback"]) is not bool:
        raise ReleaseError("SDK fallback state must be a boolean.")
    returned = object_fields(observation["returned"], ("name", "version", "type", "resolved_sha256"), where="returned prompt")
    _name(returned["name"])
    if (type(returned["version"]) is not int or returned["version"] < 0 or
            (returned["version"] == 0 and not observation["is_fallback"]) or returned["type"] not in ("text", "chat")):
        raise ReleaseError("Invalid returned prompt version/type.")
    digest(returned["resolved_sha256"], "returned prompt digest")
    fingerprint(observation)
    return copy.deepcopy(observation)


def bind_rollout_observation(observation, window, *, now=None):
    """Bind immediately in the application collector; retain even failed samples."""
    _window(window)
    observation = normalize_rollout_observation(observation)
    now = _now(now)
    return _sealed({"format_version": 1, "mode": "rollout_sample", "window_id": window["window_id"],
                    "window_sha256": fingerprint(window), "policy_sha256": window["policy_sha256"],
                    "promotion_sha256": window["promotion_sha256"], "dependency_lock_sha256": window["dependency_lock_sha256"],
                    "received_at_utc": now.isoformat(), "observation_sha256": fingerprint(observation), "observation": observation})


def _sample(sample, window):
    _integrity(sample, "rollout_sample")
    object_fields(sample, ("format_version", "mode", "window_id", "window_sha256", "policy_sha256", "promotion_sha256",
                          "dependency_lock_sha256", "received_at_utc", "observation_sha256", "observation", "evidence_sha256"), where="rollout sample")
    if (sample["window_id"] != window["window_id"] or sample["window_sha256"] != fingerprint(window) or
            any(sample[key] != window[key] for key in ("policy_sha256", "promotion_sha256", "dependency_lock_sha256"))):
        raise ReleaseError("Replayed or substituted sample belongs to a different rollout window/policy/release.")
    observation = normalize_rollout_observation(sample["observation"])
    if sample["observation_sha256"] != fingerprint(observation):
        raise ReleaseError("SDK observation changed after collection.")
    return observation, _utc(sample["received_at_utc"], "sample collection time")


def _provenance(request, policy, samples, window):
    expected = policy["provenance"]
    if request is None:
        return {"classification": "unsigned_local", "artifact_sha256": None}, [] if not expected["required"] else ["authenticated_provenance_missing"]
    object_fields(request, ("artifact_path", "trust_policy_path"), ("attestation_path", "trusted_root_path"), where="provenance verification request")
    if expected["trust_policy_sha256"] is None:
        raise ReleaseError("Pin the trusted provenance-policy digest before verifying rollout observations.")
    artifact_path, artifact = read_object(request["artifact_path"], "rollout observation artifact")
    object_fields(artifact, ("format_version", "window_sha256", "observations"), where="rollout observation artifact")
    _version(artifact["format_version"])
    if artifact["window_sha256"] != fingerprint(window) or fingerprint(artifact["observations"]) != fingerprint(samples):
        raise ReleaseError("Provenance artifact is bound to different observations or rollout window.")
    _, trust = read_object(request["trust_policy_path"], "external provenance trust policy")
    if fingerprint(trust) != expected["trust_policy_sha256"]:
        raise ReleaseError("Provenance trust policy differs from the independently reviewed rollout policy.")
    try:
        from langfuse_release_provenance import verify_provenance
    except ImportError:
        raise ReleaseError("Install the release provenance verifier before requesting authentication.") from None
    captured_bytes = artifact_path.read_bytes()
    def no_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseError("Rollout provenance artifact contains duplicate JSON keys.")
            value[key] = item
        return value
    try:
        captured_artifact = json.loads(captured_bytes, object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeError):
        raise ReleaseError("Rollout provenance artifact changed or is not valid JSON.") from None
    if fingerprint(captured_artifact) != fingerprint(artifact):
        raise ReleaseError("Rollout provenance artifact changed before verification.")
    captured_digest = hashlib.sha256(captured_bytes).hexdigest()
    result = verify_provenance(artifact_path, request["trust_policy_path"],
                               **{key: request[key] for key in ("attestation_path", "trusted_root_path") if key in request})
    # A verifier invocation is required. A caller-supplied result or claimed flag
    # is not accepted as proof; bind its actual verified bytes and trust policy.
    actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if (not isinstance(result, dict) or result.get("classification") not in ("verified_ci", "unsigned_local") or
            result.get("artifact_sha256") != captured_digest or actual_digest != captured_digest or
            result.get("trust_policy_sha256") != expected["trust_policy_sha256"]):
        raise ReleaseError("Provenance verifier result does not bind this observation artifact and trust policy.")
    reasons = []
    if expected["required"] and result["classification"] != "verified_ci":
        reasons.append("authenticated_provenance_missing")
    stable = {key: copy.deepcopy(result[key]) for key in
              ("classification", "artifact_sha256", "trust_policy_sha256", "identity", "verifier") if key in result}
    return stable, reasons


def _recovery(status, issues, promotion):
    codes = {issue["code"] for issue in issues}
    if status == "complete":
        action = "none"
        steps = ["The supplied instrumented samples met the reviewed rollout policy."]
    elif status == "collecting":
        action = "continue_observation"
        steps = ["Continue the already pinned observation window; do not claim completion before it ends."]
    elif codes & {"version_mismatch", "content_mismatch", "prompt_type_mismatch", "deployment_mismatch"}:
        action = "review_rollback"
        steps = ["Inspect affected consumers and current label/version state.",
                 "Review the previous version and application compatibility before any separately authorized label change."]
    else:
        action = "investigate_consumers"
        steps = ["Inspect missing, stale, fallback or invalid consumer samples listed in this report.",
                 "Collect a newly reviewed window after correcting the cause; preserve this failed window."]
    candidates = []
    if action == "review_rollback":
        for row in promotion["results"]:
            baseline = row.get("baseline_version")
            if type(baseline) is int and 0 < baseline < row["target_version"]:
                candidates.append({"name": row["name"], "label": promotion["label"], "current_release_version": row["target_version"],
                                   "candidate_previous_version": baseline, "current_state_verified": False})
    return {"mode": "read_only_proposal", "action": action, "steps": steps,
            "rollback_candidates": sorted(candidates, key=lambda value: value["name"]),
            "mutations_performed": 0, "automatic_rollback": False, "retry_unknown_post": False}


def build_rollout_report(observations, window, policy, promotion, dependency_lock, *, now=None, provenance=None):
    """Aggregate a closed or collecting window; invalid runtime samples never disappear."""
    implicit_now = now is None
    policy, now = validate_rollout_policy(policy), _now(now)
    start, end = _validate_window_bindings(window, policy, promotion, dependency_lock, now)
    if not isinstance(observations, list):
        raise ReleaseError("Rollout observations must be a list of bound samples.")
    samples = copy.deepcopy(observations)
    provenance_result, provenance_reasons = _provenance(provenance, policy, samples, window)
    if implicit_now:
        # External attestation verification may take seconds. Its elapsed time
        # counts toward freshness; an explicit historical time remains frozen.
        now = _now(None)
        start, end = _validate_window_bindings(window, policy, promotion, dependency_lock, now)
    issues = [{"code": code} for code in provenance_reasons]
    pending = []
    buckets = policy["window_seconds"] // policy["bucket_seconds"]
    targets = {row["name"]: row for row in window["targets"]}
    rows, events, duplicates = {}, set(), 0
    for consumer_id, consumer in sorted(policy["required_consumers"].items()):
        for name, target in sorted(targets.items()):
            rows[(consumer_id, name)] = {"consumer_id": consumer_id, "name": name, "deployment_id": consumer["deployment_id"],
                "sdk": consumer["sdk"], "expected_version": target["version"], "observations": 0, "unique_requests": 0,
                "clean_unique_requests": 0, "fallback_count": 0, "mismatch_count": 0, "invalid_time_count": 0,
                "cache_possible_count": 0, "version_counts": {}, "latest_observed_at_utc": None,
                "latest_clean_observed_at_utc": None,
                "buckets": [{"index": index, "unique_requests": 0, "clean_unique_requests": 0} for index in range(buckets)]}
    for sample in samples:
        observed, received = _sample(sample, window)
        consumer_id, returned = observed["consumer_id"], observed["returned"]
        name, request_id = returned["name"], observed["request_id"]
        identity = {"consumer_id": consumer_id, "name": name, "request_id": request_id}
        event = consumer_id, name, request_id
        if event in events:
            duplicates += 1
            issues.append({**identity, "code": "duplicate_or_replayed_request"})
            continue
        events.add(event)
        if (consumer_id, name) not in rows:
            issues.append({**identity, "code": "unexpected_consumer_or_prompt"})
            continue
        row, expected = rows[(consumer_id, name)], policy["required_consumers"][consumer_id]
        row["observations"] += 1
        row["version_counts"][str(returned["version"])] = row["version_counts"].get(str(returned["version"]), 0) + 1
        row["fallback_count"] += int(observed["is_fallback"])
        row["cache_possible_count"] += int(observed["cache_status"] == "sdk_cache_possible")
        sample_codes = []
        if observed["deployment_id"] != expected["deployment_id"]:
            sample_codes.append("deployment_mismatch")
        if observed["sdk"] != expected["sdk"]:
            sample_codes.append("sdk_mismatch")
        if observed["requested_label"] != window["label"]:
            sample_codes.append("label_mismatch")
        # Fallbacks are counted separately and cannot satisfy clean-sample counts.
        # Any non-fallback old/new version or content mixture is a hard failure.
        if not observed["is_fallback"]:
            for field, expected_value, code in (("version", targets[name]["version"], "version_mismatch"),
                                                ("resolved_sha256", targets[name]["resolved_sha256"], "content_mismatch"),
                                                ("type", targets[name]["type"], "prompt_type_mismatch")):
                if returned[field] != expected_value:
                    sample_codes.append(code)
        mismatch = bool(sample_codes)
        row["mismatch_count"] += int(mismatch)
        started = _utc(observed["request_started_at_utc"], "request start")
        timestamp = _utc(observed["observed_at_utc"], "observation time")
        time_codes = []
        if not (start <= started <= timestamp < end):
            time_codes.append("outside_window_or_invalid_access_time")
        if timestamp > now or received > now or received < timestamp:
            time_codes.append("future_or_reversed_collection_time")
        if (received - timestamp).total_seconds() > policy["max_capture_delay_seconds"]:
            time_codes.append("late_capture_or_replay")
        if (now - timestamp).total_seconds() > policy["max_observation_age_seconds"]:
            time_codes.append("stale_observation")
        if received < _utc(window["created_at_utc"], "window creation"):
            time_codes.append("capture_precedes_window_pin")
        row["invalid_time_count"] += int(bool(time_codes))
        sample_codes.extend(time_codes)
        issues.extend({**identity, "code": code} for code in sample_codes)
        if not time_codes:
            row["unique_requests"] += 1
            index = int((timestamp - start).total_seconds() // policy["bucket_seconds"])
            row["buckets"][index]["unique_requests"] += 1
            previous = row["latest_observed_at_utc"]
            if previous is None or timestamp > _utc(previous, "latest observation"):
                row["latest_observed_at_utc"] = timestamp.isoformat()
            if not sample_codes and not observed["is_fallback"]:
                row["clean_unique_requests"] += 1
                row["buckets"][index]["clean_unique_requests"] += 1
                previous = row["latest_clean_observed_at_utc"]
                if previous is None or timestamp > _utc(previous, "latest clean observation"):
                    row["latest_clean_observed_at_utc"] = timestamp.isoformat()
    for row in rows.values():
        count = row["observations"]
        row["fallback_rate"] = row["fallback_count"] / count if count else None
        row["mismatch_rate"] = row["mismatch_count"] / count if count else None
        row["covered_buckets"] = sum(bucket["clean_unique_requests"] >= policy["min_requests_per_bucket"] for bucket in row["buckets"])
        row["coverage_fraction"] = row["covered_buckets"] / buckets
        row["latest_age_seconds"] = None if row["latest_observed_at_utc"] is None else (now - _utc(row["latest_observed_at_utc"], "latest observation")).total_seconds()
        row["latest_clean_age_seconds"] = None if row["latest_clean_observed_at_utc"] is None else (now - _utc(row["latest_clean_observed_at_utc"], "latest clean observation")).total_seconds()
        coverage_codes = []
        if not row["observations"]:
            coverage_codes.append("missing_consumer_observations")
        if row["clean_unique_requests"] < policy["min_unique_requests_per_target"]:
            coverage_codes.append("insufficient_clean_unique_requests")
        if row["covered_buckets"] < policy["min_covered_buckets"]:
            coverage_codes.append("insufficient_time_bucket_coverage")
        if row["latest_clean_age_seconds"] is None or row["latest_clean_age_seconds"] > policy["max_latest_observation_age_seconds"]:
            coverage_codes.append("missing_or_stale_latest_observation")
        for code in coverage_codes:
            (pending if now < end else issues).append({"consumer_id": row["consumer_id"], "name": row["name"], "code": code})
        if row["fallback_rate"] is not None and row["fallback_rate"] > policy["max_fallback_rate"]:
            issues.append({"consumer_id": row["consumer_id"], "name": row["name"], "code": "fallback_rate_exceeded"})
    status = "failed" if issues else "collecting" if now < end else "complete"
    issues.sort(key=lambda value: (value.get("consumer_id", ""), value.get("name", ""), value.get("request_id", ""), value["code"]))
    report = {"format_version": 1, "mode": "rollout_observation", "status": status, "scope": "supplied_instrumented_samples_only",
              "window_id": window["window_id"], "window_sha256": fingerprint(window), "policy_sha256": fingerprint(policy),
              "promotion_sha256": fingerprint(promotion), "dependency_lock_sha256": fingerprint(dependency_lock),
              "start_at_utc": start.isoformat(), "end_at_utc": end.isoformat(), "recorded_at_utc": now.isoformat(),
              "observations": samples, "observations_sha256": fingerprint(samples), "counts": {
                  "received_samples": len(samples), "unique_request_prompt_events": len(events), "duplicate_events": duplicates,
                  "expected_consumer_prompt_pairs": len(rows), "observed_consumer_prompt_pairs": sum(row["observations"] > 0 for row in rows.values())},
              "consumers": list(rows.values()), "issues": issues, "pending_requirements": pending,
              "provenance": provenance_result, "recovery_proposal": _recovery(status, issues, promotion)}
    return _sealed(report)


def check_rollout_report(report, window, policy, promotion, dependency_lock, *, now=None, provenance=None):
    """Recheck bindings, samples and time-dependent freshness; only complete passes."""
    _integrity(report, "rollout_observation")
    if report.get("status") != "complete":
        raise ReleaseError("Rollout evidence is not complete.")
    implicit_now = now is None
    checked_at = _now(now)
    policy = validate_rollout_policy(policy)
    recorded = _utc(report.get("recorded_at_utc"), "report time")
    if recorded > checked_at:
        raise ReleaseError("Rollout report is from the future.")
    # Reconstruct the historical report and authenticate its source once.
    original = build_rollout_report(report.get("observations"), window, policy, promotion, dependency_lock,
                                   now=recorded, provenance=provenance)
    if fingerprint(original) != fingerprint(report):
        raise ReleaseError("Rollout report bindings, policy, provenance or computed results changed.")
    if implicit_now:
        checked_at = _now(None)
    if recorded > checked_at:
        raise ReleaseError("Rollout report is from the future after verification.")
    # For an already complete, exactly reconstructed report, only age can change
    # as time advances. Reuse its authenticated samples instead of invoking the
    # external verifier a second time merely to recalculate freshness.
    for sample in original["observations"]:
        observed_at = _utc(sample["observation"]["observed_at_utc"], "observation time")
        if (checked_at - observed_at).total_seconds() > policy["max_observation_age_seconds"]:
            raise ReleaseError("Rollout samples no longer meet freshness or the reviewed policy.")
    for consumer in original["consumers"]:
        latest_clean = _utc(consumer["latest_clean_observed_at_utc"], "latest clean observation")
        if (checked_at - latest_clean).total_seconds() > policy["max_latest_observation_age_seconds"]:
            raise ReleaseError("Rollout samples no longer meet freshness or the reviewed policy.")
    return report


def _write(path, data):
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ReleaseError("Rollout evidence must use a .json filename.")
    write_report(inside(path.parent, path.name), data, create=True)
    return data


def write_rollout_report(path, observations, window, policy, promotion, dependency_lock, *, now=None, provenance=None):
    return _write(path, build_rollout_report(observations, window, policy, promotion, dependency_lock, now=now, provenance=provenance))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--promotion", required=True)
    parser.add_argument("--dependency-lock", required=True)
    parser.add_argument("--window", required=True, help="Pinned window file; new path for --pin-window")
    parser.add_argument("--pin-window", action="store_true")
    parser.add_argument("--window-id")
    parser.add_argument("--start-at")
    parser.add_argument("--observations", help="JSON object containing observations array and window_sha256")
    parser.add_argument("--report")
    parser.add_argument("--check")
    parser.add_argument("--provenance-request", help="JSON paths for actual provenance verification")
    args = parser.parse_args()
    try:
        _, policy = read_object(args.policy, "rollout policy")
        _, promotion = read_object(args.promotion, "promotion")
        _, lock = read_object(args.dependency_lock, "dependency lock")
        if args.pin_window:
            if not args.window_id or args.observations or args.report or args.check:
                raise ReleaseError("--pin-window requires --window-id and cannot inspect existing observations.")
            _write(args.window, pin_rollout_window(policy, promotion, lock, window_id=args.window_id, start_at_utc=args.start_at))
            print("Rollout window pinned before observation.")
            return
        _, window = read_object(args.window, "rollout window")
        provenance = read_object(args.provenance_request, "provenance request")[1] if args.provenance_request else None
        if args.check:
            _, report = read_object(args.check, "rollout report")
            check_rollout_report(report, window, policy, promotion, lock, provenance=provenance)
        else:
            if not args.observations or not args.report:
                raise ReleaseError("Supply --observations and --report, or --check.")
            _, source = read_object(args.observations, "rollout observations")
            object_fields(source, ("format_version", "window_sha256", "observations"), where="observation artifact")
            _version(source["format_version"])
            if source["window_sha256"] != fingerprint(window):
                raise ReleaseError("Observation artifact belongs to another rollout window.")
            report = write_rollout_report(args.report, source["observations"], window, policy, promotion, lock, provenance=provenance)
        print(json.dumps({"status": report["status"], "counts": report["counts"], "issues": report["issues"]}))
        if report["status"] != "complete":
            parser.exit(1)
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Rollout check failed: {exc}\n")


if __name__ == "__main__":
    main()
