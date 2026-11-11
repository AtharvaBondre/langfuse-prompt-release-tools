"""Synthetic offline evidence tests. No model, SDK, tenant or real experiment runs."""
import copy
import json
import os
import stat
import unittest
from types import SimpleNamespace

import evaluate_langfuse_release as evaluation
import import_langfuse_experiment as adapter
import langfuse_release_common as common
import langfuse_release_policy as policy_module
import promote_langfuse_prompts as promotion
import publish_langfuse_prompts as publish
import test_prompt_release as base
import test_staged_release as staged


class ExperimentPolicyTests(unittest.TestCase):
    setUp = base.PromptReleaseTests.setUp
    client = staged.StagedReleaseTests.client
    snapshot = base.PromptReleaseTests.snapshot
    edit = base.PromptReleaseTests.edit

    def policy(self, names=("example",), *, remote="optional"):
        return {"format_version": 1, "id": "synthetic-contract-checks",
                "producer": {"kind": "langfuse-experiment-action", "schema_version": "v1", "script_path": "checks/contract.py"},
                "dataset": {"id": "synthetic-dataset", "name": "contract-cases", "version": "2026-11-11T00:00:00Z",
                            "cases": [{"id": name, "input_sha256": common.fingerprint({"question": name}),
                                       "expected_output_sha256": common.fingerprint({"answer": name})}
                                      for name in ("empty-answer", "normal-answer")]},
                "execution": {"task_revision": "test-task-revision", "evaluator_revision": "test-evaluator-revision",
                              "model": {"provider": "none", "name": "deterministic-synthetic-fixture", "config_sha256": common.fingerprint({})}},
                "targets": [{"name": name, "case_ids": ["empty-answer", "normal-answer"]} for name in names],
                "checks": [{"id": "valid_json", "kind": "boolean", "operator": "eq", "threshold": True},
                           {"id": "coverage", "kind": "numeric", "operator": "gte", "threshold": 0.9}],
                "remote_export": remote}

    def prepare(self, names=("example",), *, remote="optional", producer="langfuse-experiment-action", schema=None):
        api, backend = self.client([base.prompt(name) for name in names])
        manifest = self.snapshot(api)
        for name in names:
            self.edit(manifest, name, "Answer {{question}} with a concrete example.")
        policy = self.policy(names, remote=remote)
        policy["producer"]["kind"] = producer
        if schema is not None:
            policy["dataset"]["schema_sha256"] = common.fingerprint(schema)
        path = manifest.parent / "policy.json"
        path.write_text(json.dumps(policy))
        binding = policy_module.pin_policy(manifest, path)
        stage = publish.release(api, manifest, apply=True, report_name="stage.json")
        stage_path = manifest.parent / "reports" / stage["report_file"]
        dataset = adapter._dataset_identity(policy)
        capture = adapter.start_capture(manifest, stage_path, dataset=dataset, execution=policy["execution"])
        items = []
        for case in policy["dataset"]["cases"]:
            case_id = case["id"]
            for name in names:
                adapter.capture_prompt(capture, item_id=case_id, requested_name=name, requested_version=8,
                                       prompt=SimpleNamespace(name=name, version=8))
            items.append({"item": {"id": case_id, "input": {"question": case_id}, "expected_output": {"answer": case_id}},
                          "output": {"answer": case_id}, "evaluations": [
                              {"name": "valid_json", "value": True}, {"name": "coverage", "value": 1.0}]})
        source = {"schema_version": "v1", "action_metadata": {"fixture": "synthetic; no experiment executed"},
                  "results": [{"script_path": "checks/contract.py", "script_name": "contract", "runtime": "python",
                               "duration_ms": 1.0, "status": "passed", "langfuse_experiment_url": None, "error": None,
                               "experiment_result": {"item_results": items, "run_evaluations": []}}]}
        result_path = manifest.parent / "synthetic-action-results.json"
        result_path.write_text(json.dumps(source))
        capture = adapter.finalize_capture(capture, result_path, remote_export="confirmed" if remote == "required" else "not_attempted")
        capture_path = manifest.parent / "synthetic-capture.json"
        capture_path.write_text(json.dumps(capture))
        backend.requests.clear()
        return api, backend, manifest, stage_path, result_path, capture_path, source, capture, binding

    def save(self, result_path, capture_path, source, capture):
        # Deliberately rehash mutated fixtures to exercise semantic validation, not only digests.
        capture["result_sha256"] = common.fingerprint(source)
        result_path.write_text(json.dumps(source))
        capture_path.write_text(json.dumps(capture))

    def imported(self, *args, **kwargs):
        values = self.prepare(*args, **kwargs)
        report = adapter.import_experiment(values[2], values[3], values[4], values[5], report_name="experiment.json")
        return (*values, report, values[2].parent / "reports" / "experiment.json")

    def assert_blocked(self, values, *, report_path=None):
        api, backend, manifest, stage_path = values[:4]
        backend.requests.clear()
        with self.assertRaises((common.ReleaseError, OSError, ValueError)):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, evaluation_report=report_path)
        self.assertEqual([], backend.requests)

    def test_import_passes_exact_policy_targets_cases_and_scores(self):
        values = self.imported(("first", "second"))
        report = values[-2]
        self.assertEqual("passed", report["status"])
        self.assertEqual(2, report["format_version"])
        self.assertEqual(["coverage", "valid_json"], report["required_checks"])
        self.assertEqual(2, len(report["cases"]))
        self.assertEqual(2, len(report["results"]))
        self.assertEqual(values[8]["sha256"], report["policy_sha256"])
        result = promotion.promote(values[0], values[2], values[3], label="production", apply=True, evaluation_report=values[-1])
        self.assertEqual("verified", result["status"])
        self.assertEqual(2, len(values[1].patches))

    def test_sdk_producer_is_preserved_without_claiming_action_ran(self):
        values = self.imported(producer="langfuse-python-sdk")
        self.assertEqual("langfuse-python-sdk", values[-2]["producer"]["kind"])

    def test_policy_is_persisted_once_and_stage_binds_it(self):
        values = self.prepare()
        manifest, stage_path, binding = values[2], values[3], values[8]
        raw = json.loads(manifest.read_text())
        self.assertEqual(binding, raw["evaluation_policy"])
        self.assertNotIn("local_content", raw["prompts"][0])
        self.assertEqual(binding, policy_module.pin_policy(manifest, manifest.parent / "policy.json"))
        changed = self.policy(); changed["checks"][1]["threshold"] = 0.1
        changed_path = manifest.parent / "weaker-policy.json"; changed_path.write_text(json.dumps(changed))
        with self.assertRaises(common.ReleaseError):
            policy_module.pin_policy(manifest, changed_path)
        _, snapshot = publish.load_snapshot(manifest)
        self.assertEqual(common.fingerprint(snapshot), json.loads(stage_path.read_text())["snapshot_sha256"])

    def test_pin_after_staging_invalidates_older_stage(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        stage = publish.release(api, manifest, apply=True, report_name="old-stage.json")
        path = manifest.parent / "policy.json"; path.write_text(json.dumps(self.policy()))
        policy_module.pin_policy(manifest, path)
        with self.assertRaises(common.ReleaseError):
            adapter.start_capture(manifest, manifest.parent / "reports" / stage["report_file"],
                                  dataset={key: self.policy()["dataset"][key] for key in ("id", "name", "version")},
                                  execution=self.policy()["execution"])

    def test_pinned_policy_blocks_missing_evidence_and_structural_optout(self):
        values = self.prepare(); self.assert_blocked(values)
        for options in ({"require_evaluation": False, "allow_structural_only": True},
                        {"require_evaluation": False, "allow_structural_only": True, "evaluation_report": values[4]}):
            with self.subTest(options=options), self.assertRaises(common.ReleaseError):
                promotion.promote(values[0], values[2], values[3], label="production", apply=True, **options)
            self.assertEqual([], values[1].requests)

    def test_pinned_policy_blocks_legacy_recorder_and_receipt(self):
        values = self.prepare()
        with self.assertRaises(common.ReleaseError):
            evaluation.record_evaluation(values[2], values[3], values[4], required_checks=["easy"])
        legacy = {"format_version": 1, "mode": "application_evaluation", "required_checks": ["easy"]}
        path = values[2].parent / "legacy.json"; path.write_text(json.dumps(legacy))
        self.assert_blocked(values, report_path=path)

    def test_weakened_receipt_checks_cannot_override_policy_even_if_rehashed(self):
        values = self.imported()
        report = values[-2]; report["required_checks"] = ["valid_json"]
        report.pop("evidence_sha256"); report["evidence_sha256"] = common.fingerprint(report)
        values[-1].write_text(json.dumps(report))
        self.assert_blocked(values, report_path=values[-1])

    def test_removed_policy_invalidates_original_stage(self):
        values = self.imported()
        manifest = values[2]; raw = json.loads(manifest.read_text()); del raw["evaluation_policy"]
        manifest.write_text(json.dumps(raw))
        self.assert_blocked(values, report_path=values[-1])

    def test_failed_required_case_records_failure_and_blocks_all_targets(self):
        values = self.prepare(("first", "second"))
        source, capture = values[6:8]
        source["results"][0]["experiment_result"]["item_results"][1]["evaluations"][1]["value"] = 0.2
        self.save(values[4], values[5], source, capture)
        report = adapter.import_experiment(*values[2:6], report_name="failed.json")
        self.assertEqual("failed", report["status"])
        self.assert_blocked(values, report_path=values[2].parent / "reports/failed.json")

    def test_script_regression_cannot_hide_behind_passing_scores(self):
        values = self.prepare(); source, capture = values[6:8]
        source["results"][0]["status"] = "regression"
        self.save(values[4], values[5], source, capture)
        report = adapter.import_experiment(*values[2:6], report_name="regression.json")
        self.assertEqual("failed", report["status"])
        self.assert_blocked(values, report_path=values[2].parent / "reports/regression.json")

    def test_schema_version_missing_output_and_aggregate_only_fail_closed(self):
        values = self.prepare()
        for mutation in ("version", "aggregate", "output", "script", "extra_script", "schema_field", "error"):
            source, capture = copy.deepcopy(values[6:8]); script = source["results"][0]
            if mutation == "version": source["schema_version"] = "v2"
            elif mutation == "aggregate": script["experiment_result"]["item_results"] = []
            elif mutation == "output": del script["experiment_result"]["item_results"][0]["output"]
            elif mutation == "script": script["script_path"] = "other.py"
            elif mutation == "extra_script": source["results"].append(copy.deepcopy(script))
            elif mutation == "schema_field": script["unexpected"] = True
            else: script["error"] = {"name": "Error", "message": "failed", "is_regression": False}
            self.save(values[4], values[5], source, capture)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_case_and_check_coverage_is_exact(self):
        values = self.prepare()
        for mutation in ("missing_case", "extra_case", "duplicate_case", "missing_check", "extra_check", "duplicate_check"):
            source, capture = copy.deepcopy(values[6:8]); items = source["results"][0]["experiment_result"]["item_results"]
            if mutation == "missing_case": items.pop()
            elif mutation == "extra_case": extra=copy.deepcopy(items[0]); extra["item"]["id"]="unreviewed"; items.append(extra)
            elif mutation == "duplicate_case": items.append(copy.deepcopy(items[0]))
            elif mutation == "missing_check": items[0]["evaluations"].pop()
            elif mutation == "extra_check": items[0]["evaluations"].append({"name": "undeclared", "value": False})
            else: items[0]["evaluations"].append(copy.deepcopy(items[0]["evaluations"][0]))
            self.save(values[4], values[5], source, capture)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_strict_scores_reject_string_bool_number_coercion(self):
        values = self.prepare()
        for index, invalid in ((0, "true"), (0, 1), (0, None), (1, "1.0"), (1, True), (1, None)):
            source, capture = copy.deepcopy(values[6:8])
            source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][index]["value"] = invalid
            self.save(values[4], values[5], source, capture)
            with self.subTest(index=index, invalid=invalid), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_nan_inf_and_duplicate_json_keys_are_rejected(self):
        values = self.prepare()
        for raw in ('{"schema_version":"v1","schema_version":"v1"}', '{"value":NaN}', '{"value":Infinity}'):
            values[4].write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_dataset_and_runtime_identity_drift_fail_before_promotion(self):
        values = self.prepare()
        for mutation in ("input", "expected_output", "item_conflict", "dataset_version", "task", "evaluator", "model", "config"):
            source, capture = copy.deepcopy(values[6:8]); item=source["results"][0]["experiment_result"]["item_results"][0]
            if mutation in ("input", "expected_output"): item["item"][mutation] = {"changed": True}
            elif mutation == "item_conflict": item["input"] = {"other": "input"}
            elif mutation == "dataset_version": capture["dataset"]["version"] = "later"
            elif mutation == "task": capture["execution"]["task_revision"] = "unreviewed"
            elif mutation == "evaluator": capture["execution"]["evaluator_revision"] = "easy-check"
            elif mutation == "model": capture["execution"]["model"]["name"] = "other"
            else: capture["execution"]["model"]["config_sha256"] = "0" * 64
            self.save(values[4], values[5], source, capture)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_runtime_capture_prompt_and_case_coverage_fail_closed(self):
        values = self.prepare()
        for mutation in ("requested_version", "returned_version", "returned_name", "missing_prompt", "duplicate_prompt", "missing_case", "duplicate_case", "target_plan", "pre_pin"):
            source, capture = copy.deepcopy(values[6:8]); observed=capture["items"][0]["prompts"][0]
            if mutation == "requested_version": observed[mutation] = 7
            elif mutation == "returned_version": observed[mutation] = 9
            elif mutation == "returned_name": observed[mutation] = "different"
            elif mutation == "missing_prompt": capture["items"][0]["prompts"] = []
            elif mutation == "duplicate_prompt": capture["items"][0]["prompts"].append(copy.deepcopy(observed))
            elif mutation == "missing_case": capture["items"].pop()
            elif mutation == "duplicate_case": capture["items"].append(copy.deepcopy(capture["items"][0]))
            elif mutation == "target_plan": capture["targets"][0]["version"] = 9
            else: capture["started_at_utc"] = "2000-01-01T00:00:00Z"
            self.save(values[4], values[5], source, capture)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_dataset_schema_hash_is_independent_of_version_and_cannot_drift(self):
        values = self.prepare(schema={"type": "object", "required": ["question"]})
        report = adapter.import_experiment(*values[2:6])
        self.assertEqual("passed", report["status"])
        for mutation in ("change", "omit"):
            source, capture = copy.deepcopy(values[6:8])
            if mutation == "change": capture["dataset"]["schema_sha256"] = common.fingerprint({"type": "string"})
            else: del capture["dataset"]["schema_sha256"]
            self.save(values[4], values[5], source, capture)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_required_remote_export_is_independent_of_local_completion(self):
        values = self.prepare(remote="required")
        for outcome in ("failed", "not_attempted", "unconfirmed"):
            source, capture = copy.deepcopy(values[6:8]); capture["completion"]["remote_export"] = outcome
            self.save(values[4], values[5], source, capture)
            with self.subTest(outcome=outcome), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])

    def test_optional_failed_export_is_reported_without_claiming_remote_success(self):
        values = self.prepare(); source, capture = values[6:8]
        capture["completion"]["remote_export"] = "failed"; self.save(values[4], values[5], source, capture)
        report = adapter.import_experiment(*values[2:6])
        self.assertEqual("passed", report["status"])
        self.assertEqual("failed", report["completion"]["remote_export"])

    def test_attempted_sdk_flush_without_persistence_proof_stays_unconfirmed(self):
        values = self.prepare(); source, capture = values[6:8]
        capture["completion"]["remote_export"] = "unconfirmed"
        self.save(values[4], values[5], source, capture)
        report = adapter.import_experiment(*values[2:6])
        self.assertEqual("passed", report["status"])
        self.assertEqual("unconfirmed", report["completion"]["remote_export"])

    def test_source_and_capture_tampering_after_import_blocks_before_http(self):
        values = self.imported()
        for path in (values[4], values[5]):
            before = path.read_text(); changed=json.loads(before); changed["after_import"] = True; path.write_text(json.dumps(changed))
            with self.subTest(path=path): self.assert_blocked(values, report_path=values[-1])
            path.write_text(before)

    def test_rehashing_weaker_source_cannot_hide_policy_failure(self):
        values = self.imported(); source, capture = values[6:8]
        source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][1]["value"] = 0.1
        self.save(values[4], values[5], source, capture)
        report = values[-2]
        report["source_results"]["sha256"] = common.fingerprint(source)
        report["execution_capture"]["sha256"] = common.fingerprint(capture)
        report.pop("evidence_sha256"); report["evidence_sha256"] = common.fingerprint(report)
        values[-1].write_text(json.dumps(report))
        self.assert_blocked(values, report_path=values[-1])

    def test_capture_helpers_reject_wrong_sdk_return_and_repeated_capture(self):
        values = self.prepare(); policy = values[8]["policy"]
        capture = adapter.start_capture(values[2], values[3], dataset={key: policy["dataset"][key] for key in ("id", "name", "version")}, execution=policy["execution"])
        for prompt in ({"name": "example", "version": 7}, {"name": "other", "version": 8}, {"name": "example", "version": True}):
            with self.subTest(prompt=prompt), self.assertRaises(common.ReleaseError):
                adapter.capture_prompt(capture, item_id="empty-answer", requested_name="example", requested_version=8, prompt=prompt)
        adapter.capture_prompt(capture, item_id="empty-answer", requested_name="example", requested_version=8, prompt={"name": "example", "version": 8})
        with self.assertRaises(common.ReleaseError):
            adapter.capture_prompt(capture, item_id="empty-answer", requested_name="example", requested_version=8, prompt={"name": "example", "version": 8})
        finished = adapter.finalize_capture(capture, values[4])
        with self.assertRaises(common.ReleaseError): adapter.finalize_capture(finished, values[4])

    def test_capture_start_rejects_actual_execution_or_dataset_mismatch(self):
        values = self.prepare(); policy = values[8]["policy"]
        dataset = {key: policy["dataset"][key] for key in ("id", "name", "version")}
        for changed in ("dataset", "execution"):
            actual_dataset, execution = copy.deepcopy(dataset), copy.deepcopy(policy["execution"])
            if changed == "dataset": actual_dataset["version"] = "later"
            else: execution["model"]["config_sha256"] = "0" * 64
            with self.subTest(changed=changed), self.assertRaises(common.ReleaseError):
                adapter.start_capture(values[2], values[3], dataset=actual_dataset, execution=execution)

    def test_policy_input_schema_rejects_ambiguous_or_incomplete_gates(self):
        for mutation in ("empty_checks", "duplicate_checks", "empty_cases", "uncovered_case", "bad_hash", "bool_number", "number_bool", "unsupported_kind", "unknown_field"):
            policy = self.policy()
            if mutation == "empty_checks": policy["checks"] = []
            elif mutation == "duplicate_checks": policy["checks"].append(copy.deepcopy(policy["checks"][0]))
            elif mutation == "empty_cases": policy["dataset"]["cases"] = []
            elif mutation == "uncovered_case": policy["targets"][0]["case_ids"].pop()
            elif mutation == "bad_hash": policy["dataset"]["cases"][0]["input_sha256"] = "fake"
            elif mutation == "bool_number": policy["checks"][0]["threshold"] = 1
            elif mutation == "number_bool": policy["checks"][1]["threshold"] = True
            elif mutation == "unsupported_kind": policy["checks"][0]["kind"] = "string"
            else: policy["ignore_failures"] = True
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                policy_module.validate_policy(policy)

    def test_evidence_rechecked_between_label_patches(self):
        values = self.imported(("first", "second"))
        def change_after_first(backend, name, version, body):
            capture = copy.deepcopy(values[7]); capture["execution"]["task_revision"] = "changed"
            values[5].write_text(json.dumps(capture))
        values[1].on_patch = change_after_first
        with self.assertRaises(common.ReleaseError):
            promotion.promote(values[0], values[2], values[3], label="production", apply=True, evaluation_report=values[-1])
        self.assertEqual(1, len(values[1].patches))
        self.assertIn("production", values[1].versions["first"][8]["labels"])
        self.assertIn("production", values[1].versions["second"][7]["labels"])

    def dependency_capture(self):
        # This tiny fixture supplies a no-dependency lock; no server is contacted.
        from langfuse_release_dependencies import prompt_sha256
        values = self.prepare()
        manifest, stage_path = values[2:4]
        _, snapshot = publish.load_snapshot(manifest)
        stage = json.loads(stage_path.read_text())
        actual = {"name": "example", "version": 8, "type": "text",
                  "prompt": "Answer {{question}} with a concrete example.", "config": {}}
        hashed = prompt_sha256(actual)
        lock = {"format_version": 1, "mode": "dependency_lock", "status": "verified",
                "host": snapshot["host"], "snapshot_sha256": common.fingerprint(snapshot),
                "stage_report_sha256": common.fingerprint(stage), "consumer": {"sdk": "synthetic", "compiler": "synthetic"},
                "roots": [{"name": "example", "version": 8, "raw_sha256": hashed, "resolved_sha256": hashed,
                           "graph": {"root": {"id": None, "name": "example", "version": 8}, "dependencies": {}}, "nodes": []}],
                "recorded_at_utc": values[8]["pinned_at_utc"]}
        lock["evidence_sha256"] = common.fingerprint(lock)
        lock_path = manifest.parent / "synthetic-lock.json"; lock_path.write_text(json.dumps(lock))
        policy = values[8]["policy"]
        capture = adapter.start_capture(manifest, stage_path, dataset=adapter._dataset_identity(policy),
                                        execution=policy["execution"], dependency_lock_path=lock_path)
        self._dependency_fixture = (values, lock_path, actual, capture)
        return values, lock_path, actual, capture

    def test_runtime_resolved_content_and_config_bind_to_pre_execution_lock(self):
        values, lock_path, actual, capture = self.dependency_capture()
        for case in ("empty-answer", "normal-answer"):
            adapter.capture_prompt(capture, item_id=case, requested_name="example", requested_version=8,
                                   prompt=SimpleNamespace(**actual))
        capture = adapter.finalize_capture(capture, values[4])
        values[5].write_text(json.dumps(capture))
        report = adapter.import_experiment(*values[2:6])
        self.assertEqual(common.fingerprint(json.loads(lock_path.read_text())), report["dependency_lock_sha256"])
        for mutation in ("content", "config"):
            _, _, original, running = self.dependency_capture_unique()
            if mutation == "content": original["prompt"] += " changed child content"
            else: original["config"] = {"temperature": 0.9}
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.capture_prompt(running, item_id="empty-answer", requested_name="example", requested_version=8, prompt=original)

    def dependency_capture_unique(self):
        # Reuse the same independent release inputs without creating another export directory.
        values, lock_path, actual, finished = self._dependency_fixture
        policy = values[8]["policy"]
        capture = adapter.start_capture(values[2], values[3], dataset=adapter._dataset_identity(policy),
                                        execution=policy["execution"], dependency_lock_path=lock_path)
        return values, lock_path, copy.deepcopy(actual), capture

    def test_dependency_lock_cannot_be_replaced_or_resolved_capture_removed(self):
        values, lock_path, actual, capture = self.dependency_capture()
        for case in ("empty-answer", "normal-answer"):
            adapter.capture_prompt(capture, item_id=case, requested_name="example", requested_version=8, prompt=actual)
        completed = adapter.finalize_capture(capture, values[4])
        for mutation in ("missing", "different"):
            changed = copy.deepcopy(completed)
            if mutation == "missing": del changed["items"][0]["prompts"][0]["resolved_sha256"]
            else: changed["items"][0]["prompts"][0]["resolved_sha256"] = "0" * 64
            values[5].write_text(json.dumps(changed))
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                adapter.import_experiment(*values[2:6])
        values[5].write_text(json.dumps(completed))
        changed = json.loads(lock_path.read_text()); changed["consumer"]["sdk"] = "changed"
        changed.pop("evidence_sha256"); changed["evidence_sha256"] = common.fingerprint(changed)
        lock_path.write_text(json.dumps(changed))
        with self.assertRaises(common.ReleaseError): adapter.import_experiment(*values[2:6])

    def test_report_is_exclusive_and_private(self):
        values = self.imported(); path = values[-1]; before = path.read_bytes()
        with self.assertRaises(common.ReleaseError): adapter.import_experiment(*values[2:6], report_name="experiment.json")
        self.assertEqual(before, path.read_bytes())
        if os.name == "posix": self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))


if __name__ == "__main__":
    unittest.main(verbosity=2)
