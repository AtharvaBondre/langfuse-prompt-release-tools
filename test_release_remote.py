"""Offline GET-only persistence checks against pinned public API response shapes."""
import copy
import io
import json
import os
import stat
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import langfuse_release_remote as remote
from langfuse_release_common import ReleaseError, fingerprint
import test_prompt_release as base


HOST = "https://example.com"
PROJECT = "expected-project"
START = "2026-01-01T12:00:00+00:00"
END = "2026-01-01T12:00:02+00:00"


def policy_and_capture(kind="local", case_count=2, run_score=False):
    cases = [{"id": f"case-{i}", "input_sha256": fingerprint({"input": i}),
              "expected_output_sha256": fingerprint({"answer": i})} for i in range(case_count)]
    policy = {"format_version": 1, "id": "reviewed-policy", "producer": {
        "kind": "langfuse-python-sdk", "schema_version": "v1", "script_path": "experiment.py"},
        "dataset": {"id": "dataset-id", "name": "folder/dataset", "version": "local-content-hash" if kind == "local" else "2026-01-01T11:00:00+00:00", "cases": cases},
        "execution": {"task_revision": "task-sha", "evaluator_revision": "evaluator-sha", "model": {
            "provider": "local", "name": "deterministic", "config_sha256": fingerprint({})}},
        "targets": [{"name": "prompt", "case_ids": [row["id"] for row in cases]}],
        "checks": [{"id": "correct", "kind": "boolean", "operator": "eq", "threshold": True},
                   {"id": "quality", "kind": "numeric", "operator": "gte", "threshold": 0.8}],
        "remote_export": "required"}
    source = {"schema_version": "v1", "action_metadata": {"producer": "synthetic"}, "results": [{
        "script_path": "experiment.py", "script_name": "experiment.py", "runtime": "python", "duration_ms": 5,
        "status": "passed", "error": None, "langfuse_experiment_url": None,
        "experiment_result": {"experiment_id": "experiment-123", "run_name": "independent-run-123",
            "item_results": [{"item": {"id": case["id"], "input": {"input": i}, "expected_output": {"answer": i}},
                              "output": {"answer": i}, "trace_id": f"{i+1:032x}",
                              "evaluations": [{"name": "correct", "value": True}, {"name": "quality", "value": 0.9}]}
                             for i, case in enumerate(cases)],
            "run_evaluations": [{"name": "mean", "value": 0.9}] if run_score else []}}]}
    capture = {"format_version": 1, "mode": "langfuse_execution_capture", "result_sha256": fingerprint(source),
               "policy_sha256": fingerprint(policy), "started_at_utc": START, "completed_at_utc": END,
               "completion": {"local": "completed", "remote_export": "unconfirmed"},
               "dataset": {k: policy["dataset"][k] for k in ("id", "name", "version")},
               "execution": copy.deepcopy(policy["execution"]),
               "items": [{"id": case["id"], "prompts": []} for case in cases]}
    return policy, source, capture


