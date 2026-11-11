#!/usr/bin/env python3
"""Run Python/JS experiments, remote checks and rollout probes on disposable Langfuse.

Uses synthetic prompts and deterministic tasks only: no model or paid API calls.
Requires the example extra, Node 22+ and `npm ci --prefix js --ignore-scripts`.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langfuse_release_common import Client, ReleaseError, fingerprint, inside
from publish_langfuse_prompts import release, write_report
from pull_langfuse_prompts import export
from promote_langfuse_prompts import promote
from langfuse_release_policy import pin_policy
from langfuse_release_dependencies import capture_dependency_lock
from langfuse_release_adoption import capture_sdk_observation, build_adoption_receipt
from langfuse_release_remote import RemoteClient, make_remote_expectation, verify_remote
from langfuse_release_bundle import create_bundle, verify_bundle
from langfuse_release_rollout import pin_rollout_window, bind_rollout_observation, build_rollout_report
from import_langfuse_experiment import import_experiment
from examples.sdk_experiment import policy_for, run_experiment


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(host, project_id, destination):
    from langfuse import Langfuse
    if urlsplit(host).scheme != "http" or urlsplit(host).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ReleaseError("Only a disposable loopback Langfuse project is allowed.")
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, exist_ok=False)
    sdk, client = Langfuse(base_url=host), Client(host)
    env = dict(os.environ, RELEASE_PYTHON=sys.executable, RELEASE_PYTHON_PATH=str(ROOT))
    checks, evidence = [], []
    active_probes = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def js(operation, config, directory):
        config_path = directory / (operation + "-" + uuid.uuid4().hex[:8] + ".json")
        write_report(config_path, config, create=True)
        process = subprocess.run(["node", str(ROOT / "js/sdk-experiment.mjs"), operation, str(config_path)],
                                 env=env, cwd=ROOT, capture_output=True, text=True, timeout=90)
        write_report(directory / (config_path.stem + "-process.json"),
                     {"returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}, create=True)
        if process.returncode:
            raise ReleaseError(f"JavaScript {operation} failed: {process.stderr[-2000:]}")

    try:
        for language in ("python", "javascript-failing", "javascript"):
            is_javascript = language.startswith("javascript")
            name = "release-v06-" + language + "-" + uuid.uuid4().hex[:12]
            leaf, child = name + "-leaf", name + "-child"
            dependency = f"@@@langfusePrompt:name={child}|label=production@@@"
            for dependency_name, body in ((leaf, " "), (child, f"@@@langfusePrompt:name={leaf}|label=production@@@")):
                client.create({"name": dependency_name, "type": "text", "prompt": body, "config": {},
                               "labels": ["production"], "tags": ["synthetic-local-v06"]})
            client.create({"name": name, "type": "text", "prompt": '{"answer":"baseline {{answer}}"}' + dependency,
                           "config": {}, "labels": ["production"], "tags": ["synthetic-local-v06"]})
            manifest = export(client, destination / language, label="production", names=[name])
            directory = manifest.parent
            body = '{"answer":"wrong {{answer}}"}' if language.endswith("-failing") else '{"answer":"{{answer}}"}'
            inside(directory, read(manifest)["prompts"][0]["file"]).write_text(body + dependency, encoding="utf-8")
            policy = policy_for(name)
            policy.update(format_version=2, remote_export="required", remote_verification={
                "project_id": project_id, "dataset_kind": "local", "max_age_seconds": 300})
            if is_javascript:
                identity_path = directory / "javascript-identity.json"
                subprocess.run(["node", str(ROOT / "js/sdk-experiment.mjs"), "identity", str(identity_path)],
                               env=env, cwd=ROOT, check=True, timeout=30)
                identity = read(identity_path)
                policy["producer"] = {"kind": "langfuse-javascript-sdk", "schema_version": "v1", "script_path": "js/sdk-experiment.mjs"}
                policy["dataset"] = {**identity["dataset"], "cases": [
                    {"id": row["id"], "input_sha256": fingerprint(row["input"]),
                     "expected_output_sha256": fingerprint(row["expected_output"])} for row in identity["cases"]]}
                policy["execution"] = identity["execution"]
            policy_path = directory / "reviewed-policy.json"
            write_report(policy_path, policy, create=True)
            pin_policy(manifest, policy_path)
            release(client, manifest, apply=True, stage=True, report_name="stage.json")
            stage = directory / "reports/stage.json"
            lock = capture_dependency_lock(client, manifest, stage,
                consumer={"sdk": "langfuse-python/5.2.0" if language == "python" else "langfuse-js/6.1.1", "compiler": "official"},
                report_name="dependency-lock.json")
            lock_path = directory / "reports/dependency-lock.json"
            results_path, capture_path = directory / "results.json", directory / "capture.json"
            if language == "python":
                source, capture = run_experiment(sdk, manifest, stage, results_path, capture_path, dependency_lock_path=lock_path)
            else:
                js("experiment", {"manifest": str(manifest), "stage": str(stage), "lock": str(lock_path),
                   "host": host, "results": str(results_path), "capture": str(capture_path)}, directory)
                source, capture = read(results_path), read(capture_path)
            try:
                import_experiment(manifest, stage, results_path, capture_path, report_name="missing-remote.json")
            except ReleaseError:
                checks.append(language + ": required remote evidence cannot be replaced by flush")
            else:
                raise AssertionError("Missing remote evidence accepted")
            expected = make_remote_expectation(source, capture, policy, host=host, project_id=project_id, dataset_kind="local")
            write_report(directory / "remote-expectation.json", expected, create=True)
            remote_path = directory / "remote-receipt.json"
            remote = verify_remote(RemoteClient(host), expected, wait_seconds=60, report_path=remote_path)
            check(language + ": actual remote experiment, cases and typed scores persisted", remote["status"] == "confirmed")
            receipt = import_experiment(manifest, stage, results_path, capture_path,
                                        remote_report_path=remote_path, report_name="evaluation.json")
            if language.endswith("-failing"):
                check("JavaScript: real failing evaluator remains failed after persistence", receipt["status"] == "failed")
                try:
                    promote(client, manifest, stage, label="production", apply=True,
                        evaluation_report=directory / "reports/evaluation.json", dependency_lock=lock_path, report_name="failed-blocked.json")
                except ReleaseError:
                    pass
                else:
                    raise AssertionError("Failed JavaScript evaluation was allowed to promote")
                check("JavaScript: failed experiment leaves baseline label unchanged", client.get(name, label="production")["version"] == 1)
                evidence.append({"language": language, "experiment_id": expected["experiment_id"], "expected_outcome": "failed_and_blocked"})
                continue
            check(language + ": fresh remote evidence passes reviewed policy", receipt["status"] == "passed")
            check(language + ": actual SDK compiled nested server-resolved dependencies", any(
                row["name"] == leaf for row in lock["roots"][0]["nodes"]))

            if language == "javascript":
                client.create({"name": leaf, "type": "text", "prompt": "\n", "config": {},
                               "labels": ["production"], "tags": ["synthetic-local-v06"]})
                try:
                    promote(client, manifest, stage, label="production", apply=True,
                        evaluation_report=directory / "reports/evaluation.json", dependency_lock=lock_path, report_name="drift-blocked.json")
                except ReleaseError as exc:
                    check("JavaScript: transitive label drift refuses promotion", "Dependency" in str(exc))
                else:
                    raise AssertionError("Nested dependency drift passed promotion")
                check("JavaScript: dependency drift leaves deployment label unchanged", client.get(name, label="production")["version"] == 1)
                lock = capture_dependency_lock(client, manifest, stage, consumer={"sdk": "langfuse-js/6.1.1", "compiler": "official"},
                                               report_name="dependency-lock-after-drift.json")
                lock_path = directory / "reports/dependency-lock-after-drift.json"
                results_path, capture_path = directory / "results-after-drift.json", directory / "capture-after-drift.json"
                js("experiment", {"manifest": str(manifest), "stage": str(stage), "lock": str(lock_path), "host": host,
                   "results": str(results_path), "capture": str(capture_path)}, directory)
                source, capture = read(results_path), read(capture_path)
                expected = make_remote_expectation(source, capture, policy, host=host, project_id=project_id, dataset_kind="local")
                remote_path = directory / "remote-receipt-after-drift.json"
                remote = verify_remote(RemoteClient(host), expected, wait_seconds=60, report_path=remote_path)
                check("JavaScript: rerun with new lock persisted fully", remote["status"] == "confirmed")
                receipt = import_experiment(manifest, stage, results_path, capture_path,
                    remote_report_path=remote_path, report_name="evaluation-after-drift.json")
            evaluation_path = directory / "reports" / receipt["report_file"]

            probe = None
            if language == "javascript":
                probe_config = {"host": host, "name": name, "ready": str(directory / "cache-ready.json"),
                                "go": str(directory / "cache-go.json"), "output": str(directory / "cache-probe.json")}
                write_report(directory / "cache-config.json", probe_config, create=True)
                probe_log = open(directory / "cache-process.log", "w", encoding="utf-8")
                probe = subprocess.Popen(["node", str(ROOT / "js/sdk-experiment.mjs"), "cache-probe", str(directory / "cache-config.json")],
                                         env=env, cwd=ROOT, stdout=probe_log, stderr=probe_log)
                active_probes.append((probe, probe_log))
                deadline = time.monotonic() + 30
                while not Path(probe_config["ready"]).exists():
                    if probe.poll() is not None or time.monotonic() > deadline:
                        raise ReleaseError("JavaScript cache probe did not warm its baseline")
                    time.sleep(0.1)
            promoted = promote(client, manifest, stage, label="production", apply=True,
                evaluation_report=evaluation_path, dependency_lock=lock_path, report_name="promotion.json")
            check(language + ": label promotion verified on server", promoted["status"] == "verified")
            bundled = create_bundle(manifest, stage, evaluation_path, directory / "release.zip")
            checked = verify_bundle(directory / "release.zip", expected_release_id=bundled["release_id"])
            write_report(directory / "bundle-verification.json", checked, create=True)
            check(language + ": portable bundle validates preserved remote evidence", checked["status"] == "verified")
            if probe is not None:
                write_report(Path(probe_config["go"]), {"promotion_verified": True}, create=True)
                if probe.wait(timeout=40) != 0:
                    raise ReleaseError("JavaScript cache probe failed; inspect cache-process.log")
                data = read(probe_config["output"])
                required = {"js-demo": "local-v06"}
                stale = build_adoption_receipt([data["cached"]], promoted, lock, required_consumers=required)
                fresh = build_adoption_receipt([data["fresh"]], promoted, lock, required_consumers=required)
                check("JavaScript: actual warm cache remains on old version", stale["status"] == "not_adopted")
                check("JavaScript: bypassing cache observes released version", fresh["status"] == "verified")
                check("JavaScript: missing prompt fallback is recorded explicitly", data["fallback"]["is_fallback"] is True)
                write_report(directory / "cache-adoption.json", {"stale": stale, "fresh": fresh}, create=True)

                rollout_policy = {"format_version": 1, "id": "synthetic-two-consumer-window", "required_consumers": {
                    "python-demo": {"deployment_id": "local-v06", "sdk": {"name": "langfuse-python", "version": "5.2.0"}},
                    "js-demo": {"deployment_id": "local-v06", "sdk": {"name": "langfuse-js", "version": identity["sdk_version"]}}},
                    "window_seconds": 8, "bucket_seconds": 4, "min_unique_requests_per_target": 2,
                    "min_requests_per_bucket": 1, "min_covered_buckets": 2, "max_observation_age_seconds": 60,
                    "max_latest_observation_age_seconds": 30, "max_capture_delay_seconds": 10,
                    "max_fallback_rate": 0, "max_mismatch_rate": 0, "provenance": {"required": False, "trust_policy_sha256": None}}
                window = pin_rollout_window(rollout_policy, promoted, lock, window_id=uuid.uuid4().hex)
                write_report(directory / "rollout-policy.json", rollout_policy, create=True)
                write_report(directory / "rollout-window.json", window, create=True)
                started = time.monotonic()
                samples = []
                for bucket in range(2):
                    time.sleep(max(0, started + bucket * 4 + 0.1 - time.monotonic()))
                    request_start = utc()
                    prompt = sdk.get_prompt(name, label="production", cache_ttl_seconds=0)
                    observation = capture_sdk_observation(prompt, consumer_id="python-demo", deployment_id="local-v06",
                        request_id=uuid.uuid4().hex, requested_label="production", request_started_at_utc=request_start, cache_ttl_seconds=0)
                    samples.append(bind_rollout_observation(observation, window))
                    output = directory / (f"js-observation-{bucket}.json")
                    js("observe", {"host": host, "name": name, "label": "production", "consumer_id": "js-demo",
                        "deployment_id": "local-v06", "output": str(output)}, directory)
                    samples.append(bind_rollout_observation(read(output), window))
                time.sleep(max(0, started + 8.1 - time.monotonic()))
                report = build_rollout_report(samples, window, rollout_policy, promoted, lock)
                write_report(directory / "rollout-samples.json", {
                    "format_version": 1, "window_sha256": fingerprint(window), "observations": samples}, create=True)
                write_report(directory / "rollout-report.json", report, create=True)
                check("Rollout: both actual SDK consumers cover both time buckets", report["status"] == "complete")
                replayed = build_rollout_report(samples + [copy.deepcopy(samples[0])], window, rollout_policy, promoted, lock)
                write_report(directory / "replayed-rollout.json", replayed, create=True)
                check("Rollout: replayed request cannot increase adoption evidence", replayed["status"] != "complete")
            evidence.append({"language": language, "experiment_id": expected["experiment_id"], "release_id": bundled["release_id"]})
        summary = {"status": "passed", "checks": checks, "evidence": evidence, "host": host,
                   "completed_at_utc": utc(), "model_calls": 0,
                   "scope": "Synthetic local experiments and instrumented SDK probes; unsigned local runtime evidence; no fleet traffic claim"}
        write_report(destination / "summary.json", summary, create=True)
        return summary
    finally:
        for process, stream in active_probes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            stream.close()
        sdk.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--disposable-local", required=True, action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.host, args.project_id, args.out_dir), indent=2))


if __name__ == "__main__":
    main()
