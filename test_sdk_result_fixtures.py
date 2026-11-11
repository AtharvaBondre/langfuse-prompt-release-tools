"""Offline compatibility checks against recorded, synthetic official SDK runs.

The files are projections of real SDK result objects, not fabricated SDK mocks.
They establish importer compatibility, not remote persistence or CI provenance.
Only the optional upstream schema check requires the ``review`` extra.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest

import import_langfuse_experiment as adapter
from langfuse_release_common import ReleaseError

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    Draft202012Validator = FormatChecker = None


FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures" / "sdk-results"
PYTHON_RESULT = "python-sdk-5.2.0-passing.action-v1.json"
JAVASCRIPT_RESULT = "javascript-sdk-6.1.1-failing.action-v1.json"
RECORDED = {
    PYTHON_RESULT: ("langfuse-python-sdk", "python", "examples/sdk_experiment.py", True),
    JAVASCRIPT_RESULT: ("langfuse-javascript-sdk", "node", "js/sdk-experiment.mjs", False),
}
BOOLEAN_CHECK = {"id": "answer-exact", "kind": "boolean", "operator": "eq", "threshold": True}
ANSWERS = {
    "one": "A pinned prompt version is immutable.",
    "two": "A label can move between versions.",
}


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def producer_policy(name):
    """Independent minimum policy for envelope validation, not a release policy."""
    kind, _, script_path, _ = RECORDED[name]
    return {"format_version": 1, "remote_export": "optional",
            "producer": {"kind": kind, "schema_version": "v1", "script_path": script_path}}


class RecordedSdkResultTests(unittest.TestCase):
    def test_result_and_schema_bytes_match_recorded_source_digests(self):
        record = load_fixture("provenance.json")
        self.assertEqual("unsigned_local_record", record["authentication"])
        self.assertEqual(set(RECORDED), {entry["file"] for entry in record["fixtures"]})
        for entry in [*record["fixtures"], record["schema"]]:
            with self.subTest(file=entry["file"]):
                self.assertEqual(Path(entry["file"]).name, entry["file"])
                data = (FIXTURES / entry["file"]).read_bytes()
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(entry["source_sha256"], entry["sha256"])
                self.assertIs(entry["byte_identical_to_source"], True)
                if "size_bytes" in entry:
                    self.assertEqual(entry["size_bytes"], len(data))
                    self.assertEqual([], entry["redactions"])

    def test_both_actual_projections_accept_matching_producer_without_mutation(self):
        for name, (_, runtime, script_path, _) in RECORDED.items():
            with self.subTest(fixture=name):
                source = load_fixture(name)
                before = copy.deepcopy(source)
                script = adapter._action_result(source, producer_policy(name))
                self.assertEqual(runtime, script["runtime"])
                self.assertEqual(script_path, script["script_path"])
                self.assertEqual("passed", script["status"])
                self.assertIsNone(script["error"])
                self.assertEqual(before, source)

    def test_python_actual_boolean_scores_pass(self):
        script = adapter._action_result(load_fixture(PYTHON_RESULT), producer_policy(PYTHON_RESULT))
        items = script["experiment_result"]["item_results"]
        self.assertEqual(2, len(items))
        for item in items:
            self.assertEqual(1, len(item["evaluations"]))
            score = item["evaluations"][0]
            self.assertEqual("answer-exact", score["name"])
            self.assertEqual("BOOLEAN", score["data_type"])
            self.assertIs(score["value"], True)
            self.assertEqual({"id": "answer-exact", "value": True, "passed": True},
                             adapter._check_score(score, BOOLEAN_CHECK))

    def test_javascript_passed_script_preserves_failing_boolean_scores(self):
        source = load_fixture(JAVASCRIPT_RESULT)
        self.assertEqual("6.1.1", source["action_metadata"]["sdk_version"])
        script = adapter._action_result(source, producer_policy(JAVASCRIPT_RESULT))
        # The SDK task succeeded; evaluator failure must still block the check.
        self.assertEqual("passed", script["status"])
        items = script["experiment_result"]["item_results"]
        self.assertEqual(2, len(items))
        for item in items:
            self.assertEqual(1, len(item["evaluations"]))
            score = item["evaluations"][0]
            self.assertEqual("answer-exact", score["name"])
            self.assertEqual("BOOLEAN", score["data_type"])
            self.assertIs(score["value"], False)
            self.assertEqual({"id": "answer-exact", "value": False, "passed": False},
                             adapter._check_score(score, BOOLEAN_CHECK))

    def test_top_level_and_nested_case_inputs_project_consistently(self):
        for name, (_, _, _, passed) in RECORDED.items():
            with self.subTest(fixture=name):
                script = adapter._action_result(load_fixture(name), producer_policy(name))
                items = script["experiment_result"]["item_results"]
                self.assertEqual(list(ANSWERS), [item["item"]["id"] for item in items])
                for item in items:
                    answer = ANSWERS[item["item"]["id"]]
                    expected = {"answer": answer}
                    self.assertEqual(expected, adapter._item_value(item, "input"))
                    self.assertEqual(expected, adapter._item_value(item, "expected_output"))
                    self.assertEqual({"answer": answer if passed else "wrong " + answer}, item["output"])
                    self.assertTrue(item["trace_id"])
                self.assertNotEqual(items[0]["trace_id"], items[1]["trace_id"])

    def test_wrong_runtime_and_cross_sdk_producer_are_rejected(self):
        for name, (kind, runtime, _, _) in RECORDED.items():
            with self.subTest(fixture=name):
                source = load_fixture(name)
                source["results"][0]["runtime"] = "node" if runtime == "python" else "python"
                with self.assertRaisesRegex(ReleaseError, "runtime differs from the pinned SDK producer"):
                    adapter._action_result(source, producer_policy(name))
                policy = producer_policy(name)
                policy["producer"]["kind"] = ("langfuse-javascript-sdk" if kind == "langfuse-python-sdk"
                                                else "langfuse-python-sdk")
                with self.assertRaisesRegex(ReleaseError, "runtime differs from the pinned SDK producer"):
                    adapter._action_result(load_fixture(name), policy)

    def test_projection_script_path_must_match_independent_policy(self):
        for name in RECORDED:
            with self.subTest(fixture=name):
                source = load_fixture(name)
                source["results"][0]["script_path"] = "some-other-script.py"
                with self.assertRaisesRegex(ReleaseError, "differs from the pinned producer"):
                    adapter._action_result(source, producer_policy(name))

    def test_sdk_camelcase_score_field_drift_is_rejected(self):
        for name in RECORDED:
            with self.subTest(fixture=name):
                source = load_fixture(name)
                score = source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]
                score["dataType"] = score.pop("data_type")
                with self.assertRaises(ReleaseError):
                    adapter._action_result(source, producer_policy(name))

    def test_missing_projected_score_value_is_rejected(self):
        for name in RECORDED:
            with self.subTest(fixture=name):
                source = load_fixture(name)
                del source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["value"]
                with self.assertRaises(ReleaseError):
                    adapter._action_result(source, producer_policy(name))

    def test_boolean_policy_never_coerces_numeric_or_string_scores(self):
        for name in RECORDED:
            score = load_fixture(name)["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]
            for changed in (0, 1, 0.0, 1.0, "false", "true", None):
                with self.subTest(fixture=name, value=repr(changed)):
                    with self.assertRaisesRegex(ReleaseError, "nonboolean score"):
                        adapter._check_score({**score, "value": changed}, BOOLEAN_CHECK)

    def test_conflicting_nested_and_top_level_projection_is_rejected(self):
        for name in RECORDED:
            with self.subTest(fixture=name):
                item = load_fixture(name)["results"][0]["experiment_result"]["item_results"][0]
                item["input"] = {"answer": "different source input"}
                with self.assertRaisesRegex(ReleaseError, "disagree about input"):
                    adapter._item_value(item, "input")

    @unittest.skipUnless(Draft202012Validator is not None, "install the optional review extra for upstream schema validation")
    def test_both_recorded_results_validate_against_pinned_official_schema(self):
        schema = load_fixture("result-json.v1.schema.json")
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for name in RECORDED:
            with self.subTest(fixture=name):
                validator.validate(load_fixture(name))


if __name__ == "__main__":
    unittest.main()
