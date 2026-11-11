"""Offline captured-output example tests; service fixtures are test-only."""
import copy
import io
import json
import os
import stat
import unittest
from unittest.mock import patch

from examples.check_captured_outputs import REQUIRED_CHECKS, check_captured_outputs, main
import evaluate_langfuse_release as evaluation
import langfuse_release_common as common
import promote_langfuse_prompts as promotion
import publish_langfuse_prompts as publish
import test_prompt_release as base
import test_staged_release as staged


class CapturedOutputsTests(unittest.TestCase):
    setUp = base.PromptReleaseTests.setUp
    client = staged.StagedReleaseTests.client
    snapshot = base.PromptReleaseTests.snapshot
    edit = base.PromptReleaseTests.edit
    stage = staged.StagedReleaseTests.stage

    def capture(self, manifest, stage_path, output='{"answer":"A captured answer."}'):
        snapshot = publish.load_snapshot(manifest)[1]
        stage = json.loads(stage_path.read_text())
        data = {"format_version": 1, "snapshot_sha256": common.fingerprint(snapshot),
                "stage_report_sha256": common.fingerprint(stage),
                "outputs": [{"name": row["name"], "version": row["created_version"], "output": output}
                            for row in stage["results"] if row["changed"]]}
        path = manifest.parent / "captured.json"
        path.write_text(json.dumps(data))
        return path, data

    def test_failed_output_is_recorded_and_blocks_default_gate(self):
        api, backend, manifest, stage_path = self.stage()
        captured, _ = self.capture(manifest, stage_path, '{"answer":"   "}')
        output = manifest.parent / "app-results.json"
        backend.requests.clear()
        results = check_captured_outputs(manifest, stage_path, captured, output)
        self.assertEqual([True, False], [c["passed"] for c in results["results"][0]["checks"]])
        receipt = evaluation.record_evaluation(manifest, stage_path, output, required_checks=REQUIRED_CHECKS)
        self.assertEqual("failed", receipt["status"])
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              evaluation_report=manifest.parent / "reports" / receipt["report_file"])
        self.assertEqual([], backend.requests)

    def test_passing_captured_output_allows_default_gate_and_retains_provenance(self):
        api, backend, manifest, stage_path = self.stage()
        captured, source = self.capture(manifest, stage_path)
        output = manifest.parent / "app-results.json"
        backend.requests.clear()
        results = check_captured_outputs(manifest, stage_path, captured, output)
        self.assertEqual([], backend.requests)
        self.assertEqual(common.fingerprint(source), results["captured_source_sha256"])
        self.assertNotIn("output", results["results"][0])
        receipt = evaluation.record_evaluation(manifest, stage_path, output, required_checks=REQUIRED_CHECKS)
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True,
                                   evaluation_report=manifest.parent / "reports" / receipt["report_file"])
        self.assertEqual("verified", result["status"])
        self.assertEqual(1, len(backend.patches))

    def test_invalid_json_and_invalid_answer_types_generate_failed_checks(self):
        api, backend, manifest, stage_path = self.stage()
        for index, (text, expected) in enumerate([
                ("not JSON", [False, False]), ('{"answer":7}', [True, False]),
                ('["answer"]', [True, False]), ('{"answer":""}', [True, False]),
                ('{"answer":"ok","score":NaN}', [False, False]),
                ('{"answer":"one","answer":"two"}', [False, False])]):
            with self.subTest(text=text):
                captured, _ = self.capture(manifest, stage_path, text)
                result = check_captured_outputs(manifest, stage_path, captured, manifest.parent / f"result-{index}.json")
                self.assertEqual(expected, [c["passed"] for c in result["results"][0]["checks"]])

    def test_stale_bindings_rejected_without_output(self):
        api, backend, manifest, stage_path = self.stage()
        captured, source = self.capture(manifest, stage_path)
        output = manifest.parent / "must-not-exist.json"
        for key in ("snapshot_sha256", "stage_report_sha256"):
            data = copy.deepcopy(source); data[key] = "0" * 64
            captured.write_text(json.dumps(data))
            with self.subTest(key=key), self.assertRaises(common.ReleaseError):
                check_captured_outputs(manifest, stage_path, captured, output)
            self.assertFalse(output.exists())
        captured.write_text(json.dumps(source))
        self.edit(manifest, content="A later {{question}} edit.")
        with self.assertRaises(common.ReleaseError):
            check_captured_outputs(manifest, stage_path, captured, output)
        self.assertFalse(output.exists())

    def test_wrong_missing_duplicate_extra_targets_and_malformed_types_rejected(self):
        api, backend, manifest, stage_path = self.stage([base.prompt("one"), base.prompt("two")])
        captured, source = self.capture(manifest, stage_path)
        variants = []
        for field, value in (("version", 999), ("version", True), ("version", "8"),
                             ("name", ["one"]), ("output", {}), ("output", None)):
            data = copy.deepcopy(source); data["outputs"][0][field] = value; variants.append(data)
        data = copy.deepcopy(source); data["outputs"].pop(); variants.append(data)
        data = copy.deepcopy(source); data["outputs"].append(copy.deepcopy(data["outputs"][0])); variants.append(data)
        data = copy.deepcopy(source); data["outputs"].append({"name": "extra", "version": 8, "output": "{}"}); variants.append(data)
        for field, value in (("format_version", True), ("outputs", []), ("outputs", "wrong")):
            data = copy.deepcopy(source); data[field] = value; variants.append(data)
        data = copy.deepcopy(source); data["outputs"][0] = None; variants.append(data)
        output = manifest.parent / "must-not-exist.json"
        backend.requests.clear()
        for index, data in enumerate(variants):
            captured.write_text(json.dumps(data))
            with self.subTest(index=index), self.assertRaises(common.ReleaseError):
                check_captured_outputs(manifest, stage_path, captured, output)
            self.assertFalse(output.exists())
        self.assertEqual([], backend.requests)

    def test_output_never_overwrites_prior_results_or_captured_input(self):
        api, backend, manifest, stage_path = self.stage()
        captured, _ = self.capture(manifest, stage_path)
        output = manifest.parent / "app-results.json"
        output.write_text("previous evidence")
        for path in (output, captured):
            before = path.read_bytes()
            with self.subTest(path=path.name), self.assertRaises(common.ReleaseError):
                check_captured_outputs(manifest, stage_path, captured, path)
            self.assertEqual(before, path.read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX modes do not establish Windows ACL privacy")
    def test_output_mode_is_private_under_permissive_umask(self):
        previous = os.umask(0o022); self.addCleanup(os.umask, previous)
        api, backend, manifest, stage_path = self.stage()
        captured, _ = self.capture(manifest, stage_path)
        output = manifest.parent / "app-results.json"
        check_captured_outputs(manifest, stage_path, captured, output)
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))

    def test_cli_distinguishes_failed_checks_from_invalid_input(self):
        api, backend, manifest, stage_path = self.stage()
        captured, source = self.capture(manifest, stage_path, "not JSON")
        output = manifest.parent / "app-results.json"
        args = ["--manifest", str(manifest), "--stage-report", str(stage_path),
                "--captured", str(captured), "--out", str(output)]
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(1, main(args))
        self.assertTrue(output.exists())
        source["snapshot_sha256"] = "0" * 64; captured.write_text(json.dumps(source))
        invalid = manifest.parent / "invalid.json"
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(args[:-1] + [str(invalid)])
        self.assertEqual(2, caught.exception.code)
        self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
