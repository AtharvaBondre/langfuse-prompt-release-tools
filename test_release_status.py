"""Offline status tests use actual report builders with synthetic service data."""
import copy
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import langfuse_release_status as status
import langfuse_release_adoption as adoption
import langfuse_release_rollout as rollout
import promote_langfuse_prompts as promotion
import publish_langfuse_prompts as publish
from langfuse_release_common import ReleaseError, fingerprint
import test_experiment_policy as experiment_fixtures
import test_evaluation_gate as evaluation_fixtures
import test_remote_policy_integration as remote_fixtures
import test_prompt_release as prompt_fixtures


def sealed(value):
    value = copy.deepcopy(value)
    value["evidence_sha256"] = fingerprint({k: v for k, v in value.items() if k != "evidence_sha256"})
    return value


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = experiment_fixtures.ExperimentPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def prepare(self, *, promoted=False):
        values = self.fixture.imported(producer="langfuse-python-sdk")
        self.api, self.backend, self.manifest, self.stage = values[:4]
        self.evaluation, self.evaluation_path = values[-2:]
        self.lock_path = values[9]
        self.lock = json.loads(self.lock_path.read_text())
        self.root = self.manifest.parent
        if promoted:
            self.promotion = promotion.promote(self.api, self.manifest, self.stage, label="production", apply=True,
                report_name="promotion.json", evaluation_report=self.evaluation_path, dependency_lock=self.lock_path)
            self.promotion_path = self.root / "reports" / "promotion.json"
        self.now = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.backend.requests.clear()
        return values

    def inspect(self, **kwargs):
        return status.inspect_release(self.manifest, now=kwargs.pop("now", self.now), **kwargs)

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def observation(self, *, version=8):
        return adoption._capture_value("example", version, "text", self.api.get("example", version=8)["prompt"], {}, False,
            sdk_name="langfuse-python", sdk_version="5.2.0", consumer_id="api", deployment_id="app@abc",
            request_id="r1", requested_label="production", request_started_at_utc=self.now - timedelta(seconds=0.1),
            observed_at_utc=self.now, cache_ttl_seconds=60)

    def add_adoption(self, *, version=8):
        receipt = adoption.build_adoption_receipt([self.observation(version=version)], self.promotion, self.lock,
            required_consumers={"api": "app@abc"}, max_age_seconds=60, now=self.now)
        self.write(self.root / "adoption.json", receipt)
        self.backend.requests.clear()
        return receipt

    def test_recomputes_passing_evaluation_and_keeps_missing_promotion_separate(self):
        self.prepare()
        report = self.inspect()
        self.assertEqual("passed", report["sections"]["evaluation"]["state"])
        self.assertEqual("historical_results_recomputed", report["sections"]["evaluation"]["validation"])
        self.assertEqual("recorded_locked", report["sections"]["dependencies"]["state"])
        self.assertEqual("missing", report["sections"]["promotion"]["state"])
        self.assertEqual("missing", report["sections"]["adoption"]["state"])
        self.assertEqual("review_promotion", report["next_steps"][0]["action"])
        self.assertFalse(report["remote_checked"])
        self.assertEqual([], self.backend.requests)

    def test_promoted_label_never_implies_application_adoption(self):
        self.prepare(promoted=True)
        report = self.inspect()
        self.assertEqual("recorded_promoted", report["sections"]["promotion"]["state"])
        self.assertEqual("missing", report["sections"]["adoption"]["state"])
        self.assertEqual("inspect_application_observations", report["next_steps"][0]["action"])
        self.assertIn("no server check", status.render_text(report))

    def test_good_and_old_cached_observations_have_different_outcomes(self):
        self.prepare(promoted=True)
        self.add_adoption()
        report = self.inspect()
        self.assertEqual("observed_adopted", report["sections"]["adoption"]["state"], report["sections"]["adoption"])
        self.add_adoption(version=7)
        report = self.inspect()
        self.assertEqual("not_adopted", report["sections"]["adoption"]["state"])
        self.assertIn("returned_version_mismatch", {i["reason"] for i in report["sections"]["adoption"]["issues"]})

    def test_adoption_age_is_recomputed_without_erasing_historical_success(self):
        self.prepare(promoted=True); self.add_adoption()
        report = self.inspect(now=self.now + timedelta(seconds=61))
        adoption_section = report["sections"]["adoption"]
        self.assertEqual("verified", adoption_section["recorded_status"])
        self.assertEqual("stale", adoption_section["state"])
        self.assertEqual(61, adoption_section["age_seconds"])
        self.assertEqual([], self.backend.requests)

    def test_changed_source_invalidates_even_a_resealed_passing_report(self):
        values = self.prepare()
        source = values[6]
        source["results"][0]["experiment_result"]["item_results"][0]["output"] = "different"
        self.write(values[4], source)
        result = self.inspect()
        self.assertEqual("invalid", result["sections"]["evaluation"]["state"])
        self.assertIn("source_results", result["sections"]["evaluation"]["messages"][0])

    def test_resealed_forged_pass_is_recomputed_from_actual_failed_checks(self):
        values = self.prepare()
        source, capture = values[6:8]
        source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["value"] = False
        self.fixture.save(values[4], values[5], source, capture)
        import import_langfuse_experiment as adapter
        actual = adapter.import_experiment(*values[2:6], report_name="failed.json")
        report = self.inspect(evaluation_report="reports/failed.json")
        self.assertEqual("failed", report["sections"]["evaluation"]["state"])
        self.assertEqual("repair_and_rerun_evaluation", report["next_steps"][0]["action"])
        actual["status"] = "passed"
        self.write(self.root / "reports/failed.json", sealed(actual))
        self.assertEqual("invalid", self.inspect(evaluation_report="reports/failed.json")["sections"]["evaluation"]["state"])

    def test_changed_prompt_snapshot_invalidates_stage_and_dependants(self):
        self.prepare(promoted=True)
        self.fixture.edit(self.manifest, "example", "Different {{question}} content")
        report = self.inspect()
        for name in ("stage", "evaluation", "dependencies", "promotion"):
            self.assertEqual("invalid", report["sections"][name]["state"], name)

    def test_missing_and_corrupted_artifacts_are_visible(self):
        self.prepare()
        self.lock_path.unlink()
        report = self.inspect()
        self.assertEqual("missing", report["sections"]["dependencies"]["state"])
        self.assertEqual("invalid", report["sections"]["evaluation"]["state"])
        self.write(self.lock_path, {"mode": "dependency_lock", "status": "verified"})
        report = self.inspect()
        self.assertEqual("invalid", report["sections"]["dependencies"]["state"])

    def test_multiple_attempts_require_explicit_selection(self):
        self.prepare()
        second = self.root / "reports/second-stage.json"
        second.write_bytes(self.stage.read_bytes())
        report = self.inspect()
        self.assertEqual("ambiguous", report["sections"]["stage"]["state"])
        report = self.inspect(stage_report="reports/stage.json")
        self.assertEqual("recorded_verified", report["sections"]["stage"]["state"])

    def test_supported_subset_stage_preserves_selected_scope(self):
        api, _ = self.fixture.client([prompt_fixtures.prompt("one"), prompt_fixtures.prompt("two")])
        manifest = self.fixture.snapshot(api)
        for name in ("one", "two"):
            self.fixture.edit(manifest, name, "Changed {{question}} answer.")
        stage = publish.release(api, manifest, apply=True, names=["one"], report_name="subset.json")
        self.assertEqual("verified", stage["status"])
        report = status.inspect_release(manifest)
        section = report["sections"]["stage"]
        self.assertEqual("recorded_verified", section["state"], section)
        self.assertEqual({"one": 8}, section["targets"])
        self.assertEqual(["one"], section["selected_prompts"])
        self.assertEqual(["two"], section["unselected_snapshot_prompts"])
        self.assertIn("Outside this stage selection: two", status.render_text(report))

    def test_supported_unchanged_stage_has_nothing_to_promote(self):
        api, backend = self.fixture.client([prompt_fixtures.prompt("one"), prompt_fixtures.prompt("two")])
        manifest = self.fixture.snapshot(api)
        stage = publish.release(api, manifest, apply=True, names=["one"], report_name="unchanged.json")
        self.assertEqual("verified", stage["status"])
        self.assertEqual([], backend.posts)
        report = status.inspect_release(manifest)
        section = report["sections"]["stage"]
        self.assertEqual("recorded_unchanged", section["state"], section)
        self.assertEqual({}, section["targets"])
        self.assertEqual(["one"], section["selected_prompts"])
        self.assertEqual(["two"], section["unselected_snapshot_prompts"])
        self.assertEqual(["no_changes_to_promote"], [step["action"] for step in report["next_steps"]])

    def test_unchanged_stage_must_match_actual_local_content(self):
        self.prepare()
        stage = json.loads(self.stage.read_text())
        for row in stage["results"]:
            row.update(changed=False, status="unchanged")
        self.write(self.stage, stage)
        section = self.inspect()["sections"]["stage"]
        self.assertEqual("invalid", section["state"])
        self.assertIn("local content", section["messages"][0])

    def test_unknown_promotion_is_not_retried_or_called_complete(self):
        self.prepare(promoted=True)
        self.promotion["status"] = "stopped"
        self.promotion["results"][0]["status"] = "promotion_outcome_unknown_do_not_retry"
        self.write(self.promotion_path, self.promotion)
        report = self.inspect()
        self.assertEqual("uncertain_do_not_retry", report["sections"]["promotion"]["state"])
        self.assertEqual("reconcile_existing_attempt", report["next_steps"][0]["action"])

    def test_preview_is_not_a_completed_label_move(self):
        self.prepare(promoted=True)
        self.promotion.update(apply=False, status="dry_run_complete")
        self.write(self.promotion_path, self.promotion)
        report = self.inspect()
        self.assertEqual("preview_only", report["sections"]["promotion"]["state"])

    def test_malformed_promotion_nested_metadata_is_invalid(self):
        self.prepare(promoted=True)
        for field in ("evaluation", "dependency_lock"):
            for malformed in ([], None, "bad"):
                with self.subTest(field=field, value=malformed):
                    value = copy.deepcopy(self.promotion); value[field] = malformed
                    self.write(self.promotion_path, value)
                    result = self.inspect()
                    section = result["sections"]["promotion"]
                    self.assertEqual("invalid", section["state"])
                    self.assertIn("metadata must be an object", section["messages"][0])

    def test_nested_validator_attribute_error_stays_in_its_section(self):
        self.prepare()
        with patch("import_langfuse_experiment._derive", side_effect=AttributeError("Malformed nested execution field")):
            report = self.inspect()
        self.assertEqual("invalid", report["sections"]["evaluation"]["state"])
        self.assertEqual("recorded_verified", report["sections"]["stage"]["state"])

    def test_source_reference_cannot_escape_workspace(self):
        values = self.prepare()
        escaped = self.root.parent / "outside-results.json"
        escaped.write_bytes(values[4].read_bytes())
        self.evaluation["source_results"]["file"] = str(escaped)
        self.write(self.evaluation_path, sealed(self.evaluation))
        result = self.inspect()
        self.assertEqual("invalid", result["sections"]["evaluation"]["state"])
        self.assertIn("outside this workspace", result["sections"]["evaluation"]["messages"][0])

    def test_malformed_or_duplicate_json_is_not_silently_ignored(self):
        self.prepare()
        self.evaluation_path.write_text('{"mode":"application_evaluation","mode":"langfuse_experiment_evaluation"}')
        report = self.inspect(evaluation_report="reports/experiment.json")
        self.assertEqual("invalid", report["sections"]["evaluation"]["state"])
        self.assertTrue(report["warnings"])

    def test_non_string_mode_warns_in_discovery_and_is_invalid_when_selected(self):
        self.prepare()
        path = self.root / "auxiliary.json"
        for value in ([], {}, None, 2):
            with self.subTest(mode=value):
                self.write(path, {"mode": value})
                report = self.inspect()
                self.assertEqual("passed", report["sections"]["evaluation"]["state"])
                self.assertEqual(["Cannot inspect candidate auxiliary.json: mode must be a string."], report["warnings"])
                selected = self.inspect(evaluation_report="auxiliary.json")
                self.assertEqual("invalid", selected["sections"]["evaluation"]["state"])
                self.assertIn("not a evaluation report", selected["sections"]["evaluation"]["messages"][0])

    def test_cli_non_string_mode_returns_diagnostic_json_not_traceback(self):
        self.prepare()
        self.write(self.root / "auxiliary.json", {"mode": []})
        with patch("sys.stdout", new=io.StringIO()) as output:
            code = status.main(["--workspace", str(self.root), "--format", "json"])
        self.assertEqual(1, code)
        self.assertEqual("attention", json.loads(output.getvalue())["status"])

    def test_cli_outputs_json_and_leaves_every_file_unchanged(self):
        self.prepare(promoted=True); self.add_adoption()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = io.StringIO()
        with patch("sys.stdout", output), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
            code = status.main(["--workspace", str(self.root), "--json"])
        # Captures use a deterministic future instant; the real-time CLI should
        # report that honestly, not accept a future receipt as current evidence.
        self.assertEqual(1, code)
        report = json.loads(output.getvalue())
        self.assertEqual("release_status", report["mode"])
        self.assertEqual("invalid", report["sections"]["adoption"]["state"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_relative_manifest_is_resolved_once(self):
        self.prepare()
        # Windows runners keep the checkout and temporary files on different
        # drives. A relative path must start on the fixture's own drive.
        original = Path.cwd()
        try:
            os.chdir(self.root.parent)
            relative = os.path.relpath(self.manifest, Path.cwd())
            report = status.inspect_release(relative, now=self.now)
        finally:
            os.chdir(original)
        self.assertEqual(str(self.manifest.resolve()), report["manifest"])

    def test_text_summary_escapes_terminal_control_characters(self):
        self.prepare()
        report = self.inspect()
        report["warnings"] = ["unsafe\x1b[31m\nsecond"]
        text = status.render_text(report)
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u001b", text)

    def test_legacy_application_evidence_is_recomputed(self):
        fixture = evaluation_fixtures.EvaluationGateTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        values = fixture.passing_evidence()
        report = status.inspect_release(values[2])
        self.assertEqual("passed", report["sections"]["evaluation"]["state"])
        self.assertIsNone(report["project_id"])

    def test_legacy_report_cannot_claim_an_unknown_format(self):
        fixture = evaluation_fixtures.EvaluationGateTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        values = fixture.passing_evidence()
        receipt = values[6]; receipt["format_version"] = 99
        self.write(values[7], sealed(receipt))
        report = status.inspect_release(values[2])
        self.assertEqual("invalid", report["sections"]["evaluation"]["state"])

    def test_remote_receipt_reports_project_and_current_staleness(self):
        fixture = remote_fixtures.RemotePolicyIntegrationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        values, _, receipt, _, _, report_path = fixture.imported()
        fixture.promote(values, report_path)
        at = datetime.fromisoformat(receipt["checked_at_utc"]) + timedelta(seconds=61)
        report = status.inspect_release(values[2], now=at)
        self.assertEqual("passed", report["sections"]["evaluation"]["state"], report["sections"]["evaluation"])
        self.assertEqual("stale", report["sections"]["remote"]["state"], report["sections"]["remote"])
        self.assertEqual(receipt["project_id"], report["project_id"])
        self.assertEqual("recorded_promoted", report["sections"]["promotion"]["state"])
        self.assertEqual("refresh_remote_verification", report["next_steps"][0]["action"])

    def test_missing_required_remote_receipt_directs_readback_before_promotion(self):
        fixture = remote_fixtures.RemotePolicyIntegrationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        values, _, _, receipt_path, _, _ = fixture.imported()
        receipt_path.unlink()
        report = status.inspect_release(values[2])
        self.assertEqual("missing", report["sections"]["remote"]["state"])
        self.assertTrue(report["sections"]["remote"]["required"])
        actions = {step["action"] for step in report["next_steps"]}
        self.assertIn("refresh_remote_verification", actions)
        self.assertNotIn("review_promotion", actions)

    def test_rollout_requires_original_policy_and_window(self):
        self.prepare(promoted=True)
        report = sealed({"format_version": 1, "mode": "rollout_observation", "status": "complete",
                         "promotion_sha256": fingerprint(self.promotion), "dependency_lock_sha256": fingerprint(self.lock),
                         "recorded_at_utc": self.now.isoformat(), "counts": {"expected_consumer_prompt_pairs": 2, "observed_consumer_prompt_pairs": 2}})
        self.write(self.root / "rollout.json", report)
        actual = self.inspect()["sections"]["rollout"]
        self.assertEqual("unverified", actual["state"])
        self.assertEqual("incomplete_sources", actual["validation"])

    def test_malformed_rollout_provenance_is_invalid(self):
        self.prepare(promoted=True)
        for malformed in (None, [], "bad"):
            with self.subTest(provenance=malformed):
                report = sealed({"format_version": 1, "mode": "rollout_observation", "status": "complete",
                    "promotion_sha256": fingerprint(self.promotion), "dependency_lock_sha256": fingerprint(self.lock),
                    "recorded_at_utc": self.now.isoformat(), "provenance": malformed})
                self.write(self.root / "rollout.json", report)
                section = self.inspect()["sections"]["rollout"]
                self.assertEqual("invalid", section["state"])
                self.assertEqual("Rollout provenance metadata must be an object.", section["messages"][0])

    def test_rollout_coverage_recomputed_and_ages_out(self):
        self.prepare(promoted=True)
        rules = {"format_version": 1, "id": "test-window", "required_consumers": {
            "api": {"deployment_id": "app@abc", "sdk": {"name": "langfuse-python", "version": "5.2.0"}}},
            "window_seconds": 4, "bucket_seconds": 2, "min_unique_requests_per_target": 2,
            "min_requests_per_bucket": 1, "min_covered_buckets": 2, "max_observation_age_seconds": 30,
            "max_latest_observation_age_seconds": 5, "max_capture_delay_seconds": 2,
            "max_fallback_rate": 0, "max_mismatch_rate": 0,
            "provenance": {"required": False, "trust_policy_sha256": None}}
        start = self.now + timedelta(seconds=1)
        window = rollout.pin_rollout_window(rules, self.promotion, self.lock, window_id="window-1", start_at_utc=start, now=self.now)
        samples = []
        observation = self.observation()
        for offset in (0.5, 2.5):
            at = start + timedelta(seconds=offset)
            sample = {**observation, "request_id": f"req-{offset}", "observed_at_utc": at.isoformat(),
                      "request_started_at_utc": (at - timedelta(seconds=0.1)).isoformat()}
            samples.append(rollout.bind_rollout_observation(sample, window, now=at))
        end = start + timedelta(seconds=4)
        receipt = rollout.build_rollout_report(samples, window, rules, self.promotion, self.lock, now=end)
        self.assertEqual("complete", receipt["status"])
        for name, value in [("window.json", window), ("rollout-policy.json", rules), ("rollout.json", receipt)]:
            self.write(self.root / name, value)
        section = self.inspect(now=end)["sections"]["rollout"]
        self.assertEqual("complete", section["state"], section)
        self.assertEqual("coverage_recomputed", section["validation"])
        self.assertEqual(1, section["counts"]["observed_consumer_prompt_pairs"])
        self.assertEqual("stale", self.inspect(now=end + timedelta(seconds=40))["sections"]["rollout"]["state"])

    def test_cli_missing_reports_is_inspection_success_not_release_success(self):
        self.prepare()
        with patch("sys.stdout", new=io.StringIO()) as output:
            code = status.main(["--workspace", str(self.root), "--format", "json"])
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertEqual("attention", result["status"])
        self.assertEqual("missing", result["sections"]["promotion"]["state"])


if __name__ == "__main__":
    unittest.main()
