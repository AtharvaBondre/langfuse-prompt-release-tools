#!/usr/bin/env python3
"""Exercise the complete release workflow on a fresh loopback Langfuse project.

Creates synthetic prompts only. No model calls, paid APIs or real datasets.
Requires the optional example dependencies. Keep all generated evidence private.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import uuid

# Run directly from a checkout or source distribution.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse_release_common import Client, ReleaseError, inside
from pull_langfuse_prompts import export
from publish_langfuse_prompts import release, write_report
from promote_langfuse_prompts import promote
from langfuse_release_policy import pin_policy
from import_langfuse_experiment import import_experiment
from langfuse_release_dependencies import capture_dependency_lock
from langfuse_release_adoption import capture_sdk_observation, build_adoption_receipt
from reconcile_langfuse_release import reconcile
from examples.sdk_experiment import policy_for, run_experiment


def utc():
    return datetime.now(timezone.utc).isoformat()


class ObservedClient(Client):
    def __init__(self, host, *, project_id):
        super().__init__(host, project_id=project_id)
        self.patches = 0
        self.lose_patch = False

    def add_labels(self, name, version, labels):
        self.patches += 1
        response = super().add_labels(name, version, labels)
        if self.lose_patch:
            self.lose_patch = False
            raise ReleaseError("Injected response loss after a real committed PATCH", unknown_write=True)
        return response


def run(host, project_id, destination):
    from langfuse import Langfuse
    parsed = urlsplit(host)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ReleaseError("Use a disposable loopback-only Langfuse instance.")
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, exist_ok=False)
    client, sdk = ObservedClient(host, project_id=project_id), Langfuse(base_url=host)
    cached_sdk = Langfuse(base_url=host)
    checks = []

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    def location(manifest, report):
        return manifest.parent / "reports" / report["report_file"]

    prefix = "release-v05-" + uuid.uuid4().hex[:12]
    leaf, child, parent = (prefix + suffix for suffix in ("-leaf", "-child", "-parent"))
    child_body = f"@@@langfusePrompt:name={leaf}|label=production@@@"
    dependency = f"@@@langfusePrompt:name={child}|label=production@@@"
    for name, body in ((leaf, " "), (child, child_body), (parent, '{"answer":"baseline {{answer}}"}' + dependency)):
        client.create({"name": name, "type": "text", "prompt": body, "config": {},
                       "labels": ["production"], "tags": ["disposable-v05-demo"]})

    def stage_case(case_name, body):
        manifest = export(client, destination / case_name, label="production", names=[parent])
        entry = json.loads(manifest.read_text())["prompts"][0]
        inside(manifest.parent, entry["file"]).write_text(body + dependency, encoding="utf-8")
        policy_path = manifest.parent / "reviewed-policy.json"
        write_report(policy_path, policy_for(parent), create=True)
        pin_policy(manifest, policy_path)
        staged = release(client, manifest, apply=True, stage=True, report_name="stage.json")
        return manifest, location(manifest, staged)

    def evaluate(manifest, stage, attempt):
        lock = capture_dependency_lock(
            client, manifest, stage, consumer={"sdk": "langfuse-python/5.2.0", "compiler": "official"},
            report_name=f"dependencies-{attempt}.json")
        lock_path = manifest.parent / "reports" / f"dependencies-{attempt}.json"
        result_path = manifest.parent / f"experiment-{attempt}.json"
        capture_path = manifest.parent / f"capture-{attempt}.json"
        run_experiment(sdk, manifest, stage, result_path, capture_path, dependency_lock_path=lock_path)
        receipt = import_experiment(manifest, stage, result_path, capture_path,
                                    report_name=f"evaluation-{attempt}.json")
        return location(manifest, receipt), lock_path, lock

    try:
        bad_manifest, bad_stage = stage_case("failing", '{"answer":"wrong {{answer}}"}')
        bad_receipt, bad_lock_path, _ = evaluate(bad_manifest, bad_stage, "failed")
        check("actual failing SDK scores produce a failed receipt", json.loads(bad_receipt.read_text())["status"] == "failed")
        before = client.patches
        try:
            promote(client, bad_manifest, bad_stage, label="production", apply=True,
                    evaluation_report=bad_receipt, dependency_lock=bad_lock_path, report_name="blocked.json", allow_unsigned_local=True)
        except ReleaseError:
            pass
        else:
            raise AssertionError("Failed experiment was allowed to promote")
        check("failing official SDK experiment blocked without PATCH", client.patches == before)
        check("failing candidate did not move production", client.get(parent, label="production")["version"] == 1)

        manifest, stage = stage_case("passing", '{"answer":"{{answer}}"}')
        receipt, lock_path, lock = evaluate(manifest, stage, "before-drift")
        check("actual successful SDK scores pass the pinned policy", json.loads(receipt.read_text())["status"] == "passed")
        check("server graph includes the nested leaf", any(
            row["name"] == leaf for row in lock["roots"][0]["nodes"]))
        cached = cached_sdk.get_prompt(parent, label="production", cache_ttl_seconds=600)
        check("real SDK cache warmed with baseline", cached.version == 1)

        # Move a transitive child label after evaluation. Parent raw text and
        # staged version remain unchanged, but evaluated behavior has drifted.
        client.create({"name": leaf, "type": "text", "prompt": "\n", "config": {},
                       "labels": ["production"], "tags": ["disposable-v05-demo"]})
        before = client.patches
        try:
            promote(client, manifest, stage, label="production", apply=True,
                    evaluation_report=receipt, dependency_lock=lock_path, report_name="drift-blocked.json", allow_unsigned_local=True)
        except ReleaseError as exc:
            check("drift refusal identifies dependency closure", "Dependency" in str(exc))
        else:
            raise AssertionError("Transitive dependency drift was allowed to promote")
        check("nested label drift blocked without PATCH", client.patches == before)

        # Re-run the actual task against a new reviewed dependency lock. Keep
        # the failed attempt and original evaluation evidence unchanged.
        receipt, lock_path, lock = evaluate(manifest, stage, "after-drift")
        client.lose_patch = True
        try:
            promote(client, manifest, stage, label="production", apply=True,
                    evaluation_report=receipt, dependency_lock=lock_path, report_name="lost-response.json", allow_unsigned_local=True)
        except ReleaseError:
            pass
        else:
            raise AssertionError("Lost response was not classified as uncertain")
        before = client.patches
        stopped = json.loads((manifest.parent / "reports/lost-response.json").read_text())
        check("lost response remains explicitly uncertain", stopped["results"][0]["status"] == "promotion_outcome_unknown_do_not_retry")
        recovered = reconcile(client, manifest, manifest.parent / "reports/lost-response.json",
                              report_name="reconciliation.json")
        check("read-only reconciliation made no PATCH", client.patches == before)
        check("committed PATCH currently reconciles completed", all(
            row.get("classification") == "completed" for row in recovered["results"]))
        promoted = promote(client, manifest, stage, label="production", apply=True,
                           evaluation_report=receipt, dependency_lock=lock_path, report_name="verified.json", allow_unsigned_local=True)
        check("verified retry recognized already promoted without PATCH", client.patches == before)

        consumers = {"demo-app": "local-source-v05"}

        def observation(ttl):
            requested = utc()
            actual = cached_sdk.get_prompt(parent, label="production", cache_ttl_seconds=ttl)
            return capture_sdk_observation(
                actual, consumer_id="demo-app", deployment_id="local-source-v05",
                request_id=uuid.uuid4().hex, requested_label="production",
                request_started_at_utc=requested, cache_ttl_seconds=ttl)

        stale = build_adoption_receipt([observation(600)], promoted, lock, required_consumers=consumers)
        write_report(destination / "stale-adoption.json", stale, create=True)
        check("actual cached consumer is reported not adopted", stale["status"] == "not_adopted")
        fresh = build_adoption_receipt([observation(0)], promoted, lock, required_consumers=consumers)
        write_report(destination / "fresh-adoption.json", fresh, create=True)
        check("fresh SDK consumer is verified on released version", fresh["status"] == "verified")
        summary = {"status": "passed", "checks": checks, "host": host, "completed_at_utc": utc(),
                   "sdk": "langfuse-python/5.2.0", "model_calls": 0, "patches": client.patches,
                   "scope": "synthetic local prompts; SDK experiment; local complete results; no remote persistence claim"}
        write_report(destination / "summary.json", summary, create=True)
        return summary
    finally:
        sdk.shutdown()
        cached_sdk.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--out-dir", required=True, help="New private evidence directory")
    parser.add_argument("--disposable-local", required=True, action="store_true")
    args = parser.parse_args()
    summary = run(args.host, args.project_id, args.out_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
