"""Consumer recorder tests with SDK-shaped classes; no installed SDK or traffic."""
import copy
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import langfuse_release_adoption as adoption
from langfuse_release_common import ReleaseError, fingerprint
from test_release_dependencies import ResolverFixture


NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
REQUIRED = {"support-api": "app@abc123"}


class TextPromptClient:
    def __init__(self, name="root", version=2, content="R C L", fallback=False):
        self.name, self.version, self.prompt, self.config, self.is_fallback = name, version, content, {}, fallback


class ChatPromptClient(TextPromptClient):
    pass


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.api = ResolverFixture()
        self.lock = self.api.lock()
        self.lock["recorded_at_utc"] = (NOW - timedelta(seconds=30)).isoformat()
        self.lock["evidence_sha256"] = fingerprint({key: value for key, value in self.lock.items() if key != "evidence_sha256"})
        self.promotion = {"format_version": 2, "mode": "promote", "apply": True, "status": "verified",
                          "host": self.api.host, "label": "production", "snapshot_sha256": self.lock["snapshot_sha256"],
                          "stage_report": {"sha256": self.lock["stage_report_sha256"]},
                          "dependency_lock_sha256": fingerprint(self.lock),
                          "completed_at_utc": (NOW - timedelta(seconds=20)).isoformat(),
                          "results": [{"name": "root", "target_version": 2, "status": "verified"}]}
        model = types.SimpleNamespace(TextPromptClient=TextPromptClient, ChatPromptClient=ChatPromptClient)
        self.model_patch = patch.object(adoption.importlib, "import_module", return_value=model)
        self.version_patch = patch.object(adoption.importlib.metadata, "version", return_value="5.2.0")
        self.model_patch.start(); self.version_patch.start()
        self.addCleanup(self.model_patch.stop); self.addCleanup(self.version_patch.stop)

    def observation(self, prompt=None, **overrides):
        arguments = {"consumer_id": "support-api", "deployment_id": "app@abc123", "request_id": "request-123",
                     "requested_label": "production", "request_started_at_utc": NOW - timedelta(seconds=5),
                     "cache_ttl_seconds": 0, "observed_at_utc": NOW}
        arguments.update(overrides)
        return adoption.capture_sdk_observation(prompt if prompt is not None else TextPromptClient(), **arguments)

    def receipt(self, observations=None, **overrides):
        arguments = {"required_consumers": REQUIRED, "now": NOW}
        arguments.update(overrides)
        return adoption.build_adoption_receipt([self.observation()] if observations is None else observations,
                                               self.promotion, self.lock, **arguments)

    def check(self, receipt, **overrides):
        arguments = {"required_consumers": REQUIRED, "now": NOW}
        arguments.update(overrides)
        return adoption.check_adoption_receipt(receipt, self.promotion, self.lock, **arguments)

    def reasons(self, receipt):
        return {issue["reason"] for issue in receipt["issues"]}

    def test_real_object_fields_are_captured_and_valid_receipt_checks(self):
        observation = self.observation()
        self.assertEqual({"name": "langfuse-python", "version": "5.2.0"}, observation["sdk"])
        self.assertEqual("disabled", observation["cache_status"])
        self.assertEqual(self.lock["roots"][0]["resolved_sha256"], observation["returned"]["resolved_sha256"])
        receipt = self.receipt([observation])
        self.assertEqual("verified", receipt["status"])
        self.assertEqual(receipt, self.check(receipt))
        self.assertEqual("instrumented_sdk_accesses_only", receipt["scope"])

    def test_dict_or_lookalike_object_is_not_an_sdk_capture(self):
        for value in ({"name": "root", "version": 2}, types.SimpleNamespace(name="root", version=2)):
            with self.subTest(value=value), self.assertRaisesRegex(ReleaseError, "actual official SDK"):
                self.observation(value)
        class Subclass(TextPromptClient):
            pass
        with self.assertRaises(ReleaseError):
            self.observation(Subclass())

    def test_no_sdk_installed_reports_a_clear_error(self):
        with patch.object(adoption.importlib, "import_module", side_effect=ImportError):
            with self.assertRaisesRegex(ReleaseError, "Install the official"):
                self.observation()

    def test_type_spoofing_and_unknown_fallback_are_rejected(self):
        for bad in ("false", 0, None):
            with self.subTest(fallback=bad), self.assertRaises(ReleaseError):
                self.observation(TextPromptClient(fallback=bad))
        for version in (True, "2", -1, 0):
            with self.subTest(version=version), self.assertRaises(ReleaseError):
                self.observation(TextPromptClient(version=version))
        with self.assertRaises(ReleaseError):
            self.observation(TextPromptClient(content=[{"role": "user", "content": "bad type"}]))
        broken = TextPromptClient()
        del broken.is_fallback
        with self.assertRaisesRegex(ReleaseError, "missing returned"):
            self.observation(broken)

    def test_fallback_is_recorded_but_never_adopted_even_at_target_version(self):
        for version in (0, 2):
            receipt = self.receipt([self.observation(TextPromptClient(version=version, fallback=True))])
            self.assertEqual("not_adopted", receipt["status"])
            self.assertIn("fallback_used", self.reasons(receipt))
            with self.assertRaises(ReleaseError):
                self.check(receipt)

    def test_sdk_cache_possible_is_honest_and_matching_version_can_pass(self):
        observation = self.observation(cache_ttl_seconds=60)
        self.assertEqual("sdk_cache_possible", observation["cache_status"])
        self.assertEqual("verified", self.receipt([observation])["status"])
        stale = self.observation(TextPromptClient(version=1), cache_ttl_seconds=60)
        self.assertIn("returned_version_mismatch", self.reasons(self.receipt([stale])))

    def test_changed_resolved_dependency_content_prevents_adoption(self):
        receipt = self.receipt([self.observation(TextPromptClient(content="R C moved child"))])
        self.assertIn("resolved_content_mismatch", self.reasons(receipt))
        self.assertEqual("not_adopted", receipt["status"])

    def test_stale_observation_rejected_when_recording_and_on_later_recheck(self):
        receipt = self.receipt()
        with self.assertRaisesRegex(ReleaseError, "observation_stale"):
            self.check(receipt, now=NOW + timedelta(seconds=301))
        aged = self.receipt(now=NOW + timedelta(seconds=301))
        self.assertIn("observation_stale", self.reasons(aged))

    def test_access_must_start_after_promotion(self):
        observation = self.observation(request_started_at_utc=NOW - timedelta(seconds=30))
        self.assertIn("access_precedes_promotion", self.reasons(self.receipt([observation])))

    def test_future_observation_and_reversed_access_times_do_not_pass(self):
        observation = self.observation(observed_at_utc=NOW + timedelta(seconds=1))
        self.assertIn("invalid_access_time", self.reasons(self.receipt([observation])))
        with self.assertRaisesRegex(ReleaseError, "precedes"):
            self.observation(request_started_at_utc=NOW + timedelta(seconds=1))

    def test_naive_non_utc_and_malformed_times_are_rejected(self):
        for value in (NOW.replace(tzinfo=None), "2026-11-11T12:00:00", "2026-11-11T12:00:00+05:30", "invalid", None):
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                self.observation(request_started_at_utc=value)
        self.promotion["completed_at_utc"] = "2026-11-11T12:00:00"
        with self.assertRaises(ReleaseError):
            self.receipt()

    def test_missing_consumer_receipt_and_empty_observations_are_not_adopted(self):
        empty = self.receipt([])
        self.assertEqual({"missing_consumer_observation"}, self.reasons(empty))
        required = {**REQUIRED, "worker": "worker@1"}
        partial = self.receipt(required_consumers=required)
        self.assertIn({"consumer_id": "worker", "name": "root", "reason": "missing_consumer_observation"}, partial["issues"])

    def test_wrong_deployment_or_label_are_not_adopted(self):
        for field, value, reason in (("deployment_id", "app@old", "deployment_revision_mismatch"),
                                     ("requested_label", "staging", "requested_label_mismatch")):
            with self.subTest(field=field):
                observation = self.observation(**{field: value})
                self.assertIn(reason, self.reasons(self.receipt([observation])))

    def test_unexpected_prompt_or_consumer_is_not_adopted(self):
        for observation in (self.observation(TextPromptClient(name="other")), self.observation(consumer_id="other")):
            receipt = self.receipt([observation])
            self.assertIn("unexpected_consumer_or_prompt", self.reasons(receipt))
            self.assertIn("missing_consumer_observation", self.reasons(receipt))

    def test_duplicate_observation_and_tampered_cache_metadata_are_rejected(self):
        observation = self.observation()
        with self.assertRaisesRegex(ReleaseError, "one current observation"):
            self.receipt([observation, copy.deepcopy(observation)])
        observation["cache_status"] = "hit"
        with self.assertRaisesRegex(ReleaseError, "cache metadata"):
            self.receipt([observation])

    def test_tampered_receipt_or_different_promotion_is_rejected(self):
        receipt = self.receipt()
        changed = copy.deepcopy(receipt)
        changed["observations"][0]["returned"]["version"] = 10
        with self.assertRaisesRegex(ReleaseError, "changed since capture"):
            self.check(changed)
        self.promotion["completed_at_utc"] = (NOW - timedelta(seconds=19)).isoformat()
        with self.assertRaisesRegex(ReleaseError, "binding or policy"):
            self.check(receipt)

    def test_unknown_failed_conflicting_or_preview_promotion_is_rejected(self):
        for status in ("stopped", "preflight_passed", "dry_run_complete", "unknown"):
            self.promotion["status"] = status
            with self.subTest(status=status), self.assertRaises(ReleaseError):
                self.receipt()
        self.promotion["status"] = "verified"
        self.promotion["apply"] = False
        with self.assertRaises(ReleaseError):
            self.receipt()
        self.promotion["apply"] = True
        for status in ("promotion_outcome_unknown_do_not_retry", "promotion_failed", "conflicting", "pending"):
            self.promotion["results"][0]["status"] = status
            with self.subTest(target=status), self.assertRaises(ReleaseError):
                self.receipt()

    def test_promotion_and_lock_must_cover_identical_targets_and_bindings(self):
        self.promotion["results"] = []
        with self.assertRaises(ReleaseError):
            self.receipt()
        self.promotion["results"] = [{"name": "root", "target_version": True, "status": "verified"}]
        with self.assertRaises(ReleaseError):
            self.receipt()
        self.promotion["results"][0]["target_version"] = 2
        self.promotion["dependency_lock_sha256"] = "wrong"
        with self.assertRaisesRegex(ReleaseError, "exact dependency lock"):
            self.receipt()

    def test_missing_pre_promotion_lock_binding_is_rejected(self):
        del self.promotion["dependency_lock_sha256"]
        with self.assertRaisesRegex(ReleaseError, "exact dependency lock"):
            self.receipt()

    def test_posthoc_lock_cannot_establish_adoption_even_if_rebound(self):
        self.lock["recorded_at_utc"] = (NOW - timedelta(seconds=10)).isoformat()
        self.lock["evidence_sha256"] = fingerprint({key: value for key, value in self.lock.items() if key != "evidence_sha256"})
        self.promotion["dependency_lock_sha256"] = fingerprint(self.lock)
        with self.assertRaisesRegex(ReleaseError, "captured after promotion"):
            self.receipt()

    def test_policy_and_max_age_cannot_change_without_new_receipt(self):
        receipt = self.receipt()
        with self.assertRaises(ReleaseError):
            self.check(receipt, required_consumers={"support-api": "new-revision"})
        with self.assertRaises(ReleaseError):
            self.check(receipt, max_age_seconds=1000)
        for age in (True, 0, -1, float("nan")):
            with self.subTest(age=age), self.assertRaises(ReleaseError):
                self.receipt(max_age_seconds=age)

    def test_writing_preserves_not_adopted_outcomes_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adoption.json"
            receipt = adoption.write_adoption_receipt(path, [], self.promotion, self.lock, required_consumers=REQUIRED, now=NOW)
            self.assertEqual("not_adopted", receipt["status"])
            self.assertEqual(receipt, json.loads(path.read_text()))
            with self.assertRaisesRegex(ReleaseError, "already exists"):
                adoption.write_adoption_receipt(path, [self.observation()], self.promotion, self.lock, required_consumers=REQUIRED, now=NOW)


if __name__ == "__main__":
    unittest.main()
