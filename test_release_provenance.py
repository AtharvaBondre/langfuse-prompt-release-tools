"""Policy tests around a mocked official verifier, not cryptographic test evidence."""
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import langfuse_release_common as common
import langfuse_release_provenance as provenance


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="provenance-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.artifact = self.root / "artifact.zip"; self.artifact.write_bytes(b"synthetic-test-artifact")
        self.attestation = self.root / "attestation.jsonl"; self.attestation.write_text('{"fixture":"not a real signature"}')
        self.policy_path = self.root / "trusted-policy.json"
        self.policy = {"format_version": 1, "repository": "owner/repo", "workflow": ".github/workflows/release.yml",
                       "source_commit": "a" * 40, "source_ref": "refs/heads/main",
                       "oidc_issuer": "https://token.actions.githubusercontent.com", "runner": "github-hosted", "allow_unsigned": False}
        self.policy_path.write_text(json.dumps(self.policy))
        self.which = patch("langfuse_release_provenance.shutil.which", return_value="/trusted/bin/gh")
        self.which.start(); self.addCleanup(self.which.stop)
        self.run = patch("langfuse_release_provenance.subprocess.run")
        self.mock_run = self.run.start(); self.addCleanup(self.run.stop)
        self.mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self.verified_output()), "")

    def verified_output(self):
        repo = "https://github.com/" + self.policy["repository"]
        san = f"{repo}/{self.policy['workflow']}@{self.policy['source_ref']}"
        certificate = {"subjectAlternativeName": san, "issuer": self.policy["oidc_issuer"], "sourceRepositoryURI": repo,
                       "sourceRepositoryDigest": self.policy["source_commit"], "sourceRepositoryRef": self.policy["source_ref"],
                       "runnerEnvironment": "github-hosted", "buildSignerURI": san, "buildSignerDigest": self.policy["source_commit"]}
        return [{"attestation": {"fixture": "mock official-verifier output"}, "verificationResult": {
            "signature": {"certificate": certificate},
            "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-11-11T01:00:00Z", "uri": "https://rekor.sigstore.dev"}],
            "statement": {"_type": "https://in-toto.io/Statement/v1", "predicateType": "https://slsa.dev/provenance/v1",
                          "subject": [{"name": "artifact.zip", "digest": {"sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest()}}],
                          "predicate": {"untrusted": "Not consulted for authorization"}}}}]

    def verify(self, **kwargs):
        return provenance.verify_provenance(self.artifact, self.policy_path, attestation_path=self.attestation, **kwargs)

    def test_official_verifier_invocation_pins_all_independent_expectations(self):
        report = self.verify()
        self.assertEqual("verified_ci", report["classification"])
        self.assertEqual(common.fingerprint(self.policy), report["trust_policy_sha256"])
        self.assertEqual(hashlib.sha256(self.artifact.read_bytes()).hexdigest(), report["artifact_sha256"])
        args, kwargs = self.mock_run.call_args
        command = args[0]
        self.assertEqual(["/trusted/bin/gh", "attestation", "verify"], command[:3])
        for flag, value in (("--repo", "owner/repo"), ("--source-digest", "a" * 40), ("--signer-digest", "a" * 40),
                            ("--source-ref", "refs/heads/main"), ("--cert-oidc-issuer", self.policy["oidc_issuer"]),
                            ("--bundle", str(self.attestation))):
            self.assertEqual(value, command[command.index(flag) + 1])
        self.assertIn("--deny-self-hosted-runners", command)
        self.assertEqual("https://github.com/owner/repo/.github/workflows/release.yml@refs/heads/main",
                         command[command.index("--cert-identity") + 1])
        identity_flags = {"--cert-identity", "--cert-identity-regex", "--signer-repo", "--signer-workflow"}
        self.assertEqual({"--cert-identity"}, set(command) & identity_flags)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(60, kwargs["timeout"])

    def test_rejected_signature_never_falls_back_to_unsigned(self):
        self.policy["allow_unsigned"] = True; self.policy_path.write_text(json.dumps(self.policy))
        self.mock_run.return_value.returncode = 1
        with self.assertRaisesRegex(common.ReleaseError, "rejected"):
            self.verify()

    def test_wrong_repo_workflow_commit_ref_issuer_and_runner_rejected(self):
        for field in ("sourceRepositoryURI", "subjectAlternativeName", "sourceRepositoryDigest", "sourceRepositoryRef", "issuer", "runnerEnvironment", "buildSignerURI", "buildSignerDigest"):
            output = self.verified_output(); output[0]["verificationResult"]["signature"]["certificate"][field] = "unauthorized"
            self.mock_run.return_value.stdout = json.dumps(output)
            with self.subTest(field=field), self.assertRaisesRegex(common.ReleaseError, field): self.verify()

    def test_predicate_cannot_override_signed_certificate_identity(self):
        output = self.verified_output(); result=output[0]["verificationResult"]
        result["statement"]["predicate"] = {"repository": "owner/repo", "workflow": self.policy["workflow"], "source_commit": self.policy["source_commit"], "runner": "github-hosted"}
        result["signature"]["certificate"]["sourceRepositoryURI"] = "https://github.com/attacker/repo"
        self.mock_run.return_value.stdout = json.dumps(output)
        with self.assertRaises(common.ReleaseError): self.verify()

    def test_wrong_artifact_digest_cannot_replay_verified_attestation(self):
        output = self.verified_output()
        output[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "b" * 64
        self.mock_run.return_value.stdout = json.dumps(output)
        with self.assertRaisesRegex(common.ReleaseError, "artifact digest"): self.verify()

    def test_missing_or_ambiguous_verifier_evidence_rejected(self):
        for mutation in ("empty", "certificate", "timestamp", "predicate", "mixed"):
            output = self.verified_output()
            if mutation == "empty": output = []
            elif mutation == "certificate": del output[0]["verificationResult"]["signature"]["certificate"]
            elif mutation == "timestamp": output[0]["verificationResult"]["verifiedTimestamps"] = []
            elif mutation == "predicate": output[0]["verificationResult"]["statement"]["predicateType"] = "https://other.example"
            else:
                other = copy.deepcopy(output[0]); other["verificationResult"]["signature"]["certificate"]["sourceRepositoryDigest"] = "b" * 40; output.append(other)
            self.mock_run.return_value.stdout = json.dumps(output)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError): self.verify()

    def test_unsigned_local_requires_explicit_external_policy_and_runs_nothing(self):
        with self.assertRaisesRegex(common.ReleaseError, "requires signed"):
            provenance.verify_provenance(self.artifact, self.policy_path)
        self.policy["allow_unsigned"] = True; self.policy_path.write_text(json.dumps(self.policy))
        report = provenance.verify_provenance(self.artifact, self.policy_path)
        self.assertEqual("unsigned_local", report["classification"])
        self.assertIsNone(report["identity"])
        self.assertIsNone(report["verifier"])
        self.mock_run.assert_not_called()

    def test_malformed_policy_is_not_weakened_by_fallback_defaults(self):
        for field, value in (("repository", "owner/repo;echo secret"), ("workflow", "../evil.yml"), ("source_commit", "main"),
                             ("source_ref", "refs/heads/../other"), ("runner", "self-hosted"), ("allow_unsigned", "true"),
                             ("oidc_issuer", "https://untrusted.example")):
            changed = {**self.policy, field: value}; self.policy_path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(common.ReleaseError): self.verify()
        self.mock_run.assert_not_called()

    def test_custom_root_is_explicit_external_input_and_recorded(self):
        root = self.root / "trusted-root.jsonl"; root.write_text('{"fixture":"administrator supplied root"}')
        report = self.verify(trusted_root_path=root)
        self.assertEqual(hashlib.sha256(root.read_bytes()).hexdigest(), report["trusted_root_sha256"])
        command = self.mock_run.call_args.args[0]
        self.assertEqual(str(root), command[command.index("--custom-trusted-root") + 1])

    def test_missing_cli_and_timeout_fail_closed(self):
        with patch("langfuse_release_provenance.shutil.which", return_value=None), self.assertRaisesRegex(common.ReleaseError, "official GitHub CLI"):
            self.verify()
        self.mock_run.side_effect = subprocess.TimeoutExpired("gh", 60)
        with self.assertRaisesRegex(common.ReleaseError, "timed out"): self.verify()

    def test_inputs_changed_during_verification_are_rejected(self):
        for changed in ("artifact", "attestation", "policy", "root"):
            artifact, attestation, policy = self.artifact.read_bytes(), self.attestation.read_bytes(), self.policy_path.read_bytes()
            root = self.root / "root.json"; root.write_text("original root")
            output = json.dumps(self.verified_output())
            def run(*args, **kwargs):
                target = {"artifact": self.artifact, "attestation": self.attestation, "policy": self.policy_path, "root": root}[changed]
                target.write_text('{"changed":true}' if changed == "policy" else "changed")
                return subprocess.CompletedProcess([], 0, output, "")
            self.mock_run.side_effect = run
            with self.subTest(changed=changed), self.assertRaises(common.ReleaseError): self.verify(trusted_root_path=root)
            self.artifact.write_bytes(artifact); self.attestation.write_bytes(attestation); self.policy_path.write_bytes(policy)

    def test_artifact_cannot_serve_as_its_own_trust_policy(self):
        self.artifact.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(common.ReleaseError, "own trust policy"):
            provenance.verify_provenance(self.artifact, self.artifact)

    def test_input_symlinks_are_rejected_before_resolution(self):
        for field in ("artifact", "attestation", "policy"):
            target = {"artifact": self.artifact, "attestation": self.attestation, "policy": self.policy_path}[field]
            alias = self.root / (field + "-alias")
            try: alias.symlink_to(target)
            except (OSError, NotImplementedError): self.skipTest("Symlink creation unavailable")
            with self.subTest(field=field), self.assertRaises(common.ReleaseError):
                provenance.verify_provenance(alias if field == "artifact" else self.artifact,
                                             alias if field == "policy" else self.policy_path,
                                             attestation_path=alias if field == "attestation" else self.attestation)
        self.mock_run.assert_not_called()

    def test_invalid_verifier_json_and_duplicate_keys_rejected(self):
        for value in ('not json', '{}', '[{"verificationResult":{},"verificationResult":{}}]'):
            self.mock_run.return_value.stdout = value
            with self.subTest(value=value), self.assertRaises(common.ReleaseError): self.verify()

    def test_saved_claims_file_is_not_accepted_without_running_official_verifier(self):
        self.attestation.write_text(json.dumps(self.verified_output()))
        self.mock_run.return_value = subprocess.CompletedProcess([], 1, "", "not a Sigstore bundle")
        with self.assertRaises(common.ReleaseError): self.verify()
        self.mock_run.assert_called_once()

    def test_timeout_and_separate_root_constraints_are_strict(self):
        for timeout in (0, 61, True, "60"):
            with self.subTest(timeout=timeout), self.assertRaises(common.ReleaseError): self.verify(timeout=timeout)
        with self.assertRaises(common.ReleaseError): self.verify(trusted_root_path=self.artifact)
        with self.assertRaises(common.ReleaseError): provenance.verify_provenance(self.artifact, self.policy_path, trusted_root_path=self.attestation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
