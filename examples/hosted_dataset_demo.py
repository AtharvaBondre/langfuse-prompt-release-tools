#!/usr/bin/env python3
"""Exercise a real, version-pinned hosted dataset on disposable loopback Langfuse.

Requires the pinned example extra. All data is synthetic; no model calls occur.
The integration runner owns stack creation, endpoint isolation and cleanup.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
from importlib.metadata import version as package_version
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from import_langfuse_experiment import (capture_prompt, finalize_capture,
                                       import_experiment, start_capture)
from langfuse_release_common import Client, ReleaseError, fingerprint, inside
from langfuse_release_dependencies import capture_dependency_lock
from langfuse_release_policy import pin_policy, validate_policy
from langfuse_release_remote import (RemoteClient, make_remote_expectation,
                                     verify_remote)
from publish_langfuse_prompts import release, write_report
from pull_langfuse_prompts import export


SCRIPT = "examples/hosted_dataset_demo.py"
SDK_VERSION = "5.2.0"
PAGE_SIZE = 1
MAX_PAGES = 10
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
ANSWERS = ("A dataset version pins a point in time.", "A schema has its own digest.",
           "Missing exported scores remain incomplete.")
WITHHELD_SCORE = "run-all-exact"


def utc():
    return datetime.now(timezone.utc).isoformat()


def validate_target(host, project_id):
    parsed = urlsplit(host)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ReleaseError("Use a disposable loopback-only Langfuse origin.")
    if not isinstance(project_id, str) or not project_id.strip() or project_id != project_id.strip():
        raise ReleaseError("An independently supplied disposable project ID is required.")


def dataset_identity(dataset, project_id):
    """Pin the fetched dataset's real ID, explicit timestamp and returned schemas."""
    if dataset.project_id != project_id:
        raise ReleaseError("Hosted dataset belongs to a different project.")
    stamp = dataset.version
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ReleaseError("Fetch a hosted dataset at an explicit timezone-aware version.")
    return {"id": dataset.id, "name": dataset.name,
            "version": stamp.astimezone(timezone.utc).isoformat(),
            "schema_sha256": fingerprint({"inputSchema": dataset.input_schema,
                                           "expectedOutputSchema": dataset.expected_output_schema})}


def item_value(item):
    # Hosted SDK results carry DatasetItem objects, not local dicts. Project the
    # evaluated values explicitly; do not serialize timestamps or SDK internals.
    return {"id": item.id, "input": item.input, "expected_output": item.expected_output}


def policy_for(name, dataset, project_id):
    revision = fingerprint(Path(__file__).read_text(encoding="utf-8"))
    cases = [{"id": item.id, "input_sha256": fingerprint(item.input),
              "expected_output_sha256": fingerprint(item.expected_output)} for item in dataset.items]
    return validate_policy({"format_version": 2, "id": "hosted-answer-contract-v1",
        "producer": {"kind": "langfuse-python-sdk", "schema_version": "v1", "script_path": SCRIPT},
        "dataset": {**dataset_identity(dataset, project_id), "cases": cases},
        "execution": {"task_revision": revision, "evaluator_revision": revision,
                      "model": {"provider": "local", "name": "deterministic-json-renderer",
                                "config_sha256": fingerprint({})}},
        "targets": [{"name": name, "case_ids": [case["id"] for case in cases]}],
        "checks": [{"id": "answer-exact", "kind": "boolean", "operator": "eq", "threshold": True},
                   {"id": "answer-quality", "kind": "numeric", "operator": "gte", "threshold": 1.0}],
        "remote_export": "required", "remote_verification": {
            "project_id": project_id, "dataset_kind": "langfuse", "max_age_seconds": 300}})


def assert_reviewed_dataset(dataset, policy, project_id):
    identity = {key: value for key, value in policy["dataset"].items() if key != "cases"}
    actual_cases = [{"id": item.id, "input_sha256": fingerprint(item.input),
                     "expected_output_sha256": fingerprint(item.expected_output)} for item in dataset.items]
    if (dataset_identity(dataset, project_id) != identity
            or sorted(actual_cases, key=lambda row: row["id"]) != sorted(policy["dataset"]["cases"], key=lambda row: row["id"])):
        raise ReleaseError("Fetched hosted dataset identity/version/schema/cases differ from the reviewed policy.")


