#!/usr/bin/env python3
"""Verify complete experiment persistence using bounded, read-only public APIs."""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from import_langfuse_experiment import _action_result, _check_score, _item_value, _scores
from langfuse_release_common import Client, ReleaseError, _normalize_host, fingerprint, inside
from langfuse_release_policy import (digest, object_fields, read_object, text,
                                    timestamp, validate_execution, validate_policy)
from publish_langfuse_prompts import write_report


REMOTE_SCOPE = "stored Python SDK IO and expected typed scores; not independent runtime-type proof, runner authentication or fleet adoption"
NODE_REMOTE_SCOPE = "stored JavaScript SDK IO and expected typed scores; not independent runtime-type proof, runner authentication or fleet adoption"


def _scope(expected):
    return NODE_REMOTE_SCOPE if expected.get("runtime") == "node" else REMOTE_SCOPE


class RemoteClient(Client):
    """Project-scoped common transport, fenced to GET before opening a request."""
    def _request(self, method, path, query=None, payload=None):
        if method != "GET" or payload is not None:
            raise ReleaseError("Remote persistence verification permits GET only.")
        return super()._request(method, path, query, payload)

    def get_json(self, path, query=None):
        return self._request("GET", path, query)


class _Pending(ReleaseError):
    pass


def _utc(value, field):
    return timestamp(value, field).astimezone(timezone.utc)


def _id(value, field):
    text(value, field)
    if "," in value:
        raise ReleaseError(f"{field} must identify one object, not a CSV filter.")
    return value


def _number(value, field, *, minimum=0, maximum=3600):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ReleaseError(f"{field} must be finite and between {minimum} and {maximum}.")
    return value


def _typed(value, kind, field):
    if kind == "BOOLEAN":
        if type(value) is bool:
            return value
        # The official API family represents BOOLEAN as bool in v3 and 0/1 in
        # older stored projections. Explicit BOOLEAN is required for conversion.
        if type(value) in (int, float) and value in (0, 1):
            return bool(value)
        raise ReleaseError(f"{field} is not an explicitly typed boolean value.")
    if kind == "NUMERIC" and type(value) in (int, float) and math.isfinite(value):
        return value
    raise ReleaseError(f"{field} has an unsupported or mismatched score type.")


def _storage_kind(value):
    return "null" if value is None else "text" if isinstance(value, str) else "json"


def _stored_io_hash(value, kind):
    # Langfuse 4.78 forwards stored IO. Python 5.2 and JavaScript 6.1.1 leave
    # strings unchanged and serialize other JSON values once. This verifies
    # stored representation, not types lost before/during serialization.
    if kind == "null":
        if value is not None:
            raise ReleaseError("Stored null IO differs from expected null.")
        return fingerprint(None)
    if not isinstance(value, str):
        raise ReleaseError("Expected the supported API's stored string IO projection.")
    if kind == "text":
        return fingerprint(value)
    if kind != "json":
        raise ReleaseError("Unsupported expected IO storage kind.")
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ReleaseError("Stored JSON IO contains duplicate keys.")
            result[key] = item
        return result
    try:
        decoded = json.loads(value, object_pairs_hook=pairs)
        return fingerprint(decoded)
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError("Stored IO is not one complete finite JSON value.") from None


def _local_score(score, kind=None):
    if kind is None:
        kind = score.get("data_type")
        if kind is None:
            kind = "BOOLEAN" if type(score["value"]) is bool else "NUMERIC" if type(score["value"]) in (int, float) else None
    if score.get("data_type") is not None and score["data_type"] != kind:
        raise ReleaseError("Local score data_type contradicts the independently expected score kind.")
    if kind not in ("BOOLEAN", "NUMERIC"):
        raise ReleaseError("This verifier supports BOOLEAN and NUMERIC expected scores only.")
    if kind == "BOOLEAN" and type(score["value"]) is not bool:
        raise ReleaseError("Local BOOLEAN evidence must contain a boolean, not 0/1 or text.")
    return {"name": text(score["name"], "score name"), "data_type": kind,
            "value": _typed(score["value"], kind, "local score")}


