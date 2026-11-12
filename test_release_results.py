"""Synthetic SDK projections; no network, model calls or invented passing gates."""
import json
import unittest
from unittest.mock import patch

from langfuse_release_common import ReleaseError
from langfuse_release_results import project_python_result, project_javascript_result


def python_result():
    return {"experiment_id": "actual-experiment", "run_name": "actual-run", "dataset_run_id": "actual-dataset-run",
            "experiment_url": "https://langfuse.example/project/demo/experiments/actual-experiment",
            "item_results": [{"item": {"id": "actual-case", "input": {"x": "café"}, "expected_output": False},
                              "output": False, "trace_id": "actual-trace", "dataset_run_id": "actual-dataset-run",
                              "evaluations": [{"name": "gate", "value": False, "data_type": "BOOLEAN",
                                               "comment": "actual failure", "metadata": {"x": [0.2, "漢字"]}, "config_id": "actual-config"}]}],
            "run_evaluations": [{"name": "run-score", "value": 0.25, "data_type": "NUMERIC"},
                                {"name": "category", "value": "needs-work", "data_type": "CATEGORICAL"}]}


def javascript_result():
    value = python_result()
    rename = {"experiment_id": "experimentId", "run_name": "runName", "dataset_run_id": "datasetRunId",
              "experiment_url": "experimentUrl", "item_results": "itemResults", "run_evaluations": "runEvaluations",
              "trace_id": "traceId", "expected_output": "expectedOutput", "data_type": "dataType", "config_id": "configId"}
    def convert(row):
        if isinstance(row, dict):
            return {rename.get(key, key): convert(val) for key, val in row.items()}
        return [convert(val) for val in row] if isinstance(row, list) else row
    result = convert(value)
    result["itemResults"][0]["evaluations"][0]["value"] = 0
    return result


