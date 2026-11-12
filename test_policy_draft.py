"""Offline policy-setup checks: reviewed inputs, fingerprints and pin boundary."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import langfuse_release_common as common
import langfuse_release_policy as policies
import langfuse_release_policy_draft as draft
import langfuse_release_remote as remote
from examples import sdk_experiment
import test_prompt_release as base
import test_release_remote as remote_fixture


ROOT = Path(__file__).resolve().parent


class PolicyDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="langfuse-policy-draft-")
        self.addCleanup(self.tmp.cleanup)
        # macOS temporary paths may traverse the /var -> /private/var alias.
        # Select the canonical root; the helper deliberately rejects symlinks.
        self.root = Path(self.tmp.name).resolve()
        (self.root / "task.py").write_text("# A selected task source; never executed.\nraise RuntimeError('do not run')\n", encoding="utf-8")
        (self.root / "evaluator.py").write_text("# A separately selected evaluator source.\n", encoding="utf-8")
        self.cases = [{"id": "normal", "input": {"question": "Example?"}, "expected_output": {"answer": "Example."}},
                      {"id": "empty", "input": None, "expected_output": None}]
        self.spec = {
            "format_version": 1, "id": "reviewed-contract",
            "producer": {"kind": "langfuse-python-sdk", "schema_version": "v1", "script_path": "task.py"},
            "dataset": {"id": "selected-cases", "name": "Selected cases"},
            "execution": {"task_file": "task.py", "evaluator_file": "evaluator.py",
                          "model": {"provider": "local", "name": "deterministic", "config": {"temperature": 0}}},
            "targets": [{"name": "example", "case_ids": ["normal", "empty"]}],
            "checks": [{"id": "answer_exact", "kind": "boolean", "operator": "eq", "threshold": True},
                       {"id": "quality", "kind": "numeric", "operator": "gte", "threshold": 0.9}],
            "remote_export": "optional",
        }
        for target in ("socket.socket.connect", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            blocker = patch(target, side_effect=AssertionError("Policy setup must not access the network"))
            blocker.start(); self.addCleanup(blocker.stop)

    def build(self, spec=None, cases=None):
        return draft.build_policy(self.spec if spec is None else spec,
                                  self.cases if cases is None else cases, source_root=self.root)

    def files(self):
        spec_path = self.root / "review-spec.json"
        cases_path = self.root / "cases.json"
        spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        cases_path.write_text(json.dumps(self.cases), encoding="utf-8")
        return spec_path, cases_path

    def call_main(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["langfuse-release-policy-draft", *map(str, args)]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                draft.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_build_hashes_actual_cases_source_text_and_config(self):
        original_spec, original_cases = copy.deepcopy(self.spec), copy.deepcopy(self.cases)
        policy = self.build()
        self.assertEqual(policy, policies.validate_policy(policy))
        self.assertEqual(common.fingerprint(self.cases), policy["dataset"]["version"])
        self.assertEqual(common.fingerprint(None), policy["dataset"]["cases"][1]["input_sha256"])
        self.assertEqual(common.fingerprint(self.cases[0]["expected_output"]), policy["dataset"]["cases"][0]["expected_output_sha256"])
        self.assertEqual(common.fingerprint((self.root / "task.py").read_text()), policy["execution"]["task_revision"])
        self.assertEqual(common.fingerprint({"temperature": 0}), policy["execution"]["model"]["config_sha256"])
        self.assertEqual(original_spec, self.spec)
        self.assertEqual(original_cases, self.cases)
        self.assertNotIn("question", json.dumps(policy))
        self.assertNotIn("task_file", policy["execution"])

    def test_fixtures_match_working_sdk_example_policy(self):
        policy = draft.generate_policy(ROOT / "examples/policy-inputs/spec.json",
                                       ROOT / "examples/policy-inputs/cases.json", source_root=ROOT)
        self.assertEqual(sdk_experiment.CASES, draft.read_json(ROOT / "examples/policy-inputs/cases.json", where="cases"))
        self.assertEqual(sdk_experiment.policy_for("example/answer"), policy)

    def test_changed_input_and_source_change_their_fingerprints(self):
        before = self.build()
        self.cases[0]["input"]["question"] = "Changed?"
        (self.root / "task.py").write_text("# Changed actual task source.\n", encoding="utf-8")
        after = self.build()
        self.assertNotEqual(before["dataset"]["version"], after["dataset"]["version"])
        self.assertNotEqual(before["dataset"]["cases"][0]["input_sha256"], after["dataset"]["cases"][0]["input_sha256"])
        self.assertNotEqual(before["execution"]["task_revision"], after["execution"]["task_revision"])
        self.assertEqual(before["checks"], after["checks"])
        self.assertEqual(before["execution"]["evaluator_revision"], after["execution"]["evaluator_revision"])

    def test_reviewed_hosted_version_and_schema_are_preserved(self):
        self.spec["dataset"].update(version="2026-11-12T00:00:00Z", schema={"input": {"type": "object"}})
        policy = self.build()
        self.assertEqual(self.spec["dataset"]["version"], policy["dataset"]["version"])
        self.assertEqual(common.fingerprint(self.spec["dataset"]["schema"]), policy["dataset"]["schema_sha256"])

    def test_wrapped_case_file_has_same_identity(self):
        self.assertEqual(self.build(), self.build(cases={"cases": self.cases}))

    def test_missing_case_field_names_exact_index(self):
        del self.cases[1]["expected_output"]
        with self.assertRaisesRegex(common.ReleaseError, r"cases\[1\].*expected_output"):
            self.build()

    def test_duplicate_case_ids_are_rejected(self):
        self.cases[1]["id"] = "normal"
        with self.assertRaisesRegex(common.ReleaseError, r"cases\[1\].id duplicates"):
            self.build()

    def test_empty_cases_and_aggregate_counts_are_rejected(self):
        for cases in ([], {"case_count": 2}, {"cases": []}):
            with self.subTest(cases=cases), self.assertRaises(common.ReleaseError):
                self.build(cases=cases)

    def test_no_candidate_results_or_score_derived_policy_inputs(self):
        for key in ("results", "evaluations", "output"):
            cases = copy.deepcopy(self.cases)
            cases[0][key] = {"quality": 1.0}
            with self.subTest(key=key), self.assertRaisesRegex(common.ReleaseError, r"cases\[0\].*unsupported fields"):
                self.build(cases=cases)
        spec = copy.deepcopy(self.spec); spec["results"] = {"quality": 1.0}
        with self.assertRaisesRegex(common.ReleaseError, "spec.*unsupported fields: results"):
            self.build(spec=spec)
        del self.spec["checks"]
        with self.assertRaisesRegex(common.ReleaseError, "spec.*missing fields: checks"):
            self.build()

    def test_boolean_and_numeric_type_errors_name_check_field(self):
        for mutation, pattern in [({"threshold": 1}, r"spec.checks\[0\].threshold"),
                                  ({"operator": "gte"}, r"spec.checks\[0\].operator"),
                                  ({"kind": "string"}, r"spec.checks\[0\].kind")]:
            spec = copy.deepcopy(self.spec); spec["checks"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(common.ReleaseError, pattern):
                self.build(spec=spec)
        for value in (True, "0.9", float("nan"), float("inf"), 10 ** 1000):
            spec = copy.deepcopy(self.spec); spec["checks"][1]["threshold"] = value
            with self.subTest(value=value), self.assertRaisesRegex(common.ReleaseError, r"spec.checks\[1\].threshold"):
                self.build(spec=spec)

    def test_empty_duplicate_and_missing_checks_are_rejected(self):
        for value, pattern in [([], "independently selected"),
                               ([self.spec["checks"][0]] * 2, "duplicates check"),
                               ([{"id": "missing"}], "missing fields")]:
            spec = copy.deepcopy(self.spec); spec["checks"] = value
            with self.subTest(value=value), self.assertRaisesRegex(common.ReleaseError, pattern):
                self.build(spec=spec)

    def test_target_coverage_and_duplicates_fail_with_paths(self):
        for targets, pattern in [([], "spec.targets"),
                                  ([{"name": "example", "case_ids": ["normal"]}], "omits selected cases: empty"),
                                  ([{"name": "example", "case_ids": ["unknown"]}], "unknown case"),
                                  ([{"name": "example", "case_ids": ["normal", "normal"]}], "duplicates case"),
                                  (self.spec["targets"] * 2, "duplicates target")]:
            spec = copy.deepcopy(self.spec); spec["targets"] = targets
            with self.subTest(targets=targets), self.assertRaisesRegex(common.ReleaseError, pattern):
                self.build(spec=spec)

    def test_remote_v2_requires_explicit_identity_and_age(self):
        self.spec.update(format_version=2, remote_export="required")
        with self.assertRaisesRegex(common.ReleaseError, "spec.remote_verification is required"):
            self.build()
        remote = {"project_id": "independently-selected-project", "dataset_kind": "local", "max_age_seconds": 300}
        self.spec["remote_verification"] = remote
        self.assertEqual(remote, self.build()["remote_verification"])
        for value in (0, 3601, True):
            spec = copy.deepcopy(self.spec); spec["remote_verification"]["max_age_seconds"] = value
            with self.subTest(value=value), self.assertRaisesRegex(common.ReleaseError, "spec.remote_verification.max_age_seconds"):
                self.build(spec=spec)
        for kind in ("local", "langfuse"):
            self.spec["remote_verification"]["dataset_kind"] = kind
            if kind == "langfuse":
                self.spec["dataset"]["version"] = "2026-11-12T00:00:00Z"
            self.assertEqual(kind, self.build()["remote_verification"]["dataset_kind"])

    def test_hosted_dataset_requires_real_version_and_two_key_schema(self):
        self.spec.update(format_version=2, remote_export="required",
                         remote_verification={"project_id": "selected-project", "dataset_kind": "langfuse", "max_age_seconds": 60})
        with self.assertRaisesRegex(common.ReleaseError, "spec.dataset.version is required"):
            self.build()
        for version in ("local-case-hash", "2026-11-12T00:00:00"):
            self.spec["dataset"]["version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(common.ReleaseError, "spec.dataset.version"):
                self.build()
        self.spec["dataset"]["version"] = "2026-11-12T00:00:00Z"
        for schema in ({"type": "object"}, {"inputSchema": None},
                       {"inputSchema": None, "expectedOutputSchema": None, "extra": None}):
            self.spec["dataset"]["schema"] = schema
            with self.subTest(schema=schema), self.assertRaisesRegex(common.ReleaseError, "spec.dataset.schema"):
                self.build()
        self.spec["dataset"]["schema"] = {"inputSchema": None, "expectedOutputSchema": {"type": "object"}}
        self.assertEqual(common.fingerprint(self.spec["dataset"]["schema"]), self.build()["dataset"]["schema_sha256"])

    def test_generated_hosted_schema_interoperates_with_remote_verifier(self):
        original, source, capture = remote_fixture.policy_and_capture("langfuse")
        spec = copy.deepcopy(self.spec)
        spec.update(format_version=2, id=original["id"], producer=original["producer"],
                    targets=original["targets"], checks=original["checks"], remote_export="required",
                    remote_verification={"project_id": remote_fixture.PROJECT, "dataset_kind": "langfuse", "max_age_seconds": 300})
        spec["dataset"] = {key: original["dataset"][key] for key in ("id", "name", "version")}
        schema = {"inputSchema": {"type": "object"}, "expectedOutputSchema": None}
        spec["dataset"]["schema"] = schema
        cases = [{"id": f"case-{i}", "input": {"input": i}, "expected_output": {"answer": i}} for i in range(2)]
        policy = self.build(spec=spec, cases=cases)
        capture["policy_sha256"] = common.fingerprint(policy)
        capture["execution"] = copy.deepcopy(policy["execution"])
        capture["dataset"]["schema_sha256"] = policy["dataset"]["schema_sha256"]
        expected = remote.make_remote_expectation(source, capture, policy, host=remote_fixture.HOST,
                                                   project_id=remote_fixture.PROJECT, dataset_kind="langfuse")
        backend = remote_fixture.PersistenceHTTP(expected); backend.dataset.update(schema)
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "public-test", "LANGFUSE_SECRET_KEY": "secret-test"}):
            client = remote.RemoteClient(remote_fixture.HOST, timeout=1)
        client._opener = backend
        receipt = remote.verify_remote(client, expected, wait_seconds=0)
        self.assertEqual("confirmed", receipt["status"], receipt["issues"])
        self.assertEqual(policy["dataset"]["schema_sha256"], receipt["remote_dataset"]["schema_sha256"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))
        backend.dataset["inputSchema"] = {"type": "string"}
        changed = remote.verify_remote(client, expected, wait_seconds=0)
        self.assertEqual("failed", changed["status"])

    def test_remote_v1_compatibility_and_v2_optional(self):
        self.spec["remote_export"] = "required"
        self.assertEqual(1, self.build()["format_version"])
        self.spec.update(format_version=2, remote_export="optional")
        self.assertNotIn("remote_verification", self.build())
        self.spec["remote_verification"] = {"project_id": "p", "dataset_kind": "local", "max_age_seconds": 60}
        with self.assertRaisesRegex(common.ReleaseError, "remote_verification requires"):
            self.build()

    def test_all_existing_producer_kinds_are_supported(self):
        for kind in ("langfuse-experiment-action", "langfuse-python-sdk", "langfuse-javascript-sdk"):
            self.spec["producer"]["kind"] = kind
            self.assertEqual(kind, self.build()["producer"]["kind"])

    def test_source_path_boundary_and_missing_file(self):
        for name in ("../outside.py", "/tmp/outside.py", "C:\\outside.py", "dir\\task.py", "missing.py", "."):
            self.spec["execution"]["task_file"] = name
            with self.subTest(name=name), self.assertRaisesRegex(common.ReleaseError, "spec.execution.task_file"):
                self.build()

    def test_non_utf8_source_fails_without_execution(self):
        (self.root / "task.py").write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(common.ReleaseError, "UTF-8 source file"):
            self.build()

    def test_source_symlink_is_rejected(self):
        target = self.root / "linked.py"
        try:
            target.symlink_to(self.root / "task.py")
        except OSError:
            self.skipTest("This host cannot create a symlink")
        self.spec["execution"]["task_file"] = "linked.py"
        with self.assertRaisesRegex(common.ReleaseError, "must not use symlinks"):
            self.build()

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        path = self.root / "invalid.json"
        for raw, pattern in [('{"checks": [], "checks": []}', "duplicate JSON key"),
                              ('{"input": {"x": 1, "x": 2}}', "duplicate JSON key"),
                              ('{"value": NaN}', "non-finite JSON"),
                              ('{"value": Infinity}', "non-finite JSON"),
                              ('{bad json}', "line 1, column")]:
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaisesRegex(common.ReleaseError, pattern):
                draft.read_json(path, where="selected file")

    def test_generate_preserves_existing_files_and_only_creates_draft(self):
        spec, cases = self.files(); out = self.root / "new-folder/policy.json"
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        policy = draft.generate_policy(spec, cases, source_root=self.root, output_path=out)
        self.assertEqual(policy, json.loads(out.read_text()))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertNotIn("evaluation_policy", policy)
        with self.assertRaisesRegex(common.ReleaseError, "already exists"):
            draft.generate_policy(spec, cases, source_root=self.root, output_path=out)
        self.assertEqual(policy, json.loads(out.read_text()))

    def test_failed_generation_leaves_no_draft(self):
        self.spec["checks"][0]["threshold"] = "true"
        spec, cases = self.files(); out = self.root / "policy.json"
        with self.assertRaises(common.ReleaseError):
            draft.generate_policy(spec, cases, source_root=self.root, output_path=out)
        self.assertFalse(out.exists())

    def test_output_parent_symlink_is_rejected(self):
        folder = self.root / "outputs"; folder.mkdir()
        link = self.root / "linked-output"
        try:
            link.symlink_to(folder, target_is_directory=True)
        except OSError:
            self.skipTest("This host cannot create a symlink")
        spec, cases = self.files()
        with self.assertRaisesRegex(common.ReleaseError, "--out must not use symlinks"):
            draft.generate_policy(spec, cases, source_root=self.root, output_path=link / "policy.json")
        self.assertEqual([], list(folder.iterdir()))

    def test_validation_is_read_only_and_accepts_existing_policies(self):
        path = self.root / "reviewed.json"; path.write_text(json.dumps(self.build()))
        before = path.read_bytes()
        self.assertEqual(self.build(), draft.validate_policy_file(path))
        self.assertEqual(before, path.read_bytes())
        code, stdout, stderr = self.call_main(["--validate", path])
        self.assertEqual(0, code); self.assertEqual("", stderr)
        self.assertIn("does not verify sources or server state", stdout)
        self.assertEqual(before, path.read_bytes())

    def test_existing_policy_validation_names_malformed_check(self):
        policy = self.build(); policy["checks"][1]["threshold"] = 10 ** 1000
        path = self.root / "invalid-policy.json"; path.write_text(json.dumps(policy))
        before = path.read_bytes()
        code, _, stderr = self.call_main(["--validate", path])
        self.assertEqual(1, code)
        self.assertIn("policy.checks[1].threshold", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(before, path.read_bytes())

    def test_cli_build_reports_output_and_review_boundary(self):
        spec, cases = self.files(); out = self.root / "policy.json"
        code, stdout, stderr = self.call_main(["--spec", spec, "--cases", cases, "--source-root", self.root, "--out", out])
        self.assertEqual(0, code); self.assertEqual("", stderr)
        self.assertIn(str(out.resolve()), stdout)
        self.assertIn("before explicitly pinning", stdout)
        self.assertEqual(self.build(), json.loads(out.read_text()))

    def test_cli_requires_complete_and_unambiguous_actions(self):
        spec, cases = self.files()
        for args in ([], ["--spec", spec], ["--validate", spec, "--cases", cases],
                     ["--validate", spec, "--source-root", self.root], ["--validate", spec, "--out", "new.json"]):
            with self.subTest(args=args):
                code, _, _ = self.call_main(args)
                self.assertEqual(2, code)


class PolicyDraftPinIntegrationTests(unittest.TestCase):
    def setUp(self):
        base.PromptReleaseTests.setUp(self)
        self.root = self.root.resolve()

    client = base.PromptReleaseTests.client
    snapshot = base.PromptReleaseTests.snapshot

    def test_generated_working_example_is_reviewed_then_explicitly_pinned(self):
        api, backend = self.client([base.prompt("example/answer")])
        manifest = self.snapshot(api)
        before = manifest.read_bytes()
        path = self.root / "policy.json"
        backend.requests.clear()
        policy = draft.generate_policy(ROOT / "examples/policy-inputs/spec.json",
                                       ROOT / "examples/policy-inputs/cases.json", source_root=ROOT,
                                       output_path=path)
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual([], backend.requests)
        draft.validate_policy_file(path)
        binding = policies.pin_policy(manifest, path)
        self.assertEqual(policy, binding["policy"])
        self.assertEqual(common.fingerprint(policy), binding["sha256"])
        self.assertEqual([], backend.requests)
        changed = copy.deepcopy(policy); changed["checks"][0]["threshold"] = False
        replacement = self.root / "different-policy.json"; replacement.write_text(json.dumps(changed))
        with self.assertRaisesRegex(common.ReleaseError, "fresh export"):
            policies.pin_policy(manifest, replacement)


if __name__ == "__main__":
    unittest.main()