def make_remote_expectation(source_results, execution_capture, policy, *, host, project_id,
                            dataset_kind="local"):
    """Derive expected persisted values, independently of remote_export claims.

    Persistence is a separate question from passing thresholds. The caller must
    independently validate staged prompt/runtime bindings with the importer.
    Never mutate the finalized capture after generating its receipt.
    """
    policy = validate_policy(policy)
    host, project_id = _normalize_host(host), _id(project_id, "expected project ID")
    source, capture = source_results, execution_capture
    script = _action_result(source, policy)
    producer_runtime = {"langfuse-python-sdk": "python", "langfuse-javascript-sdk": "node"}
    required_runtime = producer_runtime.get(policy["producer"]["kind"])
    if required_runtime is not None and script["runtime"] != required_runtime:
        raise ReleaseError("Experiment runtime differs from the independently pinned SDK producer.")
    if not isinstance(capture, dict) or type(capture.get("format_version")) is not int or capture.get("format_version") != 1 or capture.get("mode") != "langfuse_execution_capture":
        raise ReleaseError("A finalized execution capture is required.")
    if capture.get("result_sha256") != fingerprint(source) or capture.get("policy_sha256") != fingerprint(policy):
        raise ReleaseError("Result/capture/policy bindings do not match.")
    completion = capture.get("completion")
    if not isinstance(completion, dict) or completion.get("local") != "completed":
        raise ReleaseError("Local execution is incomplete; remote reads cannot repair it.")
    started = _utc(capture.get("started_at_utc"), "capture start")
    completed = _utc(capture.get("completed_at_utc"), "capture completion")
    if completed < started:
        raise ReleaseError("Execution completed before it started.")
    dataset = {key: policy["dataset"][key] for key in ("id", "name", "version", "schema_sha256") if key in policy["dataset"]}
    if capture.get("dataset") != dataset:
        raise ReleaseError("Captured dataset identity differs from policy.")
    validate_execution(capture.get("execution"))
    if capture["execution"] != policy["execution"]:
        raise ReleaseError("Captured execution identity differs from policy.")
    if dataset_kind not in ("local", "langfuse"):
        raise ReleaseError("dataset_kind must explicitly be local or langfuse.")
    if dataset_kind == "langfuse":
        _utc(dataset["version"], "remote dataset version")
    experiment = script["experiment_result"]
    experiment_id = _id(experiment.get("experiment_id"), "experiment ID")
    run_name = text(experiment.get("run_name"), "experiment run name")
    expected_cases = {row["id"]: row for row in policy["dataset"]["cases"]}
    expected_checks = {row["id"]: row for row in policy["checks"]}
    cases, seen_traces = {}, set()
    for item in experiment["item_results"]:
        case = text(item["item"].get("id"), "local case ID")
        if case not in expected_cases or case in cases:
            raise ReleaseError("Result cases are duplicated or outside the pinned dataset.")
        trace_id = item.get("trace_id")
        if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id) or trace_id in seen_traces:
            raise ReleaseError("Every case requires its own actual 32-character SDK trace ID.")
        seen_traces.add(trace_id)
        expected = expected_cases[case]
        for key in ("input", "expected_output"):
            if fingerprint(_item_value(item, key)) != expected[key + "_sha256"]:
                raise ReleaseError("Result input/expected output differs from the independently pinned dataset case.")
        if "output" not in item:
            raise ReleaseError("Every case must preserve the actual evaluated output.")
        scores = {score["name"]: score for score in item["evaluations"]}
        if set(scores) != set(expected_checks):
            raise ReleaseError("Result scores must exactly cover independently pinned checks.")
        for name, check in expected_checks.items():
            _check_score(scores[name], check)  # Strict local types; persistence does not require passing.
        cases[case] = {"id": case, "trace_id": trace_id,
                       "input_sha256": expected["input_sha256"],
                       "expected_output_sha256": expected["expected_output_sha256"],
                       "output_sha256": fingerprint(item["output"]),
                       "input_storage": _storage_kind(_item_value(item, "input")),
                       "expected_output_storage": _storage_kind(_item_value(item, "expected_output")),
                       "output_storage": _storage_kind(item["output"]),
                       "scores": [_local_score(scores[name], "BOOLEAN" if expected_checks[name]["kind"] == "boolean" else "NUMERIC")
                                  for name in sorted(scores)]}
    if set(cases) != set(expected_cases):
        raise ReleaseError("Result is missing required cases; sampled-out cases cannot pass persistence verification.")
    captured_ids = []
    if not isinstance(capture.get("items"), list):
        raise ReleaseError("Capture must include all expected case IDs.")
    for item in capture["items"]:
        if not isinstance(item, dict):
            raise ReleaseError("Malformed captured case.")
        captured_ids.append(text(item.get("id"), "captured case ID"))
    if len(captured_ids) != len(set(captured_ids)) or set(captured_ids) != set(cases):
        raise ReleaseError("Capture has incomplete or duplicate cases.")
    run_scores = [_local_score(score) for score in _scores(experiment["run_evaluations"], "run evaluations")]
    expected = {"format_version": 1, "mode": "remote_experiment_expectation", "host": host,
                "project_id": project_id, "experiment_id": experiment_id, "run_name": run_name,
                "dataset": {"kind": dataset_kind, **dataset},
                "started_at_utc": started.isoformat(), "completed_at_utc": completed.isoformat(),
                "source_results_sha256": fingerprint(source), "execution_capture_sha256": fingerprint(capture),
                "policy_sha256": fingerprint(policy), "cases": [cases[name] for name in sorted(cases)],
                "run_scores": sorted(run_scores, key=lambda row: row["name"])}
    # Keep existing Python receipt hashes stable while explicitly binding the
    # newly audited Node storage contract and its separate receipt scope.
    if script["runtime"] == "node":
        expected["runtime"] = "node"
    expected["evidence_sha256"] = fingerprint(expected)
    return expected