def result_artifact(result, duration_ms):
    from langfuse_release_results import project_python_result
    # Keep this recipe's explicit case/input projection while delegating score,
    # run identity and Action v1 validation to the supported helper. Detached
    # views preserve the original SDK objects and their optional score fields.
    projected = copy.copy(result)
    projected.item_results = []
    for row in result.item_results:
        if row.experiment_id != result.experiment_id:
            raise ReleaseError("SDK item belongs to a different experiment run.")
        item = copy.copy(row)
        item.item = item_value(row.item)
        item.input, item.expected_output = row.item.input, row.item.expected_output
        projected.item_results.append(item)
    return project_python_result(projected, script_path=SCRIPT, duration_ms=duration_ms,
                                 metadata={"producer": "hosted-dataset-sdk-example"})


@contextmanager
def withhold_run_score(sdk):
    """Fault injection: keep the actual local evaluation, omit its SDK score write."""
    original = sdk.create_score
    withheld = []
    def create_score(**kwargs):
        if kwargs.get("dataset_run_id") and kwargs.get("name") == WITHHELD_SCORE:
            withheld.append(kwargs["name"])
            return None
        return original(**kwargs)
    sdk.create_score = create_score
    try:
        yield withheld
    finally:
        sdk.create_score = original


class ObservedRemoteClient(RemoteClient):
    def __init__(self, host):
        super().__init__(host, timeout=10)
        self.pages = []

    def get_json(self, path, query=None):
        response = super().get_json(path, query)
        if query is not None and "limit" in query:
            self.pages.append({"path": path, "cursor_request": "cursor" in query,
                               "observation_scores": "observationId" in query,
                               "run_scores": "experimentId" in query and path.endswith("/scores")})
        return response


def changed_expectation(expected, **changes):
    """Negative probe input only. Never used as an evaluation receipt."""
    result = copy.deepcopy(expected)
    result.update(changes)
    result["evidence_sha256"] = fingerprint({key: value for key, value in result.items() if key != "evidence_sha256"})
    return result


def run_experiment(sdk, dataset, manifest, stage, policy, project_id, lock_path, attempt):
    from langfuse import Evaluation
    assert_reviewed_dataset(dataset, policy, project_id)
    capture = start_capture(manifest, stage, dataset=dataset_identity(dataset, project_id),
                            execution=policy["execution"], dependency_lock_path=lock_path)
    target, = capture["targets"]

    def task(*, item, **kwargs):
        prompt = sdk.get_prompt(target["name"], version=target["version"], cache_ttl_seconds=0)
        capture_prompt(capture, item_id=item.id, requested_name=target["name"],
                       requested_version=target["version"], prompt=prompt)
        return json.loads(prompt.compile(**item.input))

    def evaluate(*, output, expected_output, **kwargs):
        exact = output == expected_output
        return [Evaluation(name="answer-exact", value=exact, data_type="BOOLEAN"),
                Evaluation(name="answer-quality", value=1.0 if exact else 0.0, data_type="NUMERIC")]

    def evaluate_run(*, item_results, **kwargs):
        values = [row.output == row.item.expected_output for row in item_results]
        complete = len(values) == len(dataset.items)
        return [Evaluation(name=WITHHELD_SCORE, value=complete and all(values), data_type="BOOLEAN"),
                Evaluation(name="run-mean-quality", value=sum(values) / len(dataset.items), data_type="NUMERIC")]

    started = time.monotonic()
    result = dataset.run_experiment(name="hosted-release-contract", run_name=attempt + "-" + uuid.uuid4().hex,
                                    task=task, evaluators=[evaluate], run_evaluators=[evaluate_run], max_concurrency=1)
    source = result_artifact(result, int((time.monotonic() - started) * 1000))
    result_path, capture_path = (manifest.parent / (attempt + suffix) for suffix in ("-results.json", "-capture.json"))
    write_report(result_path, source, create=True)
    sdk.flush()
    final = finalize_capture(capture, result_path, remote_export="unconfirmed")
    write_report(capture_path, final, create=True)
    return source, final, result_path, capture_path


