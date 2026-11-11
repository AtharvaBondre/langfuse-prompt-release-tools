"""Offline staging/promotion tests. Run: python -m unittest -v test_staged_release."""
import copy
import http.client
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import langfuse_release_common as common
import publish_langfuse_prompts as publish
import verify_langfuse_prompts as verify
import promote_langfuse_prompts as promotion
import test_prompt_release as fixtures


class PromotionHTTP(fixtures.FakeHTTP):
    """Add the official label PATCH shape to the synthetic prompt service."""
    def __init__(self, prompts):
        super().__init__(prompts)
        self.on_patch = None

    @property
    def patches(self):
        return [row for row in self.requests if row["method"] == "PATCH"]

    def open(self, request, timeout=None):
        if request.method != "PATCH":
            return super().open(request, timeout)
        url = urllib.parse.urlsplit(request.full_url)
        if url.scheme != "https" or url.netloc != "example.com":
            raise AssertionError("Only synthetic example.com requests are permitted")
        prefix = "/api/public/v2/prompts/"
        if not url.path.startswith(prefix) or "/versions/" not in url.path:
            raise AssertionError("Unexpected PATCH endpoint")
        encoded_name, version = url.path[len(prefix):].rsplit("/versions/", 1)
        name, version = urllib.parse.unquote(encoded_name), int(version)
        body = json.loads(request.data)
        self.requests.append({"method": "PATCH", "path": url.path, "query": {}, "body": body})
        if self.on_patch is not None:
            override = self.on_patch(self, name, version, body)
            if override is not None:
                return fixtures.FakeResponse(override)
        for existing in self.versions[name].values():
            if existing["version"] != version:
                existing["labels"] = [label for label in existing["labels"] if label not in body["newLabels"]]
        target = self.versions[name][version]
        target["labels"] = list(dict.fromkeys(target["labels"] + body["newLabels"]))
        return fixtures.FakeResponse(target)


class StagedReleaseTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting/re-running the entire v01 test class.
    setUp = fixtures.PromptReleaseTests.setUp
    snapshot = fixtures.PromptReleaseTests.snapshot
    edit = fixtures.PromptReleaseTests.edit
    report = fixtures.PromptReleaseTests.report

    def client(self, prompts=None):
        api = common.Client("https://example.com", timeout=5)
        backend = PromotionHTTP(prompts or [fixtures.prompt()])
        api._opener = backend
        return api, backend

    def stage(self, prompts=None):
        api, backend = self.client(prompts)
        manifest = self.snapshot(api)
        for entry in json.loads(manifest.read_text())["prompts"]:
            self.edit(manifest, entry["name"], "Answer {{question}} with a concrete example.")
        report = publish.release(api, manifest, apply=True, stage=True, report_name="stage.json")
        return api, backend, manifest, manifest.parent / "reports" / report["report_file"]

    def test_default_publication_stages_without_moving_production(self):
        api, backend = self.client()
        manifest = self.snapshot(api); self.edit(manifest)
        result = publish.release(api, manifest, apply=True)
        self.assertEqual("stage", result["mode"])
        self.assertEqual("stage", result["release_strategy"])
        self.assertIs(False, result["allow_direct_release"])
        self.assertEqual([], backend.posts[0]["body"]["labels"])
        self.assertEqual(7, api.get("example", label="production")["version"])

    def test_direct_publication_requires_explicit_consistent_boolean_optout(self):
        api, backend = self.client()
        manifest = self.snapshot(api); self.edit(manifest)
        backend.requests.clear()
        for options in ({"stage": False}, {"stage": True, "allow_direct_release": True},
                        {"stage": "false"}, {"apply": 1}, {"allow_direct_release": "true"},
                        {"allow_contract_change": 1}):
            with self.subTest(options=options), self.assertRaises(common.ReleaseError):
                publish.release(api, manifest, **options)
            self.assertEqual([], backend.requests)
        result = publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual("direct", result["release_strategy"])
        self.assertIs(True, result["allow_direct_release"])
        self.assertEqual(8, api.get("example", label="production")["version"])

    def test_publish_cli_defaults_to_stage_and_rejects_conflicting_flags(self):
        api, backend = self.client()
        manifest = self.snapshot(api); self.edit(manifest)
        argv = ["publish", "--host", api.host, "--manifest", str(manifest), "--apply", "--report", "cli.json"]
        with patch.object(sys, "argv", argv), patch.object(publish, "Client", return_value=api), patch("sys.stdout", new=io.StringIO()):
            publish.main()
        self.assertEqual("stage", self.report(manifest, "cli.json")["mode"])
        self.assertEqual(7, api.get("example", label="production")["version"])
        backend.requests.clear()
        with patch.object(sys, "argv", argv + ["--stage", "--allow-direct-release"]), patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                publish.main()
        self.assertEqual(2, caught.exception.code)
        self.assertEqual([], backend.requests)

    def test_publish_cli_explicit_direct_flag_moves_deployment_label(self):
        api, backend = self.client()
        manifest = self.snapshot(api); self.edit(manifest)
        argv = ["publish", "--host", api.host, "--manifest", str(manifest), "--apply", "--allow-direct-release", "--report", "direct-cli.json"]
        with patch.object(sys, "argv", argv), patch.object(publish, "Client", return_value=api), patch("sys.stdout", new=io.StringIO()):
            publish.main()
        result = self.report(manifest, "direct-cli.json")
        self.assertIs(True, result["allow_direct_release"])
        self.assertEqual("direct", result["release_strategy"])
        self.assertEqual(8, api.get("example", label="production")["version"])

    def test_stage_preview_sends_no_write_and_explains_latest(self):
        original = fixtures.prompt(); original["labels"] = ["production", "staging", "latest"]
        api, backend = self.client([original]); manifest = self.snapshot(api); self.edit(manifest)
        result = publish.release(api, manifest, stage=True)
        self.assertEqual("dry_run", result["mode"])
        self.assertEqual("stage", result["release_strategy"])
        self.assertEqual([], result["results"][0]["labels_to_move"])
        self.assertTrue(result["results"][0]["latest_assigned_automatically"])
        self.assertEqual([], backend.posts)
        self.assertEqual([], backend.patches)

    def test_stage_preserves_production_pointer_but_advances_latest(self):
        original = fixtures.prompt(); original["labels"] = ["production", "staging", "latest"]
        api, backend, manifest, stage_path = self.stage([original])
        report = json.loads(stage_path.read_text())
        self.assertEqual("stage", report["mode"])
        self.assertEqual(2, report["format_version"])
        self.assertEqual([], backend.posts[0]["body"]["labels"])
        self.assertEqual(["production", "staging"], backend.versions["example"][7]["labels"])
        self.assertEqual(["latest"], backend.versions["example"][8]["labels"])
        self.assertEqual(7, api.get("example", label="production")["version"])
        self.assertEqual(8, api.get("example", label="latest")["version"])
        self.assertEqual([], backend.patches)
        self.assertEqual(1, verify.verify(api, manifest, stage_path))

    def test_stage_report_binds_snapshot_and_edited_files(self):
        api, backend, manifest, stage_path = self.stage()
        _, snapshot = publish.load_snapshot(manifest)
        self.assertEqual(common.fingerprint(snapshot), json.loads(stage_path.read_text())["snapshot_sha256"])
        backend.requests.clear(); self.edit(manifest, content="Later {{question}} edit.")
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, stage_path)
        self.assertEqual([], backend.requests)

    def test_stage_report_detects_edited_manifest_metadata(self):
        api, backend, manifest, stage_path = self.stage()
        data = json.loads(manifest.read_text()); data["prompts"][0]["baseline"]["tags"].append("later-edit")
        manifest.write_text(json.dumps(data)); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, stage_path)
        self.assertEqual([], backend.requests)

    def test_direct_apply_report_also_binds_snapshot(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        result = publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        path = manifest.parent / "reports" / result["report_file"]
        self.assertEqual(2, result["format_version"])
        self.assertEqual("direct", result["release_strategy"])
        self.edit(manifest, content="Another {{question}} edit."); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, path)
        self.assertEqual([], backend.requests)

    def test_staging_unchanged_null_config_verifies_without_post(self):
        api, backend = self.client([fixtures.prompt(config=None)]); manifest = self.snapshot(api)
        result = publish.release(api, manifest, apply=True, stage=True)
        self.assertEqual([], backend.posts)
        self.assertEqual(1, verify.verify(api, manifest, manifest.parent / "reports" / result["report_file"]))

    def test_real_client_patch_request_shape_and_label_dedup(self):
        api, backend = self.client([fixtures.prompt("folder/example")])
        result = api.add_labels("folder/example", 7, ["staging", "staging"])
        self.assertEqual(7, result["version"])
        self.assertEqual("/api/public/v2/prompts/folder%2Fexample/versions/7", backend.patches[0]["path"])
        self.assertEqual({"newLabels": ["staging"]}, backend.patches[0]["body"])

    def test_real_client_invalid_patch_labels_rejected_before_http(self):
        api, backend = self.client()
        for labels in [[], ["latest"], ["UPPER"], "production"]:
            with self.subTest(labels=labels), self.assertRaises(common.ReleaseError):
                api.add_labels("example", 7, labels)
        self.assertEqual([], backend.requests)

    def test_real_client_patch_uncertainty_never_retries(self):
        for failure in ["transport", "incomplete", "invalid_json", "invalid_schema"]:
            with self.subTest(failure=failure):
                api, backend = self.client()
                def fail(backend, name, version, payload):
                    if failure == "transport":
                        raise urllib.error.URLError("synthetic lost response")
                    if failure == "incomplete":
                        raise http.client.IncompleteRead(b"partial", 20)
                    return b"invalid-json" if failure == "invalid_json" else {"name": name}
                backend.on_patch = fail
                with self.assertRaises(common.ReleaseError) as caught:
                    api.add_labels("example", 7, ["staging"])
                self.assertTrue(caught.exception.unknown_write)
                self.assertEqual(1, len(backend.patches))

    def test_promotion_preview_is_read_only(self):
        api, backend, manifest, stage_path = self.stage()
        backend.requests.clear()
        result = promotion.promote(api, manifest, stage_path, label="production", require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.patches)
        self.assertEqual([], backend.posts)
        self.assertEqual(7, api.get("example", label="production")["version"])
        self.assertIn(result["status"], {"dry_run_complete", "preview_complete"})

    def test_promotion_moves_only_requested_label_and_reads_exact_version(self):
        original = fixtures.prompt(); original["labels"] = ["production", "staging", "latest"]
        api, backend, manifest, stage_path = self.stage([original]); backend.requests.clear()
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        self.assertEqual("verified", result["status"])
        self.assertEqual(1, len(backend.patches))
        self.assertEqual({"newLabels": ["production"]}, backend.patches[0]["body"])
        self.assertEqual(["staging"], backend.versions["example"][7]["labels"])
        self.assertEqual({"latest", "production"}, set(backend.versions["example"][8]["labels"]))
        patch_index = next(i for i, row in enumerate(backend.requests) if row["method"] == "PATCH")
        queries = [r["query"] for r in backend.requests[patch_index+1:] if r["method"] == "GET"]
        self.assertIn({"resolve": "false", "version": "8"}, queries)
        self.assertIn({"resolve": "false", "label": "production"}, queries)
        self.assertEqual([], backend.posts)

    def test_already_promoted_run_skips_patch(self):
        api, backend, manifest, stage_path = self.stage()
        promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="first-promotion.json", require_evaluation=False, allow_structural_only=True)
        backend.requests.clear()
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="second-promotion.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.patches)
        self.assertEqual("verified", result["status"])
        self.assertIn("already", result["results"][0]["status"])

    def test_promotion_wrong_label_fails_without_patch(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        for label in ["latest", "unknown-label"]:
            with self.subTest(label=label), self.assertRaises(common.ReleaseError):
                promotion.promote(api, manifest, stage_path, label=label, apply=True, require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.patches)

    def test_promotion_hash_mismatch_blocks_before_http(self):
        api, backend, manifest, stage_path = self.stage(); self.edit(manifest, content="Edited again {{question}}.")
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.requests)

    def test_failed_second_target_preflight_prevents_all_patches(self):
        api, backend, manifest, stage_path = self.stage([fixtures.prompt("first"), fixtures.prompt("second")])
        backend.versions["second"][8]["config"] = "unexpected"
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.patches)
        self.assertEqual(7, api.get("first", label="production")["version"])

    def test_changed_baseline_and_extra_target_labels_block_promotion(self):
        for mutation in ["baseline_content", "baseline_config", "baseline_tags", "baseline_labels", "target_label"]:
            with self.subTest(mutation=mutation):
                # Each subcase gets a separate fixture folder and report history.
                old_root = self.root; self.root = old_root / mutation; self.root.mkdir()
                try:
                    api, backend, manifest, stage_path = self.stage()
                    baseline, target = backend.versions["example"][7], backend.versions["example"][8]
                    if mutation == "baseline_content": baseline["prompt"] = "Other content."
                    elif mutation == "baseline_config": baseline["config"] = {"changed": True}
                    elif mutation == "baseline_tags": baseline["tags"] = ["changed"]
                    elif mutation == "baseline_labels": baseline["labels"].append("unexpected")
                    else: target["labels"].append("unreviewed")
                    backend.requests.clear()
                    with self.assertRaises(common.ReleaseError):
                        promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
                    self.assertEqual([], backend.patches)
                finally:
                    self.root = old_root

    def test_production_pointer_race_before_patch_stops(self):
        api, backend, manifest, stage_path = self.stage()
        backend.get_counts.clear(); backend.requests.clear()
        def race(backend, name, query, count):
            if query.get("label") == "production" and count >= 2:
                return {**copy.deepcopy(backend.versions[name][7]), "version": 9}
        backend.on_get = race
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        self.assertEqual([], backend.patches)

    def test_promotion_readback_mismatch_stops_next_patch(self):
        api, backend, manifest, stage_path = self.stage([fixtures.prompt("first"), fixtures.prompt("second")])
        backend.requests.clear()
        def fail_readback(backend, name, query, count):
            if backend.patches and name == "first" and "version" in query:
                return {**copy.deepcopy(backend.versions[name][8]), "prompt": "Wrong readback."}
        backend.on_get = fail_readback
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="failed-promotion.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual(1, len(backend.patches))
        report = self.report(manifest, "failed-promotion.json")
        self.assertEqual("stopped", report["status"])
        self.assertEqual(7, backend.versions["second"][7]["version"])
        self.assertIn("production", backend.versions["second"][7]["labels"])

    def test_promotion_unknown_patch_is_reported_without_retry(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        backend.on_patch = lambda *args: b"not-json"
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="uncertain.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual(1, len(backend.patches))
        report = self.report(manifest, "uncertain.json")
        self.assertEqual("stopped", report["status"])
        self.assertIn("unknown", report["results"][0]["status"])

    def test_label_pointer_race_after_patch_fails_even_when_exact_version_matches(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        def race_after_patch(backend, name, query, count):
            if backend.patches and query.get("label") == "production":
                return {**copy.deepcopy(backend.versions[name][8]), "version": 9}
        backend.on_get = race_after_patch
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="pointer-race.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual(1, len(backend.patches))
        self.assertEqual("patch_returned_but_verification_failed", self.report(manifest, "pointer-race.json")["results"][0]["status"])

    def test_partial_promotion_keeps_first_success_and_recovery_checks_unknown_write(self):
        api, backend, manifest, stage_path = self.stage([fixtures.prompt("first"), fixtures.prompt("second")])
        backend.requests.clear()
        def commit_then_lose_response(backend, name, version, payload):
            if name == "second":
                for old in backend.versions[name].values():
                    if old["version"] != version:
                        old["labels"] = [label for label in old["labels"] if label not in payload["newLabels"]]
                target = backend.versions[name][version]
                target["labels"] = list(dict.fromkeys(target["labels"] + payload["newLabels"]))
                raise urllib.error.URLError("synthetic response lost after label change")
        backend.on_patch = commit_then_lose_response
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="partial.json", require_evaluation=False, allow_structural_only=True)
        report = self.report(manifest, "partial.json")
        self.assertEqual("verified", report["results"][0]["status"])
        self.assertEqual("promotion_outcome_unknown_do_not_retry", report["results"][1]["status"])
        self.assertEqual(2, len(backend.patches))
        backend.on_patch = None; backend.requests.clear()
        recovered = promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="recovered.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual("verified", recovered["status"])
        self.assertEqual([], backend.patches)
        self.assertTrue(all(r["status"] == "already_promoted" for r in recovered["results"]))

    def test_promotion_existing_report_is_preserved_before_http(self):
        api, backend, manifest, stage_path = self.stage()
        promotion.promote(api, manifest, stage_path, label="production", report_name="preview.json", require_evaluation=False, allow_structural_only=True)
        path = manifest.parent / "reports/preview.json"; previous = path.read_bytes()
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="preview.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual(previous, path.read_bytes())
        self.assertEqual([], backend.requests)

    def test_promotion_report_verifier_reads_version_and_label_without_writes(self):
        api, backend, manifest, stage_path = self.stage()
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        report_path = manifest.parent / "reports" / result["report_file"]
        backend.requests.clear()
        self.assertEqual(1, verify.verify(api, manifest, report_path))
        self.assertEqual([{ "resolve": "false", "version": "8"}, {"resolve": "false", "label": "production"}],
                         [row["query"] for row in backend.requests])
        self.assertEqual([], backend.patches)
        self.assertEqual([], backend.posts)

    def test_promotion_report_verifier_detects_label_pointer_drift(self):
        api, backend, manifest, stage_path = self.stage()
        result = promotion.promote(api, manifest, stage_path, label="production", apply=True, require_evaluation=False, allow_structural_only=True)
        report_path = manifest.parent / "reports" / result["report_file"]
        def changed_pointer(backend, name, query, count):
            if query.get("label") == "production":
                return {**copy.deepcopy(backend.versions[name][8]), "version": 9}
        backend.on_get = changed_pointer; backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, report_path)
        self.assertEqual([], backend.patches)

    def test_promotion_report_verifier_rejects_preview_and_unapplied_report(self):
        api, backend, manifest, stage_path = self.stage()
        preview = promotion.promote(api, manifest, stage_path, label="production", require_evaluation=False, allow_structural_only=True)
        path = manifest.parent / "reports" / preview["report_file"]
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, path)
        preview["status"] = "verified"
        path.write_text(json.dumps(preview))
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, path)
        self.assertEqual([], backend.requests)

    def test_verifier_rejects_stage_baseline_version_mismatch_before_http(self):
        api, backend, manifest, stage_path = self.stage()
        stage = json.loads(stage_path.read_text()); stage["results"][0]["baseline_version"] = 6
        stage_path.write_text(json.dumps(stage)); backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, stage_path)
        self.assertEqual([], backend.requests)

    def test_commit_then_http409_is_unknown_and_recovery_does_not_repeat_patch(self):
        api, backend, manifest, stage_path = self.stage(); backend.requests.clear()
        def commit_then_conflict(backend, name, version, payload):
            for old in backend.versions[name].values():
                if old["version"] != version:
                    old["labels"] = [label for label in old["labels"] if label not in payload["newLabels"]]
            target = backend.versions[name][version]
            target["labels"] = list(dict.fromkeys(target["labels"] + payload["newLabels"]))
            raise urllib.error.HTTPError("https://example.com", 409, "synthetic dependency conflict", {}, io.BytesIO(b"omitted"))
        backend.on_patch = commit_then_conflict
        with self.assertRaises(common.ReleaseError) as caught:
            promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="conflict.json", require_evaluation=False, allow_structural_only=True)
        self.assertTrue(caught.exception.unknown_write)
        self.assertEqual(1, len(backend.patches))
        self.assertEqual("promotion_outcome_unknown_do_not_retry", self.report(manifest, "conflict.json")["results"][0]["status"])
        backend.on_patch = None; backend.requests.clear()
        recovered = promotion.promote(api, manifest, stage_path, label="production", apply=True, report_name="recovered-409.json", require_evaluation=False, allow_structural_only=True)
        self.assertEqual("already_promoted", recovered["results"][0]["status"])
        self.assertEqual([], backend.patches)
        self.assertEqual(1, verify.verify(api, manifest, manifest.parent / "reports" / recovered["report_file"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
