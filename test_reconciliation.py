"""Offline interrupted-release fault scenarios; no real network or credentials."""
import copy
import io
import json
import os
import stat
import sys
import unittest
import urllib.error
from unittest.mock import patch

import langfuse_release_common as common
import publish_langfuse_prompts as publish
import promote_langfuse_prompts as promotion
import reconcile_langfuse_release as reconciliation
import test_prompt_release as fixtures
from test_staged_release import PromotionHTTP


class ReconciliationTests(unittest.TestCase):
    setUp = fixtures.PromptReleaseTests.setUp
    snapshot = fixtures.PromptReleaseTests.snapshot
    edit = fixtures.PromptReleaseTests.edit
    report = fixtures.PromptReleaseTests.report

    def client(self, prompts=None):
        api = common.Client("https://example.com", timeout=5, project_id="synthetic-project")
        backend = PromotionHTTP(prompts or [fixtures.prompt()])
        api._opener = backend
        return api, backend

    def read_client(self, backend):
        api = reconciliation.ReadOnlyClient("https://example.com", timeout=5, project_id="synthetic-project")
        api._opener = backend
        return api

    def create_report(self, *, stage=True, apply=True, prompts=None):
        api, backend = self.client(prompts)
        manifest = self.snapshot(api)
        self.edit(manifest)
        publish.release(api, manifest, apply=apply, stage=stage,
                        allow_direct_release=not stage, report_name="release.json")
        return api, backend, manifest, manifest.parent / "reports" / "release.json"

    def check(self, backend, manifest, source, **options):
        before = copy.deepcopy(backend.versions)
        original = source.read_bytes()
        backend.requests.clear()
        result = reconciliation.reconcile(self.read_client(backend), manifest, source,
                                          report_name=options.pop("report_name", "reconciliation.json"), **options)
        self.assertEqual(before, backend.versions, "Reconciliation changed server state")
        self.assertEqual(original, source.read_bytes(), "Original receipt was overwritten")
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))
        self.assertEqual([], backend.posts)
        self.assertEqual([], backend.patches)
        return result

    def add_version(self, backend, body, *, labels=None):
        name = body["name"]
        version = max(backend.versions[name]) + 1
        labels = body.get("labels", []) if labels is None else labels
        for prior in backend.versions[name].values():
            prior["labels"] = [v for v in prior["labels"] if v not in labels and v != "latest"]
        created = dict(copy.deepcopy(body), version=version, labels=list(labels) + ["latest"])
        backend.versions[name][version] = created
        return created

    def interrupted_create(self, *, direct=False):
        api, backend = self.client()
        manifest = self.snapshot(api)
        self.edit(manifest)
        def committed_but_response_lost(service, body):
            self.add_version(service, body)
            raise TimeoutError("synthetic lost response")
        backend.on_post = committed_but_response_lost
        with self.assertRaises(common.ReleaseError) as caught:
            publish.release(api, manifest, apply=True, stage=not direct,
                            allow_direct_release=direct, report_name="lost.json")
        self.assertTrue(caught.exception.unknown_write)
        backend.on_post = None
        return api, backend, manifest, manifest.parent / "reports" / "lost.json"

    def promotion_report(self, *, lost=False, apply=True):
        api, backend, manifest, stage = self.create_report()
        if lost:
            def committed_but_response_lost(service, name, version, body):
                for prior in service.versions[name].values():
                    prior["labels"] = [v for v in prior["labels"] if v not in body["newLabels"]]
                service.versions[name][version]["labels"] += body["newLabels"]
                raise TimeoutError("synthetic lost PATCH response")
            backend.on_patch = committed_but_response_lost
        options = dict(label="production", apply=apply, report_name="promotion.json",
                       require_evaluation=False, allow_structural_only=True)
        if lost:
            with self.assertRaises(common.ReleaseError):
                promotion.promote(api, manifest, stage, **options, allow_unsigned_local=True)
        else:
            promotion.promote(api, manifest, stage, **options, allow_unsigned_local=True)
        backend.on_patch = None
        return api, backend, manifest, manifest.parent / "reports" / "promotion.json"

    def test_lost_post_response_is_unknown_even_with_one_exact_candidate(self):
        _, backend, manifest, source = self.interrupted_create()
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertIsNone(row["acknowledged_target_version"])
        candidates = row["observations"]["candidate_versions"]
        self.assertEqual([8], [v["version"] for v in candidates])
        self.assertIs(False, row["observations"]["candidate_identity_proven"])
        self.assertTrue(row["observations"]["candidate_scan_complete"])
        self.assertTrue(candidates[0]["labels_match_intent"])
        self.assertIn("Do not retry", row["reason"])

    def test_lost_direct_post_with_moved_label_stays_unknown(self):
        _, backend, manifest, source = self.interrupted_create(direct=True)
        result = self.check(backend, manifest, source)
        self.assertEqual("unknown", result["results"][0]["classification"])
        self.assertIn("production", result["results"][0]["observations"]["candidate_versions"][0]["labels"])

    def test_identical_concurrent_creator_is_not_proof(self):
        _, backend, manifest, source = self.interrupted_create()
        self.add_version(backend, backend.versions["example"][8])
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertEqual([9, 8], [v["version"] for v in row["observations"]["candidate_versions"]])
        self.assertIs(False, row["observations"]["candidate_identity_proven"])

    def test_unrelated_newer_version_does_not_hide_older_candidate(self):
        _, backend, manifest, source = self.interrupted_create()
        self.add_version(backend, {**backend.versions["example"][8], "prompt": "An unrelated edit.", "labels": []})
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertEqual([9, 8], row["observations"]["inspected_versions"])
        self.assertEqual([8], [v["version"] for v in row["observations"]["candidate_versions"]])

    def test_no_matching_candidate_does_not_authorize_retry(self):
        _, backend, manifest, source = self.interrupted_create()
        backend.versions["example"][8]["prompt"] = "Other caller's content."
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertEqual([], row["observations"]["candidate_versions"])

    def test_bounded_candidate_scan_reports_incompleteness(self):
        _, backend, manifest, source = self.interrupted_create()
        self.add_version(backend, {**backend.versions["example"][8], "prompt": "Different.", "labels": []})
        row = self.check(backend, manifest, source, max_versions=1)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertFalse(row["observations"]["candidate_scan_complete"])
        self.assertEqual([9], row["observations"]["inspected_versions"])

    def test_mutable_metadata_does_not_erase_content_candidate(self):
        _, backend, manifest, source = self.interrupted_create()
        backend.versions["example"][8]["tags"] = ["changed-after-create"]
        backend.versions["example"][8]["labels"] = ["staging", "latest"]
        row = self.check(backend, manifest, source)["results"][0]
        candidate = row["observations"]["candidate_versions"][0]
        self.assertEqual(8, candidate["version"])
        self.assertFalse(candidate["labels_match_intent"])
        self.assertFalse(candidate["tags_match_intent"])
        self.assertEqual("unknown", row["classification"])

    def test_lost_patch_response_can_observe_completed_target(self):
        _, backend, manifest, source = self.promotion_report(lost=True)
        self.assertEqual("promotion_outcome_unknown_do_not_retry", json.loads(source.read_text())["results"][0]["status"])
        result = self.check(backend, manifest, source)
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", result["results"][0]["classification"])
        self.assertEqual(8, result["results"][0]["observations"]["labels"]["production"]["version"])
        self.assertIn("does not identify the actor", result["results"][0]["reason"])

    def test_stable_concurrent_label_move_is_conflicting(self):
        _, backend, manifest, source = self.promotion_report(lost=True)
        self.add_version(backend, {**backend.versions["example"][8], "prompt": "Concurrent change."}, labels=["production"])
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("conflicting", row["classification"])
        self.assertEqual(9, row["observations"]["labels"]["production"]["version"])

    def test_label_moves_during_inspection_are_unknown(self):
        _, backend, manifest, source = self.promotion_report(lost=True)
        backend.get_counts.clear()
        def move_on_second_label_read(service, name, query, count):
            if query.get("label") == "production" and count == 2:
                self.add_version(service, {**service.versions[name][8], "prompt": "Concurrent."}, labels=["production"])
        backend.on_get = move_on_second_label_read
        backend.requests.clear()
        result = reconciliation.reconcile(self.read_client(backend), manifest, source)
        self.assertEqual("unknown", result["results"][0]["classification"])
        self.assertIn("changed during reconciliation", result["results"][0]["reason"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_not_yet_promoted_target_is_pending(self):
        _, backend, manifest, source = self.promotion_report(apply=False)
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("pending", row["classification"])
        self.assertEqual(7, row["observations"]["labels"]["production"]["version"])

    def test_acknowledged_stage_and_direct_versions_complete(self):
        for stage in (True, False):
            with self.subTest(stage=stage):
                # Separate fixture directory per release.
                self.root = self.root / ("stage" if stage else "direct")
                self.root.mkdir()
                _, backend, manifest, source = self.create_report(stage=stage)
                result = self.check(backend, manifest, source)
                self.assertEqual("completed", result["status"])
                self.assertEqual(1, result["counts"]["completed"])

    def test_unsubmitted_preview_is_pending_not_unknown(self):
        _, backend, manifest, source = self.create_report(apply=False)
        result = self.check(backend, manifest, source)
        self.assertEqual("pending", result["results"][0]["classification"])
        self.assertEqual("needs_review", result["status"])
        self.assertFalse(any(row["path"] == "/api/public/v2/prompts" for row in backend.requests))

    def test_unsubmitted_baseline_drift_is_conflicting(self):
        _, backend, manifest, source = self.create_report(apply=False)
        self.add_version(backend, {**backend.versions["example"][7], "prompt": "Concurrent."}, labels=["production"])
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("conflicting", row["classification"])

    def test_unchanged_item_completed_without_create_normalization(self):
        api, backend = self.client()
        manifest = self.snapshot(api)
        publish.release(api, manifest, apply=True, report_name="unchanged.json")
        result = self.check(backend, manifest, manifest.parent / "reports" / "unchanged.json")
        self.assertEqual("completed", result["results"][0]["classification"])
        self.assertEqual([], backend.posts)

    def test_source_disk_failure_after_committed_post_keeps_unknown_boundary(self):
        api, backend = self.client()
        manifest = self.snapshot(api)
        self.edit(manifest)
        real_write = publish.write_report
        def failing_disk(path, report, **kwargs):
            if report.get("results") and report["results"][0].get("created_version"):
                raise OSError("synthetic full disk after server commit")
            return real_write(path, report, **kwargs)
        with patch.object(publish, "write_report", side_effect=failing_disk), self.assertRaises(OSError):
            publish.release(api, manifest, apply=True, report_name="disk-failed.json")
        source = manifest.parent / "reports" / "disk-failed.json"
        self.assertEqual("submitting", json.loads(source.read_text())["results"][0]["status"])
        self.assertIn(8, backend.versions["example"])
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertEqual([8], [v["version"] for v in row["observations"]["candidate_versions"]])

    def test_reconciliation_disk_failure_never_changes_source_or_remote(self):
        _, backend, manifest, source = self.create_report()
        original, before = source.read_bytes(), copy.deepcopy(backend.versions)
        backend.requests.clear()
        real_write = reconciliation.write_report
        def failing_disk(path, report, **kwargs):
            if not kwargs.get("create"):
                raise OSError("synthetic disk failure")
            return real_write(path, report, **kwargs)
        with patch.object(reconciliation, "write_report", side_effect=failing_disk), self.assertRaises(OSError):
            reconciliation.reconcile(self.read_client(backend), manifest, source, report_name="unfinished.json")
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(before, backend.versions)
        self.assertEqual("inspecting", self.report(manifest, "unfinished.json")["status"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_unwritable_output_fails_before_http(self):
        _, backend, manifest, source = self.create_report()
        original = source.read_bytes()
        backend.requests.clear()
        with patch.object(reconciliation, "write_report", side_effect=OSError("synthetic read-only disk")), self.assertRaises(OSError):
            reconciliation.reconcile(self.read_client(backend), manifest, source)
        self.assertEqual([], backend.requests)
        self.assertEqual(original, source.read_bytes())

    def test_missing_corrupt_or_mismatched_report_fails_before_http(self):
        _, backend, manifest, source = self.create_report()
        original = source.read_bytes()
        backend.requests.clear()
        source.unlink()
        with self.assertRaises(OSError):
            reconciliation.reconcile(self.read_client(backend), manifest, source)
        for raw in (b"{broken", b"[]", original.replace(b'"format_version": 2', b'"format_version": 3'),
                    original.replace(b'"created_version": 8', b'"created_version": true')):
            source.write_bytes(raw)
            with self.subTest(raw=raw[:30]), self.assertRaises(common.ReleaseError):
                reconciliation.reconcile(self.read_client(backend), manifest, source)
            self.assertEqual(raw, source.read_bytes())
        self.assertEqual([], backend.requests)

    def test_edited_snapshot_is_rejected_before_http(self):
        _, backend, manifest, source = self.create_report()
        self.edit(manifest, content="New edit after this release.")
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            reconciliation.reconcile(self.read_client(backend), manifest, source)
        self.assertEqual([], backend.requests)

    def test_duplicate_names_and_missing_acknowledged_target_are_rejected(self):
        _, backend, manifest, source = self.create_report()
        original = json.loads(source.read_text())
        backend.requests.clear()
        for change in ("duplicate", "missing_target", "wrong_labels", "false_changed"):
            data = copy.deepcopy(original)
            if change == "duplicate":
                data["results"].append(copy.deepcopy(data["results"][0]))
            elif change == "missing_target":
                data["results"][0].pop("created_version")
            elif change == "wrong_labels":
                data["results"][0]["labels_to_move"] = ["production"]
            else:
                data["results"][0]["changed"] = False
            source.write_text(json.dumps(data))
            with self.subTest(change=change), self.assertRaises(common.ReleaseError):
                reconciliation.reconcile(self.read_client(backend), manifest, source)
        self.assertEqual([], backend.requests)

    def test_empty_preflight_does_not_infer_unrecorded_scope(self):
        _, backend, manifest, source = self.create_report(apply=False)
        data = json.loads(source.read_text())
        data["results"] = []
        data["status"] = "preflight"
        source.write_text(json.dumps(data))
        result = self.check(backend, manifest, source)
        self.assertEqual("unknown", result["results"][0]["classification"])
        self.assertIn("outside the selected release scope", result["results"][0]["reason"])
        self.assertTrue(all(row["method"] == "GET" and row["path"] == "/api/public/projects" for row in backend.requests))

    def test_existing_report_and_original_path_cannot_be_overwritten(self):
        _, backend, manifest, source = self.create_report()
        original = source.read_bytes()
        backend.requests.clear()
        for report_name in (source.name, "../prompt-index.json", "../prompts/fail.txt"):
            with self.subTest(report_name=report_name), self.assertRaises(common.ReleaseError):
                reconciliation.reconcile(self.read_client(backend), manifest, source, report_name=report_name)
        result = self.check(backend, manifest, source)
        completed = manifest.parent / "reports" / result["report_file"]
        previous = completed.read_bytes()
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            reconciliation.reconcile(self.read_client(backend), manifest, source, report_name=completed.name)
        self.assertEqual([], backend.requests)
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(previous, completed.read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits do not establish Windows ACL privacy")
    def test_reconciliation_output_is_private(self):
        _, backend, manifest, source = self.create_report()
        result = self.check(backend, manifest, source)
        self.assertEqual(0o600, stat.S_IMODE((manifest.parent / "reports" / result["report_file"]).stat().st_mode))

    def test_read_only_transport_rejects_every_mutation_before_open(self):
        _, backend = self.client()
        api = self.read_client(backend)
        payload = common.make_payload(fixtures.prompt(), "Updated {{question}}.")
        for invoke in (lambda: api.create(payload), lambda: api.add_labels("example", 7, ["production"]),
                       lambda: api._request("DELETE", "/api/public/v2/prompts/example"),
                       lambda: api._request("GET", "/api/public/v2/prompts", payload={})):
            with self.assertRaises(common.ReleaseError):
                invoke()
        self.assertEqual([], backend.requests)

    def test_version_listing_uses_no_label_filter_and_encoded_query_name(self):
        _, backend = self.client([fixtures.prompt("folder/example")])
        api = self.read_client(backend)
        self.assertEqual([7], api.list_versions("folder/example"))
        self.assertEqual({"name": "folder/example", "page": "1", "limit": "100"}, backend.requests[0]["query"])
        self.assertEqual("GET", backend.requests[0]["method"])

    def test_version_listing_rejects_partial_or_corrupt_metadata(self):
        _, backend = self.client()
        api = self.read_client(backend)
        original = {"data": [fixtures.prompt_meta(fixtures.prompt())],
                    "meta": {"page": 1, "limit": 100, "totalPages": 1, "totalItems": 1}}
        for mutation in ("boolean_version", "duplicate_version", "partial", "wrong_page", "too_many_pages"):
            data = copy.deepcopy(original)
            if mutation == "boolean_version": data["data"][0]["versions"] = [True]
            elif mutation == "duplicate_version": data["data"][0]["versions"] = [7, 7]
            elif mutation == "partial": data["meta"]["totalItems"] = 2
            elif mutation == "wrong_page": data["meta"]["page"] = 2
            else: data["meta"].update(totalItems=10001, totalPages=101)
            backend.list_pages = {1: data}
            with self.subTest(mutation=mutation), self.assertRaises(common.ReleaseError):
                api.list_versions("example")
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_failed_get_remains_unknown(self):
        _, backend, manifest, source = self.create_report()
        def inaccessible(*args):
            raise urllib.error.HTTPError("https://example.com/omitted", 404, "not found", {}, io.BytesIO())
        backend.on_get = inaccessible
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertIn("HTTP 404", row["reason"])

    def test_partial_batch_classifies_each_item_independently(self):
        api, backend = self.client([fixtures.prompt(name) for name in ("first", "second", "third")])
        manifest = self.snapshot(api)
        for name in ("first", "second", "third"):
            self.edit(manifest, name=name)
        def lose_second_response(service, body):
            if body["name"] == "second":
                self.add_version(service, body)
                raise TimeoutError("synthetic second response lost")
        backend.on_post = lose_second_response
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, report_name="partial.json")
        backend.on_post = None
        source = manifest.parent / "reports" / "partial.json"
        result = self.check(backend, manifest, source)
        self.assertEqual({"first": "completed", "second": "unknown", "third": "pending"},
                         {row["name"]: row["classification"] for row in result["results"]})
        self.assertEqual({"completed": 1, "pending": 1, "conflicting": 0, "unknown": 1}, result["counts"])

    def test_selected_subset_does_not_infer_work_for_excluded_prompts(self):
        api, backend = self.client([fixtures.prompt("included"), fixtures.prompt("excluded")])
        manifest = self.snapshot(api)
        self.edit(manifest, name="included")
        self.edit(manifest, name="excluded")
        publish.release(api, manifest, apply=True, names=["included"], report_name="subset.json")
        result = self.check(backend, manifest, manifest.parent / "reports" / "subset.json")
        self.assertEqual(["included"], [row["name"] for row in result["results"]])
        self.assertEqual("completed", result["status"])
        self.assertFalse(any("excluded" in row["path"] for row in backend.requests))

    def test_acknowledged_version_reconciles_without_using_latest_as_identity(self):
        _, backend, manifest, source = self.create_report()
        self.add_version(backend, {**backend.versions["example"][8], "prompt": "A later staged edit."}, labels=[])
        row = self.check(backend, manifest, source)["results"][0]
        self.assertEqual("completed", row["classification"])
        self.assertEqual(8, row["acknowledged_target_version"])
        self.assertFalse(any(row["query"].get("label") == "latest" for row in backend.requests))

    def test_changed_evidence_during_inspection_invalidates_completion(self):
        _, backend, manifest, source = self.create_report()
        changed = False
        def change_local_evidence(service, name, query, count):
            nonlocal changed
            if not changed:
                source.write_bytes(source.read_bytes() + b"\n")
                changed = True
        backend.on_get = change_local_evidence
        backend.requests.clear()
        result = reconciliation.reconcile(self.read_client(backend), manifest, source)
        self.assertEqual("unknown", result["results"][0]["classification"])
        self.assertFalse(result["source_evidence_unchanged"])
        self.assertIn("Local source evidence changed", result["results"][0]["reason"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_changed_version_listing_marks_candidate_scan_incomplete(self):
        _, backend, manifest, source = self.interrupted_create()
        backend.requests.clear()
        api = self.read_client(backend)
        original = api.list_versions
        calls = 0
        def changing_list(name):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.add_version(backend, {**backend.versions[name][8], "prompt": "Concurrent version."}, labels=[])
            return original(name)
        with patch.object(api, "list_versions", side_effect=changing_list):
            result = reconciliation.reconcile(api, manifest, source)
        row = result["results"][0]
        self.assertEqual("unknown", row["classification"])
        self.assertFalse(row["observations"]["candidate_scan_complete"])
        self.assertFalse(row["observations"]["version_listing_stable"])
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))

    def test_cli_reports_counts_and_review_exit_code(self):
        _, backend, manifest, source = self.create_report(apply=False)
        api = self.read_client(backend)
        backend.requests.clear()
        argv = ["reconcile", "--host", api.host, "--project-id", "synthetic-project", "--manifest", str(manifest), "--release-report", str(source)]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(reconciliation, "ReadOnlyClient", return_value=api), \
             patch("sys.stdout", new=output), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            reconciliation.main()
        self.assertEqual(2, caught.exception.code)
        self.assertIn("1 pending", output.getvalue())
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))


if __name__ == "__main__":
    unittest.main()
