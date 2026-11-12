"""Offline hosted-demo contracts; live SDK/server acceptance is a separate run."""
import copy
from datetime import datetime, timezone
import json
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import patch

from examples import hosted_dataset_demo as demo
from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_remote import make_remote_expectation, verify_remote, validate_remote_receipt
from test_release_remote import END, HOST, PROJECT, START, PersistenceHTTP


def dataset():
    return Obj(id="actual-hosted-dataset-id", name="hosted/synthetic-contract", project_id=PROJECT,
               version=datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
               input_schema=copy.deepcopy(demo.SCHEMA), expected_output_schema=copy.deepcopy(demo.SCHEMA),
               items=[Obj(id="server-item-" + str(i), input={"answer": value}, expected_output={"answer": value})
                      for i, value in enumerate(demo.ANSWERS)])


def result(data):
    scores = [Obj(name="answer-exact", value=True, data_type="BOOLEAN"),
              Obj(name="answer-quality", value=1.0, data_type="NUMERIC")]
    return Obj(experiment_id="sdk-actual-run-id", run_name="actual-hosted-run", experiment_url=None,
               item_results=[Obj(item=item, output=item.expected_output, trace_id=f"{i+1:032x}",
                                 experiment_id="sdk-actual-run-id", evaluations=copy.deepcopy(scores))
                             for i, item in enumerate(data.items)],
               run_evaluations=[Obj(name=demo.WITHHELD_SCORE, value=True, data_type="BOOLEAN"),
                                Obj(name="run-mean-quality", value=1.0, data_type="NUMERIC")])


