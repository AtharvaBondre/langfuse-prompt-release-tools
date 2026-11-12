"""Source-checkout CI wrapper regressions; no Docker, SDK or network operations."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tools import run_live_ci as live


class LiveCIOrchestrationTests(unittest.TestCase):
    def exercise(self, case):
        before = (live.ROOT / "compose.demo.json").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            private, public = Path(directory) / "private", Path(directory) / "public"
            calls = []
            environment = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64",
                           "GITHUB_SHA": "a" * 40, "LANGFUSE_SECRET_KEY": "inherited-secret",
                           "AWS_ACCESS_KEY_ID": "unrelated-credential", "DOCKER_HOST": "ssh://unrelated"}
            version = "4.59.0" if case == "missing_api" else "4.78.0"

            def process(argv, **kwargs):
                calls.append(argv)
                env = kwargs["env"]
                self.assertNotIn("AWS_ACCESS_KEY_ID", env)
                self.assertNotIn("DOCKER_HOST", env)
                self.assertNotEqual(env.get("LANGFUSE_SECRET_KEY"), "inherited-secret")
                if argv[0] == "docker":
                    self.assertEqual(argv[:3], ["docker", "--context", "default"])
                    output = ""
                    if "context" in argv:
                        output = "ssh://unexpected.invalid" if case == "remote_docker" else "unix:///var/run/docker.sock\n"
                    if "image" in argv:
                        output = "linux/amd64\n"
                    if "logs" in argv:
                        values = live.read_values(private)
                        basic = base64.b64encode((values["LANGFUSE_PUBLIC_KEY"] + ":" + values["LANGFUSE_SECRET_KEY"]).encode()).decode()
                        output = (values["LANGFUSE_SECRET_KEY"] + " postgresql://postgres:" + values["POSTGRES_PASSWORD"]
                                  + "@db/test\nAuthorization: Basic " + basic)
                    code = 1 if "down" in argv and case == "cleanup_failed" else 0
                    return subprocess.CompletedProcess(argv, code, output, "")
                if case == "timeout":
                    raise subprocess.TimeoutExpired(argv, 360, output=env["LANGFUSE_SECRET_KEY"])
                destination = Path(argv[argv.index("--out-dir") + 1])
                destination.mkdir()
                (destination / "summary.json").write_text(json.dumps({
                    "status": "passed", "model_calls": 0, "checks": ["Mock unit-test fixture, not live evidence"]}))
                return subprocess.CompletedProcess(argv, 0, "", "")

            def request(path, values=None):
                if path == "/api/public/health":
                    return 200, {"version": version}
                if path == "/api/public/projects":
                    return 200, {"data": [{"id": values["LANGFUSE_INIT_PROJECT_ID"]}]}
                url, query = urlsplit(path), parse_qs(urlsplit(path).query)
                if url.path in ("/api/public/experiments", "/api/public/experiment-items"):
                    self.assertTrue(query["fromStartTime"])
                    self.assertTrue(query["toStartTime"])
                return (404 if case == "missing_api" else 500 if case == "api_500" else 200), {"data": []}

            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(live.platform, "machine", return_value="x86_64"), \
                    patch.object(live.subprocess, "run", side_effect=process), \
                    patch.object(live, "request_json", side_effect=request), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = live.run(version, private, public)
            self.assertEqual(code, 0 if case in ("passed", "missing_api") else 1)
            summary = json.loads((public / "summary.json").read_text())
            expected = "passed" if case == "passed" else "unsupported_missing_apis" if case == "missing_api" else "failed"
            self.assertEqual(summary["status"], expected)
            self.assertEqual({path.name for path in public.iterdir()}, {"summary.json", "sanitized.log"})
            all_public = "".join(path.read_text() for path in public.iterdir())
            for value in live.read_values(private).values():
                self.assertNotIn(value, all_public)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE((private / "runtime.env").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            config = json.loads((private / "compose.json").read_text())
            for service in config["services"].values():
                self.assertEqual(service["platform"], "linux/amd64")
                self.assertIn("@sha256:", service["image"])
            if case == "remote_docker":
                self.assertTrue(all("compose" not in args for args in calls))
            if case == "cleanup_failed":
                self.assertEqual(summary["cleanup_status"], "failed")
                self.assertEqual(summary["status_before_cleanup_failure"], "passed")
        self.assertEqual((live.ROOT / "compose.demo.json").read_bytes(), before)

    def test_both_disposable_demo_clients_accept_the_required_project(self):
        from examples.sdk_release_demo import ObservedClient as SDKClient
        from integration_langfuse_release import ObservedClient as LegacyClient
        from langfuse_release_common import Client
        # Run the constructors used by both real CLI demos without opening HTTP.
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "synthetic", "LANGFUSE_SECRET_KEY": "synthetic"}), \
                patch.object(Client, "_request", side_effect=AssertionError("No remote calls in constructor")):
            for client_type in (SDKClient, LegacyClient):
                client = client_type(live.HOST, project_id="synthetic-project")
                self.assertEqual("synthetic-project", client.expected_project_id)

    def test_success_and_artifact_privacy(self):
        self.exercise("passed")

    def test_explicit_missing_api_is_classified_unsupported(self):
        self.exercise("missing_api")

    def test_server_error_is_not_compatibility_success(self):
        self.exercise("api_500")

    def test_timeout_fails_and_redacts_captured_output(self):
        self.exercise("timeout")

    def test_cleanup_failure_prevents_green_result(self):
        self.exercise("cleanup_failed")

    def test_remote_docker_is_never_used_even_during_cleanup(self):
        self.exercise("remote_docker")

    def test_refuses_execution_outside_explicit_ci_runner(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                patch.object(live.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "isolated GitHub Linux X64"):
                live.run("4.78.0", Path(directory) / "private", Path(directory) / "public")
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
