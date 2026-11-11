"""Portable offline tests: python -m unittest -v test_prompt_release.py.

All HTTP is handled by an in-memory fake opener. No network, Langfuse instance,
live credentials, or third-party Python dependencies are required.
"""
import copy
import http.client
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import langfuse_release_common as common
import pull_langfuse_prompts as pull
import publish_langfuse_prompts as publish
import verify_langfuse_prompts as verify


def prompt(name="example", content="Answer {{question}} clearly.", *, kind="text", config=None):
    return {"name": name, "type": kind, "prompt": content, "version": 7,
            "labels": ["production", "latest"], "tags": ["demo"],
            "config": config, "isUserManaged": True}


def prompt_meta(value, versions=None):
    """Public list-response PromptMeta shape, distinct from a named prompt GET."""
    return {"name": value["name"], "type": value["type"],
            "versions": list(versions or [value["version"]]),
            "labels": copy.deepcopy(value["labels"]), "tags": copy.deepcopy(value["tags"]),
            "lastUpdatedAt": "2026-01-01T00:00:00Z", "lastConfig": copy.deepcopy(value["config"])}


class FakeResponse:
    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.raw


class FakeHTTP:
    """Minimal synthetic v2 prompt service, with hooks for failure injection."""
    def __init__(self, prompts):
        self.versions = {p["name"]: {p["version"]: copy.deepcopy(p)} for p in prompts}
        self.requests = []
        self.get_counts = {}
        self.on_get = None
        self.on_post = None
        self.list_pages = None

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def latest(self, name):
        return self.versions[name][max(self.versions[name])]

    def open(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        if url.scheme != "https" or url.netloc != "example.com":
            raise AssertionError("Only the synthetic example.com host is permitted")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        body = json.loads(request.data) if request.data is not None else None
        row = {"method": request.method, "path": url.path, "query": query, "body": body}
        self.requests.append(row)
        if request.method == "POST":
            if self.on_post is not None:
                override = self.on_post(self, body)
                if override is not None:
                    return FakeResponse(override)
            name = body["name"]
            version = max(self.versions.get(name, {0: None})) + 1
            for old in self.versions.setdefault(name, {}).values():
                old["labels"] = [x for x in old["labels"] if x not in body["labels"] and x != "latest"]
            created = {**copy.deepcopy(body), "version": version, "labels": list(body["labels"]) + ["latest"], "isUserManaged": True}
            self.versions[name][version] = created
            return FakeResponse(created)
        if request.method != "GET":
            raise AssertionError("Unexpected HTTP method")
        if url.path == "/api/public/v2/prompts":
            page = int(query["page"])
            if self.list_pages is not None:
                return FakeResponse(self.list_pages[page])
            names = list(self.versions)
            return FakeResponse({"data": [prompt_meta(self.latest(n), sorted(self.versions[n])) for n in names[(page-1)*100:page*100]],
                                 "meta": {"page": page, "limit": 100, "totalPages": (len(names)+99)//100, "totalItems": len(names)}})
        prefix = "/api/public/v2/prompts/"
        if not url.path.startswith(prefix):
            raise AssertionError("Unexpected HTTP path")
        name = urllib.parse.unquote(url.path[len(prefix):])
        key = (name, tuple(sorted(query.items())))
        self.get_counts[key] = self.get_counts.get(key, 0) + 1
        if self.on_get is not None:
            override = self.on_get(self, name, query, self.get_counts[key])
            if override is not None:
                return FakeResponse(override)
        if "version" in query:
            result = self.versions[name][int(query["version"])]
        else:
            labeled = [p for p in self.versions[name].values() if query.get("label") in p["labels"]]
            if not labeled:
                raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"omitted"))
            result = max(labeled, key=lambda p: p["version"])
        return FakeResponse(result)


class PromptReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="langfuse-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # OpenSSL on Windows needs these OS paths even for offline TLS setup.
        # Keep credentials/proxy settings isolated without breaking that setup.
        test_env = {key: value for key, value in os.environ.items()
                    if key.upper() in {"SYSTEMROOT", "WINDIR"}}
        test_env.update({"LANGFUSE_PUBLIC_KEY": "public-test", "LANGFUSE_SECRET_KEY": "secret-test"})
        self.env = patch.dict(os.environ, test_env, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        for target in ["socket.socket.connect", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"]:
            blocker = patch(target, side_effect=AssertionError("Real network disabled"))
            blocker.start(); self.addCleanup(blocker.stop)

    def client(self, prompts=None):
        api = common.Client("https://example.com", timeout=5)
        backend = FakeHTTP(prompts or [prompt()])
        api._opener = backend
        return api, backend

    def snapshot(self, api, names=None, label="production", version=None):
        destination = self.root / "snapshot"
        return pull.export(api, destination, names=names, label=label, version=version)

    def edit(self, manifest, name="example", content="Answer {{question}} with one example."):
        data = json.loads(manifest.read_text())
        entry = next(e for e in data["prompts"] if e["name"] == name)
        file = manifest.parent / entry["file"]
        file.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    def report(self, manifest, name=None):
        path = manifest.parent / "reports" / name if name else max((manifest.parent / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        return json.loads(path.read_text())

    def test_get_requires_exactly_one_selector(self):
        api, backend = self.client()
        for options in [{}, {"label": "production", "version": 7}, {"version": 0}, {"version": True}, {"label": ""}]:
            with self.subTest(options=options), self.assertRaises(common.ReleaseError):
                api.get("example", **options)
        self.assertEqual([], backend.requests)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not establish Windows ACL privacy")
    def test_export_and_initial_report_files_are_private_under_permissive_umask(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        api, backend = self.client()
        # An existing empty directory is also an authorized private output target.
        destination = self.root / "snapshot"
        destination.mkdir(mode=0o755)
        manifest = self.snapshot(api)
        entry = json.loads(manifest.read_text())["prompts"][0]
        self.assertEqual(0o700, stat.S_IMODE(destination.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE((destination / "prompts").stat().st_mode))
        for path in (manifest, destination / entry["file"]):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        receipt = destination / "reports" / "private.json"
        publish.write_report(receipt, {"private": "synthetic detail"}, create=True)
        self.assertEqual(0o700, stat.S_IMODE(receipt.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(receipt.stat().st_mode))

    def test_export_never_overwrites_file_created_after_initial_empty_check(self):
        api, backend = self.client()
        destination = self.root / "snapshot"
        import hashlib
        relative = "prompts/example-" + hashlib.sha256(b"example").hexdigest()[:12] + ".txt"
        occupied = destination / relative
        def concurrent_file_creation(*args):
            backend.on_get = None
            occupied.parent.mkdir(parents=True)
            occupied.write_text("created by another invocation")
        backend.on_get = concurrent_file_creation
        with self.assertRaises(FileExistsError):
            self.snapshot(api)
        self.assertEqual("created by another invocation", occupied.read_text())
        self.assertFalse((destination / "prompt-index.json").exists())

    def test_deep_json_write_response_records_unknown_outcome_without_retry(self):
        api, backend = self.client()
        manifest = self.snapshot(api); self.edit(manifest)
        backend.on_post = lambda *args: b"[" * 2000 + b"0" + b"]" * 2000
        with self.assertRaises(common.ReleaseError) as caught:
            publish.release(api, manifest, apply=True, report_name="nested.json")
        self.assertTrue(caught.exception.unknown_write)
        self.assertEqual(1, len(backend.posts))
        persisted = self.report(manifest, "nested.json")
        self.assertEqual("stopped", persisted["status"])
        self.assertEqual("write_outcome_unknown_do_not_retry", persisted["results"][0]["status"])

    def test_get_label_is_encoded_and_composition_resolution_disabled(self):
        api, backend = self.client([prompt("folder/example")])
        api.get("folder/example", label="production")
        self.assertEqual("/api/public/v2/prompts/folder%2Fexample", backend.requests[0]["path"])
        self.assertEqual({"label": "production", "resolve": "false"}, backend.requests[0]["query"])

    def test_wrong_response_label_or_version_is_rejected(self):
        api, backend = self.client()
        backend.on_get = lambda *args: {**prompt(), "labels": ["staging"]}
        with self.assertRaises(common.ReleaseError):
            api.get("example", label="production")
        backend.on_get = lambda *args: {**prompt(), "version": 9}
        with self.assertRaises(common.ReleaseError):
            api.get("example", version=7)

    def test_config_json_shapes_are_preserved(self):
        for config in [None, False, 0, 1.5, "scalar", [], ["a", 1], {"temperature": 0.2}]:
            with self.subTest(config=config):
                original = prompt(config=config)
                payload = common.make_payload(original, "Edited {{question}}.")
                self.assertEqual({} if config is None else config, payload["config"])
                actual = {**payload, "version": 8, "labels": ["production", "latest"]}
                common.verify_exact(actual, payload, 8)

    def test_empty_metadata_is_not_treated_as_unspecified(self):
        original = prompt(); original["labels"] = ["latest"]; original["tags"] = []
        payload = common.make_payload(original, original["prompt"])
        self.assertEqual([], payload["labels"])
        self.assertEqual([], payload["tags"])
        for field in ["labels", "tags"]:
            actual = {**copy.deepcopy(payload), "version": 8, "labels": ["latest"]}
            actual[field] = ["unexpected"]
            with self.subTest(field=field), self.assertRaises(common.ReleaseError):
                common.verify_exact(actual, payload, 8)

    def test_reserved_latest_label_is_not_sent(self):
        payload = common.make_payload(prompt(), "Edited {{question}}.")
        self.assertEqual(["production"], payload["labels"])
        api, backend = self.client()
        payload["labels"].append("latest")
        with self.assertRaises(common.ReleaseError):
            api.create(payload)
        self.assertEqual([], backend.posts)

    def test_chat_messages_placeholders_and_contract_changes(self):
        content = [{"type": "chatmessage", "role": "system", "content": "Answer {{question}}."}, {"type": "placeholder", "name": "history"}]
        common.validate_prompt(prompt(content=content, kind="chat"))
        edited = copy.deepcopy(content); edited[1]["name"] = "messages"
        self.assertNotEqual(common.contract_signature(content), common.contract_signature(edited))
        edited = copy.deepcopy(content); edited[0]["content"] = "Answer {{other}}."
        self.assertNotEqual(common.contract_signature(content), common.contract_signature(edited))
        for invalid in [[{"role": "system", "content": 4}], [{"type": "placeholder", "name": 2}], [{"type": "unknown", "name": "x"}], [{"name": "history"}], [{"type": "placeholder", "name": "bad-name"}]]:
            with self.subTest(invalid=invalid), self.assertRaises(common.ReleaseError):
                common.validate_prompt(prompt(content=invalid, kind="chat"))

    def test_chatmessage_discriminator_is_normalized_for_runtime(self):
        content = [{"type": "chatmessage", "role": "system", "content": "Answer {{question}}."}, {"type": "placeholder", "name": "history"}]
        payload = common.make_payload(prompt(content=content, kind="chat"), content)
        self.assertNotIn("type", payload["prompt"][0])
        self.assertEqual("placeholder", payload["prompt"][1]["type"])
        actual = {**payload, "version": 8, "labels": ["production", "latest"]}
        common.verify_exact(actual, payload, 8)

    def test_output_schema_addition_removal_and_malformed_markers(self):
        plain = "Answer."
        schema = "Answer. <OUTPUT_SCHEMA>{a}</OUTPUT_SCHEMA>"
        self.assertNotEqual(common.contract_signature(plain), common.contract_signature(schema))
        self.assertNotEqual(common.contract_signature(schema), common.contract_signature(plain))
        for invalid in ["<OUTPUT_SCHEMA>unclosed", "</OUTPUT_SCHEMA>", "<OUTPUT_SCHEMA><OUTPUT_SCHEMA>x</OUTPUT_SCHEMA></OUTPUT_SCHEMA>"]:
            with self.subTest(invalid=invalid), self.assertRaises(common.ReleaseError):
                common.contract_signature(invalid)

    def test_exact_verification_preserves_whitespace_and_checks_config_type(self):
        payload = common.make_payload(prompt(), "Answer.\n")
        for delta in [{"prompt": "Answer."}, {"config": "different"}, {"type": "chat", "prompt": []}, {"name": "other"}, {"version": 9}]:
            actual = {**copy.deepcopy(payload), "version": 8, **delta}
            with self.subTest(delta=delta), self.assertRaises(common.ReleaseError):
                common.verify_exact(actual, payload, 8)

    def test_paths_reject_parent_absolute_windows_and_symlink(self):
        for value in ["../outside", "/outside", "C:/outside", "a\\b", ""]:
            with self.subTest(value=value), self.assertRaises(common.ReleaseError):
                common.inside(self.root, value)
        (self.root / "link").symlink_to(self.root / "target")
        with self.assertRaises(common.ReleaseError):
            common.inside(self.root, "link/file.json")

    def test_hosts_reject_credentials_external_http_and_queries(self):
        for host in ["http://example.com", "https://user:secret@example.com", "https://example.com?key=test", "https://example.com#fragment"]:
            with self.subTest(host=host), self.assertRaises(common.ReleaseError):
                common.Client(host)

    def test_post_transport_failure_is_unknown_and_never_retried(self):
        api, backend = self.client()
        def fail(*args):
            raise urllib.error.URLError("synthetic transport loss")
        backend.on_post = fail
        with self.assertRaises(common.ReleaseError) as caught:
            api.create(common.make_payload(prompt(), "Edited {{question}}."))
        self.assertTrue(caught.exception.unknown_write)
        self.assertEqual(1, len(backend.posts))

    def test_invalid_post_response_is_unknown_and_never_retried(self):
        for response in [{"name": "example"}, b"not-json", {**prompt(), "version": True}]:
            with self.subTest(response=response):
                api, backend = self.client()
                backend.on_post = lambda *args, value=response: value
                with self.assertRaises(common.ReleaseError) as caught:
                    api.create(common.make_payload(prompt(), "Edited {{question}}."))
                self.assertTrue(caught.exception.unknown_write)
                self.assertEqual(1, len(backend.posts))

    def test_incomplete_http_body_marks_write_outcome_unknown(self):
        api, backend = self.client()
        def fail(*args):
            raise http.client.IncompleteRead(b"partial", 20)
        backend.on_post = fail
        with self.assertRaises(common.ReleaseError) as caught:
            api.create(common.make_payload(prompt(), "Edited {{question}}."))
        self.assertTrue(caught.exception.unknown_write)
        self.assertEqual(1, len(backend.posts))

    def test_http_error_body_is_omitted_and_write_uncertainty_is_recorded(self):
        for status, unknown in [(400, False), (500, True), (302, True)]:
            with self.subTest(status=status):
                api, backend = self.client()
                def fail(*args):
                    raise urllib.error.HTTPError("https://example.com", status, "synthetic", {}, io.BytesIO(b"PRIVATE_RESPONSE_SENTINEL"))
                backend.on_post = fail
                with self.assertRaises(common.ReleaseError) as caught:
                    api.create(common.make_payload(prompt(), "Edited {{question}}."))
                self.assertEqual(unknown, caught.exception.unknown_write)
                self.assertNotIn("PRIVATE_RESPONSE_SENTINEL", str(caught.exception))
                self.assertEqual(1, len(backend.posts))

    def test_export_uses_distinct_files_for_previous_filename_collision(self):
        api, backend = self.client([prompt("a/b", "First."), prompt("a_b", "Second.")])
        manifest = self.snapshot(api)
        entries = json.loads(manifest.read_text())["prompts"]
        self.assertEqual(2, len({entry["file"] for entry in entries}))
        self.assertEqual(["First.", "Second."], [(manifest.parent / e["file"]).read_text() for e in entries])

    def test_export_text_chat_and_scalar_config_snapshot(self):
        chat = [{"role": "system", "content": "Answer {{question}}."}, {"type": "placeholder", "name": "history"}]
        api, _ = self.client([prompt("text", config=2), prompt("chat", chat, kind="chat", config=None)])
        manifest = self.snapshot(api)
        _, data = publish.load_snapshot(manifest)
        self.assertEqual(chat, next(e for e in data["prompts"] if e["name"] == "chat")["local_content"])
        self.assertEqual(2, next(e for e in data["prompts"] if e["name"] == "text")["baseline"]["config"])

    def test_export_refuses_nonempty_directory_without_deleting(self):
        api, backend = self.client()
        out = self.root / "existing"; out.mkdir(); marker = out / "keep.txt"; marker.write_text("keep")
        with self.assertRaises(common.ReleaseError):
            pull.export(api, out, label="production")
        self.assertEqual("keep", marker.read_text())
        self.assertEqual([], backend.requests)

    def test_export_rejects_output_root_symlink_before_http(self):
        api, backend = self.client()
        target = self.root / "real-output"; target.mkdir()
        link = self.root / "linked-output"; link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(common.ReleaseError):
            pull.export(api, link, label="production")
        self.assertEqual([], backend.requests)
        self.assertEqual([], list(target.iterdir()))

    def test_release_rejects_manifest_file_symlink_before_http(self):
        api, backend = self.client(); manifest = self.snapshot(api)
        linked = self.root / "linked-index.json"; linked.symlink_to(manifest)
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            publish.release(api, linked, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual([], backend.requests)

    def test_release_rejects_manifest_parent_symlink_before_http(self):
        api, backend = self.client(); manifest = self.snapshot(api)
        alias = self.root / "linked-snapshot"; alias.symlink_to(manifest.parent, target_is_directory=True)
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            publish.release(api, alias / manifest.name, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual([], backend.requests)

    def test_export_page_cap_and_malformed_pagination_fail(self):
        for pages in [{1: {"data": [prompt_meta(prompt())], "meta": {"page": 1, "limit": 100, "totalPages": 2, "totalItems": 101}}},
                      {1: {"data": [prompt_meta(prompt())]}},
                      {1: {"data": [], "meta": {"page": 1, "limit": 100, "totalPages": 1, "totalItems": 1}}}]:
            with self.subTest(pages=pages):
                api, backend = self.client(); backend.list_pages = pages
                with self.assertRaises(common.ReleaseError):
                    pull.export(api, self.root / "incomplete", label="production", max_pages=1)
                self.assertFalse((self.root / "incomplete" / "prompt-index.json").exists())

    def test_pagination_rejects_changed_totals_and_duplicate_names(self):
        first = [prompt_meta(prompt(f"p{i}")) for i in range(100)]
        for last, count in [([prompt_meta(prompt("p0"))], 101), ([prompt_meta(prompt("p100")), prompt_meta(prompt("p101"))], 102)]:
            with self.subTest(count=count):
                api, backend = self.client()
                backend.list_pages = {
                    1: {"data": first, "meta": {"page": 1, "limit": 100, "totalPages": 2, "totalItems": 101}},
                    2: {"data": last, "meta": {"page": 2, "limit": 100, "totalPages": 2, "totalItems": count}},
                }
                with self.assertRaises(common.ReleaseError):
                    api.list_names("production")

    def test_version_export_requires_names_and_is_review_only(self):
        api, _ = self.client()
        with self.assertRaises(common.ReleaseError):
            pull.export(api, self.root / "bad", version=7)
        manifest = self.snapshot(api, names=["example"], label=None, version=7)
        self.edit(manifest)
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)

    def test_dry_run_has_zero_post_and_report(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        result = publish.release(api, manifest, stage=False, allow_direct_release=True)
        self.assertEqual("dry_run_complete", result["status"])
        self.assertEqual([], backend.posts)
        self.assertTrue(result["results"][0]["changed"])

    def test_preview_diff_and_metadata_describe_planned_release(self):
        original = prompt(config=None); original["labels"] = ["staging", "production", "latest"]
        original["tags"] = ["demo", "example"]
        api, backend = self.client([original]); manifest = self.snapshot(api); self.edit(manifest)
        result = publish.release(api, manifest, stage=False, allow_direct_release=True)
        entry = result["results"][0]
        self.assertIn("--- exported baseline\n", entry["diff"])
        self.assertIn("+++ local edit\n", entry["diff"])
        self.assertIn("-Answer {{question}} clearly.\n", entry["diff"])
        self.assertIn("+Answer {{question}} with one example.\n", entry["diff"])
        self.assertIn("\\ No newline at end of file", entry["diff"])
        self.assertEqual(["staging", "production"], entry["labels_to_move"])
        self.assertEqual(["demo", "example"], entry["tags_preserved"])
        self.assertTrue(entry["config_normalized_from_null"])
        self.assertTrue(entry["latest_assigned_automatically"])
        self.assertEqual([], backend.posts)

    def test_contract_block_preserves_every_diff_without_post(self):
        api, backend = self.client([prompt("first"), prompt("second")]); manifest = self.snapshot(api)
        self.edit(manifest, "first", "Changed {{other}}."); self.edit(manifest, "second")
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        report = self.report(manifest)
        self.assertEqual("stopped", report["status"])
        self.assertEqual(2, len(report["results"]))
        self.assertTrue(report["results"][0]["contract_changed"])
        self.assertFalse(report["results"][1]["contract_changed"])
        self.assertIn("+Changed {{other}}.", report["results"][0]["diff"])
        self.assertIn("+Answer {{question}} with one example.", report["results"][1]["diff"])
        self.assertEqual([], backend.posts)

    def test_preview_makes_final_newline_change_visible(self):
        api, backend = self.client([prompt(content="Answer.")]); manifest = self.snapshot(api)
        self.edit(manifest, content="Answer.\n")
        result = publish.release(api, manifest, stage=False, allow_direct_release=True)
        entry = result["results"][0]
        self.assertTrue(entry["changed"])
        self.assertIn("-Answer.\n", entry["diff"])
        self.assertIn("+Answer.\n", entry["diff"])
        self.assertIn("\\ No newline at end of file", entry["diff"])
        self.assertEqual([], backend.posts)

    def test_preflight_all_prompts_before_any_post(self):
        api, backend = self.client([prompt("first"), prompt("second")]); manifest = self.snapshot(api)
        self.edit(manifest, "first"); self.edit(manifest, "second", "Changed {{other}}.")
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)
        self.assertEqual("stopped", self.report(manifest)["status"])
        self.assertEqual("preflight", self.report(manifest)["phase"])

    def test_placeholder_variable_collision_blocks_whole_batch_even_with_override(self):
        chat = [{"role": "system", "content": "Answer {{question}}."}, {"type": "placeholder", "name": "history"}]
        api, backend = self.client([prompt("first"), prompt("second", chat, kind="chat")])
        manifest = self.snapshot(api)
        self.edit(manifest, "first")
        invalid = copy.deepcopy(chat); invalid[0]["content"] = "Answer {{question}} using {{history}}."
        self.edit(manifest, "second", invalid)
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, allow_contract_change=True, stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)

    def test_schema_addition_and_removal_block_apply_without_override(self):
        for before, after in [("Text.", "Text. <OUTPUT_SCHEMA>{a}</OUTPUT_SCHEMA>"), ("<OUTPUT_SCHEMA>{a}</OUTPUT_SCHEMA>", "Text.")]:
            with self.subTest(before=before):
                api, backend = self.client([prompt(content=before)])
                directory = self.root / ("with-schema" if "OUTPUT" in before else "without-schema")
                manifest = pull.export(api, directory, label="production")
                self.edit(manifest, content=after)
                with self.assertRaises(common.ReleaseError):
                    publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
                self.assertEqual([], backend.posts)

    def test_chat_placeholder_change_blocked_then_explicit_override_verified(self):
        chat = [{"role": "system", "content": "Answer {{question}}."}, {"type": "placeholder", "name": "history"}]
        api, backend = self.client([prompt(content=chat, kind="chat", config=False)])
        manifest = self.snapshot(api); edited = copy.deepcopy(chat); edited[1]["name"] = "messages"
        self.edit(manifest, content=edited)
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, report_name="blocked.json", stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)
        result = publish.release(api, manifest, apply=True, allow_contract_change=True, stage=False, allow_direct_release=True)
        self.assertEqual("verified", result["status"])
        self.assertEqual(False, backend.posts[0]["body"]["config"])

    def test_missing_selected_name_rejects_before_post(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, names=["example", "missing"], stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)

    def test_snapshot_remote_metadata_and_version_drift_block(self):
        for field, value in [("version", 8), ("prompt", "Remote edit."), ("config", {"new": True}), ("tags", [])]:
            with self.subTest(field=field):
                api, backend = self.client()
                directory = self.root / field
                manifest = pull.export(api, directory, label="production"); self.edit(manifest)
                backend.latest("example")[field] = value
                with self.assertRaises(common.ReleaseError):
                    publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
                self.assertEqual([], backend.posts)

    def test_optimistic_recheck_detects_change_after_preflight(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        count = 0
        def change(backend, name, query, nth):
            nonlocal count
            count += 1
            if count == 2:
                changed = copy.deepcopy(backend.latest(name)); changed["config"] = "changed"
                return changed
        backend.on_get = change
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)
        self.assertEqual("recheck", self.report(manifest)["phase"])

    def test_canary_readback_is_pinned_and_precedes_next_post(self):
        api, backend = self.client([prompt("second"), prompt("first")]); manifest = self.snapshot(api)
        self.edit(manifest, "first"); self.edit(manifest, "second")
        result = publish.release(api, manifest, apply=True, canary="first", stage=False, allow_direct_release=True)
        self.assertEqual("verified", result["status"])
        posts = [i for i, r in enumerate(backend.requests) if r["method"] == "POST"]
        self.assertEqual("first", backend.requests[posts[0]]["body"]["name"])
        readbacks = [r for r in backend.requests[posts[0]+1:posts[1]] if r["query"].get("version") == "8"]
        self.assertEqual(1, len(readbacks))
        self.assertEqual({"version": "8", "resolve": "false"}, readbacks[0]["query"])

    def test_failed_canary_readback_stops_later_post_and_records_created_version(self):
        api, backend = self.client([prompt("first"), prompt("second")]); manifest = self.snapshot(api)
        self.edit(manifest, "first"); self.edit(manifest, "second")
        def mismatch(backend, name, query, nth):
            if "version" in query:
                return {**copy.deepcopy(backend.latest(name)), "prompt": "Wrong readback."}
        backend.on_get = mismatch
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, canary="first", stage=False, allow_direct_release=True)
        self.assertEqual(1, len(backend.posts))
        first = self.report(manifest)["results"][0]
        self.assertEqual(8, first["created_version"])
        self.assertEqual("created_but_verification_failed", first["status"])

    def test_ambiguous_post_stops_and_records_no_retry(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        backend.on_post = lambda *args: b"not-json"
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual(1, len(backend.posts))
        self.assertEqual("write_outcome_unknown_do_not_retry", self.report(manifest)["results"][0]["status"])

    def test_non_new_create_version_stops_and_is_recorded(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        backend.on_post = lambda backend, body: {**body, "version": 7, "labels": ["production", "latest"]}
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
        self.assertEqual(1, len(backend.posts))
        result = self.report(manifest)["results"][0]
        self.assertEqual(7, result["created_version"])
        self.assertEqual("created_but_verification_failed", result["status"])

    def test_report_traversal_is_rejected_before_post(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, report_name="../../escape.json", stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)
        self.assertFalse((self.root / "escape.json").exists())

    def test_duplicate_names_and_paths_rejected(self):
        for duplicate in ["name", "file"]:
            with self.subTest(duplicate=duplicate):
                api, backend = self.client([prompt("first"), prompt("second")])
                manifest = pull.export(api, self.root / duplicate, label="production")
                data = json.loads(manifest.read_text())
                data["prompts"][1][duplicate] = data["prompts"][0][duplicate]
                manifest.write_text(json.dumps(data))
                with self.assertRaises(common.ReleaseError):
                    publish.release(api, manifest, apply=True, stage=False, allow_direct_release=True)
                self.assertEqual([], backend.posts)

    def test_completed_apply_can_be_independently_verified_by_exact_version(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        publish.release(api, manifest, apply=True, report_name="apply.json", stage=False, allow_direct_release=True)
        backend.requests.clear()
        count = verify.verify(api, manifest, manifest.parent / "reports/apply.json")
        self.assertEqual(1, count)
        self.assertEqual({"version": "8", "resolve": "false"}, backend.requests[0]["query"])

    def test_verifier_rejects_dry_run_and_changed_local_content(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        publish.release(api, manifest, report_name="preview.json", stage=False, allow_direct_release=True)
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, manifest.parent / "reports/preview.json")
        publish.release(api, manifest, apply=True, report_name="apply.json", stage=False, allow_direct_release=True)
        self.edit(manifest, content="Unpublished later edit {{question}}.")
        with self.assertRaises(common.ReleaseError):
            verify.verify(api, manifest, manifest.parent / "reports/apply.json")

    def test_existing_report_is_not_overwritten_or_followed_by_http(self):
        api, backend = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        publish.release(api, manifest, report_name="preserve.json", stage=False, allow_direct_release=True)
        report_path = manifest.parent / "reports/preserve.json"
        previous = report_path.read_bytes()
        backend.requests.clear()
        with self.assertRaises(common.ReleaseError):
            publish.release(api, manifest, apply=True, report_name="preserve.json", stage=False, allow_direct_release=True)
        self.assertEqual(previous, report_path.read_bytes())
        self.assertEqual([], backend.requests)

    def test_default_reports_are_unique(self):
        api, _ = self.client(); manifest = self.snapshot(api); self.edit(manifest)
        publish.release(api, manifest, stage=False, allow_direct_release=True)
        publish.release(api, manifest, stage=False, allow_direct_release=True)
        self.assertEqual(2, len(list((manifest.parent / "reports").glob("*.json"))))

    def test_unchanged_null_config_can_be_verified_without_write_normalization(self):
        api, backend = self.client([prompt(config=None)]); manifest = self.snapshot(api)
        result = publish.release(api, manifest, apply=True, report_name="unchanged.json", stage=False, allow_direct_release=True)
        self.assertEqual([], backend.posts)
        self.assertEqual([], result["results"][0]["labels_to_move"])
        self.assertFalse(result["results"][0]["latest_assigned_automatically"])
        self.assertFalse(result["results"][0]["config_normalized_from_null"])
        self.assertEqual("", result["results"][0]["diff"])
        self.assertEqual(1, verify.verify(api, manifest, manifest.parent / "reports/unchanged.json"))

    def test_malformed_reports_are_rejected_cleanly(self):
        api, _ = self.client(); manifest = self.snapshot(api)
        _, snapshot = publish.load_snapshot(manifest)
        samples = [None, [], 5, "not an object", {"format_version": 2, "host": "https://example.com", "mode": "apply", "status": "verified", "snapshot_sha256": common.fingerprint(snapshot), "results": [None]}]
        path = self.root / "malformed-report.json"
        for data in samples:
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                with self.assertRaises(common.ReleaseError):
                    verify.verify(api, manifest, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
