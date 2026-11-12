"""Promotion-gate tests around mocked official verification, not signed evidence."""
import copy
import json
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import langfuse_release_bundle as bundle
import langfuse_release_trust as trust
import pull_langfuse_prompts as pull
import promote_langfuse_prompts as promotion
from langfuse_release_common import ReleaseError
from publish_langfuse_prompts import load_snapshot, write_report
import test_experiment_policy as fixtures


class TrustedPromotionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExperimentPolicyTests.setUp(self)
        self.root = self.root.resolve()
        self.values = self.imported()
        self.artifact = self.root / "release.zip"
        self.created = bundle.create_bundle(self.values[2], self.values[3], self.values[-1], self.artifact)
        self.attestation = self.root / "attestation.jsonl"
        self.attestation.write_text('{"fixture":"not a real signature"}')
        self.policy_path = self.root / "independently-reviewed-policy.json"
        self.signer_policy = {"format_version": 1, "repository": "owner/repo", "workflow": ".github/workflows/release.yml",
                              "source_commit": "a" * 40, "source_ref": "refs/heads/main",
                              "oidc_issuer": "https://token.actions.githubusercontent.com", "runner": "github-hosted",
                              "allow_unsigned": False}
        self.policy_path.write_text(json.dumps(self.signer_policy))
        self.which = patch("langfuse_release_provenance.shutil.which", return_value="/trusted/bin/gh")
        self.which.start(); self.addCleanup(self.which.stop)
        self.run = patch("langfuse_release_provenance.subprocess.run")
        self.mock_run = self.run.start(); self.addCleanup(self.run.stop)
        self.mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self.verified_output()), "")

    client = fixtures.ExperimentPolicyTests.client
    edit = fixtures.ExperimentPolicyTests.edit
    policy = fixtures.ExperimentPolicyTests.policy
    prepare = fixtures.ExperimentPolicyTests.prepare
    imported = fixtures.ExperimentPolicyTests.imported

    def snapshot(self, api, names=None, label="production", version=None):
        return pull.export(api, self.root / "snapshot", names=names, label=label, version=version,
                           project_id="synthetic-project")

    def verified_output(self):
        policy = self.signer_policy
        repo = "https://github.com/" + policy["repository"]
        san = f"{repo}/{policy['workflow']}@{policy['source_ref']}"
        return [{"verificationResult": {
            "signature": {"certificate": {"subjectAlternativeName": san, "issuer": policy["oidc_issuer"],
                "sourceRepositoryURI": repo, "sourceRepositoryDigest": policy["source_commit"],
                "sourceRepositoryRef": policy["source_ref"], "runnerEnvironment": "github-hosted",
                "buildSignerURI": san, "buildSignerDigest": policy["source_commit"]}},
            "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-11-11T01:00:00Z", "uri": "https://rekor.sigstore.dev"}],
            "statement": {"_type": "https://in-toto.io/Statement/v1", "predicateType": "https://slsa.dev/provenance/v1",
                          "subject": [{"name": "release.zip", "digest": {"sha256": self.created["artifact_sha256"]}}]}}}]

    def options(self, **changes):
        return {"expected_host": "https://example.com", "expected_project_id": "synthetic-project",
                "expected_label": "production", "expected_release_id": self.created["release_id"],
                "trust_policy_path": self.policy_path, "attestation_path": self.attestation, **changes}

    def enter(self, **changes):
        return trust.trusted_promotion(self.artifact, **self.options(**changes))

    def test_materializes_original_bytes_and_ignores_original_locators(self):
        raw_evaluation = self.values[-1].read_bytes()
        raw_capture = self.values[5].read_bytes()
        shutil.rmtree(self.values[2].parent)
        with patch("import_langfuse_experiment.read_object", side_effect=AssertionError("Old locators forbidden")):
            with self.enter() as evidence:
                self.assertEqual(raw_evaluation, evidence.evaluation_report_path.read_bytes())
                self.assertEqual(str(self.values[5]), json.loads(raw_evaluation)["execution_capture"]["file"])
                self.assertEqual(raw_capture, (evidence.dependency_lock_path.parent / "capture.json").read_bytes())
                _, snapshot = load_snapshot(evidence.manifest_path)
                stage = json.loads(evidence.stage_report_path.read_text())
                self.assertEqual("passed", evidence.validate_evaluation(snapshot, stage)["status"])
                evidence.assert_paths(evidence.manifest_path, evidence.stage_report_path,
                                      evidence.evaluation_report_path, evidence.dependency_lock_path)
                temporary = evidence.manifest_path.parent
                self.assertEqual("verified_ci", evidence.report["trust"]["classification"])
                self.assertEqual("synthetic-project", evidence.report["destination"]["project_id"])
        self.assertFalse(temporary.exists())

    def test_preview_and_apply_entries_each_run_the_official_verifier(self):
        for _ in ("preview", "apply", "already_promoted"):
            with self.enter() as evidence:
                self.assertEqual("verified_ci", evidence.report["trust"]["classification"])
        self.assertEqual(3, self.mock_run.call_count)
        command = self.mock_run.call_args[0][0]
        self.assertEqual(["/trusted/bin/gh", "attestation", "verify"], command[:3])
        self.assertIn("--source-digest", command)

    def test_required_gate_rejects_unsigned_missing_expectations_and_downgrade(self):
        for changes in ({"attestation_path": None}, {"trust_policy_path": None}, {"expected_release_id": None},
                        {"expected_project_id": ""}, {"allow_unsigned_local": True},
                        {"require_trusted_ci": False}, {"require_trusted_ci": 1}):
            with self.subTest(changes=changes), self.assertRaises(ReleaseError), self.enter(**changes):
                self.fail("Gate yielded unauthorized evidence")
        self.mock_run.assert_not_called()
        self.signer_policy["allow_unsigned"] = True
        self.policy_path.write_text(json.dumps(self.signer_policy))
        with self.assertRaisesRegex(ReleaseError, "permits unsigned"), self.enter():
            pass

    def test_explicit_unsigned_compatibility_is_classified_and_bad_signature_never_falls_back(self):
        with self.enter(require_trusted_ci=False, allow_unsigned_local=True, trust_policy_path=None,
                        attestation_path=None) as evidence:
            self.assertEqual("unsigned_local", evidence.report["trust"]["classification"])
            self.assertFalse(evidence.report["required_trusted_ci"])
        self.mock_run.assert_not_called()
        self.mock_run.return_value.returncode = 1
        with self.assertRaisesRegex(ReleaseError, "rejected"), self.enter(require_trusted_ci=False, allow_unsigned_local=True):
            pass

    def test_wrong_signer_saved_claims_and_replay_fail_closed(self):
        for field in ("sourceRepositoryURI", "subjectAlternativeName", "sourceRepositoryDigest", "sourceRepositoryRef",
                      "issuer", "runnerEnvironment", "buildSignerDigest"):
            output = self.verified_output()
            output[0]["verificationResult"]["signature"]["certificate"][field] = "unauthorized"
            self.mock_run.return_value.stdout = json.dumps(output)
            with self.subTest(field=field), self.assertRaises(ReleaseError), self.enter():
                pass
        self.attestation.write_text(json.dumps({"classification": "verified_ci", "authenticated": True}))
        self.mock_run.return_value.returncode = 1
        with self.assertRaisesRegex(ReleaseError, "rejected"), self.enter():
            pass
        with self.assertRaisesRegex(ReleaseError, "replay"), self.enter(expected_release_id="0" * 64):
            pass

    def test_destination_host_project_and_label_are_external_expectations(self):
        for changes in ({"expected_host": "https://other.example.com"}, {"expected_project_id": "other-project"},
                        {"expected_label": "staging"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ReleaseError, "destination"), self.enter(**changes):
                pass
        with self.assertRaisesRegex(ReleaseError, "non-latest"), self.enter(expected_label="latest"):
            pass
        with self.assertRaisesRegex(ReleaseError, "re-export"):
            trust._destination({"format_version": 1, "host": "https://example.com",
                                "selector": {"label": "production"}},
                               host="https://example.com", project_id="synthetic-project", label="production")

    def test_policy_must_be_independent_and_all_input_paths_regular(self):
        for role, source in (("trust_policy_path", self.artifact), ("attestation_path", self.policy_path)):
            with self.subTest(role=role), self.assertRaisesRegex(ReleaseError, "distinct"), self.enter(**{role: source}):
                pass
        link = self.root / "policy-link.json"
        link.symlink_to(self.policy_path)
        with self.assertRaisesRegex(ReleaseError, "symlinks"), self.enter(trust_policy_path=link):
            pass
        directory_link = self.root / "external-link"
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseError, "symlinks"), self.enter(trust_policy_path=directory_link / self.policy_path.name):
            pass

    def test_external_source_change_during_verification_is_detected(self):
        valid = self.mock_run.return_value
        def changed(*args, **kwargs):
            self.policy_path.write_text(json.dumps({**self.signer_policy, "allow_unsigned": True}))
            return valid
        self.mock_run.side_effect = changed
        with self.assertRaisesRegex(ReleaseError, "changed after verification"), self.enter():
            pass

    def test_copied_artifact_change_during_verification_is_detected(self):
        valid = self.mock_run.return_value
        def changed(command, **kwargs):
            path = Path(command[3]); path.chmod(0o600); path.write_bytes(b"replaced during verifier call")
            return valid
        self.mock_run.side_effect = changed
        with self.assertRaisesRegex(ReleaseError, "changed during verification"), self.enter():
            pass

    def test_changed_evidence_and_outside_role_substitution_are_blocked(self):
        with self.enter() as evidence:
            outside = self.root / "outside-stage.json"
            outside.write_bytes(evidence.stage_report_path.read_bytes())
            with self.assertRaisesRegex(ReleaseError, "exact materialized"):
                evidence.assert_paths(evidence.manifest_path, outside, evidence.evaluation_report_path, evidence.dependency_lock_path)
            evidence.stage_report_path.chmod(0o600)
            evidence.stage_report_path.write_bytes(evidence.stage_report_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ReleaseError, "changed after verification"):
                evidence.check_integrity()

    def test_materialized_symlink_escape_is_blocked(self):
        with self.enter() as evidence:
            outside = self.root / "outside.json"
            outside.write_bytes(evidence.stage_report_path.read_bytes())
            evidence.stage_report_path.chmod(0o600)  # Clear Windows read-only attribute for tamper injection.
            evidence.stage_report_path.unlink()
            evidence.stage_report_path.symlink_to(outside)
            with self.assertRaisesRegex(ReleaseError, "symlinks"):
                evidence.check_integrity()

    def test_materialized_reports_persist_and_evidence_has_no_execute_bits(self):
        destination = self.root / "promote-reviewed-release"
        with self.enter(materialize_dir=destination) as evidence:
            report_path = evidence.manifest_path.parent / "reports" / "promotion-preview.json"
            write_report(report_path, {"status": "dry_run_complete", "trust": evidence.report}, create=True)
            evidence.check_integrity()
            if os.name == "posix":
                self.assertEqual(0o700, stat.S_IMODE(destination.stat().st_mode))
                for path in (evidence.manifest_path, evidence.stage_report_path, evidence.evaluation_report_path):
                    self.assertEqual(0o400, stat.S_IMODE(path.stat().st_mode))
            altered = evidence.report; altered["trust"]["classification"] = "forged"
            self.assertEqual("verified_ci", evidence.report["trust"]["classification"])
        self.assertTrue(report_path.is_file())
        self.assertEqual("dry_run_complete", json.loads(report_path.read_text())["status"])
        with self.assertRaisesRegex(ReleaseError, "context has ended"):
            evidence.check_integrity()
        with self.assertRaisesRegex(ReleaseError, "absent directory"), self.enter(materialize_dir=destination):
            pass
        link = self.root / "linked-materialization"
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseError, "symlinks"), self.enter(materialize_dir=link):
            pass

    def test_materialization_fsync_failures_stop_before_promotion_patch(self):
        original_write, original_fsync = trust._write_private, os.fsync
        for location in ("copied-input", "materialized-evidence"):
            destination = self.root / location
            active_path, failures = [], []
            def tracked_write(path, raw):
                active_path.append(Path(path))
                try:
                    return original_write(path, raw)
                finally:
                    active_path.pop()
            def failed_fsync(descriptor):
                if active_path and stat.S_ISREG(os.fstat(descriptor).st_mode):
                    materialized = destination in active_path[-1].parents
                    if materialized == (location == "materialized-evidence"):
                        failures.append(active_path[-1])
                        raise OSError("Injected evidence fsync failure")
                return original_fsync(descriptor)
            self.values[1].requests.clear()
            with self.subTest(location=location), patch.object(trust, "_write_private", side_effect=tracked_write), \
                    patch.object(trust.os, "fsync", side_effect=failed_fsync):
                with self.assertRaisesRegex(OSError, "evidence fsync"), self.enter(materialize_dir=destination) as evidence:
                    self.promote(evidence, apply=True)
            self.assertEqual(1, len(failures))
            self.assertEqual([], self.values[1].requests)
            self.assertEqual([], self.values[1].patches)

    def test_materialization_directory_sync_failure_stops_before_promotion_patch(self):
        destination = self.root / "directory-sync-failure"
        original_sync, failures = trust.sync_directory, []
        def failed_sync(path):
            if Path(path) == destination:
                failures.append(path)
                raise OSError("Injected materialization directory sync failure")
            return original_sync(path)
        self.values[1].requests.clear()
        with patch.object(trust, "sync_directory", side_effect=failed_sync):
            with self.assertRaisesRegex(OSError, "directory sync"), self.enter(materialize_dir=destination) as evidence:
                self.promote(evidence, apply=True)
        self.assertEqual(1, len(failures))
        self.assertEqual([], self.values[1].requests)
        self.assertEqual([], self.values[1].patches)

    def test_evaluation_recheck_uses_current_freshness_and_rejects_substitution(self):
        with self.enter() as evidence:
            _, snapshot = load_snapshot(evidence.manifest_path)
            stage = json.loads(evidence.stage_report_path.read_text())
            with patch("langfuse_release_trust.validate_experiment_artifacts", wraps=trust.validate_experiment_artifacts) as validator:
                evidence.validate_evaluation(snapshot, stage)
                self.assertIs(False, validator.call_args.kwargs["historical"])
                self.assertIsNotNone(validator.call_args.kwargs["dependency_lock"])
            changed = copy.deepcopy(stage); changed["status"] = "substituted"
            with self.assertRaisesRegex(ReleaseError, "differs from the authenticated"):
                evidence.validate_evaluation(snapshot, changed)

    def test_stable_input_reader_detects_replacement_while_opening(self):
        source = self.root / "race-input.json"
        source.write_bytes(b"original")
        saved_open = os.open
        def replaced(path, flags, *args, **kwargs):
            if Path(path) == source:
                alternate = self.root / "replacement.json"
                alternate.write_bytes(b"changed")
                alternate.replace(source)
            return saved_open(path, flags, *args, **kwargs)
        with patch("langfuse_release_trust.os.open", side_effect=replaced):
            with self.assertRaisesRegex(ReleaseError, "replaced during"):
                trust._read_input(source)

    def test_stable_input_reader_accepts_different_path_and_descriptor_timestamp_representations(self):
        source = self.root / "portable-stat.json"
        source.write_bytes(b"original bytes")
        original_fstat = os.fstat
        def descriptor_stat(descriptor):
            value = original_fstat(descriptor)
            # Model Windows stat/fstat creation/change-time and precision
            # differences; neither view changes over the course of the read.
            return SimpleNamespace(**{field: getattr(value, field) for field in
                ("st_dev", "st_ino", "st_mode", "st_size")},
                st_mtime_ns=value.st_mtime_ns + 1_000_000,
                st_ctime_ns=value.st_ctime_ns - 10_000_000)
        with patch.object(trust.os, "fstat", side_effect=descriptor_stat):
            self.assertEqual(b"original bytes", trust._read_input(source))

    def test_stable_input_reader_still_detects_descriptor_changes_with_different_timestamps(self):
        source = self.root / "descriptor-race.json"
        source.write_bytes(b"original bytes")
        original_fstat = os.fstat
        for field in ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns"):
            calls = []
            def descriptor_stat(descriptor):
                value = original_fstat(descriptor)
                attributes = {name: getattr(value, name) for name in
                    ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")}
                attributes["st_mtime_ns"] += 1_000_000
                attributes["st_ctime_ns"] -= 10_000_000
                if calls:
                    attributes[field] += 1
                calls.append(descriptor)
                return SimpleNamespace(**attributes)
            with self.subTest(field=field), patch.object(trust.os, "fstat", side_effect=descriptor_stat):
                with self.assertRaisesRegex(ReleaseError, "changed during"):
                    trust._read_input(source)

    def test_stable_input_reader_still_detects_path_changes_after_descriptor_closes(self):
        source = self.root / "path-race.json"
        source.write_bytes(b"original bytes")
        original_stat = Path.stat
        for field in ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns"):
            calls = []
            def path_stat(path, *args, **kwargs):
                value = original_stat(path, *args, **kwargs)
                if path != source or kwargs.get("follow_symlinks") is False:
                    return value
                attributes = {name: getattr(value, name) for name in
                    ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")}
                if calls:
                    attributes[field] += 1
                calls.append(path)
                return SimpleNamespace(**attributes)
            with self.subTest(field=field), patch.object(Path, "stat", path_stat):
                with self.assertRaisesRegex(ReleaseError, "changed during"):
                    trust._read_input(source)

    def test_stable_input_reader_requires_same_size_across_path_descriptor_and_bytes(self):
        source = self.root / "size-race.json"
        source.write_bytes(b"original bytes")
        original_fstat = os.fstat
        def descriptor_stat(descriptor):
            value = original_fstat(descriptor)
            return SimpleNamespace(**{field: getattr(value, field) for field in
                ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")}, st_size=value.st_size + 1)
        with patch.object(trust.os, "fstat", side_effect=descriptor_stat):
            with self.assertRaisesRegex(ReleaseError, "changed during"):
                trust._read_input(source)

    def promote(self, evidence, **changes):
        options = {"label": "production", "apply": False, "evaluation_report": evidence.evaluation_report_path,
                   "dependency_lock": evidence.dependency_lock_path, "trusted_evidence": evidence,
                   "report_name": "promotion.json", **changes}
        return promotion.promote(self.values[0], evidence.manifest_path, evidence.stage_report_path, **options)

    def test_actual_promoter_accepts_signed_preview_apply_and_fresh_noop(self):
        shutil.rmtree(self.values[2].parent)
        for index, apply in enumerate((False, True, True)):
            with self.enter(materialize_dir=self.root / f"promotion-{index}") as evidence:
                with patch("import_langfuse_experiment.read_object", side_effect=AssertionError("Old locators forbidden")):
                    report = self.promote(evidence, apply=apply)
                self.assertEqual("verified" if apply else "dry_run_complete", report["status"])
                self.assertEqual("verified_ci", report["trust"]["trust"]["classification"])
        self.assertEqual(3, self.mock_run.call_count)
        self.assertEqual(1, len(self.values[1].patches))

    def test_actual_promoter_cannot_downgrade_unsigned_evidence_or_reuse_context(self):
        with self.enter(require_trusted_ci=False, allow_unsigned_local=True,
                        trust_policy_path=None, attestation_path=None) as evidence:
            self.values[1].requests.clear()
            with self.assertRaisesRegex(ReleaseError, "trust mode"):
                self.promote(evidence, apply=True)
            self.assertEqual([], self.values[1].requests)
            self.assertEqual("dry_run_complete", self.promote(evidence, allow_unsigned_local=True)["status"])
        with self.enter() as evidence:
            self.promote(evidence)
            with self.assertRaisesRegex(ReleaseError, "already authorized"):
                self.promote(evidence, apply=True, report_name="second-attempt.json")
        self.assertEqual([], self.values[1].patches)

    def test_actual_promoter_rejects_saved_trust_claims_and_changed_materialization(self):
        self.values[1].requests.clear()
        with self.assertRaisesRegex(ReleaseError, "active verified"):
            promotion.promote(self.values[0], self.values[2], self.values[3], label="production", apply=True,
                              trusted_evidence={"classification": "verified_ci"})
        with self.assertRaisesRegex(ReleaseError, "saved reports"):
            trust.PromotionEvidence(self.root, {}, {}, {}, {}, {})
        with self.enter() as evidence:
            evidence.evaluation_report_path.chmod(0o600)
            evidence.evaluation_report_path.write_bytes(evidence.evaluation_report_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ReleaseError, "changed after verification"):
                self.promote(evidence, apply=True)
        self.assertEqual([], self.values[1].requests)
        self.assertEqual([], self.values[1].patches)

    def test_actual_promoter_rechecks_materialization_immediately_before_patch(self):
        with self.enter() as evidence:
            original = self.values[0].get_project_id
            seen = []
            def change_after_remote_check():
                result = original()
                seen.append(result)
                if len(seen) == 2:
                    evidence.evaluation_report_path.chmod(0o600)
                    evidence.evaluation_report_path.write_bytes(evidence.evaluation_report_path.read_bytes() + b"\n")
                return result
            with patch.object(self.values[0], "get_project_id", side_effect=change_after_remote_check):
                with self.assertRaisesRegex(ReleaseError, "changed after verification"):
                    self.promote(evidence, apply=True)
        self.assertEqual([], self.values[1].patches)

    def test_valid_signature_does_not_bypass_fresh_remote_content_verification(self):
        with self.enter() as evidence:
            self.values[1].versions["example"][8]["prompt"] = "Content drifted after the attested evaluation"
            with self.assertRaises(ReleaseError):
                self.promote(evidence, apply=True)
        self.assertEqual(1, self.mock_run.call_count)
        self.assertEqual([], self.values[1].patches)

    def test_actual_promoter_defaults_to_required_trust_before_remote_reads(self):
        self.values[1].requests.clear()
        with self.assertRaisesRegex(ReleaseError, "freshly verified"):
            promotion.promote(self.values[0], self.values[2], self.values[3], label="production", apply=True,
                              evaluation_report=self.values[-1], dependency_lock=self.values[9])
        self.assertEqual([], self.values[1].requests)

    def test_ci_promotion_harness_uses_actual_engine_and_reconstructs_only_signed_data(self):
        from tools.check_ci_evidence import check_signed_promotion
        root = self.root / "trusted-root.jsonl"
        root.write_text('{"fixture":"mock trust root; unit test only"}')
        wrong_policy = self.root / "wrong-source-commit.json"
        wrong_policy.write_text(json.dumps({**self.signer_policy, "source_commit": "0" * 40}))
        shutil.rmtree(self.values[2].parent)
        summary = check_signed_promotion(self.root, self.artifact, self.attestation, self.policy_path, root,
                                         expected_release_id=self.created["release_id"], wrong_policy_path=wrong_policy)
        self.assertEqual("dry_run_complete", summary["preview_status"])
        self.assertEqual("verified", summary["apply_status"])
        self.assertEqual("verified", summary["retry_status"])
        self.assertEqual(1, summary["verified_prompts"])
        self.assertEqual(1, summary["patch_count"])
        self.assertEqual(["unsigned-promotion", "wrong-signer-promotion"], summary["rejections"])
        self.assertEqual(4, self.mock_run.call_count)
        self.assertEqual("synthetic-project", summary["destination"]["project_id"])
        self.assertTrue((self.root / "promotion-signed-apply" / "snapshot" / "reports" / "promotion.json").is_file())


if __name__ == "__main__":
    unittest.main()