class ResultProjectionTests(unittest.TestCase):
    def project(self, value=None, **options):
        return project_python_result(value if value is not None else python_result(),
                                     script_path="checks/task.py", duration_ms=12, **options)

    def test_preserves_actual_ids_typed_failing_scores_and_metadata(self):
        artifact = self.project()
        script = artifact["results"][0]
        self.assertEqual("passed", script["status"])
        result = script["experiment_result"]
        self.assertEqual("actual-experiment", result["experiment_id"])
        self.assertEqual("actual-run", result["run_name"])
        self.assertEqual("actual-dataset-run", result["dataset_run_id"])
        row = result["item_results"][0]
        self.assertEqual("actual-case", row["item"]["id"])
        self.assertEqual("actual-trace", row["trace_id"])
        self.assertEqual("actual-dataset-run", row["dataset_run_id"])
        self.assertIs(row["evaluations"][0]["value"], False)
        self.assertEqual(python_result()["item_results"][0]["evaluations"], row["evaluations"])
        self.assertEqual(python_result()["run_evaluations"], result["run_evaluations"])

    def test_js_projection_matches_python_typed_contract(self):
        javascript = project_javascript_result(javascript_result(), script_path="checks/task.js", duration_ms=12)
        self.assertEqual(self.project()["results"][0]["experiment_result"], javascript["results"][0]["experiment_result"])
        self.assertEqual("node", javascript["results"][0]["runtime"])

    def test_result_and_nested_values_are_detached(self):
        source = python_result()
        artifact = self.project(source)
        source["item_results"][0]["evaluations"][0]["metadata"]["x"].append("changed")
        self.assertEqual([0.2, "漢字"], artifact["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["metadata"]["x"])

    def test_preserves_absent_remote_ids_without_inventing_them(self):
        result = python_result()
        result["dataset_run_id"] = None
        result["experiment_url"] = None
        for row in result["item_results"]:
            row["trace_id"] = row["dataset_run_id"] = None
        projected = self.project(result)["results"][0]
        self.assertNotIn("dataset_run_id", projected["experiment_result"])
        self.assertNotIn("trace_id", projected["experiment_result"]["item_results"][0])
        self.assertIsNone(projected["langfuse_experiment_url"])

    def test_missing_ids_empty_cases_and_duplicate_ids_are_rejected(self):
        for field in ("experiment_id", "run_name", "item_results", "run_evaluations"):
            result = python_result(); del result[field]
            with self.subTest(field=field), self.assertRaises(ReleaseError):
                self.project(result)
        for kind in ("missing-case", "empty", "duplicate"):
            result = python_result()
            if kind == "missing-case": del result["item_results"][0]["item"]["id"]
            elif kind == "empty": result["item_results"] = []
            else: result["item_results"] *= 2
            with self.subTest(kind=kind), self.assertRaises(ReleaseError):
                self.project(result)

    def test_invalid_typed_scores_do_not_get_truthiness_conversion(self):
        for value in (2, -1, "0", True, None):
            result = javascript_result(); result["itemResults"][0]["evaluations"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                project_javascript_result(result, script_path="task.js", duration_ms=0)
        for data_type, value in (("BOOLEAN", 1), ("NUMERIC", True), ("CATEGORICAL", 1), ("unknown", 1), (None, "0"), ("NUMERIC", float("nan"))):
            result = python_result(); result["item_results"][0]["evaluations"][0].update(data_type=data_type, value=value)
            with self.subTest(data_type=data_type, value=value), self.assertRaises(ReleaseError):
                self.project(result)

    def test_score_duplicates_and_non_json_outputs_use_common_validator(self):
        result = python_result(); result["run_evaluations"] *= 2
        with self.assertRaises(ReleaseError): self.project(result)
        result = python_result(); result["item_results"][0]["output"] = object()
        with self.assertRaises(ReleaseError): self.project(result)
        with self.assertRaises(ReleaseError): self.project(metadata={"not-a-string": {}})
        with self.assertRaises(ReleaseError): self.project(status="success")

    def test_byte_bound_is_enforced(self):
        with patch("langfuse_release_results.MAX_RESULT_BYTES", 100):
            with self.assertRaisesRegex(ReleaseError, "8 MB"): self.project()

    def test_javascript_expected_output_alias_collision_is_rejected(self):
        result = javascript_result(); result["itemResults"][0]["item"]["expected_output"] = False
        with self.assertRaisesRegex(ReleaseError, "ambiguous"):
            project_javascript_result(result, script_path="task.js", duration_ms=0)

    def test_real_official_python_sdk_result_and_hosted_item_objects(self):
        try:
            from langfuse.experiment import ExperimentResult, ExperimentItemResult, Evaluation
            from langfuse.api.commons.types.dataset_item import DatasetItem
        except ImportError:
            self.skipTest("Optional official Python SDK is not installed")
        item = DatasetItem(id="hosted-case", status="ACTIVE", input={"x": 1}, expected_output=False,
                           metadata={"review": "synthetic"}, dataset_id="hosted-dataset", dataset_name="synthetic",
                           created_at="2026-11-11T00:00:00Z", updated_at="2026-11-11T00:00:00Z", media_references=[])
        score = Evaluation(name="gate", value=False, data_type="BOOLEAN", config_id="actual-config")
        row = ExperimentItemResult(item=item, output=False, evaluations=[score], trace_id="actual-trace", dataset_run_id="actual-run")
        result = ExperimentResult(name="synthetic", run_name="actual-run", description=None, item_results=[row],
                                  run_evaluations=[score], experiment_id="actual-run", dataset_run_id="actual-run")
        artifact = self.project(result)["results"][0]["experiment_result"]
        self.assertEqual("hosted-dataset", artifact["item_results"][0]["item"]["datasetId"])
        self.assertIs(artifact["item_results"][0]["item"]["expected_output"], False)
        self.assertEqual("actual-run", artifact["dataset_run_id"])
        self.assertIs(artifact["run_evaluations"][0]["value"], False)
        json.dumps(artifact, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