def _validate_expectation(expected):
    required = ("format_version", "mode", "host", "project_id", "experiment_id", "run_name", "dataset",
                "started_at_utc", "completed_at_utc", "source_results_sha256", "execution_capture_sha256",
                "policy_sha256", "cases", "run_scores", "evidence_sha256")
    object_fields(expected, required, ("runtime",), where="remote expectation")
    if "runtime" in expected and expected["runtime"] != "node":
        raise ReleaseError("Unsupported explicit remote expectation runtime.")
    if type(expected["format_version"]) is not int or expected["format_version"] != 1 or expected["mode"] != "remote_experiment_expectation":
        raise ReleaseError("Unsupported remote expectation format.")
    if expected["evidence_sha256"] != fingerprint({k: v for k, v in expected.items() if k != "evidence_sha256"}):
        raise ReleaseError("Remote expectation digest mismatch.")
    if _normalize_host(expected["host"]) != expected["host"]:
        raise ReleaseError("Expected host must be normalized.")
    for key in ("project_id", "experiment_id"):
        _id(expected[key], key)
    text(expected["run_name"], "run name")
    for key in ("source_results_sha256", "execution_capture_sha256", "policy_sha256"):
        digest(expected[key], key)
    if _utc(expected["started_at_utc"], "start") > _utc(expected["completed_at_utc"], "completion"):
        raise ReleaseError("Expected execution time window is reversed.")
    dataset = object_fields(expected["dataset"], ("kind", "id", "name", "version"), ("schema_sha256",), where="dataset")
    if dataset["kind"] not in ("local", "langfuse"):
        raise ReleaseError("Unsupported dataset kind.")
    for key in ("id", "name", "version"):
        text(dataset[key], "dataset " + key)
    if dataset["kind"] == "langfuse":
        _utc(dataset["version"], "dataset version")
    if "schema_sha256" in dataset:
        digest(dataset["schema_sha256"], "dataset schema digest")
    if not isinstance(expected["cases"], list) or not expected["cases"] or len(expected["cases"]) > 10000:
        raise ReleaseError("Expected cases must contain 1 to 10000 cases.")
    names, traces = set(), set()
    for case in expected["cases"]:
        object_fields(case, ("id", "trace_id", "input_sha256", "expected_output_sha256", "output_sha256", "input_storage", "expected_output_storage", "output_storage", "scores"), where="expected case")
        name = text(case["id"], "case ID")
        trace = case["trace_id"]
        if name in names or not isinstance(trace, str) or not re.fullmatch(r"[0-9a-f]{32}", trace) or trace in traces:
            raise ReleaseError("Duplicate case or invalid/duplicate trace ID.")
        names.add(name); traces.add(trace)
        for key in ("input_sha256", "expected_output_sha256", "output_sha256"):
            digest(case[key], key)
        for key in ("input_storage", "expected_output_storage", "output_storage"):
            if case[key] not in ("null", "text", "json"):
                raise ReleaseError("Expected IO storage kind is invalid.")
        _validate_expected_scores(case["scores"], nonempty=True)
    _validate_expected_scores(expected["run_scores"])
    return expected


