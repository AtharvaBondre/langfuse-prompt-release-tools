"""Offline policy-v2 import, promotion and portable remote-receipt regressions.

The only persistence backend is synthetic GET-only HTTP. These tests establish
evidence consistency, not cryptographic authentication of a caller-owned file.
"""
import copy
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import import_langfuse_experiment as adapter
import langfuse_release_bundle as bundle
import langfuse_release_policy as policies
import langfuse_release_remote as remote
import promote_langfuse_prompts as promotion
import publish_langfuse_prompts as publish
from langfuse_release_common import ReleaseError, fingerprint
import test_experiment_policy as fixtures
from test_release_remote import HOST, PROJECT, PersistenceHTTP


def reseal(value):
    value["evidence_sha256"] = fingerprint({k: v for k, v in value.items() if k != "evidence_sha256"})
    return value


class RemotePolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExperimentPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root.resolve()
        original = self.fixture.policy

        def policy(*args, **kwargs):
            value = original(*args, **kwargs)
            value.update(format_version=2, remote_export="required", remote_verification={
                "project_id": PROJECT, "dataset_kind": "local", "max_age_seconds": 60})
            return value

        self.fixture.policy = policy

    def prepare(self):
        values = self.fixture.prepare(remote="required", producer="langfuse-python-sdk")
        source, capture = values[6:8]
        experiment = source["results"][0]["experiment_result"]
        experiment.update(experiment_id="synthetic-experiment", run_name="synthetic-run")
        for index, item in enumerate(experiment["item_results"]):
            item["trace_id"] = f"{index+1:032x}"
        capture["completion"]["remote_export"] = "unconfirmed"
        self.fixture.save(values[4], values[5], source, capture)
        policy = self.fixture.policy()
        policy["producer"]["kind"] = "langfuse-python-sdk"
        expected = remote.make_remote_expectation(source, capture, policy, host=HOST, project_id=PROJECT)
        backend = PersistenceHTTP(expected)
        backend.summary.update(startTime=capture["started_at_utc"], endTime=capture["completed_at_utc"])
        for row, item in zip(backend.items, experiment["item_results"]):
            row.update(startTime=capture["started_at_utc"], endTime=capture["completed_at_utc"],
                       input=json.dumps(item["item"]["input"]), output=json.dumps(item["output"]),
                       expectedOutput=json.dumps(item["item"]["expected_output"]))
        api = remote.RemoteClient(HOST, timeout=1)
        api._opener = backend
        receipt = remote.verify_remote(api, expected, wait_seconds=0)
        self.assertEqual("confirmed", receipt["status"], receipt["issues"])
        self.assertTrue(backend.requests)
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))
        path = values[2].parent / "remote-persistence.json"
        path.write_text(json.dumps(receipt))
        return values, expected, receipt, path

    def imported(self):
        values, expected, receipt, receipt_path = self.prepare()
        report = adapter.import_experiment(*values[2:6], remote_report_path=receipt_path, report_name="remote-evaluation.json")
        report_path = values[2].parent / "reports" / report["report_file"]
        return values, expected, receipt, receipt_path, report, report_path

    def promote(self, values, report_path):
        return promotion.promote(values[0], values[2], values[3], label="production", apply=True,
                                 evaluation_report=report_path, dependency_lock=values[9], report_name="remote-promotion.json")

    def assert_import_blocked(self, values, receipt_path):
        with self.assertRaises((ReleaseError, OSError, ValueError)):
            adapter.import_experiment(*values[2:6], remote_report_path=receipt_path, report_name="blocked.json")
        self.assertFalse((values[2].parent / "reports" / "blocked.json").exists())
        self.assertEqual([], values[1].requests)

    def test_v2_confirmed_receipt_is_bound_through_import_and_promotion(self):
        values, _, receipt, receipt_path, report, report_path = self.imported()
        self.assertEqual("unconfirmed", report["completion"]["remote_export"])
        self.assertEqual(fingerprint(receipt), report["remote_verification_sha256"])
        self.assertEqual({"file": str(receipt_path.resolve()), "sha256": fingerprint(receipt)}, report["remote_verification"])
        promoted = self.promote(values, report_path)
        self.assertEqual("verified", promoted["status"])
        self.assertEqual(1, len(values[1].patches))

    def test_caller_confirmed_string_cannot_satisfy_required_v2_remote_policy(self):
        values, _, _, _ = self.prepare()
        values[7]["completion"]["remote_export"] = "confirmed"
        self.fixture.save(values[4], values[5], values[6], values[7])
        self.assert_import_blocked(values, None)

    def test_missing_file_or_minimal_forged_confirmed_receipt_fails_before_output(self):
        values, _, _, receipt_path = self.prepare()
        self.assert_import_blocked(values, receipt_path.with_name("missing.json"))
        receipt_path.write_text(json.dumps(reseal({"format_version": 1, "mode": "remote_experiment_persistence", "status": "confirmed"})))
        self.assert_import_blocked(values, receipt_path)

    def test_rehashed_wrong_project_host_source_or_capture_is_rejected(self):
        values, _, receipt, receipt_path = self.prepare()
        for field, wrong in (("project_id", "other-project"), ("host", "https://other.example"),
                             ("source_results_sha256", "a" * 64), ("execution_capture_sha256", "b" * 64)):
            with self.subTest(field=field):
                changed = copy.deepcopy(receipt)
                changed[field] = wrong
                receipt_path.write_text(json.dumps(reseal(changed)))
                self.assert_import_blocked(values, receipt_path)

    def test_rehashed_incomplete_cases_or_typed_scores_cannot_be_imported(self):
        values, _, receipt, receipt_path = self.prepare()
        missing_case = copy.deepcopy(receipt)
        missing_case["observed_cases"].pop()
        missing_score = copy.deepcopy(receipt)
        missing_score["observed_cases"][0]["scores"].pop()
        wrong_type = copy.deepcopy(receipt)
        next(score for score in wrong_type["observed_cases"][0]["scores"] if score["name"] == "valid_json")["data_type"] = "NUMERIC"
        for index, changed in enumerate((missing_case, missing_score, wrong_type)):
            with self.subTest(mutation=index):
                receipt_path.write_text(json.dumps(reseal(changed)))
                self.assert_import_blocked(values, receipt_path)

    def test_expired_receipt_blocks_import_and_live_promotion_without_http(self):
        values, _, receipt, receipt_path, _, report_path = self.imported()
        future = datetime.fromisoformat(receipt["checked_at_utc"]) + timedelta(seconds=61)
        with patch.object(remote, "datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            self.assert_import_blocked(values, receipt_path)
            with self.assertRaisesRegex(ReleaseError, "stale"):
                self.promote(values, report_path)
        self.assertEqual([], values[1].requests)

    def test_remote_receipt_references_and_digests_cannot_be_removed_or_replaced(self):
        values, _, receipt, _, original, report_path = self.imported()
        mutations = []
        missing = copy.deepcopy(original)
        missing.pop("remote_verification")
        missing.pop("remote_verification_sha256")
        mutations.append(missing)
        wrong_digest = copy.deepcopy(original)
        wrong_digest["remote_verification"]["sha256"] = "f" * 64
        mutations.append(wrong_digest)
        wrong_path = copy.deepcopy(original)
        wrong_path["remote_verification"]["file"] = str(self.root / "missing.json")
        mutations.append(wrong_path)
        for report in mutations:
            with self.subTest(reference=report.get("remote_verification")):
                report_path.write_text(json.dumps(reseal(report)))
                with self.assertRaises((ReleaseError, OSError)):
                    self.promote(values, report_path)
                self.assertEqual([], values[1].requests)

    def test_receipt_rewrite_after_import_is_detected_even_if_remote_is_rehashed(self):
        values, _, receipt, receipt_path, _, report_path = self.imported()
        receipt["attempts"] += 1
        receipt_path.write_text(json.dumps(reseal(receipt)))
        with self.assertRaisesRegex(ReleaseError, "changed"):
            self.promote(values, report_path)
        self.assertEqual([], values[1].requests)

    def test_historical_bundle_remains_portable_after_remote_freshness_expires(self):
        values, _, receipt, _, _, report_path = self.imported()
        target = self.root / "portable.zip"
        created = bundle.create_bundle(values[2], values[3], report_path, target)
        shutil.rmtree(values[2].parent)
        future = datetime.fromisoformat(receipt["checked_at_utc"]) + timedelta(days=365)
        with patch.object(remote, "datetime", wraps=datetime) as clock, patch.object(
                adapter, "read_object", side_effect=AssertionError("Original-machine evidence reads forbidden")):
            clock.now.return_value = future
            checked = bundle.verify_bundle(target, expected_release_id=created["release_id"])
        self.assertEqual("verified", checked["status"])
        self.assertEqual("unsigned_local", checked["trust"]["classification"])
        self.assertTrue(any(row["role"] == "remote_verification" for row in checked["inventory"]["files"]))

    def test_historical_validation_still_rejects_receipt_not_fresh_at_import_time(self):
        values, _, receipt, _, report, _ = self.imported()
        _, snapshot = publish.load_snapshot(values[2])
        stage = json.loads(values[3].read_text())
        lock = json.loads(values[9].read_text())
        for seconds in (-1, 61):
            changed = copy.deepcopy(report)
            changed["recorded_at_utc"] = (datetime.fromisoformat(receipt["checked_at_utc"]) + timedelta(seconds=seconds)).isoformat()
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ReleaseError, "stale|future"):
                adapter.validate_experiment_artifacts(reseal(changed), snapshot, stage, values[6], values[7],
                                                      dependency_lock=lock, remote_receipt=receipt, historical=True)

    def test_remote_source_cannot_be_overwritten_by_import_output(self):
        values, _, receipt, receipt_path = self.prepare()
        report_path = values[2].parent / "reports" / "remote-source.json"
        report_path.write_text(json.dumps(receipt))
        before = report_path.read_bytes()
        with self.assertRaises((ReleaseError, OSError)):
            adapter.import_experiment(*values[2:6], remote_report_path=report_path, report_name=report_path.name)
        self.assertEqual(before, report_path.read_bytes())

    def test_policy_v2_cannot_omit_identity_age_or_downgrade_remote_requirement(self):
        original = self.fixture.policy()
        mutations = []
        for field in ("project_id", "dataset_kind", "max_age_seconds"):
            value = copy.deepcopy(original)
            value["remote_verification"].pop(field)
            mutations.append(value)
        for field, replacement in (("max_age_seconds", 0), ("max_age_seconds", True),
                                   ("max_age_seconds", 3601), ("dataset_kind", "guessed"), ("project_id", "")):
            value = copy.deepcopy(original)
            value["remote_verification"][field] = replacement
            mutations.append(value)
        value = copy.deepcopy(original); value.pop("remote_verification"); mutations.append(value)
        value = copy.deepcopy(original); value["remote_export"] = "optional"; mutations.append(value)
        value = copy.deepcopy(original); value["format_version"] = 1; mutations.append(value)
        for value in mutations:
            with self.subTest(policy=value), self.assertRaises(ReleaseError):
                policies.validate_policy(value)


if __name__ == "__main__":
    unittest.main()
