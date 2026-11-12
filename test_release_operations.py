"""Native OS-lock, exclusive-journal and real subprocess crash boundaries.

The HTTP service is a disposable loopback test server backed by synthetic data.
These tests exercise process death, not power failure or distributed locking.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

import langfuse_release_operations as operations
from langfuse_release_common import Client, ReleaseError
import publish_langfuse_prompts as publish
from pull_langfuse_prompts import export
from reconcile_langfuse_release import ReadOnlyClient, reconcile
from test_prompt_release import FakeHTTP, prompt


ROOT = Path(__file__).resolve().parent
PROJECT = "synthetic-project"


class OperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_policy_pin_cannot_edit_a_snapshot_during_an_operation(self):
        from langfuse_release_policy import pin_policy
        manifest = self.root / "prompt-index.json"
        manifest.write_text("original evidence")
        with operations.snapshot_lock(self.root), self.assertRaisesRegex(ReleaseError, "holds this snapshot lock"):
            pin_policy(manifest, self.root / "policy.json")
        self.assertEqual("original evidence", manifest.read_text())

    def test_exclusive_create_preserves_previous_evidence(self):
        path = self.root / "attempt.json"
        operations.durable_json(path, {"status": "submitting"}, create=True)
        original = path.read_bytes()
        with self.assertRaisesRegex(ReleaseError, "already exists"):
            operations.durable_json(path, {"status": "verified"}, create=True)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([], list(self.root.glob(".release-*.json")))

    def test_replace_failure_preserves_complete_previous_journal(self):
        path = self.root / "attempt.json"
        operations.durable_json(path, {"status": "submitting"}, create=True)
        original = path.read_bytes()
        with patch.object(operations.os, "replace", side_effect=OSError("injected replacement failure")), self.assertRaises(OSError):
            operations.durable_json(path, {"status": "verified"})
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([], list(self.root.glob(".release-*.json")))

    def test_file_sync_failure_cannot_publish_new_evidence(self):
        path = self.root / "attempt.json"
        with patch.object(operations.os, "fsync", side_effect=OSError("injected full disk")), self.assertRaises(OSError):
            operations.durable_json(path, {"status": "submitting"}, create=True)
        self.assertFalse(path.exists())
        self.assertEqual([], list(self.root.glob(".release-*.json")))

    @unittest.skipIf(os.name == "nt", "Windows has no portable directory fsync; process-crash tests still run")
    def test_new_reports_directory_entry_is_synced_before_journal_is_reported_durable(self):
        synced = []
        actual = operations.sync_directory
        def observe(path):
            synced.append(Path(path).resolve())
            return actual(path)
        with patch.object(operations, "sync_directory", side_effect=observe):
            operations.durable_json(self.root / "reports" / "attempt.json", {"status": "submitting"}, create=True)
        self.assertIn(self.root.resolve(), synced, "The new reports directory entry must be durable in its parent")
        self.assertIn((self.root / "reports").resolve(), synced)

    def test_invalid_json_leaves_no_report_or_partial_file(self):
        with self.assertRaises(ValueError):
            operations.durable_json(self.root / "attempt.json", {"value": float("nan")}, create=True)
        self.assertFalse((self.root / "attempt.json").exists())
        self.assertEqual([], list(self.root.glob(".release-*.json")))

    def test_two_processes_cannot_overwrite_the_same_new_journal(self):
        code = """
import sys
from langfuse_release_operations import durable_json
from langfuse_release_common import ReleaseError
try:
    durable_json(sys.argv[1], {'writer': sys.argv[2], 'payload': 'x' * 65536}, create=True)
except ReleaseError:
    sys.exit(3)
"""
        path = self.root / "attempt.json"
        children = [subprocess.Popen([sys.executable, "-c", code, str(path), name], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for name in ("one", "two")]
        try:
            for child in children:
                _, error = child.communicate(timeout=10)
                self.assertIn(child.returncode, (0, 3), error)
            self.assertEqual([0, 3], sorted(child.returncode for child in children))
            report = json.loads(path.read_text())
            self.assertIn(report["writer"], ("one", "two"))
            self.assertEqual("x" * 65536, report["payload"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


class SubprocessReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = FakeHTTP([prompt()])
        backend = self.backend
        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length) if length else None
                request = urllib.request.Request("https://example.com" + self.path, data=payload, method=self.command)
                try:
                    with backend.open(request) as response:
                        raw = response.read()
                except Exception:
                    self.send_error(500, "synthetic test service failed")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = handle_request
            do_POST = handle_request
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop():
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        self.addCleanup(stop)
        self.host = f"http://127.0.0.1:{server.server_port}"
        environment = patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "synthetic-public", "LANGFUSE_SECRET_KEY": "synthetic-secret"})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = Client(self.host, timeout=3, project_id=PROJECT)
        self.client._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.manifest = export(self.client, self.root / "snapshot", label="production", names=["example"])
        entry = json.loads(self.manifest.read_text())["prompts"][0]
        (self.manifest.parent / entry["file"]).write_text("Answer {{question}} with a useful example.")

    def child_environment(self):
        allowed = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "VIRTUAL_ENV")
        return {**{key: os.environ[key] for key in allowed if key in os.environ},
                "LANGFUSE_PUBLIC_KEY": "synthetic-public", "LANGFUSE_SECRET_KEY": "synthetic-secret",
                "NO_PROXY": "localhost,127.0.0.1,::1"}

    def crash_release(self, after_commit):
        code = """
