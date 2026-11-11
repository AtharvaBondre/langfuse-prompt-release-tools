"""Offline rollout aggregation tests; samples are synthetic, not traffic proof."""
import copy
import hashlib
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import langfuse_release_rollout as rollout
from langfuse_release_common import ReleaseError, fingerprint
from test_release_dependencies import ResolverFixture


NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def sealed(value):
    value = copy.deepcopy(value)
    value.pop("evidence_sha256", None)
    value["evidence_sha256"] = fingerprint(value)
    return value


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.api = ResolverFixture()
        self.lock = self.api.lock()
        self.lock["recorded_at_utc"] = (NOW - timedelta(seconds=60)).isoformat()
        self.lock = sealed(self.lock)
        self.promotion = {"format_version": 2, "mode": "promote", "status": "verified", "apply": True,
            "host": self.api.host, "label": "production", "snapshot_sha256": self.lock["snapshot_sha256"],
            "stage_report": {"sha256": self.lock["stage_report_sha256"]}, "dependency_lock_sha256": fingerprint(self.lock),
            "completed_at_utc": (NOW - timedelta(seconds=40)).isoformat(),
            "results": [{"name": "root", "baseline_version": 1, "target_version": 2, "status": "verified"}]}
        self.policy = {"format_version": 1, "id": "support-rollout/v1", "required_consumers": {
            "api": {"deployment_id": "api@abc", "sdk": {"name": "langfuse-python", "version": "5.2.0"}},
            "worker": {"deployment_id": "worker@def", "sdk": {"name": "langfuse-js", "version": "6.3.0"}}},
            "window_seconds": 4, "bucket_seconds": 2, "min_unique_requests_per_target": 2,
            "min_requests_per_bucket": 1, "min_covered_buckets": 2, "max_observation_age_seconds": 30,
            "max_latest_observation_age_seconds": 5, "max_capture_delay_seconds": 2,
            "max_fallback_rate": 0, "max_mismatch_rate": 0, "provenance": {"required": False, "trust_policy_sha256": None}}
        self.start = NOW + timedelta(seconds=1)
        self.window = self.pin()
        self.finish = self.start + timedelta(seconds=4)

    def pin(self, window_id="rollout-1"):
        return rollout.pin_rollout_window(self.policy, self.promotion, self.lock, window_id=window_id,
                                         start_at_utc=self.start, now=NOW)

    def sample(self, consumer="api", offset=0.5, request_id=None, *, updates=None, received=None, window=None):
        expected = self.policy["required_consumers"][consumer]
        observed_at = self.start + timedelta(seconds=offset)
        observation = {"format_version": 1, "capture_method": rollout.CAPTURE_METHODS[expected["sdk"]["name"]],
            "consumer_id": consumer, "deployment_id": expected["deployment_id"], "request_id": request_id or f"{consumer}-{offset}",
            "sdk": copy.deepcopy(expected["sdk"]), "requested_label": "production", "request_started_at_utc": (observed_at - timedelta(seconds=0.1)).isoformat(),
            "observed_at_utc": observed_at.isoformat(), "cache_ttl_seconds": 0, "cache_status": "disabled", "is_fallback": False,
            "returned": {"name": "root", "version": 2, "type": "text", "resolved_sha256": self.lock["roots"][0]["resolved_sha256"]}}
        if updates:
            observation.update(updates)
        return rollout.bind_rollout_observation(observation, window or self.window, now=received or observed_at)

    def samples(self):
        return [self.sample(consumer, offset) for consumer in ("api", "worker") for offset in (0.5, 2.5)]

    def report(self, samples=None, *, now=None, provenance=None):
        return rollout.build_rollout_report(self.samples() if samples is None else samples, self.window, self.policy,
                                             self.promotion, self.lock, now=now or self.finish, provenance=provenance)

    def check(self, report, *, now=None, provenance=None):
        return rollout.check_rollout_report(report, self.window, self.policy, self.promotion, self.lock,
                                             now=now or self.finish, provenance=provenance)

    def codes(self, report):
        return {issue["code"] for issue in report["issues"]}

    def test_two_consumers_two_buckets_complete_with_honest_counts(self):
        report = self.report()
        self.assertEqual("complete", report["status"])
        self.assertEqual(4, report["counts"]["unique_request_prompt_events"])
        self.assertEqual(2, report["counts"]["observed_consumer_prompt_pairs"])
        for row in report["consumers"]:
            self.assertEqual(2, row["clean_unique_requests"])
            self.assertEqual(2, row["covered_buckets"])
            self.assertEqual(1, row["coverage_fraction"])
            self.assertEqual(0, row["fallback_rate"])
            self.assertEqual({"2": 2}, row["version_counts"])
        self.assertEqual("unsigned_local", report["provenance"]["classification"])
        self.assertEqual("supplied_instrumented_samples_only", report["scope"])
        self.assertEqual(report, self.check(report))

    def test_clean_samples_still_collecting_until_window_closes(self):
        report = self.report(now=self.start + timedelta(seconds=3))
        self.assertEqual("collecting", report["status"])
        self.assertEqual("continue_observation", report["recovery_proposal"]["action"])
        with self.assertRaisesRegex(ReleaseError, "not complete"):
            self.check(report)

    def test_missing_consumer_and_empty_window_cannot_complete(self):
        report = self.report([self.sample(offset=0.5), self.sample(offset=2.5)])
        self.assertEqual("failed", report["status"])
        self.assertIn("missing_consumer_observations", self.codes(report))
        worker = next(row for row in report["consumers"] if row["consumer_id"] == "worker")
        self.assertIsNone(worker["fallback_rate"])
        self.assertEqual(0, worker["observations"])
        self.assertEqual("failed", self.report([])["status"])

    def test_insufficient_window_coverage_is_distinct_from_sample_count(self):
        samples = [self.sample(consumer, offset) for consumer in ("api", "worker") for offset in (0.3, 0.7, 1.2)]
        report = self.report(samples)
        self.assertNotIn("insufficient_clean_unique_requests", self.codes(report))
        self.assertIn("insufficient_time_bucket_coverage", self.codes(report))
        self.assertEqual(0.5, report["consumers"][0]["coverage_fraction"])

    def test_duplicate_and_replayed_request_ids_do_not_inflate_counts(self):
        samples = self.samples()
        samples.append(copy.deepcopy(samples[0]))
        samples.append(self.sample(offset=1.5, request_id="api-0.5"))
        report = self.report(samples)
        self.assertEqual("failed", report["status"])
        self.assertEqual(2, report["counts"]["duplicate_events"])
        self.assertEqual(4, report["counts"]["unique_request_prompt_events"])
        self.assertIn("duplicate_or_replayed_request", self.codes(report))

    def test_mixed_old_new_versions_and_content_have_zero_tolerance(self):
        samples = self.samples()
        old = copy.deepcopy(samples[0]["observation"]["returned"])
        old.update(version=1, resolved_sha256="0" * 64)
        samples.append(self.sample(offset=1.5, updates={"returned": old}))
        report = self.report(samples)
        self.assertEqual("failed", report["status"])
        self.assertTrue({"version_mismatch", "content_mismatch"} <= self.codes(report))
        self.assertEqual({"2": 2, "1": 1}, report["consumers"][0]["version_counts"])
        self.assertEqual(1 / 3, report["consumers"][0]["mismatch_rate"])
        proposal = report["recovery_proposal"]
        self.assertEqual("review_rollback", proposal["action"])
        self.assertEqual(1, proposal["rollback_candidates"][0]["candidate_previous_version"])
        self.assertFalse(proposal["rollback_candidates"][0]["current_state_verified"])

    def test_wrong_deployment_sdk_label_and_type_are_reported(self):
        variants = [({"deployment_id": "api@old"}, "deployment_mismatch"),
                    ({"sdk": {"name": "langfuse-python", "version": "4.0.0"}}, "sdk_mismatch"),
                    ({"requested_label": "staging"}, "label_mismatch"),
                    ({"returned": {**self.samples()[0]["observation"]["returned"], "type": "chat"}}, "prompt_type_mismatch")]
        for updates, code in variants:
            with self.subTest(code=code):
                report = self.report(self.samples() + [self.sample(offset=1.5, updates=updates)])
                self.assertIn(code, self.codes(report))
                self.assertEqual("failed", report["status"])

    def test_fallback_rates_never_supply_clean_coverage(self):
        fallback = {**self.samples()[0]["observation"]["returned"], "version": 0, "resolved_sha256": "0" * 64}
        samples = self.samples() + [self.sample(offset=1.5, updates={"is_fallback": True, "returned": fallback})]
        report = self.report(samples)
        self.assertIn("fallback_rate_exceeded", self.codes(report))
        self.assertNotIn("version_mismatch", self.codes(report))
        self.policy["max_fallback_rate"] = 0.5
        self.window = self.pin()
        samples = self.samples() + [self.sample(offset=1.5, updates={"is_fallback": True, "returned": fallback})]
        report = self.report(samples)
        self.assertEqual("complete", report["status"])
        self.assertEqual(2, report["consumers"][0]["clean_unique_requests"])
        self.assertAlmostEqual(1 / 3, report["consumers"][0]["fallback_rate"])

    def test_recent_fallback_cannot_hide_stale_latest_clean_access(self):
        self.policy.update(min_covered_buckets=1, min_unique_requests_per_target=1, max_latest_observation_age_seconds=1.5, max_fallback_rate=1)
        self.window = self.pin()
        samples = [self.sample(c, 0.5) for c in ("api", "worker")]
        samples += [self.sample(c, 3.5, updates={"is_fallback": True}) for c in ("api", "worker")]
        report = self.report(samples)
        self.assertIn("missing_or_stale_latest_observation", self.codes(report))
        self.assertEqual(0.5, report["consumers"][0]["latest_age_seconds"])
        self.assertEqual(3.5, report["consumers"][0]["latest_clean_age_seconds"])

    def test_wrong_consumer_or_prompt_is_not_silently_filtered(self):
        for updates in ({"consumer_id": "unknown"}, {"returned": {**self.samples()[0]["observation"]["returned"], "name": "other"}}):
            report = self.report(self.samples() + [self.sample(offset=1.5, updates=updates)])
            self.assertIn("unexpected_consumer_or_prompt", self.codes(report))

    def test_window_is_half_open_and_requests_must_start_within_it(self):
        for offset in (-1, 0, 4, 5):
            with self.subTest(offset=offset):
                report = self.report(self.samples() + [self.sample(offset=offset)], now=self.finish + timedelta(seconds=2))
                self.assertIn("outside_window_or_invalid_access_time", self.codes(report))

    def test_stale_future_late_and_reversed_collection_cannot_pass(self):
        variants = [(self.sample(offset=1.5, received=self.start + timedelta(seconds=4)), "late_capture_or_replay"),
                    (self.sample(offset=1.5, received=self.start + timedelta(seconds=1)), "future_or_reversed_collection_time"),
                    (self.sample(offset=1.5, received=self.finish + timedelta(seconds=1)), "future_or_reversed_collection_time")]
        for sample, code in variants:
            with self.subTest(code=code):
                self.assertIn(code, self.codes(self.report(self.samples() + [sample])))
        self.assertIn("stale_observation", self.codes(self.report(now=self.finish + timedelta(seconds=31))))
        with self.assertRaisesRegex(ReleaseError, "freshness"):
            self.check(self.report(), now=self.finish + timedelta(seconds=10))

    def test_policy_window_and_release_substitution_are_rejected(self):
        samples = self.samples()
        self.policy["min_unique_requests_per_target"] = 1
        with self.assertRaisesRegex(ReleaseError, "substituted"):
            self.report(samples)
        self.policy["min_unique_requests_per_target"] = 2
        self.window = self.pin("different-window")
        with self.assertRaisesRegex(ReleaseError, "Replayed or substituted"):
            self.report(samples)
        self.promotion["completed_at_utc"] = (NOW - timedelta(seconds=39)).isoformat()
        with self.assertRaisesRegex(ReleaseError, "substituted"):
            self.report([])

    def test_observation_or_report_tampering_is_rejected(self):
        samples = self.samples()
        samples[0]["observation"]["request_id"] = "changed"
        with self.assertRaisesRegex(ReleaseError, "changed since capture"):
            self.report(samples)
        report = self.report()
        report["consumers"][0]["unique_requests"] = 999
        with self.assertRaisesRegex(ReleaseError, "changed since capture"):
            self.check(report)
        report = sealed(report)
        with self.assertRaisesRegex(ReleaseError, "computed results changed"):
            self.check(report)

    def test_pin_cannot_be_retrospective_or_precede_verified_promotion(self):
        with self.assertRaisesRegex(ReleaseError, "retrospective"):
            rollout.pin_rollout_window(self.policy, self.promotion, self.lock, window_id="late", start_at_utc=NOW, now=self.start)
        self.promotion["status"] = "stopped"
        with self.assertRaises(ReleaseError):
            self.pin()
        self.promotion["status"] = "verified"
        del self.promotion["dependency_lock_sha256"]
        with self.assertRaises(ReleaseError):
            self.pin()

    def test_policy_rejects_bool_nan_weak_mismatch_and_unknown_fields(self):
        variants = [("window_seconds", True), ("bucket_seconds", 3), ("min_covered_buckets", 3),
                    ("min_unique_requests_per_target", 0), ("max_fallback_rate", float("nan")),
                    ("max_fallback_rate", 2), ("max_mismatch_rate", 0.1), ("max_mismatch_rate", False),
                    ("max_capture_delay_seconds", -1), ("unknown", True)]
        for field, value in variants:
            policy = copy.deepcopy(self.policy); policy[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ReleaseError):
                rollout.validate_rollout_policy(policy)

    def test_bad_sdk_method_type_and_naive_timestamp_are_rejected(self):
        for updates in ({"capture_method": "invented"}, {"is_fallback": "false"},
                        {"observed_at_utc": "2026-11-11T12:00:00"}, {"cache_status": "hit"},
                        {"returned": {"name": "root", "version": True, "type": "text", "resolved_sha256": "0" * 64}}):
            with self.subTest(updates=updates), self.assertRaises(ReleaseError):
                self.sample(updates=updates)

    def test_optional_provenance_is_unsigned_and_claimed_verified_flag_is_rejected(self):
        self.assertEqual("unsigned_local", self.report()["provenance"]["classification"])
        with self.assertRaisesRegex(ReleaseError, "unsupported fields"):
            self.report(provenance={"verified": True})
        self.policy["provenance"] = {"required": True, "trust_policy_sha256": "0" * 64}
        self.window = self.pin()
        self.assertIn("authenticated_provenance_missing", self.codes(self.report()))

    def test_actual_verifier_is_called_and_artifact_and_external_trust_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            trust = {"independent_test_trust": "expected"}
            self.policy["provenance"] = {"required": True, "trust_policy_sha256": fingerprint(trust)}
            self.window = self.pin()
            samples = self.samples()
            artifact = root / "observations.json"; trust_path = root / "trust.json"
            artifact.write_text(json.dumps({"format_version": 1, "window_sha256": fingerprint(self.window), "observations": samples}))
            trust_path.write_text(json.dumps(trust))
            result = {"classification": "verified_ci", "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                      "trust_policy_sha256": fingerprint(trust), "identity": {"repository": "synthetic/test"}, "verifier": "mock-for-offline-test"}
            verifier = unittest.mock.Mock(return_value=result)
            request = {"artifact_path": str(artifact), "trust_policy_path": str(trust_path)}
            with patch.dict("sys.modules", {"langfuse_release_provenance": types.SimpleNamespace(verify_provenance=verifier)}):
                report = self.report(samples, provenance=request)
                self.assertEqual("complete", report["status"])
                self.assertEqual("verified_ci", report["provenance"]["classification"])
                verifier.assert_called_once()
                self.check(report, provenance=request)
                # A self-claimed authentication result cannot substitute another artifact.
                verifier.return_value = {**result, "artifact_sha256": "0" * 64}
                with self.assertRaisesRegex(ReleaseError, "does not bind"):
                    self.report(samples, provenance=request)
            trust_path.write_text(json.dumps({"independent_test_trust": "changed"}))
            with self.assertRaisesRegex(ReleaseError, "independently reviewed"):
                self.report(samples, provenance=request)

    def test_provenance_for_other_observations_or_window_is_rejected_before_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            trust = {"test": True}
            self.policy["provenance"]["trust_policy_sha256"] = fingerprint(trust)
            self.window = self.pin()
            (root / "trust.json").write_text(json.dumps(trust))
            artifact = {"format_version": 1, "window_sha256": fingerprint(self.window), "observations": []}
            (root / "observations.json").write_text(json.dumps(artifact))
            with self.assertRaisesRegex(ReleaseError, "different observations"):
                self.report(provenance={"artifact_path": root / "observations.json", "trust_policy_path": root / "trust.json"})

    def test_cached_old_version_is_reported_separately_from_cache_uncertainty(self):
        old = {**self.samples()[0]["observation"]["returned"], "version": 1}
        sample = self.sample(offset=1.5, updates={"returned": old, "cache_ttl_seconds": 60, "cache_status": "sdk_cache_possible"})
        report = self.report(self.samples() + [sample])
        self.assertIn("version_mismatch", self.codes(report))
        self.assertEqual(1, report["consumers"][0]["cache_possible_count"])

    def test_provenance_artifact_mutation_during_verification_cannot_authenticate_old_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            trust = {"test_trust": "pinned"}
            self.policy["provenance"] = {"required": True, "trust_policy_sha256": fingerprint(trust)}
            self.window = self.pin()
            samples = self.samples()
            artifact_path, trust_path = root / "samples.json", root / "trust.json"
            artifact = {"format_version": 1, "window_sha256": fingerprint(self.window), "observations": samples}
            artifact_path.write_text(json.dumps(artifact))
            trust_path.write_text(json.dumps(trust))
            def changed_artifact(*args, **kwargs):
                artifact["observations"] = []
                artifact_path.write_text(json.dumps(artifact))
                return {"classification": "verified_ci", "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                        "trust_policy_sha256": fingerprint(trust)}
            with patch.dict("sys.modules", {"langfuse_release_provenance": types.SimpleNamespace(verify_provenance=changed_artifact)}):
                with self.assertRaisesRegex(ReleaseError, "does not bind"):
                    self.report(samples, provenance={"artifact_path": artifact_path, "trust_policy_path": trust_path})

    def test_required_provenance_cannot_be_satisfied_by_unsigned_verifier_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            trust = {"allow_unsigned": True}
            self.policy["provenance"] = {"required": True, "trust_policy_sha256": fingerprint(trust)}
            self.window = self.pin()
            samples = self.samples()
            artifact_path, trust_path = root / "samples.json", root / "trust.json"
            artifact_path.write_text(json.dumps({"format_version": 1, "window_sha256": fingerprint(self.window), "observations": samples}))
            trust_path.write_text(json.dumps(trust))
            result = {"classification": "unsigned_local", "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                      "trust_policy_sha256": fingerprint(trust)}
            with patch.dict("sys.modules", {"langfuse_release_provenance": types.SimpleNamespace(verify_provenance=lambda *a, **k: result)}):
                report = self.report(samples, provenance={"artifact_path": artifact_path, "trust_policy_path": trust_path})
                self.assertEqual("failed", report["status"])
                self.assertIn("authenticated_provenance_missing", self.codes(report))

    def test_report_and_recovery_are_deterministic_and_send_no_api_requests(self):
        old = {**self.samples()[0]["observation"]["returned"], "version": 1}
        samples = self.samples() + [self.sample(offset=1.5, updates={"returned": old})]
        with patch("langfuse_release_common.Client._request", side_effect=AssertionError("No API requests are authorized")):
            first, second = self.report(samples), self.report(samples)
        self.assertEqual(first, second)
        self.promotion["results"][0]["status"] = "promotion_outcome_unknown_do_not_retry"
        with self.assertRaises(ReleaseError):
            self.report(samples)

    def test_failed_reports_persist_exclusively_and_recovery_never_mutates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.json"
            report = rollout.write_rollout_report(path, [], self.window, self.policy, self.promotion, self.lock, now=self.finish)
            self.assertEqual("failed", json.loads(path.read_text())["status"])
            self.assertEqual(0, report["recovery_proposal"]["mutations_performed"])
            self.assertFalse(report["recovery_proposal"]["automatic_rollback"])
            self.assertFalse(report["recovery_proposal"]["retry_unknown_post"])
            with self.assertRaisesRegex(ReleaseError, "already exists"):
                rollout.write_rollout_report(path, self.samples(), self.window, self.policy, self.promotion, self.lock, now=self.finish)


if __name__ == "__main__":
    unittest.main()