def run(host, project_id, destination):
    validate_target(host, project_id)
    if package_version("langfuse") != SDK_VERSION:
        raise ReleaseError("This demo requires the reviewed langfuse==5.2.0 example extra.")
    from langfuse import Langfuse
    import httpx
    remote = ObservedRemoteClient(host)
    projects = remote.get_json("/api/public/projects")
    if [row.get("id") for row in projects.get("data", [])] != [project_id]:
        raise ReleaseError("API key does not identify the independently supplied disposable project.")
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, exist_ok=False)
    checks, sdk_pages = [], []
    def observe_request(request):
        if request.method == "GET" and request.url.path == "/api/public/dataset-items":
            sdk_pages.append({"page": request.url.params.get("page"), "limit": request.url.params.get("limit"),
                              "version_pinned": request.url.params.get("version") is not None})
    transport = httpx.Client(timeout=10, trust_env=False, event_hooks={"request": [observe_request]})
    sdk = Langfuse(base_url=host, timeout=10, httpx_client=transport, sample_rate=1.0)
    client = Client(host, timeout=10, project_id=project_id)
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
    def verify(label, expectation, *, wait=0):
        report = verify_remote(remote, expectation, wait_seconds=wait, page_size=PAGE_SIZE,
                               max_pages=MAX_PAGES, report_path=destination / (label + ".json"))
        return report
    try:
        prefix = "hosted-release-" + uuid.uuid4().hex[:12]
        dataset_name = prefix + "-dataset"
        sdk.create_dataset(name=dataset_name, input_schema=SCHEMA, expected_output_schema=SCHEMA)
        created = [sdk.create_dataset_item(dataset_name=dataset_name, input={"answer": answer},
                                           expected_output={"answer": answer}) for answer in ANSWERS]
        # Use a server-returned timestamp, not the local clock or a content hash.
        # Older compatible projections can omit item.version; updated_at from a
        # freshly created item is also a concrete server point-in-time selector.
        stamp = max(item.version or item.updated_at for item in created)
        dataset = sdk.get_dataset(dataset_name, version=stamp, fetch_items_page_size=PAGE_SIZE)
        check("hosted SDK fetch traversed all dataset pages at the pinned version",
              len(dataset.items) == len(ANSWERS) and {row["page"] for row in sdk_pages} == {"1", "2", "3"}
              and all(row["limit"] == "1" and row["version_pinned"] for row in sdk_pages))
        check("hosted schemas returned by server match the reviewed contract",
              dataset.input_schema == SCHEMA and dataset.expected_output_schema == SCHEMA)
        client.create({"name": prefix, "type": "text", "prompt": '{"answer":"baseline {{answer}}"}',
                       "config": {}, "labels": ["production"], "tags": ["disposable-hosted-demo"]})
        manifest = export(client, destination / "release", label="production", names=[prefix], project_id=project_id)
        snapshot = json.loads(manifest.read_text())
        inside(manifest.parent, snapshot["prompts"][0]["file"]).write_text('{"answer":"{{answer}}"}', encoding="utf-8")
        policy = policy_for(prefix, dataset, project_id)
        policy_path = manifest.parent / "reviewed-policy.json"
        write_report(policy_path, policy, create=True)
        pin_policy(manifest, policy_path)
        release(client, manifest, apply=True, stage=True, report_name="stage.json")
        stage, lock_path = (manifest.parent / "reports" / name for name in ("stage.json", "dependency-lock.json"))
        capture_dependency_lock(client, manifest, stage, consumer={"sdk": "langfuse-python/5.2.0", "compiler": "official"},
                                report_name="dependency-lock.json")

        source, capture, results_path, capture_path = run_experiment(
            sdk, dataset, manifest, stage, policy, project_id, lock_path, "complete")
        expected = make_remote_expectation(source, capture, policy, host=host, project_id=project_id, dataset_kind="langfuse")
        write_report(destination / "remote-expectation.json", expected, create=True)
        confirmed = verify("complete-remote", expected, wait=60)
        check("actual hosted ID version schema cases and typed case/run scores persisted", confirmed["status"] == "confirmed")
        check("actual cursor reads cover item case-score and run-score pagination", all(
            any(row["cursor_request"] and predicate(row) for row in remote.pages) for predicate in (
                lambda row: row["path"] == "/api/public/experiment-items",
                lambda row: row["observation_scores"], lambda row: row["run_scores"])))
        receipt = import_experiment(manifest, stage, results_path, capture_path,
                                    remote_report_path=destination / "complete-remote.json", report_name="evaluation.json")
        check("complete hosted evidence passes the pinned release policy", receipt["status"] == "passed")

        with withhold_run_score(sdk) as withheld:
            missing_source, missing_capture, missing_results, missing_capture_path = run_experiment(
                sdk, dataset, manifest, stage, policy, project_id, lock_path, "missing-export")
        check("fault injection withheld exactly one actual SDK run-score write", withheld == [WITHHELD_SCORE])
        missing_expected = make_remote_expectation(missing_source, missing_capture, policy,
            host=host, project_id=project_id, dataset_kind="langfuse")
        pending = verify("missing-export-remote", missing_expected, wait=20)
        check("missing real run-score export remains pending after bounded polling",
              pending["status"] == "pending" and any(WITHHELD_SCORE in issue for issue in pending["issues"]))
        try:
            import_experiment(manifest, stage, missing_results, missing_capture_path,
                remote_report_path=destination / "missing-export-remote.json", report_name="missing-export-evaluation.json")
        except ReleaseError:
            checks.append("incomplete hosted export cannot produce a passing evaluation receipt")
        else:
            raise AssertionError("Missing hosted export passed the release gate")
        wrong_run = changed_expectation(expected, run_name=missing_expected["run_name"])
        check("another actual run name cannot substitute for the captured run",
              verify("wrong-run-remote", wrong_run)["status"] == "failed")
        other = sdk.create_dataset(name=prefix + "-other-dataset", input_schema=SCHEMA, expected_output_schema=SCHEMA)
        wrong_dataset = changed_expectation(expected, dataset={**expected["dataset"], "id": other.id, "name": other.name})
        check("another actual hosted dataset cannot substitute for the evaluated dataset",
              verify("wrong-dataset-remote", wrong_dataset)["status"] == "failed")
        changed = sdk.create_dataset_item(dataset_name=dataset_name, id=created[0].id,
            input={"answer": "Changed after review."}, expected_output={"answer": "Changed after review."})
        next_stamp = changed.version or changed.updated_at
        changed_dataset = sdk.get_dataset(dataset_name, version=next_stamp, fetch_items_page_size=PAGE_SIZE)
        try:
            assert_reviewed_dataset(changed_dataset, policy, project_id)
        except ReleaseError:
            checks.append("changed hosted version/content is rejected before experiment execution")
        else:
            raise AssertionError("Changed hosted content matched the reviewed dataset")
        wrong_version = changed_expectation(expected, dataset={**expected["dataset"], "version": next_stamp.isoformat()})
        check("changed hosted dataset version cannot relabel an earlier experiment",
              verify("changed-version-remote", wrong_version)["status"] == "failed")
        original = sdk.get_dataset(dataset_name, version=stamp, fetch_items_page_size=PAGE_SIZE)
        assert_reviewed_dataset(original, policy, project_id)
        checks.append("original hosted dataset version remains reproducible after an item edit")
        sdk.create_dataset(name=dataset_name, input_schema={**SCHEMA, "description": "Changed after review"},
                           expected_output_schema=SCHEMA)
        schema_drift = verify("changed-schema-remote", expected)
        check("live hosted schema drift fails independently of the dataset timestamp",
              schema_drift["status"] == "failed" and any("schema" in issue for issue in schema_drift["issues"]))
        summary = {"status": "passed", "checks": checks, "completed_at_utc": utc(), "model_calls": 0,
                   "sdk": "langfuse-python/5.2.0", "dataset_kind": "langfuse", "case_count": len(dataset.items),
                   "case_score_types": ["BOOLEAN", "NUMERIC"], "run_score_types": ["BOOLEAN", "NUMERIC"],
                   "page_size": PAGE_SIZE, "max_pages": MAX_PAGES,
                   "remote_statuses": {"complete": confirmed["status"], "missing_export": pending["status"],
                                       "changed_schema": schema_drift["status"]},
                   "scope": "Synthetic dataset hosted on the disposable local Langfuse server; no cloud tenant or model calls"}
        write_report(destination / "pagination.json", {"sdk_dataset_requests": sdk_pages, "remote_page_requests": remote.pages}, create=True)
        write_report(destination / "summary.json", summary, create=True)
        return summary
    finally:
        sdk.shutdown()
        transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--out-dir", required=True, help="New private evidence directory")
    parser.add_argument("--disposable-local", required=True, action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.host, args.project_id, args.out_dir), indent=2))


if __name__ == "__main__":
    main()