class PersistenceHTTP:
    def __init__(self, expected):
        self.expected = expected
        self.requests = []
        self.rounds = 0
        self.override = None
        self.projects = {"data": [{"id": PROJECT, "name": "synthetic project"}]}
        dataset = expected["dataset"]
        remote_id = dataset["id"] if dataset["kind"] == "langfuse" else None
        self.dataset = {"id": dataset["id"], "name": dataset["name"], "inputSchema": None, "expectedOutputSchema": None}
        self.summary = {"id": expected["experiment_id"], "name": expected["run_name"], "description": None,
                        "startTime": START, "endTime": END, "itemCount": len(expected["cases"]), "datasetId": remote_id}
        self.items, self.scores = [], {}
        for i, case in enumerate(expected["cases"]):
            observation_id = f"{i+1:016x}"
            self.items.append({"id": observation_id, "traceId": case["trace_id"],
                "startTime": "2026-01-01T12:00:00.500000+00:00", "endTime": "2026-01-01T12:00:01.500000+00:00",
                "level": "DEFAULT", "environment": "langfuse-evaluation", "experimentId": expected["experiment_id"],
                "experimentName": expected["run_name"], "experimentItemId": case["id"] if remote_id else f"sdk-input-hash-{i}",
                "experimentDatasetId": remote_id, "experimentItemVersion": dataset["version"] if remote_id else None,
                "input": json.dumps({"input": i}), "output": json.dumps({"answer": i}), "expectedOutput": json.dumps({"answer": i})})
            self.scores[observation_id] = [self.score(score, f"score-{i}-{j}", "observation", observation_id, case["trace_id"])
                                           for j, score in enumerate(case["scores"])]
        self.run_scores = [self.score(score, f"run-score-{i}", "experiment", expected["experiment_id"])
                           for i, score in enumerate(expected["run_scores"])]

    def score(self, score, identity, kind, subject_id, trace_id=None):
        subject = {"kind": kind, "id": subject_id}
        if trace_id:
            subject["traceId"] = trace_id
        return {"id": identity, "projectId": PROJECT, "name": score["name"], "dataType": score["data_type"],
                "value": score["value"], "subject": subject, "source": "API", "timestamp": END,
                "createdAt": END, "updatedAt": END, "environment": "langfuse-evaluation"}

    def page(self, rows, query):
        page_size = int(query["limit"])
        offset = int(query.get("cursor", "offset-0").split("-")[-1])
        selected = rows[offset:offset+page_size]
        meta = {"cursor": f"offset-{offset+page_size}"} if offset + page_size < len(rows) else {}
        return {"data": selected, "meta": meta}

    def open(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        if url.scheme != "https" or url.netloc != "example.com" or request.method != "GET" or request.data is not None:
            raise AssertionError("Only GETs to the synthetic server are allowed")
        query = {k: value[0] for k, value in urllib.parse.parse_qs(url.query).items()}
        self.requests.append({"method": request.method, "path": url.path, "query": query})
        if url.path == "/api/public/projects":
            self.rounds += 1
        if self.override:
            value = self.override(url.path, query)
            if value is not None:
                return base.FakeResponse(copy.deepcopy(value))
        if url.path == "/api/public/projects":
            value = self.projects
        elif url.path.startswith("/api/public/v2/datasets/"):
            value = self.dataset
        elif url.path == "/api/public/experiments":
            value = self.page([self.summary] if self.summary else [], query)
        elif url.path == "/api/public/experiment-items":
            value = self.page(self.items, query)
        elif url.path == "/api/public/v3/scores":
            if "observationId" in query and "traceId" not in query:
                raise AssertionError("Official v3 observationId requires traceId")
            value = self.page(self.scores.get(query.get("observationId"), []) if "observationId" in query else self.run_scores, query)
            value["meta"]["limit"] = int(query["limit"])
        else:
            raise AssertionError("Unexpected endpoint: " + url.path)
        return base.FakeResponse(copy.deepcopy(value))


class RemotePersistenceTests(unittest.TestCase):
    setUp = base.PromptReleaseTests.setUp

    def prepare(self, kind="local", case_count=2, run_score=False):
        policy, source, capture = policy_and_capture(kind, case_count, run_score)
        expected = remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT, dataset_kind=kind)
        backend = PersistenceHTTP(expected)
        api = remote.RemoteClient(HOST, timeout=1)
        api._opener = backend
        return api, backend, expected, policy, source, capture

    def verify(self, api, expected, **options):
        return remote.verify_remote(api, expected, wait_seconds=options.pop("wait_seconds", 0), **options)

    def test_complete_local_cases_and_boolean_numeric_scores_confirm_get_only(self):
        api, backend, expected, *_ = self.prepare()
        report = self.verify(api, expected)
        self.assertEqual("confirmed", report["status"], report["issues"])
        self.assertEqual(2, len(report["observed_cases"]))
        self.assertIsNone(report["remote_dataset"])
        self.assertIn("local producer identity", report["dataset_version_evidence"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))
        self.assertEqual(report, remote.validate_remote_receipt(report, expected))

    def test_local_case_id_is_not_confused_with_sdk_input_hash(self):
        api, backend, expected, *_ = self.prepare()
        self.assertNotEqual(expected["cases"][0]["id"], backend.items[0]["experimentItemId"])
        self.assertEqual("confirmed", self.verify(api, expected)["status"])

    def test_remote_dataset_id_name_item_id_and_version_are_verified(self):
        api, backend, expected, *_ = self.prepare("langfuse")
        report = self.verify(api, expected)
        self.assertEqual("confirmed", report["status"], report["issues"])
        self.assertEqual({"id": "dataset-id", "name": "folder/dataset"}, report["remote_dataset"])
        self.assertEqual("experimentItemVersion", report["dataset_version_evidence"])
        self.assertTrue(any(row["path"] == "/api/public/v2/datasets/folder%2Fdataset" for row in backend.requests))
        remote.validate_remote_receipt(report, expected)

    def test_all_cursor_pages_of_items_and_scores_are_consumed(self):
        api, backend, expected, *_ = self.prepare(case_count=3, run_score=True)
        report = self.verify(api, expected, page_size=1)
        self.assertEqual("confirmed", report["status"], report["issues"])
        self.assertEqual(3, len(report["observed_cases"]))
        self.assertEqual(1, len(report["observed_run_scores"]))
        self.assertTrue(any(row["query"].get("cursor") for row in backend.requests if row["path"] == "/api/public/v3/scores"))
        self.assertTrue(any(row["query"].get("cursor") for row in backend.requests if row["path"] == "/api/public/experiment-items"))
        remote.validate_remote_receipt(report, expected)

    def test_wrong_project_fails_before_experiment_reads_without_fallback(self):
        api, backend, expected, *_ = self.prepare()
        backend.projects["data"][0]["id"] = "other-project"
        report = self.verify(api, expected, wait_seconds=30)
        self.assertEqual("failed", report["status"])
        self.assertEqual(1, report["attempts"])
        self.assertEqual(["/api/public/projects"], [row["path"] for row in backend.requests])

    def test_empty_or_org_scoped_project_identity_is_rejected(self):
        for data in ([], [{"id": PROJECT}, {"id": "other"}]):
            api, backend, expected, *_ = self.prepare()
            backend.projects["data"] = data
            self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_wrong_client_host_rejected_without_http(self):
        api, backend, expected, *_ = self.prepare()
        api.host = "https://other.example"
        with self.assertRaises(ReleaseError):
            self.verify(api, expected)
        self.assertEqual([], backend.requests)

    def test_missing_export_or_sampled_cases_remain_pending(self):
        for missing in ("experiment", "count", "item", "scores", "completion"):
            api, backend, expected, *_ = self.prepare()
            if missing == "experiment": backend.summary = None
            elif missing == "count": backend.summary["itemCount"] = 1
            elif missing == "item": backend.items.pop()
            elif missing == "scores": backend.scores[backend.items[0]["id"]].pop()
            else: backend.items[0]["endTime"] = None
            with self.subTest(missing=missing):
                report = self.verify(api, expected)
                self.assertEqual("pending", report["status"], report)
                self.assertEqual([], report["observed_cases"])
                with self.assertRaises(ReleaseError):
                    remote.validate_remote_receipt(report, expected)

    def test_eventual_consistency_retries_complete_whole_inspection(self):
        api, backend, expected, *_ = self.prepare()
        def delayed(path, query):
            if backend.rounds == 1 and path == "/api/public/experiments":
                return {"data": [], "meta": {}}
        backend.override = delayed
        with patch.object(remote.time, "sleep"):
            result = self.verify(api, expected, wait_seconds=5, poll_interval_seconds=0.01)
        self.assertEqual("confirmed", result["status"])
        self.assertEqual(2, result["attempts"])

    def test_pending_stops_at_deadline_without_busy_retry(self):
        api, backend, expected, *_ = self.prepare()
        backend.summary = None
        with patch.object(remote.time, "monotonic", side_effect=[0, 0, 2]), patch.object(remote.time, "sleep") as sleeper:
            report = self.verify(api, expected, wait_seconds=1, poll_interval_seconds=0.5)
        self.assertEqual("pending", report["status"])
        self.assertEqual(2, report["attempts"])
        sleeper.assert_called_once_with(0.5)

    def test_transient_transport_429_and_503_are_pending(self):
        for status in (429, 503, None):
            api, backend, expected, *_ = self.prepare()
            def fail(path, query):
                if status is None:
                    raise TimeoutError("synthetic timeout")
                raise urllib.error.HTTPError(HOST, status, "omitted", {}, io.BytesIO(b"no sensitive data"))
            backend.override = fail
            with self.subTest(status=status):
                report = self.verify(api, expected)
                self.assertEqual("pending", report["status"])
                self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_auth_failure_and_unsupported_endpoint_are_failed_not_confirmed(self):
        for status in (401, 403, 404):
            api, backend, expected, *_ = self.prepare()
            def fail(path, query):
                raise urllib.error.HTTPError(HOST, status, "omitted", {}, io.BytesIO(b"no sensitive data"))
            backend.override = fail
            with self.subTest(status=status):
                report = self.verify(api, expected, wait_seconds=5)
                self.assertEqual("failed", report["status"])
                self.assertEqual(1, report["attempts"])

    def test_wrong_experiment_and_dataset_identity_fail(self):
        for field, value in (("id", "other"), ("name", "other-run"), ("datasetId", "other-dataset")):
            api, backend, expected, *_ = self.prepare()
            backend.summary[field] = value
            with self.subTest(field=field):
                self.assertEqual("failed", self.verify(api, expected)["status"])
        for field, value in (("id", "other"), ("name", "other")):
            api, backend, expected, *_ = self.prepare("langfuse")
            backend.dataset[field] = value
            self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_remote_dataset_version_and_case_mismatch_fail(self):
        for field, value in (("experimentItemVersion", "2026-01-01T11:01:00Z"), ("experimentItemId", "other-case"),
                             ("experimentDatasetId", "other")):
            api, backend, expected, *_ = self.prepare("langfuse")
            backend.items[0][field] = value
            with self.subTest(field=field):
                self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_missing_remote_version_and_local_remote_version_claim_fail(self):
        api, backend, expected, *_ = self.prepare("langfuse")
        del backend.items[0]["experimentItemVersion"]
        self.assertEqual("failed", self.verify(api, expected)["status"])
        api, backend, expected, *_ = self.prepare()
        backend.items[0]["experimentItemVersion"] = START
        self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_score_boolean_numeric_conversion_requires_explicit_boolean(self):
        api, backend, expected, *_ = self.prepare()
        correct = backend.scores[backend.items[0]["id"]][0]
        self.assertEqual("BOOLEAN", correct["dataType"])
        correct["value"] = 1
        self.assertEqual("confirmed", self.verify(api, expected)["status"])
        correct["dataType"] = "NUMERIC"
        self.assertEqual("failed", self.verify(api, expected)["status"])
        correct["dataType"] = "BOOLEAN"
        for value in ("true", "1", 2, None):
            correct["value"] = value
            with self.subTest(value=value):
                self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_wrong_score_value_source_project_and_subject_fail(self):
        changes = (("value", False), ("projectId", "other-project"), ("source", "ANNOTATION"),
                   ("subject", {"kind": "trace", "id": "wrong"}), ("dataType", "TEXT"))
        for field, value in changes:
            api, backend, expected, *_ = self.prepare()
            backend.scores[backend.items[0]["id"]][0][field] = value
            with self.subTest(field=field):
                self.assertEqual("failed", self.verify(api, expected)["status"])
        api, backend, expected, *_ = self.prepare()
        backend.scores[backend.items[0]["id"]][0]["subject"]["traceId"] = "other"
        self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_duplicate_required_score_does_not_choose_a_passing_value(self):
        api, backend, expected, *_ = self.prepare()
        duplicate = copy.deepcopy(backend.scores[backend.items[0]["id"]][0])
        duplicate.update(id="other-score", value=False)
        backend.scores[backend.items[0]["id"]].append(duplicate)
        self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_pagination_missing_meta_repeated_cursor_duplicate_rows_and_bound_fail(self):
        for mode in ("meta", "cursor", "duplicate", "bound"):
            api, backend, expected, *_ = self.prepare()
            def corrupt(path, query):
                if path == "/api/public/experiment-items":
                    if mode == "meta": return {"data": backend.items}
                    if mode == "cursor": return {"data": [backend.items[0]], "meta": {"cursor": "same"}}
                    if mode == "duplicate": return {"data": [backend.items[0], backend.items[0]], "meta": {}}
                return None
            backend.override = corrupt
            with self.subTest(mode=mode):
                result = self.verify(api, expected, page_size=1 if mode == "bound" else 100, max_pages=1 if mode == "bound" else 100)
                self.assertEqual("failed", result["status"], result)

    def test_extra_case_wrong_trace_or_content_and_failed_remote_execution_fail(self):
        for mode in ("extra", "trace", "input", "output", "error", "time"):
            api, backend, expected, *_ = self.prepare()
            if mode == "extra": backend.summary["itemCount"] += 1
            elif mode == "trace": backend.items[0]["traceId"] = "f" * 32
            elif mode in ("input", "output"): backend.items[0][mode] = {"wrong": "value"}
            elif mode == "error": backend.items[0]["level"] = "ERROR"
            else: backend.items[0]["endTime"] = "2026-01-01T12:05:00Z"
            with self.subTest(mode=mode):
                self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_changed_summary_during_reads_is_pending(self):
        api, backend, expected, *_ = self.prepare()
        reads = 0
        def changing(path, query):
            nonlocal reads
            if path == "/api/public/experiments":
                reads += 1
                if reads == 2:
                    changed = copy.deepcopy(backend.summary); changed["endTime"] = "2026-01-01T12:00:03Z"
                    return {"data": [changed], "meta": {}}
        backend.override = changing
        self.assertEqual("pending", self.verify(api, expected)["status"])

    def test_producer_flush_or_confirmed_claim_never_skips_remote_reads(self):
        api, backend, expected, policy, source, capture = self.prepare()
        capture["completion"]["remote_export"] = "confirmed"
        expected = remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT)
        backend.summary = None
        self.assertEqual("pending", self.verify(api, expected)["status"])
        self.assertGreater(len(backend.requests), 0)

    def test_missing_cases_scores_trace_or_result_binding_rejected_before_http(self):
        for change in ("case", "score", "trace", "captured_case", "result_hash", "policy"):
            api, backend, expected, policy, source, capture = self.prepare()
            if change == "case": source["results"][0]["experiment_result"]["item_results"].pop()
            elif change == "score": source["results"][0]["experiment_result"]["item_results"][0]["evaluations"].pop()
            elif change == "trace": source["results"][0]["experiment_result"]["item_results"][0]["trace_id"] = None
            elif change == "captured_case": capture["items"].pop()
            elif change == "policy": policy["checks"].pop()
            else: capture["result_sha256"] = "0" * 64
            if change != "result_hash": capture["result_sha256"] = fingerprint(source)
            with self.subTest(change=change), self.assertRaises(ReleaseError):
                remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT)
            self.assertEqual([], backend.requests)

    def test_failed_local_check_can_be_persisted_but_is_not_claimed_passed(self):
        _, _, _, policy, source, capture = self.prepare()
        source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["value"] = False
        capture["result_sha256"] = fingerprint(source)
        expected = remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT)
        api = remote.RemoteClient(HOST); backend = PersistenceHTTP(expected); api._opener = backend
        receipt = self.verify(api, expected)
        self.assertEqual("confirmed", receipt["status"])
        self.assertIs(False, receipt["observed_cases"][0]["scores"][0]["value"])
        self.assertNotIn("passed", receipt)

    def test_readonly_client_blocks_inherited_writes_and_delete(self):
        api, backend, expected, *_ = self.prepare()
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method), self.assertRaises(ReleaseError):
                api._request(method, "/api/public/scores", payload={})
        with self.assertRaises(ReleaseError):
            api._request("GET", "/api/public/projects", payload={})
        self.assertEqual([], backend.requests)

    def test_private_exclusive_report_and_output_failure_before_get(self):
        api, backend, expected, *_ = self.prepare()
        path = self.root / "receipt.json"
        receipt = self.verify(api, expected, report_path=path)
        if os.name == "posix": self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        original = path.read_bytes(); backend.requests.clear()
        with self.assertRaises(ReleaseError): self.verify(api, expected, report_path=path)
        self.assertEqual(original, path.read_bytes()); self.assertEqual([], backend.requests)
        with patch.object(remote, "write_report", side_effect=OSError("synthetic full disk")), self.assertRaises(OSError):
            self.verify(api, expected, report_path=self.root / "new.json")
        self.assertEqual([], backend.requests)
        remote.validate_remote_receipt(receipt, expected)

    def test_receipt_digest_bindings_complete_scores_and_freshness_revalidated(self):
        api, _, expected, *_ = self.prepare()
        receipt = self.verify(api, expected)
        for mode in ("status", "cases", "scores", "value", "project", "subject", "summary", "future"):
            modified = copy.deepcopy(receipt)
            if mode == "status": modified["status"] = "pending"
            elif mode == "cases": modified["observed_cases"].pop()
            elif mode == "scores": modified["observed_cases"][0]["scores"].pop()
            elif mode == "value": modified["observed_cases"][0]["scores"][0]["value"] = False
            elif mode == "project": modified["project_id"] = "other"
            elif mode == "subject": modified["observed_cases"][0]["scores"][0]["subject"]["id"] = "other"
            elif mode == "summary": modified["remote_experiment"]["item_count"] = 100
            else: modified["checked_at_utc"] = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
            modified["evidence_sha256"] = fingerprint({k: v for k, v in modified.items() if k != "evidence_sha256"})
            with self.subTest(mode=mode), self.assertRaises(ReleaseError):
                remote.validate_remote_receipt(modified, expected)
        checked = datetime.fromisoformat(receipt["checked_at_utc"])
        with self.assertRaises(ReleaseError):
            remote.validate_remote_receipt(receipt, expected, now=(checked + timedelta(seconds=301)).isoformat())

    def test_stored_json_is_decoded_once_and_plain_text_is_never_coerced(self):
        self.assertEqual(fingerprint({"answer": 1}), remote._stored_io_hash('{"answer":1}', "json"))
        self.assertEqual(fingerprint('{"answer":1}'), remote._stored_io_hash('{"answer":1}', "text"))
        self.assertEqual(fingerprint("true"), remote._stored_io_hash("true", "text"))
        self.assertEqual(fingerprint(None), remote._stored_io_hash(None, "null"))
        for value, kind in (("null", "null"), ({"answer": 1}, "json"), ('{"answer":1,"answer":2}', "json"),
                            ("NaN", "json"), ("not-json", "json")):
            with self.subTest(value=value, kind=kind), self.assertRaises(ReleaseError):
                remote._stored_io_hash(value, kind)
        self.assertNotEqual(fingerprint({"answer": 1}), remote._stored_io_hash('"{\\"answer\\":1}"', "json"))

    def test_explicit_local_score_type_must_match_policy(self):
        _, _, _, policy, source, capture = self.prepare()
        source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["data_type"] = "NUMERIC"
        capture["result_sha256"] = fingerprint(source)
        with self.assertRaisesRegex(ReleaseError, "contradicts"):
            remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT)

    def test_remote_schema_digest_is_checked_separately_from_dataset_version(self):
        api, backend, _, policy, source, capture = self.prepare("langfuse")
        schema = {"inputSchema": None, "expectedOutputSchema": None}
        policy["dataset"]["schema_sha256"] = fingerprint(schema)
        capture["dataset"]["schema_sha256"] = fingerprint(schema)
        capture["policy_sha256"] = fingerprint(policy)
        expected = remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT, dataset_kind="langfuse")
        receipt = self.verify(api, expected)
        self.assertEqual("confirmed", receipt["status"], receipt["issues"])
        remote.validate_remote_receipt(receipt, expected)
        backend.dataset["inputSchema"] = {"type": "string"}
        self.assertEqual("failed", self.verify(api, expected)["status"])
        del backend.dataset["inputSchema"]
        self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_remote_nonfinite_scores_and_malformed_json_do_not_confirm(self):
        api, backend, expected, *_ = self.prepare()
        backend.scores[backend.items[0]["id"]][1]["value"] = float("nan")
        self.assertEqual("failed", self.verify(api, expected)["status"])
        backend.override = lambda path, query: b"not json"
        self.assertEqual("failed", self.verify(api, expected)["status"])

    def test_output_disk_failure_after_reads_does_not_return_confirmed(self):
        api, backend, expected, *_ = self.prepare()
        real_write = remote.write_report
        path = self.root / "interrupted.json"
        def disk_failure(path, report, **kwargs):
            if not kwargs.get("create"):
                raise OSError("synthetic full disk after reads")
            return real_write(path, report, **kwargs)
        with patch.object(remote, "write_report", side_effect=disk_failure), self.assertRaises(OSError):
            self.verify(api, expected, report_path=path)
        self.assertEqual("pending", json.loads(path.read_text())["status"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_expectation_digest_and_limits_fail_before_get(self):
        api, backend, expected, *_ = self.prepare()
        modified = copy.deepcopy(expected); modified["project_id"] = "other"
        with self.assertRaises(ReleaseError): self.verify(api, modified)
        for option in ({"wait_seconds": float("nan")}, {"max_pages": True}, {"max_pages": 0},
                       {"page_size": 101}, {"poll_interval_seconds": 0}):
            with self.subTest(option=option), self.assertRaises(ReleaseError): self.verify(api, expected, **option)
        self.assertEqual([], backend.requests)


if __name__ == "__main__":
    unittest.main()
