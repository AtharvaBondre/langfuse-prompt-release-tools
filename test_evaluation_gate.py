"""Offline evaluation-evidence gate tests; no model or application service runs.

The known-bad example uses a tiny deterministic local JSON/output check. The
recorder validates supplied check evidence; it does not generate model outputs.
"""
import copy
import io
import json
import os
import stat
import sys
import unittest
from unittest.mock import patch

import evaluate_langfuse_release as evaluation
import langfuse_release_common as common
import promote_langfuse_prompts as promotion
import publish_langfuse_prompts as publish
import test_prompt_release as base_fixtures
import test_staged_release as staged_fixtures


class EvaluationGateTests(unittest.TestCase):
    setUp = base_fixtures.PromptReleaseTests.setUp
    client = staged_fixtures.StagedReleaseTests.client
    snapshot = base_fixtures.PromptReleaseTests.snapshot
    edit = base_fixtures.PromptReleaseTests.edit
    stage = staged_fixtures.StagedReleaseTests.stage

    required = ["valid_json", "nonempty_answer"]

    def input_results(self, manifest, stage_path, *, candidate='{"answer":"Example answer"}'):
        """Deterministic synthetic application checks, not LLM evaluation."""
        try:
            output = json.loads(candidate)
            valid_json = True
        except (TypeError, json.JSONDecodeError):
            output, valid_json = None, False
        answer_ok = isinstance(output, dict) and isinstance(output.get("answer"), str) and bool(output["answer"].strip())
        _, snapshot = publish.load_snapshot(manifest)
        stage = json.loads(stage_path.read_text())
        data = {
            "format_version": 1,
            "snapshot_sha256": common.fingerprint(snapshot),
            "stage_report_sha256": common.fingerprint(stage),
            "results": [
                {"name": item["name"], "version": item["created_version"],
                 "checks": [{"id": "valid_json", "passed": valid_json, "detail": "Synthetic JSON parser check"},
                            {"id": "nonempty_answer", "passed": answer_ok, "detail": "Synthetic nonempty answer check"}]}
                for item in stage["results"] if item.get("changed")
            ],
        }
        path = manifest.parent / "synthetic-application-results.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def record(self, manifest, stage_path, results_path, *, report_name="evaluation.json", required=None):
        report = evaluation.record_evaluation(manifest, stage_path, results_path,
                                              required_checks=self.required if required is None else required,
                                              report_name=report_name)
        return report, manifest.parent / "reports" / report["report_file"]

    def passing_evidence(self, prompts=None):
        api, backend, manifest, stage_path = self.stage(prompts)
        source, raw = self.input_results(manifest, stage_path)
        report, path = self.record(manifest, stage_path, source)
        backend.requests.clear()
        return api, backend, manifest, stage_path, source, raw, report, path

    def test_required_gate_blocks_missing_evidence_before_http(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=True, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_default_promotion_requires_evidence_for_preview_and_apply(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaises(common.ReleaseError):
                promotion.promote(api, manifest, stage_path, label="production", apply=apply, allow_unsigned_local=True)
            self.assertEqual([], backend.requests)

    def test_structural_only_requires_explicit_consistent_boolean_optout(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        for options in ({"require_evaluation": False}, {"require_evaluation": True, "allow_structural_only": True},
                        {"require_evaluation": "false"}, {"allow_structural_only": 1}, {"apply": "true"}):
            with self.subTest(options=options), self.assertRaises(common.ReleaseError):
                promotion.promote(api, manifest, stage_path, label="production", **options, allow_unsigned_local=True)
            self.assertEqual([], backend.requests)
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True,
                                   require_evaluation=False, allow_structural_only=True, allow_unsigned_local=True)
        self.assertEqual("verified", result["status"])
        self.assertIs(True, result["evaluation"]["allow_structural_only"])
        self.assertIs(False, result["evaluation"]["required"])
        self.assertEqual("not_requested", result["evaluation"]["status"])

    def test_explicit_structural_optout_still_rejects_supplied_failed_evidence(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path, candidate="not JSON")
        report, path = self.record(manifest, stage_path, source)
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=False, allow_structural_only=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not establish Windows ACL privacy")
    def test_evaluation_receipt_is_private_under_permissive_umask(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_promote_cli_defaults_to_required_evidence_and_rejects_conflicting_flags(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        argv = ["promote", "--allow-unsigned-local", "--host", api.host, "--project-id", "synthetic-project", "--manifest", str(manifest), "--stage-report", str(stage_path), "--label", "production", "--apply"]
        with patch.object(sys, "argv", argv), patch.object(promotion, "Client", return_value=api), patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                promotion.main()
        self.assertEqual(1, caught.exception.code)
        self.assertEqual([], backend.requests)
        with patch.object(sys, "argv", argv + ["--require-evaluation", "--allow-structural-only"]), patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                promotion.main()
        self.assertEqual(2, caught.exception.code)
        self.assertEqual([], backend.requests)

    def test_promote_cli_explicit_structural_flag_is_recorded(self):
        api, backend, manifest, stage_path = self.stage()
        argv = ["promote", "--allow-unsigned-local", "--host", api.host, "--project-id", "synthetic-project", "--manifest", str(manifest), "--stage-report", str(stage_path), "--label", "production", "--apply", "--allow-structural-only", "--report", "structural-cli.json"]
        with patch.object(sys, "argv", argv), patch.object(promotion, "Client", return_value=api), patch("sys.stdout", new=io.StringIO()):
            promotion.main()
        report = json.loads((manifest.parent / "reports" / "structural-cli.json").read_text())
        self.assertIs(True, report["evaluation"]["allow_structural_only"])
        self.assertEqual("verified", report["status"])

    def test_required_gate_blocks_nonexistent_report_before_http(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        with self.assertRaises((common.ReleaseError, OSError)):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=self.root / "absent.json", allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_passing_report_is_bound_to_exact_snapshot_stage_source_and_versions(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        self.assertEqual("passed", report["status"])
        self.assertEqual("application_evaluation", report["mode"])
        self.assertEqual(common.fingerprint(raw), report["source_results"]["sha256"])
        self.assertEqual(common.fingerprint(json.loads(stage_path.read_text())), report["stage_report"]["sha256"])
        self.assertEqual(raw["snapshot_sha256"], report["snapshot_sha256"])
        self.assertEqual(8, report["results"][0]["version"])
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True,
                                   require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual("verified", result["status"])
        self.assertEqual("passed", result["evaluation"]["status"])
        self.assertEqual(common.fingerprint(report), result["evaluation"]["sha256"])
        self.assertEqual(1, len(backend.patches))

    def test_known_bad_candidate_records_failure_and_cannot_promote(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path, candidate="This is prose, not the required JSON.")
        report, path = self.record(manifest, stage_path, source)
        self.assertEqual("failed", report["status"])
        self.assertTrue(path.exists())
        self.assertFalse(raw["results"][0]["checks"][0]["passed"])
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_valid_json_with_bad_application_shape_still_fails(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path, candidate='{"answer":"  "}')
        report, path = self.record(manifest, stage_path, source)
        self.assertTrue(raw["results"][0]["checks"][0]["passed"])
        self.assertFalse(raw["results"][0]["checks"][1]["passed"])
        self.assertEqual("failed", report["status"])
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_supplied_failed_evidence_is_checked_without_require_flag(self):
        api, backend, manifest, stage_path = self.stage()
        source, _ = self.input_results(manifest, stage_path, candidate="bad")
        _, path = self.record(manifest, stage_path, source); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_missing_required_check_rejected(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path)
        raw["results"][0]["checks"] = raw["results"][0]["checks"][:1]
        source.write_text(json.dumps(raw)); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            self.record(manifest, stage_path, source)
        self.assertEqual([], backend.requests)

    def test_nonboolean_pass_values_rejected(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path)
        for value in [1, 0, "true", None]:
            with self.subTest(value=value):
                case = copy.deepcopy(raw); case["results"][0]["checks"][0]["passed"] = value
                source.write_text(json.dumps(case))
                with self.assertRaises(common.ReleaseError):
                    self.record(manifest, stage_path, source)

    def test_empty_or_duplicate_check_configuration_rejected(self):
        api, backend, manifest, stage_path = self.stage()
        source, _ = self.input_results(manifest, stage_path)
        for required in [[], [""], ["valid_json", "valid_json"]]:
            with self.subTest(required=required), self.assertRaises(common.ReleaseError):
                self.record(manifest, stage_path, source, required=required)

    def test_duplicate_check_ids_and_targets_rejected(self):
        api, backend, manifest, stage_path = self.stage()
        source, original = self.input_results(manifest, stage_path)
        for duplicate in ["check", "target"]:
            raw = copy.deepcopy(original)
            if duplicate == "check": raw["results"][0]["checks"].append(copy.deepcopy(raw["results"][0]["checks"][0]))
            else: raw["results"].append(copy.deepcopy(raw["results"][0]))
            source.write_text(json.dumps(raw))
            with self.subTest(duplicate=duplicate), self.assertRaises(common.ReleaseError):
                self.record(manifest, stage_path, source)

    def test_missing_extra_wrong_version_and_empty_targets_rejected(self):
        api, backend, manifest, stage_path = self.stage([base_fixtures.prompt("first"), base_fixtures.prompt("second")])
        source, original = self.input_results(manifest, stage_path)
        for mutation in ["missing", "extra", "version", "empty"]:
            raw = copy.deepcopy(original)
            if mutation == "missing": raw["results"].pop()
            elif mutation == "extra": raw["results"].append({**copy.deepcopy(raw["results"][0]), "name": "unrelated"})
            elif mutation == "version": raw["results"][1]["version"] = 9
            else: raw["results"] = []
            source.write_text(json.dumps(raw))
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                self.record(manifest, stage_path, source)

    def test_failed_additional_check_cannot_hide_behind_passing_required_checks(self):
        api, backend, manifest, stage_path = self.stage()
        source, raw = self.input_results(manifest, stage_path)
        raw["results"][0]["checks"].append({"id": "adversarial_case", "passed": False})
        source.write_text(json.dumps(raw))
        report, path = self.record(manifest, stage_path, source)
        self.assertEqual("failed", report["status"])
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_failed_second_target_blocks_promotion_for_entire_batch(self):
        api, backend, manifest, stage_path = self.stage([base_fixtures.prompt("first"), base_fixtures.prompt("second")])
        source, raw = self.input_results(manifest, stage_path)
        raw["results"][1]["checks"][1]["passed"] = False
        source.write_text(json.dumps(raw))
        report, path = self.record(manifest, stage_path, source)
        self.assertEqual("failed", report["status"])
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)
        self.assertIn("production", backend.versions["first"][7]["labels"])

    def test_recorder_rejects_mismatched_source_bindings(self):
        api, backend, manifest, stage_path = self.stage()
        source, original = self.input_results(manifest, stage_path)
        for field in ["snapshot_sha256", "stage_report_sha256"]:
            raw = copy.deepcopy(original); raw[field] = "0" * 64; source.write_text(json.dumps(raw))
            with self.subTest(field=field), self.assertRaises(common.ReleaseError):
                self.record(manifest, stage_path, source)

    def test_edited_local_prompt_invalidates_passing_evaluation_before_http(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        self.edit(manifest, content="A later {{question}} edit.")
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_changed_raw_results_invalidates_passing_report_before_http(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        raw["results"][0]["checks"][0]["passed"] = False; source.write_text(json.dumps(raw))
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_tampered_evaluation_report_invalidates_gate(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        report["results"][0]["version"] = 9; path.write_text(json.dumps(report))
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_changed_stage_report_invalidates_gate(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        stage = json.loads(stage_path.read_text()); stage["review_note"] = "changed evidence"
        stage_path.write_text(json.dumps(stage))
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_missing_raw_source_results_blocks_gate(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        source.unlink()
        with self.assertRaises((common.ReleaseError, OSError)):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, allow_unsigned_local=True)
        self.assertEqual([], backend.requests)

    def test_evaluation_report_reuse_cannot_overwrite_evidence(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        before = path.read_bytes()
        with self.assertRaises(common.ReleaseError):
            self.record(manifest, stage_path, source)
        self.assertEqual(before, path.read_bytes())

    def test_results_changed_during_remote_preflight_are_rechecked_before_patch(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence()
        def change_after_initial_gate(backend, name, query, count):
            changed = copy.deepcopy(raw); changed["results"][0]["checks"][0]["passed"] = False
            source.write_text(json.dumps(changed))
        backend.on_get = change_after_initial_gate
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, report_name="changed-during-preflight.json", allow_unsigned_local=True)
        self.assertTrue(backend.requests)
        self.assertEqual([], backend.patches)
        stopped = json.loads((manifest.parent / "reports/changed-during-preflight.json").read_text())
        self.assertEqual("evaluation", stopped["phase"])
        self.assertEqual("blocked", stopped["evaluation"]["status"])

    def test_results_changed_after_first_patch_block_second_and_preserve_first(self):
        api, backend, manifest, stage_path, source, raw, report, path = self.passing_evidence(
            [base_fixtures.prompt("first"), base_fixtures.prompt("second")])
        def change_evidence_after_first_submission(backend, name, version, payload):
            changed = copy.deepcopy(raw); changed["results"][1]["checks"][0]["passed"] = False
            source.write_text(json.dumps(changed))
        backend.on_patch = change_evidence_after_first_submission
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True,
                              require_evaluation=True, evaluation_report=path, report_name="changed-mid-batch.json", allow_unsigned_local=True)
        self.assertEqual(1, len(backend.patches))
        stopped = json.loads((manifest.parent / "reports/changed-mid-batch.json").read_text())
        self.assertEqual("verified", stopped["results"][0]["status"])
        self.assertEqual("stopped_before_patch", stopped["results"][1]["status"])
        self.assertEqual("evaluation", stopped["phase"])
        self.assertIn("production", backend.versions["first"][8]["labels"])
        self.assertIn("production", backend.versions["second"][7]["labels"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