def _validate_expected_scores(scores, *, nonempty=False):
    if not isinstance(scores, list) or (nonempty and not scores):
        raise ReleaseError("Expected scores must be an array with required case scores.")
    seen = set()
    for score in scores:
        object_fields(score, ("name", "data_type", "value"), where="expected score")
        name = text(score["name"], "score name")
        if name in seen:
            raise ReleaseError("Duplicate expected score.")
        seen.add(name)
        if score["data_type"] == "BOOLEAN" and type(score["value"]) is not bool:
            raise ReleaseError("Expected BOOLEAN score must be an actual boolean.")
        _typed(score["value"], score["data_type"], "expected score")


def _get(client, path, query=None):
    try:
        response = client.get_json(path, query)
        fingerprint(response)
        return response
    except ReleaseError as exc:
        message = str(exc)
        status = re.search(r"HTTP (\d{3})", message)
        if "transport failed" in message or (status and (int(status[1]) == 429 or int(status[1]) >= 500)):
            raise _Pending(message) from None
        raise


def _pages(client, path, query, max_pages, page_size):
    items, cursor, seen_cursors, ids = [], None, set(), set()
    for _ in range(max_pages):
        request = {**query, "limit": page_size}
        if cursor is not None:
            request["cursor"] = cursor
        response = _get(client, path, request)
        if not isinstance(response, dict) or not isinstance(response.get("data"), list) or not isinstance(response.get("meta"), dict):
            raise ReleaseError("Remote pagination is missing data or metadata.")
        allowed_meta = {"cursor", "limit"} if path == "/api/public/v3/scores" else {"cursor"}
        if set(response["meta"]) - allowed_meta or len(response["data"]) > page_size:
            raise ReleaseError("Unsupported or inconsistent cursor pagination metadata.")
        if path == "/api/public/v3/scores" and (type(response["meta"].get("limit")) is not int or response["meta"]["limit"] != page_size):
            raise ReleaseError("Score cursor pagination limit is missing or inconsistent.")
        for row in response["data"]:
            if not isinstance(row, dict):
                raise ReleaseError("Remote response contains a malformed row.")
            identity = _id(row.get("id"), "remote row ID")
            if identity in ids:
                raise ReleaseError("Remote pagination repeated a row; a complete stable export is not established.")
            ids.add(identity); items.append(row)
        cursor = response["meta"].get("cursor")
        if cursor is None:
            return items
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors or not response["data"]:
            raise ReleaseError("Remote pagination cursor is empty, repeated or incomplete.")
        seen_cursors.add(cursor)
    raise ReleaseError("Remote pagination exceeds max_pages; refusing a partial export.")


def _score_evidence(rows, expected_scores, project_id, subject_kind, subject_id, trace_id=None):
    wanted = {score["name"]: score for score in expected_scores}
    found = {}
    for row in rows:
        if row.get("projectId") != project_id:
            raise ReleaseError("A score belongs to a different project.")
        subject = row.get("subject")
        if not isinstance(subject, dict) or subject.get("kind") != subject_kind or subject.get("id") != subject_id:
            raise ReleaseError("A score is attached to the wrong experiment or observation.")
        if trace_id is not None and subject.get("traceId") != trace_id:
            raise ReleaseError("A score belongs to another trace.")
        if row.get("name") not in wanted:
            continue  # Other annotations are outside this required-score receipt.
        name = row["name"]
        if name in found:
            raise ReleaseError("Duplicate required score names are ambiguous; no value was chosen automatically.")
        intended = wanted[name]
        if row.get("dataType") != intended["data_type"]:
            raise ReleaseError(f"Persisted score {name} dataType differs from local typed evidence.")
        actual = _typed(row.get("value"), row["dataType"], "remote score")
        if (type(actual) is bool) != (type(intended["value"]) is bool):
            raise ReleaseError("Persisted score boolean/numeric type mismatch.")
        if actual != intended["value"]:
            raise ReleaseError(f"Persisted score {name} differs from the actual local evaluation.")
        if row.get("source") != "API":
            raise ReleaseError("Required score was not recorded through the SDK/API score path.")
        found[name] = {"id": row["id"], "name": name, "data_type": row["dataType"],
                       "value": actual, "project_id": project_id,
                       "subject": copy.deepcopy(subject)}
    if set(found) != set(wanted):
        raise _Pending("Required scores have not appeared for " + subject_id + ": " + ", ".join(sorted(set(wanted) - set(found))) + ". Flush or sampling does not establish persistence.")
    return [found[name] for name in sorted(found)]


