#!/usr/bin/env python3
"""Observe prompts at a consuming application's official Python SDK boundary.

Receipts establish what the instrumented accesses reported, not fleet-wide
traffic coverage or authenticated provenance. The core remains standard-library
only; the observation helper lazily imports the application's installed SDK.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import importlib.metadata
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from evaluate_langfuse_release import _read_object
from langfuse_release_common import ReleaseError, _content, _label, _name, fingerprint, inside
from langfuse_release_dependencies import prompt_sha256, validate_lock_integrity
from publish_langfuse_prompts import write_report


CAPTURE_METHOD = "langfuse_release_adoption.capture_sdk_observation/v1"
JS_CAPTURE_METHOD = "langfuse_release_adoption.capture_js_sdk_observation/v1"


def _utc(value, field):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ReleaseError(f"{field} must be an aware UTC timestamp.") from None
    if (not isinstance(value, datetime) or value.tzinfo is None or
            value.utcoffset() is None or value.utcoffset().total_seconds() != 0):
        raise ReleaseError(f"{field} must be an aware UTC timestamp.")
    return value.astimezone(timezone.utc)


def _now(value):
    return datetime.now(timezone.utc) if value is None else _utc(value, "now")


def _identifier(value, field):
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or
            any(ord(char) < 32 for char in value)):
        raise ReleaseError(f"{field} must be an explicit non-empty identity without controls.")
    return value


def _seconds(value, field, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value) or
            value < 0 or (positive and value == 0)):
        raise ReleaseError(f"{field} must be a {'positive' if positive else 'non-negative'} finite number.")
    return value


def capture_sdk_observation(prompt_client, *, consumer_id, deployment_id, request_id,
                            requested_label, request_started_at_utc, cache_ttl_seconds,
                            observed_at_utc=None):
    """Call immediately after the application's actual get_prompt access.

    Only official installed Python SDK prompt client types are accepted. No API
    call is made here. A real object is still not cryptographic proof of how it
    was obtained; instrument the real request path and retain trusted CI logs.
    """
    try:
        model = importlib.import_module("langfuse.model")
        sdk_version = importlib.metadata.version("langfuse")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise ReleaseError("Install the official Langfuse Python SDK in the consuming application.") from None
    if type(prompt_client) is model.TextPromptClient:
        prompt_type = "text"
    elif type(prompt_client) is model.ChatPromptClient:
        prompt_type = "chat"
    else:
        raise ReleaseError("Observation requires an actual official SDK TextPromptClient or ChatPromptClient, not a dict.")
    try:
        name, version = prompt_client.name, prompt_client.version
        content, config, fallback = prompt_client.prompt, prompt_client.config, prompt_client.is_fallback
    except AttributeError:
        raise ReleaseError("SDK object is missing returned prompt identity or fallback state.") from None
    return _capture_value(name, version, prompt_type, content, config, fallback,
                          sdk_name="langfuse-python", sdk_version=sdk_version,
                          consumer_id=consumer_id, deployment_id=deployment_id, request_id=request_id,
                          requested_label=requested_label, request_started_at_utc=request_started_at_utc,
                          cache_ttl_seconds=cache_ttl_seconds, observed_at_utc=observed_at_utc)


def capture_js_observation(prompt_snapshot, *, sdk_version, **metadata):
    """Normalize the snapshot captured by the JS helper from an actual SDK object.

    The Python bridge cannot authenticate a serialized JS object; provenance
    must identify a trusted capturing application, just as for Python receipts.
    """
    fields = {"name", "version", "type", "prompt", "config", "is_fallback"}
    if not isinstance(prompt_snapshot, dict) or set(prompt_snapshot) != fields:
        raise ReleaseError("Expected the exact JavaScript SDK capture snapshot.")
    if prompt_snapshot["type"] not in ("text", "chat"):
        raise ReleaseError("Unknown JavaScript prompt type.")
    return _capture_value(prompt_snapshot["name"], prompt_snapshot["version"], prompt_snapshot["type"],
                          prompt_snapshot["prompt"], prompt_snapshot["config"], prompt_snapshot["is_fallback"],
                          sdk_name="langfuse-js", sdk_version=sdk_version, **metadata)


def _capture_value(name, version, prompt_type, content, config, fallback, *, sdk_name, sdk_version,
                   consumer_id, deployment_id, request_id, requested_label, request_started_at_utc,
                   cache_ttl_seconds, observed_at_utc=None):
    _identifier(sdk_version, "sdk.version")
    _name(name)
    if type(fallback) is not bool or type(version) is not int or version < 0 or (not fallback and version == 0):
        raise ReleaseError("SDK returned an invalid version or unknown fallback state.")
    _content(content, prompt_type)
    fingerprint(config)
    observed, started = _now(observed_at_utc), _utc(request_started_at_utc, "request_started_at_utc")
    if started > observed:
        raise ReleaseError("Observation precedes its application request.")
    ttl = _seconds(cache_ttl_seconds, "cache_ttl_seconds")
    value = {"name": name, "version": version, "type": prompt_type, "prompt": content, "config": config}
    return {"format_version": 1, "capture_method": CAPTURE_METHOD if sdk_name == "langfuse-python" else JS_CAPTURE_METHOD,
            "consumer_id": _identifier(consumer_id, "consumer_id"),
            "deployment_id": _identifier(deployment_id, "deployment_id"),
            "request_id": _identifier(request_id, "request_id"),
            "sdk": {"name": sdk_name, "version": sdk_version},
            "requested_label": _label(requested_label),
            "request_started_at_utc": started.isoformat(), "observed_at_utc": observed.isoformat(),
            "cache_ttl_seconds": ttl, "cache_status": "disabled" if ttl == 0 else "sdk_cache_possible",
            "is_fallback": fallback, "returned": {"name": name, "version": version, "type": prompt_type,
            "resolved_sha256": prompt_sha256(value)}}


def _required(value):
    if not isinstance(value, dict) or not value:
        raise ReleaseError("required_consumers must map consumer IDs to expected deployment revisions.")
    return {_identifier(key, "consumer_id"): _identifier(item, "deployment_id")
            for key, item in sorted(value.items())}


def _promotion_targets(promotion, lock, now):
    validate_lock_integrity(lock)
    if (not isinstance(promotion, dict) or type(promotion.get("format_version")) is not int or
            promotion["format_version"] != 2 or promotion.get("mode") != "promote" or
            promotion.get("status") != "verified" or promotion.get("apply") is not True):
        raise ReleaseError("Adoption requires a completed, verified applied promotion, not a preview or uncertain write.")
    completed = _utc(promotion.get("completed_at_utc"), "promotion.completed_at_utc")
    if completed > now:
        raise ReleaseError("Promotion completion timestamp is in the future.")
    if (promotion.get("host") != lock.get("host") or
            promotion.get("snapshot_sha256") != lock.get("snapshot_sha256") or
            not isinstance(promotion.get("stage_report"), dict) or
            promotion["stage_report"].get("sha256") != lock.get("stage_report_sha256")):
        raise ReleaseError("Promotion and dependency lock refer to different releases.")
    if promotion.get("dependency_lock_sha256") != fingerprint(lock):
        raise ReleaseError("Promotion must record this exact dependency lock before adoption can be checked.")
    captured = _utc(lock.get("recorded_at_utc"), "dependency_lock.recorded_at_utc")
    if captured > completed:
        raise ReleaseError("Dependency lock was captured after promotion; post-hoc closure cannot establish adoption.")
    _label(promotion.get("label"))
    rows = promotion.get("results")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("Promotion has no verified targets.")
    targets, roots = {}, {root["name"]: root for root in lock["roots"]}
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in ("verified", "already_promoted"):
            raise ReleaseError("Promotion contains unknown, failed or conflicting target outcomes.")
        name, version = row.get("name"), row.get("target_version")
        if (not isinstance(name, str) or name in targets or name not in roots or
                type(version) is not int or version != roots[name]["version"]):
            raise ReleaseError("Promotion targets differ from the dependency lock.")
        targets[name] = roots[name]
    if set(targets) != set(roots):
        raise ReleaseError("Promotion does not cover all dependency-locked targets.")
    return completed, targets


def _observations(value):
    if not isinstance(value, list):
        raise ReleaseError("Application observations must be a list.")
    rows = copy.deepcopy(value)
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("format_version")) is not int or row["format_version"] != 1 or
                row.get("capture_method") not in (CAPTURE_METHOD, JS_CAPTURE_METHOD)):
            raise ReleaseError("Unsupported or missing application SDK observation.")
        for key in ("consumer_id", "deployment_id", "request_id"):
            _identifier(row.get(key), key)
        _label(row.get("requested_label"))
        _utc(row.get("request_started_at_utc"), "request_started_at_utc")
        _utc(row.get("observed_at_utc"), "observed_at_utc")
        ttl = _seconds(row.get("cache_ttl_seconds"), "cache_ttl_seconds")
        if row.get("cache_status") != ("disabled" if ttl == 0 else "sdk_cache_possible"):
            raise ReleaseError("Observation cache metadata is inconsistent.")
        if type(row.get("is_fallback")) is not bool:
            raise ReleaseError("Observation fallback status must be a boolean.")
        sdk, returned = row.get("sdk"), row.get("returned")
        expected_sdk = "langfuse-python" if row["capture_method"] == CAPTURE_METHOD else "langfuse-js"
        if not isinstance(sdk, dict) or sdk.get("name") != expected_sdk:
            raise ReleaseError("Observation capture method and official SDK identity disagree.")
        _identifier(sdk.get("version"), "sdk.version")
        if not isinstance(returned, dict):
            raise ReleaseError("Observation is missing returned prompt identity.")
        _name(returned.get("name"))
        if (type(returned.get("version")) is not int or returned["version"] < 0 or
                returned.get("type") not in ("text", "chat") or
                not isinstance(returned.get("resolved_sha256"), str) or
                not re.fullmatch(r"[a-f0-9]{64}", returned["resolved_sha256"])):
            raise ReleaseError("Observation returned identity/hash is invalid.")
        key = row["consumer_id"], returned["name"]
        if key in seen:
            raise ReleaseError("Provide one current observation per required consumer and prompt.")
        seen.add(key)
    return sorted(rows, key=lambda row: (row["consumer_id"], row["returned"]["name"]))


def build_adoption_receipt(observations, promotion, dependency_lock, *, required_consumers,
                           max_age_seconds=300, now=None):
    """Record complete evidence, including explicit not_adopted outcomes."""
    now = _now(now)
    max_age_seconds = _seconds(max_age_seconds, "max_age_seconds", positive=True)
    required = _required(required_consumers)
    completed, targets = _promotion_targets(promotion, dependency_lock, now)
    rows, issues, seen = _observations(observations), [], set()
    for row in rows:
        consumer, returned = row["consumer_id"], row["returned"]
        name = returned["name"]
        key = {"consumer_id": consumer, "name": name}
        problems = []
        if consumer not in required or name not in targets:
            problems.append("unexpected_consumer_or_prompt")
        else:
            seen.add((consumer, name))
            target = targets[name]
            if row["deployment_id"] != required[consumer]:
                problems.append("deployment_revision_mismatch")
            if returned["version"] != target["version"]:
                problems.append("returned_version_mismatch")
            if returned["resolved_sha256"] != target["resolved_sha256"]:
                problems.append("resolved_content_mismatch")
        if row["requested_label"] != promotion["label"]:
            problems.append("requested_label_mismatch")
        if row["is_fallback"]:
            problems.append("fallback_used")
        started = _utc(row["request_started_at_utc"], "request_started_at_utc")
        observed = _utc(row["observed_at_utc"], "observed_at_utc")
        if started < completed or observed < completed:
            problems.append("access_precedes_promotion")
        if observed < started or observed > now or started > now:
            problems.append("invalid_access_time")
        if (now - observed).total_seconds() > max_age_seconds:
            problems.append("observation_stale")
        issues.extend({**key, "reason": reason} for reason in problems)
    for consumer in required:
        for name in sorted(targets):
            if (consumer, name) not in seen:
                issues.append({"consumer_id": consumer, "name": name, "reason": "missing_consumer_observation"})
    receipt = {"format_version": 1, "mode": "consumer_adoption", "status": "not_adopted" if issues else "verified",
               "host": promotion["host"], "promotion_sha256": fingerprint(promotion),
               "dependency_lock_sha256": fingerprint(dependency_lock), "required_consumers": required,
               "max_age_seconds": max_age_seconds, "observations": rows, "issues": issues,
               "recorded_at_utc": now.isoformat(), "scope": "instrumented_sdk_accesses_only"}
    receipt["evidence_sha256"] = fingerprint(receipt)
    return receipt


def check_adoption_receipt(receipt, promotion, dependency_lock, *, required_consumers,
                           max_age_seconds=300, now=None):
    """Fail closed if evidence was changed, is incomplete, or has aged out."""
    if (not isinstance(receipt, dict) or type(receipt.get("format_version")) is not int or receipt["format_version"] != 1 or
            receipt.get("mode") != "consumer_adoption" or receipt.get("status") != "verified"):
        raise ReleaseError("A verified consumer-adoption receipt is required.")
    if receipt.get("evidence_sha256") != fingerprint({key: value for key, value in receipt.items() if key != "evidence_sha256"}):
        raise ReleaseError("Consumer-adoption receipt changed since capture.")
    checked_at = _now(now)
    if _utc(receipt.get("recorded_at_utc"), "receipt.recorded_at_utc") > checked_at:
        raise ReleaseError("Consumer-adoption receipt was recorded in the future.")
    fresh = build_adoption_receipt(receipt.get("observations"), promotion, dependency_lock,
                                   required_consumers=required_consumers, max_age_seconds=max_age_seconds, now=checked_at)
    for field in ("host", "promotion_sha256", "dependency_lock_sha256", "required_consumers", "max_age_seconds", "scope", "observations"):
        if fresh[field] != receipt.get(field):
            raise ReleaseError("Consumer-adoption receipt binding or policy changed.")
    if fresh["status"] != "verified" or fresh["issues"] != receipt.get("issues"):
        raise ReleaseError("Application adoption is not verified: " + ", ".join(sorted({issue["reason"] for issue in fresh["issues"]})))
    return receipt


def write_adoption_receipt(path, observations, promotion, dependency_lock, *, required_consumers,
                           max_age_seconds=300, now=None):
    receipt = build_adoption_receipt(observations, promotion, dependency_lock, required_consumers=required_consumers,
                                     max_age_seconds=max_age_seconds, now=now)
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ReleaseError("Adoption receipt must be a .json file.")
    path = inside(path.parent, path.name)
    write_report(path, receipt, create=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promotion", required=True)
    parser.add_argument("--dependency-lock", required=True)
    parser.add_argument("--consumers", required=True, help="JSON object mapping consumer IDs to expected deployment IDs")
    parser.add_argument("--observations", help="JSON object containing an observations array from application instrumentation")
    parser.add_argument("--report")
    parser.add_argument("--check", help="Check an existing adoption receipt")
    parser.add_argument("--max-age-seconds", type=float, default=300)
    args = parser.parse_args()
    try:
        _, promotion = _read_object(args.promotion, "promotion")
        _, lock = _read_object(args.dependency_lock, "dependency lock")
        _, consumers = _read_object(args.consumers, "consumer policy")
        if args.check:
            _, receipt = _read_object(args.check, "consumer adoption receipt")
            check_adoption_receipt(receipt, promotion, lock, required_consumers=consumers, max_age_seconds=args.max_age_seconds)
        else:
            if not args.observations or not args.report:
                raise ReleaseError("Recording requires --observations and --report.")
            _, observations = _read_object(args.observations, "application observations")
            receipt = write_adoption_receipt(args.report, observations.get("observations"), promotion, lock,
                                             required_consumers=consumers, max_age_seconds=args.max_age_seconds)
        print(json.dumps({"status": receipt["status"], "issues": receipt["issues"]}))
        if receipt["status"] != "verified":
            parser.exit(1)
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Adoption check failed: {exc}\n")


if __name__ == "__main__":
    main()
