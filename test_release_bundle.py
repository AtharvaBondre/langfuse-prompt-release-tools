"""Offline portable evidence tests; every artifact is a synthetic release fixture."""
import copy
import hashlib
import json
import os
import shutil
import stat
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import import_langfuse_experiment as adapter
import langfuse_release_bundle as bundle
import langfuse_release_common as common
import test_experiment_policy as fixtures


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExperimentPolicyTests.setUp(self)
        self.root = self.root.resolve()

    client = fixtures.ExperimentPolicyTests.client
    snapshot = fixtures.ExperimentPolicyTests.snapshot
    edit = fixtures.ExperimentPolicyTests.edit
    policy = fixtures.ExperimentPolicyTests.policy
    prepare = fixtures.ExperimentPolicyTests.prepare
    imported = fixtures.ExperimentPolicyTests.imported

    def create(self):
        values = self.imported()
        target = self.root / "release.zip"
        report = bundle.create_bundle(values[2], values[3], values[-1], target)
        return values, target, report

    def entries(self, path):
        with zipfile.ZipFile(path) as archive:
            return {entry.filename: archive.read(entry) for entry in archive.infolist()}

    def rewrite(self, path, values, *, reseal=False):
        if reseal:
            manifest = json.loads(values["bundle.json"])
            for row in manifest["files"]:
                if row["path"] in values:
                    row["size"] = len(values[row["path"]])
                    row["sha256"] = hashlib.sha256(values[row["path"]]).hexdigest()
            manifest.pop("manifest_sha256", None)
            manifest["manifest_sha256"] = common.fingerprint(manifest)
            values["bundle.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for name, raw in values.items():
                archive.writestr(name, raw)

    def test_bundle_verifies_after_original_machine_files_are_gone(self):
        values, path, report = self.create()
        moved = self.root / "second-machine" / "relocated.zip"
        moved.parent.mkdir(); shutil.copyfile(path, moved)
        shutil.rmtree(values[2].parent)
        with patch("import_langfuse_experiment.read_object", side_effect=AssertionError("Original evidence reads forbidden")):
            result = bundle.verify_bundle(moved, expected_release_id=report["release_id"])
        self.assertEqual("verified", result["status"])
        self.assertEqual("expected_release", result["release_binding"])
        self.assertEqual("unsigned_local", result["trust"]["classification"])
        self.assertEqual(report["artifact_sha256"], result["artifact_sha256"])

    def test_bundle_preserves_original_bytes_and_relative_prompt_mapping(self):
        values, path, _ = self.create()
        contents = self.entries(path)
        self.assertEqual(values[2].read_bytes(), contents["snapshot/" + values[2].name])
        self.assertEqual(values[-1].read_bytes(), contents["evidence/evaluation.json"])
        self.assertEqual(values[5].read_bytes(), contents["evidence/capture.json"])
        self.assertEqual(values[9].read_bytes(), contents["evidence/dependency_lock.json"])
        snapshot = json.loads(values[2].read_text())
        for entry in snapshot["prompts"]:
            self.assertEqual((values[2].parent / entry["file"]).read_bytes(), contents["snapshot/" + entry["file"]])
        manifest = json.loads(contents["bundle.json"])
        self.assertTrue(any(ref["pointer"] == "/dependency_lock/file" for ref in manifest["references"]))

    def test_creation_is_deterministic_exclusive_and_private(self):
        values, path, report = self.create()
        second = self.root / "same-evidence.zip"
        bundle.create_bundle(values[2], values[3], values[-1], second)
        self.assertEqual(path.read_bytes(), second.read_bytes())
        before = path.read_bytes()
        with self.assertRaises(common.ReleaseError): bundle.create_bundle(values[2], values[3], values[-1], path)
        self.assertEqual(before, path.read_bytes())
        if os.name == "posix": self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("private", report["inventory"]["privacy"])
        self.assertFalse(report["inventory"]["upload_performed"])

    def test_only_explicit_evidence_is_collected(self):
        values = self.imported()
        (values[2].parent / ".env").write_text("LANGFUSE_SECRET_KEY=do-not-bundle")
        path = self.root / "private.zip"
        bundle.create_bundle(values[2], values[3], values[-1], path)
        contents = self.entries(path)
        self.assertFalse(any(".env" in name for name in contents))
        self.assertNotIn(b"do-not-bundle", path.read_bytes())

    def test_traversal_absolute_windows_and_nonportable_paths_rejected(self):
        _, path, _ = self.create(); original = self.entries(path)
        for name in ("../escape", "/absolute", "C:/drive", "a\\b", "a//b", "a/./b", "a/NUL.txt", "a/aux", "a/name.", "a/\x00nul"):
            changed = dict(original); changed[name] = b"bad"
            # ZipFile truncates names at a NUL, so verify the safe parser directly for that case.
            with self.subTest(name=name):
                if "\x00" in name:
                    with self.assertRaises(common.ReleaseError): bundle._safe_path(name)
                else:
                    self.rewrite(path, changed)
                    with self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_symlinks_devices_and_duplicate_archive_entries_rejected(self):
        _, path, _ = self.create(); original = self.entries(path)
        for mode in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFDIR):
            self.rewrite(path, original)
            with zipfile.ZipFile(path, "a") as archive:
                info = zipfile.ZipInfo("unlisted-link"); info.create_system = 3; info.external_attr = (mode | 0o777) << 16
                archive.writestr(info, "target")
            with self.subTest(mode=mode), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)
        for duplicate in ("bundle.json", "BUNDLE.JSON"):
            self.rewrite(path, original)
            with zipfile.ZipFile(path, "a") as archive:
                with __import__('warnings').catch_warnings():
                    __import__('warnings').simplefilter("ignore", UserWarning)
                    archive.writestr(duplicate, original["bundle.json"])
            with self.subTest(duplicate=duplicate), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_missing_modified_and_unlisted_files_rejected(self):
        _, path, _ = self.create(); original = self.entries(path)
        for mutation in ("missing", "modified", "extra"):
            changed = dict(original)
            if mutation == "missing": del changed["evidence/results.json"]
            elif mutation == "modified": changed["evidence/results.json"] += b" "
            else: changed[".env"] = b"secret"
            self.rewrite(path, changed)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_duplicate_json_keys_rejected_in_inventory_and_original_artifacts(self):
        _, path, _ = self.create(); original = self.entries(path)
        for key in ("bundle.json", "evidence/results.json"):
            changed = dict(original); source = changed[key].decode().rstrip(); changed[key] = (source[:-1] + ', "schema_version":"v1", "schema_version":"v1"}').encode()
            self.rewrite(path, changed, reseal=key != "bundle.json")
            with self.subTest(key=key), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_resealed_inventory_cannot_weaken_required_checks(self):
        _, path, _ = self.create(); contents = self.entries(path)
        source = json.loads(contents["evidence/results.json"])
        source["results"][0]["experiment_result"]["item_results"][0]["evaluations"][0]["value"] = False
        capture = json.loads(contents["evidence/capture.json"]); capture["result_sha256"] = common.fingerprint(source)
        receipt = json.loads(contents["evidence/evaluation.json"])
        receipt["source_results"]["sha256"] = common.fingerprint(source)
        receipt["execution_capture"]["sha256"] = common.fingerprint(capture)
        receipt.pop("evidence_sha256"); receipt["evidence_sha256"] = common.fingerprint(receipt)
        for name, value in (("results", source), ("capture", capture), ("evaluation", receipt)):
            contents[f"evidence/{name}.json"] = json.dumps(value).encode()
        self.rewrite(path, contents, reseal=True)
        with self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_wrong_expected_release_rejects_cross_release_replay(self):
        _, path, _ = self.create()
        with self.assertRaisesRegex(common.ReleaseError, "different expected release"):
            bundle.verify_bundle(path, expected_release_id="0" * 64)

    def test_reference_mapping_cannot_redirect_to_another_role(self):
        _, path, _ = self.create(); contents = self.entries(path)
        manifest = json.loads(contents["bundle.json"])
        manifest["references"][0]["to"] = "evidence/results.json"
        contents["bundle.json"] = json.dumps(manifest).encode()
        self.rewrite(path, contents, reseal=True)
        with self.assertRaisesRegex(common.ReleaseError, "reference mapping"): bundle.verify_bundle(path)

    def test_inventory_cannot_add_unknown_or_duplicate_roles(self):
        _, path, _ = self.create(); original = self.entries(path)
        for mutation in ("unknown", "duplicate", "size_bool", "duplicate_path"):
            contents = dict(original); manifest = json.loads(contents["bundle.json"])
            if mutation == "unknown": manifest["files"][0]["role"] = "credential"
            elif mutation == "duplicate": manifest["files"][0]["role"] = "snapshot"
            elif mutation == "size_bool": manifest["files"][0]["size"] = True
            else: manifest["files"].append(copy.deepcopy(manifest["files"][0]))
            manifest.pop("manifest_sha256"); manifest["manifest_sha256"] = common.fingerprint(manifest)
            contents["bundle.json"] = json.dumps(manifest).encode()
            self.rewrite(path, contents)
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)

    def test_recognizable_credentials_block_creation_without_writing_bundle(self):
        values = self.prepare(); source, capture = values[6:8]
        source["results"][0]["experiment_result"]["item_results"][0]["output"] = {"api_key": "must-stay-private"}
        capture["result_sha256"] = common.fingerprint(source)
        values[4].write_text(json.dumps(source)); values[5].write_text(json.dumps(capture))
        report = adapter.import_experiment(*values[2:6])
        path = self.root / "secret.zip"
        with self.assertRaisesRegex(common.ReleaseError, "credential"):
            bundle.create_bundle(values[2], values[3], values[2].parent / "reports" / report["report_file"], path)
        self.assertFalse(path.exists())

    def test_input_and_bundle_symlinks_rejected(self):
        values, path, _ = self.create()
        link = self.root / "alias.zip"
        try: link.symlink_to(path)
        except (OSError, NotImplementedError): self.skipTest("Symlink creation unavailable")
        with self.assertRaises(common.ReleaseError): bundle.verify_bundle(link)
        original = values[4]; saved = original.with_suffix(".saved"); original.rename(saved); original.symlink_to(saved)
        with self.assertRaises(common.ReleaseError): bundle.create_bundle(values[2], values[3], values[-1], self.root / "new.zip")

    def test_attestation_cannot_supply_its_own_trust_policy(self):
        _, path, _ = self.create()
        with self.assertRaisesRegex(common.ReleaseError, "separately supplied trust policy"):
            bundle.verify_bundle(path, attestation_path=self.root / "attestation.json")

    def test_provenance_for_other_bytes_cannot_authenticate_this_bundle(self):
        _, path, _ = self.create()
        with patch("langfuse_release_provenance.verify_provenance", return_value={"classification": "verified_ci", "artifact_sha256": "0" * 64}):
            with self.assertRaisesRegex(common.ReleaseError, "Signed artifact differs"):
                bundle.verify_bundle(path, trust_policy_path=self.root / "external-policy.json")

    def test_archive_resource_limits_are_enforced(self):
        _, path, _ = self.create()
        with patch.object(bundle, "MAX_FILES", 1), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)
        with patch.object(bundle, "MAX_BYTES", 100), self.assertRaises(common.ReleaseError): bundle.verify_bundle(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