def _inspect(client, expected, max_pages, page_size):
    projects = _get(client, "/api/public/projects")
    if not isinstance(projects, dict) or not isinstance(projects.get("data"), list) or len(projects["data"]) != 1:
        raise ReleaseError("Use one project-scoped API key; project identity is ambiguous.")
    if not isinstance(projects["data"][0], dict) or projects["data"][0].get("id") != expected["project_id"]:
        raise ReleaseError("Connected API key belongs to a different independently expected project.")
    dataset = expected["dataset"]
    remote_dataset = None
    if dataset["kind"] == "langfuse":
        actual_dataset = _get(client, "/api/public/v2/datasets/" + quote(dataset["name"], safe=""))
        if not isinstance(actual_dataset, dict) or actual_dataset.get("id") != dataset["id"] or actual_dataset.get("name") != dataset["name"]:
            raise ReleaseError("Remote dataset ID/name differs from the captured dataset.")
        remote_dataset = {"id": actual_dataset["id"], "name": actual_dataset["name"]}
        if "schema_sha256" in dataset:
            if "inputSchema" not in actual_dataset or "expectedOutputSchema" not in actual_dataset:
                raise ReleaseError("Remote dataset omitted pinned schema evidence.")
            schema_hash = fingerprint({key: actual_dataset[key] for key in ("inputSchema", "expectedOutputSchema")})
            if schema_hash != dataset["schema_sha256"]:
                raise ReleaseError("Remote dataset schema differs from the independently pinned schema.")
            remote_dataset["schema_sha256"] = schema_hash
    start = _utc(expected["started_at_utc"], "start") - timedelta(seconds=1)
    end = _utc(expected["completed_at_utc"], "completion") + timedelta(seconds=1)
    window = {"fromStartTime": start.isoformat(), "toStartTime": end.isoformat()}
    query = {**window, "id": expected["experiment_id"], "fields": "core"}
    summaries = _pages(client, "/api/public/experiments", query, max_pages, page_size)
    if not summaries:
        raise _Pending("The experiment has not appeared in the queried project/time window.")
    if len(summaries) != 1:
        raise ReleaseError("The experiment identity is ambiguous.")
    summary = summaries[0]
    expected_dataset_id = dataset["id"] if dataset["kind"] == "langfuse" else None
    if (summary.get("id") != expected["experiment_id"] or summary.get("name") != expected["run_name"]
            or "datasetId" not in summary or summary["datasetId"] != expected_dataset_id):
        raise ReleaseError("Remote experiment ID/run name/dataset differs from local evidence.")
    count = summary.get("itemCount")
    if type(count) is not int or count < 0:
        raise ReleaseError("Experiment itemCount is invalid.")
    if count < len(expected["cases"]):
        raise _Pending("The experiment summary is missing expected cases.")
    if count > len(expected["cases"]):
        raise ReleaseError("Experiment contains unexpected repeated/additional cases in the captured run window.")
    rows = _pages(client, "/api/public/experiment-items",
                  {**window, "experimentId": expected["experiment_id"], "fields": "core,dataset,io"}, max_pages, page_size)
    if len(rows) < len(expected["cases"]):
        raise _Pending("Experiment item pages are incomplete for the expected cases.")
    if len(rows) > len(expected["cases"]):
        raise ReleaseError("Experiment item pages contain unexpected extra cases.")
    by_trace = {case["trace_id"]: case for case in expected["cases"]}
    observed, seen = [], set()
    for row in rows:
        trace_id = row.get("traceId")
        if trace_id not in by_trace or trace_id in seen:
            raise ReleaseError("Remote case trace is unexpected or duplicated.")
        seen.add(trace_id)
        case = by_trace[trace_id]
        if (row.get("experimentId") != expected["experiment_id"] or row.get("experimentName") != expected["run_name"]
                or "experimentDatasetId" not in row or row["experimentDatasetId"] != expected_dataset_id):
            raise ReleaseError("Experiment item identity or dataset differs from expected evidence.")
        _id(row.get("experimentItemId"), "experiment item ID")
        if "experimentItemVersion" not in row:
            raise ReleaseError("Experiment item omitted requested dataset version evidence.")
        if dataset["kind"] == "langfuse":
            if row["experimentItemId"] != case["id"] or _utc(row["experimentItemVersion"], "remote dataset version") != _utc(dataset["version"], "expected dataset version"):
                raise ReleaseError("Remote dataset case ID/version differs from execution capture.")
        elif row["experimentItemVersion"] is not None:
            raise ReleaseError("Local experiment unexpectedly claims a remote dataset version.")
        if row.get("level") == "ERROR":
            raise ReleaseError("Remote experiment item reports an execution error.")
        if row.get("level") not in ("DEBUG", "DEFAULT", "WARNING"):
            raise ReleaseError("Remote experiment item has an unknown level.")
        row_start = _utc(row.get("startTime"), "remote item start")
        if not start <= row_start < end:
            raise ReleaseError("Remote experiment item is outside the captured execution window.")
        if row.get("endTime") is None:
            raise _Pending("Remote experiment item has not recorded completion.")
        row_end = _utc(row["endTime"], "remote item end")
        if row_end < row_start or row_end > end:
            raise ReleaseError("Remote experiment item completion is outside the captured execution window.")
        for remote_key, expected_key in (("input", "input_sha256"), ("output", "output_sha256"), ("expectedOutput", "expected_output_sha256")):
            if remote_key not in row:
                raise _Pending("Remote item is missing an expected input/output field.")
            storage_key = expected_key.replace("_sha256", "_storage")
            if _stored_io_hash(row[remote_key], case[storage_key]) != case[expected_key]:
                raise ReleaseError("Persisted case input/output differs from the evaluated local artifact.")
        scores = _pages(client, "/api/public/v3/scores", {"observationId": row["id"], "traceId": trace_id, "fields": "subject"}, max_pages, page_size)
        score_evidence = _score_evidence(scores, case["scores"], expected["project_id"], "observation", row["id"], trace_id)
        observed.append({"id": case["id"], "trace_id": trace_id, "observation_id": row["id"],
                         "experiment_item_id": row["experimentItemId"], "dataset_id": row["experimentDatasetId"],
                         "dataset_version": row["experimentItemVersion"],
                         "input_sha256": case["input_sha256"], "expected_output_sha256": case["expected_output_sha256"],
                         "output_sha256": case["output_sha256"],
                         "input_storage": case["input_storage"], "output_storage": case["output_storage"],
                         "expected_output_storage": case["expected_output_storage"], "scores": score_evidence})
    run_scores = []
    if expected["run_scores"]:
        scores = _pages(client, "/api/public/v3/scores", {"experimentId": expected["experiment_id"], "fields": "subject"}, max_pages, page_size)
        run_scores = _score_evidence(scores, expected["run_scores"], expected["project_id"], "experiment", expected["experiment_id"])
    again = _pages(client, "/api/public/experiments", query, max_pages, page_size)
    if fingerprint(again) != fingerprint(summaries):
        raise _Pending("Experiment summary changed during pagination/score reads; inspect again.")
    return {"observed_cases": sorted(observed, key=lambda row: row["id"]), "observed_run_scores": run_scores,
            "remote_dataset": remote_dataset, "remote_experiment": {"id": summary["id"], "name": summary["name"],
            "dataset_id": summary["datasetId"], "item_count": count},
            "dataset_version_evidence": "experimentItemVersion" if dataset["kind"] == "langfuse" else "local producer identity; remote association is null"}