import os, sys, urllib.request
from langfuse_release_common import Client
from publish_langfuse_prompts import release
class CrashClient(Client):
    def create(self, payload):
        if sys.argv[3] == 'after':
            super().create(payload)
        os._exit(86)
client = CrashClient(sys.argv[1], timeout=3, project_id='synthetic-project')
client._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
release(client, sys.argv[2], apply=True, report_name='crashed.json')
"""
        process = subprocess.run([sys.executable, "-c", code, self.host, str(self.manifest),
                                  "after" if after_commit else "before"], cwd=ROOT,
                                 env=self.child_environment(), capture_output=True, text=True, timeout=15)
        self.assertEqual(86, process.returncode, process.stdout + process.stderr)
        journal = self.manifest.parent / "reports/crashed.json"
        report = json.loads(journal.read_text())
        self.assertEqual("submitting", report["results"][0]["status"])
        self.assertNotIn("created_version", report["results"][0])
        # Native flock/msvcrt locks must be released even though Python finally
        # blocks never executed. Do not delete or replace the retained lock file.
        with operations.snapshot_lock(self.manifest.parent):
            self.assertTrue((self.manifest.parent / ".release.lock").exists())
        return journal

    def test_process_death_after_server_commit_preserves_submitting_and_never_replays(self):
        journal = self.crash_release(after_commit=True)
        self.assertEqual(1, len(self.backend.posts))
        self.assertIn(8, self.backend.versions["example"])
        before, raw = copy.deepcopy(self.backend.versions), journal.read_bytes()
        self.backend.requests.clear()
        reader = ReadOnlyClient(self.host, timeout=3, project_id=PROJECT)
        reader._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        report = reconcile(reader, self.manifest, journal, report_name="reconciled.json")
        self.assertEqual("unknown", report["results"][0]["classification"])
        self.assertEqual([8], [row["version"] for row in report["results"][0]["observations"]["candidate_versions"]])
        self.assertEqual(before, self.backend.versions)
        self.assertEqual(raw, journal.read_bytes())
        self.assertTrue(all(row["method"] == "GET" for row in self.backend.requests))
        proposal = report["resume_proposal"]
        self.assertIs(False, proposal["automatic_execution"])
        self.assertEqual("stop_and_inspect", proposal["actions"][0]["action"])

    def test_process_death_before_post_is_unknown_not_permission_to_retry(self):
        journal = self.crash_release(after_commit=False)
        self.assertEqual([], self.backend.posts)
        self.assertEqual([7], list(self.backend.versions["example"]))
        reader = ReadOnlyClient(self.host, timeout=3, project_id=PROJECT)
        reader._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        report = reconcile(reader, self.manifest, journal, report_name="reconciled.json")
        self.assertEqual("unknown", report["results"][0]["classification"])
        self.assertEqual([], self.backend.posts)

    def test_concurrent_snapshot_operation_stops_before_http_and_abrupt_exit_releases_lock(self):
        publish.release(self.client, self.manifest, report_name="preview.json")
        ready = self.root / "locked.txt"
        code = """
import sys, time
from pathlib import Path
from langfuse_release_operations import snapshot_lock
with snapshot_lock(sys.argv[1]):
    Path(sys.argv[2]).write_text('locked')
    time.sleep(30)
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.manifest.parent), str(ready)], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.child_environment(), text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "Lock holder failed to become ready within five seconds")
            self.backend.requests.clear()
            with self.assertRaisesRegex(ReleaseError, "Another local operation"):
                publish.release(self.client, self.manifest, apply=True, report_name="competing.json")
            reader = ReadOnlyClient(self.host, timeout=3, project_id=PROJECT)
            with self.assertRaisesRegex(ReleaseError, "Another local operation"):
                reconcile(reader, self.manifest, self.manifest.parent / "reports/preview.json")
            self.assertEqual([], self.backend.requests)
            self.assertFalse((self.manifest.parent / "reports/competing.json").exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        # A new legitimate operation can proceed on the same retained lock file.
        report = publish.release(self.client, self.manifest, apply=True, report_name="after-exit.json")
        self.assertEqual("verified", report["status"])
        self.assertEqual(1, len(self.backend.posts))


if __name__ == "__main__":
    unittest.main()