class HostedDatasetContractTests(unittest.TestCase):
    def fixture(self):
        data = dataset()
        policy = demo.policy_for("prompt", data, PROJECT)
        source = demo.result_artifact(result(data), 12)
        capture = {"format_version": 1, "mode": "langfuse_execution_capture", "result_sha256": fingerprint(source),
                   "policy_sha256": fingerprint(policy), "started_at_utc": START, "completed_at_utc": END,
                   "completion": {"local": "completed", "remote_export": "unconfirmed"},
                   "dataset": demo.dataset_identity(data, PROJECT), "execution": policy["execution"],
                   "items": [{"id": item.id, "prompts": []} for item in data.items]}
        expected = make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT, dataset_kind="langfuse")
        backend = PersistenceHTTP(expected)
        backend.dataset.update(id=data.id, name=data.name, inputSchema=data.input_schema,
                               expectedOutputSchema=data.expected_output_schema)
        cases = {item.id: item for item in data.items}
        for row in backend.items:
            item = cases[row["experimentItemId"]]
            row.update(input=json.dumps(item.input), output=json.dumps(item.expected_output),
                       expectedOutput=json.dumps(item.expected_output))
        with patch.dict("os.environ", {"LANGFUSE_PUBLIC_KEY": "synthetic", "LANGFUSE_SECRET_KEY": "synthetic"}):
            client = demo.ObservedRemoteClient(HOST)
        client._opener = backend
        return data, policy, source, capture, expected, backend, client

    def test_host_and_explicit_project_guard_run_before_any_network_or_write(self):
        for host in ("https://cloud.langfuse.com", "http://example.com", "http://localhost/path",
                     "http://secret@localhost", "http://localhost?proxy=elsewhere", "http://localhost#other"):
            with self.subTest(host=host), self.assertRaises(ReleaseError):
                demo.validate_target(host, PROJECT)
        for project in (None, "", " ", " project "):
            with self.subTest(project=project), self.assertRaises(ReleaseError):
                demo.validate_target("http://127.0.0.1:13830", project)
        demo.validate_target("http://[::1]:13830", PROJECT)

    def test_hosted_policy_pins_real_ids_timestamp_schema_and_all_cases(self):
        data = dataset()
        policy = demo.policy_for("prompt", data, PROJECT)
        self.assertEqual("langfuse", policy["remote_verification"]["dataset_kind"])
        self.assertEqual("required", policy["remote_export"])
        self.assertEqual(data.id, policy["dataset"]["id"])
        self.assertEqual(data.version.isoformat(), policy["dataset"]["version"])
        self.assertEqual(fingerprint({"inputSchema": data.input_schema, "expectedOutputSchema": data.expected_output_schema}),
                         policy["dataset"]["schema_sha256"])
        self.assertEqual({item.id for item in data.items}, {row["id"] for row in policy["dataset"]["cases"]})

    def test_no_implicit_latest_version_or_wrong_project_allowed(self):
        for stamp in (None, "latest", datetime(2026, 1, 1)):
            data = dataset()
            data.version = stamp
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ReleaseError, "explicit timezone-aware"):
                demo.dataset_identity(data, PROJECT)
        with self.assertRaisesRegex(ReleaseError, "different project"):
            demo.dataset_identity(dataset(), "other-project")

    def test_changed_hosted_identity_version_schema_or_contents_reject_before_execution(self):
        policy = demo.policy_for("prompt", dataset(), PROJECT)
        for field in ("id", "name", "version", "schema", "input", "expected", "missing", "duplicate"):
            data = dataset()
            if field in ("id", "name"): setattr(data, field, "changed")
            elif field == "version": data.version = datetime(2026, 1, 2, tzinfo=timezone.utc)
            elif field == "schema": data.input_schema["description"] = "Changed contract"
            elif field == "input": data.items[0].input = {"answer": "changed"}
            elif field == "expected": data.items[0].expected_output = {"answer": "changed"}
            elif field == "missing": data.items.pop()
            else: data.items.append(copy.deepcopy(data.items[0]))
            with self.subTest(field=field), self.assertRaisesRegex(ReleaseError, "reviewed policy"):
                demo.assert_reviewed_dataset(data, policy, PROJECT)
        same = dataset()
        same.items.reverse()
        demo.assert_reviewed_dataset(same, policy, PROJECT)

    def test_sdk_hosted_objects_project_to_complete_typed_action_evidence(self):
        data, _, source, _, expected, _, _ = self.fixture()
        experiment = source["results"][0]["experiment_result"]
        self.assertEqual([demo.item_value(row) for row in data.items], [row["item"] for row in experiment["item_results"]])
        self.assertEqual({"BOOLEAN", "NUMERIC"}, {row["data_type"] for row in expected["run_scores"]})
        self.assertIs(type(expected["run_scores"][0]["value"]), bool)
        self.assertIs(type(expected["run_scores"][1]["value"]), float)
        self.assertEqual(3, len(expected["cases"]))
        json.dumps(source, allow_nan=False)

    def test_wrong_item_run_cannot_be_projected_as_the_captured_run(self):
        value = result(dataset())
        value.item_results[0].experiment_id = "another-run"
        with self.assertRaisesRegex(ReleaseError, "different experiment"):
            demo.result_artifact(value, 1)

    def test_score_fault_injection_preserves_unrelated_writes_and_restores_on_error(self):
        sent = []
        sdk = Obj(create_score=lambda **kwargs: sent.append(kwargs))
        original = sdk.create_score
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with demo.withhold_run_score(sdk) as withheld:
                sdk.create_score(dataset_run_id="run", name=demo.WITHHELD_SCORE, value=True)
                sdk.create_score(dataset_run_id="run", name="run-mean-quality", value=1.0)
                sdk.create_score(observation_id="item", name=demo.WITHHELD_SCORE, value=True)
                raise RuntimeError("injected")
        self.assertEqual([demo.WITHHELD_SCORE], withheld)
        self.assertEqual(2, len(sent))
        self.assertIs(original, sdk.create_score)

    def test_demo_evidence_verifies_both_score_scopes_with_pagination(self):
        _, _, _, _, expected, _, client = self.fixture()
        report = verify_remote(client, expected, wait_seconds=0, page_size=1, max_pages=10)
        self.assertEqual("confirmed", report["status"], report["issues"])
        self.assertEqual(expected["dataset"]["schema_sha256"], report["remote_dataset"]["schema_sha256"])
        self.assertEqual(3, len(report["observed_cases"]))
        self.assertEqual(2, len(report["observed_run_scores"]))
        self.assertTrue(any(row["cursor_request"] and row["run_scores"] for row in client.pages))
        self.assertTrue(any(row["cursor_request"] and row["observation_scores"] for row in client.pages))
        validate_remote_receipt(report, expected)

    def test_missing_run_export_is_pending_and_cannot_validate_as_complete(self):
        _, _, _, _, expected, backend, client = self.fixture()
        backend.run_scores = [row for row in backend.run_scores if row["name"] != demo.WITHHELD_SCORE]
        report = verify_remote(client, expected, wait_seconds=0, page_size=1, max_pages=10)
        self.assertEqual("pending", report["status"])
        self.assertIn(demo.WITHHELD_SCORE, report["issues"][0])
        self.assertEqual([], report["observed_cases"])
        with self.assertRaisesRegex(ReleaseError, "confirmed remote persistence"):
            validate_remote_receipt(report, expected)

    def test_live_dataset_schema_wrong_run_and_version_probes_fail(self):
        for change in ("schema", "run", "version", "dataset"):
            _, _, _, _, expected, backend, client = self.fixture()
            if change == "schema": backend.dataset["inputSchema"]["description"] = "Changed"
            elif change == "run": expected = demo.changed_expectation(expected, run_name="actual-other-run")
            elif change == "version": expected = demo.changed_expectation(expected, dataset={**expected["dataset"], "version": "2026-01-02T11:00:00Z"})
            else:
                backend.dataset.update(id="other-hosted-id", name="other-hosted-name")
                expected = demo.changed_expectation(expected, dataset={**expected["dataset"], "id": "other-hosted-id", "name": "other-hosted-name"})
            with self.subTest(change=change):
                report = verify_remote(client, expected, wait_seconds=0, page_size=1, max_pages=10)
                self.assertEqual("failed", report["status"], report)


if __name__ == "__main__":
    unittest.main()