def verify_remote(client, expectation, *, wait_seconds=60, poll_interval_seconds=2, max_pages=100,
                  page_size=100, report_path=None):
    """Read until complete, permanently failed, or the bounded polling window ends.

    Each request also has the Client timeout. A request already in flight can
    finish after the polling deadline; there are no unbounded retries.
    """
    expected = copy.deepcopy(_validate_expectation(expectation))
    _number(wait_seconds, "wait_seconds")
    _number(poll_interval_seconds, "poll_interval_seconds", minimum=0.01, maximum=60)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise ReleaseError("max_pages must be an integer between 1 and 1000.")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ReleaseError("page_size must be an integer between 1 and 100.")
    if client.host != expected["host"]:
        raise ReleaseError("Remote verification client host differs from the independently expected host.")
    if _utc(expected["completed_at_utc"], "completion") > datetime.now(timezone.utc):
        raise ReleaseError("Execution capture completion is in the future.")
    report = {"format_version": 1, "mode": "remote_experiment_persistence", "status": "pending",
              "host": expected["host"], "project_id": expected["project_id"], "experiment_id": expected["experiment_id"],
              "expectation_sha256": fingerprint(expected), "source_results_sha256": expected["source_results_sha256"],
              "execution_capture_sha256": expected["execution_capture_sha256"], "policy_sha256": expected["policy_sha256"],
              "attempts": 0, "issues": [], "observed_cases": [], "observed_run_scores": [],
              "scope": _scope(expected),
              "wait_seconds": wait_seconds, "poll_interval_seconds": poll_interval_seconds,
              "max_pages": max_pages, "page_size": page_size}
    path = None
    if report_path is not None:
        candidate = Path(report_path)
        if candidate.suffix.lower() != ".json":
            raise ReleaseError("Remote receipt must be a new .json file.")
        path = inside(candidate.parent, candidate.name)
        write_report(path, report, create=True)  # Reserve before GETs; no source can be overwritten.
    deadline = time.monotonic() + wait_seconds
    while True:
        report["attempts"] += 1
        try:
            report.update(_inspect(client, expected, max_pages, page_size))
            report["status"], report["issues"] = "confirmed", []
        except _Pending as exc:
            report["status"], report["issues"] = "pending", [str(exc)]
        except (ReleaseError, OSError, ValueError) as exc:
            report["status"], report["issues"] = "failed", [str(exc)]
        report["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
        report.pop("evidence_sha256", None)
        report["evidence_sha256"] = fingerprint(report)
        if path is not None:
            write_report(path, report)
        remaining = deadline - time.monotonic()
        if report["status"] != "pending" or remaining <= 0:
            return report
        time.sleep(min(poll_interval_seconds, remaining))


def validate_remote_receipt(receipt, expectation, *, max_age_seconds=300, now=None):
    """Validate saved evidence and binding. This is not a signature verifier."""
    expected = _validate_expectation(expectation)
    _number(max_age_seconds, "max_age_seconds", minimum=0.01, maximum=86400)
    if not isinstance(receipt, dict) or type(receipt.get("format_version")) is not int or receipt["format_version"] != 1 or receipt.get("mode") != "remote_experiment_persistence" or receipt.get("status") != "confirmed":
        raise ReleaseError("A confirmed remote persistence receipt is required; a caller's flush/confirmed statement is insufficient.")
    if receipt.get("evidence_sha256") != fingerprint({k: v for k, v in receipt.items() if k != "evidence_sha256"}):
        raise ReleaseError("Remote receipt changed since verification.")
    for key in ("host", "project_id", "experiment_id", "source_results_sha256", "execution_capture_sha256", "policy_sha256"):
        if receipt.get(key) != expected[key]:
            raise ReleaseError("Remote persistence receipt belongs to different execution evidence or project.")
    if receipt.get("scope") != _scope(expected) or type(receipt.get("attempts")) is not int or receipt["attempts"] < 1:
        raise ReleaseError("Remote receipt verification scope or attempt count is invalid.")
    if receipt.get("expectation_sha256") != fingerprint(expected) or receipt.get("issues") != []:
        raise ReleaseError("Remote receipt expectation binding or findings are invalid.")
    checked = _utc(receipt.get("checked_at_utc"), "receipt check time")
    current = datetime.now(timezone.utc) if now is None else _utc(now, "now")
    if checked < _utc(expected["completed_at_utc"], "execution completion"):
        raise ReleaseError("Remote receipt predates local execution completion.")
    if not 0 <= (current - checked).total_seconds() <= max_age_seconds:
        raise ReleaseError("Remote persistence receipt is stale or dated in the future.")
    observed = receipt.get("observed_cases")
    if not isinstance(observed, list) or len(observed) != len(expected["cases"]):
        raise ReleaseError("Remote receipt is missing verified cases.")
    by_case = {case["id"]: case for case in expected["cases"]}
    seen, observations, score_ids = set(), set(), set()
    for row in observed:
        if not isinstance(row, dict) or row.get("id") not in by_case or row["id"] in seen:
            raise ReleaseError("Remote receipt has unexpected or duplicate cases.")
        seen.add(row["id"]); case = by_case[row["id"]]
        observation = _id(row.get("observation_id"), "observed observation ID")
        if observation in observations:
            raise ReleaseError("Remote receipt repeats an observation.")
        observations.add(observation)
        for field in ("trace_id", "input_sha256", "expected_output_sha256", "output_sha256", "input_storage", "expected_output_storage", "output_storage"):
            if row.get(field) != case[field]:
                raise ReleaseError("Remote receipt case differs from expected execution evidence.")
        dataset = expected["dataset"]
        if row.get("dataset_id") != (dataset["id"] if dataset["kind"] == "langfuse" else None):
            raise ReleaseError("Remote receipt dataset association differs.")
        if dataset["kind"] == "langfuse":
            if row.get("experiment_item_id") != case["id"] or _utc(row.get("dataset_version"), "dataset version") != _utc(dataset["version"], "expected dataset version"):
                raise ReleaseError("Remote receipt dataset version/case differs.")
        elif row.get("dataset_version") is not None:
            raise ReleaseError("Local receipt cannot claim a remote dataset version.")
        _validate_saved_scores(row.get("scores"), case["scores"], expected["project_id"], "observation", observation, score_ids, case["trace_id"])
    _validate_saved_scores(receipt.get("observed_run_scores"), expected["run_scores"], expected["project_id"], "experiment", expected["experiment_id"], score_ids)
    summary = receipt.get("remote_experiment")
    if not isinstance(summary, dict) or type(summary.get("item_count")) is not int or summary != {"id": expected["experiment_id"], "name": expected["run_name"], "dataset_id": expected["dataset"]["id"] if expected["dataset"]["kind"] == "langfuse" else None, "item_count": len(expected["cases"])}:
        raise ReleaseError("Remote receipt experiment summary differs from expected coverage.")
    dataset = expected["dataset"]
    remote_dataset = {key: dataset[key] for key in ("id", "name", "schema_sha256") if key in dataset} if dataset["kind"] == "langfuse" else None
    if receipt.get("remote_dataset") != remote_dataset:
        raise ReleaseError("Remote receipt dataset identity/schema differs from the expected dataset.")
    return copy.deepcopy(receipt)


def _validate_saved_scores(rows, expected, project, kind, subject_id, score_ids, trace_id=None):
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ReleaseError("Remote receipt required score coverage is incomplete.")
    reconstructed = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseError("Remote receipt score is malformed.")
        _id(row.get("id"), "score ID")
        if row["id"] in score_ids:
            raise ReleaseError("Remote receipt repeats a score ID.")
        score_ids.add(row["id"])
        reconstructed.append({"id": row["id"], "name": row.get("name"), "value": row.get("value"),
                              "dataType": row.get("data_type"), "projectId": row.get("project_id"),
                              "source": "API", "subject": row.get("subject")})
    _score_evidence(reconstructed, expected, project, kind, subject_id, trace_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--policy", required=True, help="Plain independently reviewed policy JSON.")
    parser.add_argument("--dataset-kind", choices=("local", "langfuse"), required=True)
    parser.add_argument("--report", required=True, help="New private JSON receipt; never overwrites an existing file.")
    parser.add_argument("--wait-seconds", type=float, default=60)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    try:
        _, source = read_object(args.results, "experiment results")
        _, capture = read_object(args.capture, "execution capture")
        _, policy = read_object(args.policy, "evaluation policy")
        expected = make_remote_expectation(source, capture, policy, host=args.host,
                                           project_id=args.project_id, dataset_kind=args.dataset_kind)
        receipt = verify_remote(RemoteClient(args.host, args.timeout), expected, wait_seconds=args.wait_seconds,
                                poll_interval_seconds=args.poll_interval_seconds, max_pages=args.max_pages,
                                report_path=args.report)
        print(f"Remote persistence: {receipt['status']}; {len(receipt['observed_cases'])} complete cases.")
        if receipt["status"] != "confirmed":
            parser.exit(2 if receipt["status"] == "pending" else 1)
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Remote verification stopped: {exc}\n")


if __name__ == "__main__":
    main()
