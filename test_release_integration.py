"""Cross-component policy/lock/promotion failure tests, using synthetic HTTP."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from langfuse_release_common import ReleaseError, fingerprint
import langfuse_release_dependencies as dependencies
import promote_langfuse_prompts as promotion
import test_experiment_policy as fixtures


class IntegratedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExperimentPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_promotion(self, values, **overrides):
        options = dict(label="production", apply=True, evaluation_report=values[-1],
                       dependency_lock=values[9], report_name="integration.json")
        options.update(overrides)
        return promotion.promote(values[0], values[2], values[3], **options, allow_unsigned_local=True)

    def test_completed_promotion_binds_evaluated_lock_and_completion_time(self):
        values = self.fixture.imported()
        report = self.run_promotion(values)
        self.assertEqual("verified", report["status"])
        self.assertEqual(values[-2]["dependency_lock_sha256"], report["dependency_lock_sha256"])
        self.assertEqual(fingerprint(json.loads(values[9].read_text())), report["dependency_lock_sha256"])
        self.assertIsNotNone(datetime.fromisoformat(report["completed_at_utc"]).tzinfo)
        self.assertEqual("passed", report["dependency_lock"]["status"])

    def test_new_valid_lock_cannot_replace_the_one_evaluated(self):
        values = self.fixture.imported()
        lock = json.loads(values[9].read_text())
        # Content and graph still match. Only the evidence identity differs.
        lock["consumer"]["compiler"] = "different-runner"
        lock["evidence_sha256"] = fingerprint({k: v for k, v in lock.items() if k != "evidence_sha256"})
        replacement = values[2].parent / "replacement.json"
        replacement.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ReleaseError, "lock captured by the evaluated task"):
            self.run_promotion(values, dependency_lock=replacement)
        self.assertEqual([], values[1].patches)

    def test_lock_is_rechecked_immediately_before_patch(self):
        values = self.fixture.imported()
        original = dependencies.validate_dependency_lock
        calls = []

        def validate(*args):
            calls.append(1)
            if len(calls) == 2:
                raise ReleaseError("Injected child-label movement after preflight")
            return original(*args)

        with patch.object(dependencies, "validate_dependency_lock", side_effect=validate):
            with self.assertRaisesRegex(ReleaseError, "after preflight"):
                self.run_promotion(values)
        self.assertEqual(2, len(calls))
        self.assertEqual([], values[1].patches)

    def test_dependency_drift_after_write_records_partial_outcome(self):
        values = self.fixture.imported(("first", "second"))
        original = values[0].get_resolved

        def resolved(name, *, version):
            response = original(name, version=version)
            if values[1].patches and name == "second":
                response["prompt"] += " drift"
            return response

        values[0].get_resolved = resolved
        with self.assertRaises(ReleaseError):
            self.run_promotion(values)
        report = json.loads((values[2].parent / "reports/integration.json").read_text())
        self.assertEqual(1, len(values[1].patches))
        self.assertEqual("readback", report["phase"])
        self.assertEqual("patch_returned_but_verification_failed", report["results"][0]["status"])
        self.assertEqual("blocked", report["dependency_lock"]["status"])
        self.assertNotIn("completed_at_utc", report)
        self.assertIn("production", values[1].versions["second"][7]["labels"])

    def test_promotion_report_cannot_overwrite_its_dependency_lock(self):
        values = self.fixture.imported()
        existing = values[9].read_bytes()
        # Put an independent copy inside reports, then attempt a name collision.
        path = values[2].parent / "reports/lock-source.json"
        path.write_bytes(existing)
        with self.assertRaisesRegex(ReleaseError, "overwrite"):
            self.run_promotion(values, dependency_lock=path, report_name="lock-source.json")
        self.assertEqual(existing, path.read_bytes())
        self.assertEqual([], values[1].patches)

    def test_already_promoted_path_rechecks_dependencies_before_green(self):
        values = self.fixture.imported()
        self.run_promotion(values)
        values[1].requests.clear()
        original_inspect, original_resolved = promotion._inspect, values[0].get_resolved
        inspected = []

        def inspect(*args):
            result = original_inspect(*args)
            inspected.append(True)
            return result

        def resolved(*args, **kwargs):
            result = original_resolved(*args, **kwargs)
            if inspected:
                result["prompt"] += " changed after preflight"
            return result

        values[0].get_resolved = resolved
        with patch.object(promotion, "_inspect", side_effect=inspect), self.assertRaises(ReleaseError):
            self.run_promotion(values, report_name="already-promoted.json")
        report = json.loads((values[2].parent / "reports/already-promoted.json").read_text())
        self.assertEqual("stopped", report["status"])
        self.assertEqual("final_verification", report["phase"])
        self.assertNotIn("completed_at_utc", report)
        self.assertEqual([], values[1].patches)


if __name__ == "__main__":
    unittest.main()
