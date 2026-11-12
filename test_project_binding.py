"""Independently selected project identity is required before release writes."""
import copy
import json
import unittest
from unittest.mock import patch

import langfuse_release_common as common
import publish_langfuse_prompts as publish
import pull_langfuse_prompts as pull
import promote_langfuse_prompts as promotion
import reconcile_langfuse_release as reconciliation
import test_prompt_release as fixtures


PROJECT = "synthetic-project"


class ProjectBindingTests(unittest.TestCase):
    setUp = fixtures.PromptReleaseTests.setUp
    snapshot = fixtures.PromptReleaseTests.snapshot
    edit = fixtures.PromptReleaseTests.edit

    def client(self, project_id=PROJECT):
        client = common.Client("https://example.com", timeout=1, project_id=project_id)
        backend = fixtures.FakeHTTP([fixtures.prompt()])
        client._opener = backend
        return client, backend

    def test_export_requires_independent_project_and_does_not_autodiscover_authority(self):
        client, backend = self.client(None)
        with self.assertRaisesRegex(common.ReleaseError, "explicit nonempty project"):
            pull.export(client, self.root / "missing", label="production", names=["example"])
        self.assertEqual([], backend.requests)
        self.assertFalse((self.root / "missing").exists())

    def test_export_binds_selected_project_only_after_live_identity_matches(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        snapshot = json.loads(manifest.read_text())
        self.assertEqual(2, snapshot["format_version"])
        self.assertEqual(PROJECT, snapshot["project_id"])
        self.assertEqual("/api/public/projects", backend.requests[0]["path"])

    def test_same_host_wrong_project_refuses_export_before_prompt_reads(self):
        client, backend = self.client()
        backend.project_id = "other-project"
        with self.assertRaisesRegex(common.ReleaseError, "different Langfuse project"):
            pull.export(client, self.root / "wrong", label="production", names=["example"])
        self.assertEqual(["/api/public/projects"], [row["path"] for row in backend.requests])
        self.assertFalse((self.root / "wrong").exists())

    def test_same_host_wrong_project_blocks_stage_and_records_preflight_failure(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        backend.project_id = "other-project"
        backend.requests.clear()
        before = copy.deepcopy(backend.versions)
        with self.assertRaisesRegex(common.ReleaseError, "different Langfuse project"):
            publish.release(client, manifest, apply=True, report_name="wrong-project.json")
        self.assertEqual(before, backend.versions)
        self.assertEqual(["/api/public/projects"], [row["path"] for row in backend.requests])
        journal = json.loads((manifest.parent / "reports/wrong-project.json").read_text())
        self.assertEqual("stopped", journal["status"])
        self.assertEqual("preflight", journal["phase"])
        self.assertEqual([], journal["results"])
        self.assertIn("different Langfuse project", journal["error"])

    def test_old_snapshot_is_readable_offline_but_cannot_authorize_mutation(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        old = json.loads(manifest.read_text())
        old["format_version"] = 1
        del old["project_id"]
        manifest.write_text(json.dumps(old))
        publish.load_snapshot(manifest)
        backend.requests.clear()
        with self.assertRaisesRegex(common.ReleaseError, "Legacy snapshot"):
            publish.release(client, manifest, apply=True, project_id=PROJECT)
        self.assertEqual([], backend.requests)
        self.assertEqual([], backend.posts)

    def test_duplicate_snapshot_project_is_rejected_before_any_remote_write(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        manifest.write_text(manifest.read_text().replace("{", '{"project_id":"contradictory-first-value",', 1))
        backend.requests.clear()
        with self.assertRaisesRegex(common.ReleaseError, "[Dd]uplicate JSON key"):
            publish.release(client, manifest, apply=True)
        self.assertEqual([], backend.requests)

    def test_duplicate_chat_content_is_rejected_in_preview_and_apply(self):
        client, backend = self.client()
        chat = fixtures.prompt(kind="chat", content=[{"role": "user", "content": "Answer {{question}} clearly."}])
        backend.versions = {"example": {7: chat}}
        manifest = self.snapshot(client)
        entry = json.loads(manifest.read_text())["prompts"][0]
        path = manifest.parent / entry["file"]
        path.write_text('[{"role":"user","content":"Misleading first value","content":"Answer {{question}} clearly."}]')
        backend.requests.clear()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(common.ReleaseError, "[Dd]uplicate JSON key"):
                publish.release(client, manifest, apply=apply)
        self.assertEqual([], backend.requests)

    def test_duplicate_stage_or_recovery_status_never_selects_a_silent_winner(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        publish.release(client, manifest, apply=True, report_name="stage.json")
        stage = manifest.parent / "reports/stage.json"
        stage.write_text(stage.read_text().replace("{", '{"status":"contradictory-first-value",', 1))
        backend.requests.clear()
        with self.assertRaisesRegex(common.ReleaseError, "[Dd]uplicate JSON key"):
            promotion.promote(client, manifest, stage, label="production", apply=True,
                              allow_unsigned_local=True, require_evaluation=False, allow_structural_only=True)
        reader = reconciliation.ReadOnlyClient(client.host, timeout=1, project_id=PROJECT)
        reader._opener = backend
        with self.assertRaisesRegex(common.ReleaseError, "[Dd]uplicate JSON key"):
            reconciliation.reconcile(reader, manifest, stage)
        self.assertEqual([], backend.requests)

    def test_snapshot_binding_cannot_be_overridden_by_new_explicit_project(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        backend.project_id = "other-project"
        backend.requests.clear()
        with self.assertRaisesRegex(common.ReleaseError, "Snapshot project ID"):
            publish.release(client, manifest, apply=True, project_id="other-project")
        self.assertEqual([], backend.requests)

    def test_direct_transport_write_requires_project_even_without_snapshot_wrapper(self):
        for selected in (None, "wrong-project"):
            client, backend = self.client(selected)
            with self.subTest(selected=selected), self.assertRaises(common.ReleaseError):
                client.create(common.make_payload(fixtures.prompt(), "Updated {{question}}."))
            self.assertEqual([], backend.posts)
            self.assertTrue(all(row["path"] == "/api/public/projects" for row in backend.requests))

    def test_ambiguous_or_malformed_project_api_response_never_authorizes_write(self):
        responses = ({"data": []}, {"data": [{"id": PROJECT}, {"id": "another"}]},
                     {"data": [{"id": ""}]}, {"data": [{"id": True}]}, {"data": [None]}, [])
        for response in responses:
            client, backend = self.client()
            original = backend.open
            def answer(request, timeout=None):
                if request.full_url.endswith("/api/public/projects"):
                    return fixtures.FakeResponse(response)
                return original(request, timeout)
            with self.subTest(response=response), patch.object(backend, "open", side_effect=answer), self.assertRaises(common.ReleaseError):
                client.create(common.make_payload(fixtures.prompt(), "Updated {{question}}."))
            self.assertEqual([], backend.posts)

    def test_project_drift_on_last_baseline_read_is_caught_before_post(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        backend.get_counts.clear()
        def change_project(service, name, query, count):
            if query.get("label") == "production" and count == 2:
                service.project_id = "other-project"
        backend.on_get = change_project
        with self.assertRaisesRegex(common.ReleaseError, "different Langfuse project"):
            publish.release(client, manifest, apply=True, report_name="drift.json")
        self.assertEqual([], backend.posts)

    def test_local_prompt_edit_after_preflight_is_rejected_before_post(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        backend.get_counts.clear()
        def change_local_content(service, name, query, count):
            if query.get("label") == "production" and count == 2:
                self.edit(manifest, content="A different {{question}} edit after review.")
        backend.on_get = change_local_content
        with self.assertRaisesRegex(common.ReleaseError, "snapshot changed after preflight"):
            publish.release(client, manifest, apply=True, report_name="local-drift.json")
        self.assertEqual([], backend.posts)

    def test_project_drift_during_export_cannot_publish_a_bound_snapshot(self):
        client, backend = self.client()
        def change_project(service, *_):
            service.project_id = "other-project"
        backend.on_get = change_project
        with self.assertRaisesRegex(common.ReleaseError, "different Langfuse project"):
            pull.export(client, self.root / "drifting", label="production", names=["example"])
        self.assertFalse((self.root / "drifting/prompt-index.json").exists())

    def test_reconciliation_rejects_missing_or_wrong_source_report_project_binding(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        publish.release(client, manifest, apply=True, report_name="stage.json")
        source = manifest.parent / "reports/stage.json"
        original = json.loads(source.read_text())
        for value in (None, "other-project"):
            changed = copy.deepcopy(original)
            if value is None:
                changed.pop("project_id")
            else:
                changed["project_id"] = value
            source.write_text(json.dumps(changed))
            reader = reconciliation.ReadOnlyClient(client.host, timeout=1, project_id=PROJECT)
            reader._opener = backend
            backend.requests.clear()
            with self.subTest(value=value), self.assertRaises(common.ReleaseError):
                reconciliation.reconcile(reader, manifest, source)
            self.assertFalse(any(row["method"] != "GET" for row in backend.requests))
            self.assertFalse(any("/prompts" in row["path"] for row in backend.requests))

    def test_reconciliation_cannot_claim_completion_if_project_changes_during_inspection(self):
        client, backend = self.client()
        manifest = self.snapshot(client)
        self.edit(manifest)
        publish.release(client, manifest, apply=True, report_name="stage.json")
        source = manifest.parent / "reports/stage.json"
        def drift(service, *_):
            service.project_id = "other-project"
        backend.on_get = drift
        reader = reconciliation.ReadOnlyClient(client.host, timeout=1, project_id=PROJECT)
        reader._opener = backend
        backend.requests.clear()
        try:
            report = reconciliation.reconcile(reader, manifest, source, report_name="drift.json")
        except common.ReleaseError:
            pass
        else:
            self.assertNotEqual("completed", report["status"])
            self.assertTrue(all(row["classification"] == "unknown" for row in report["results"]))
        self.assertTrue(all(row["method"] == "GET" for row in backend.requests))


if __name__ == "__main__":
    unittest.main()
