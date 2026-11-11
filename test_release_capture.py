"""Offline tests for the allowlisted Python sidecar and JS observation contract."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Support an independent worktree reviewing the parent's in-progress module.
# Installed/source tests use their normal colocated modules when unset.
SOURCE = Path(os.environ.get("RELEASE_PYTHON_PATH", Path(__file__).parent)).resolve()
if os.environ.get("RELEASE_PYTHON_PATH"):
    sys.path.insert(0, str(SOURCE))

import langfuse_release_capture as capture
import langfuse_release_adoption as adoption
from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_dependencies import prompt_sha256


def snapshot(**updates):
    value = {"name": "root", "version": 2, "type": "text", "prompt": "café 漢字 🌍 {{name}}",
             "config": {"temperature": 0.2}, "is_fallback": False}
    value.update(updates)
    return value


def metadata():
    return {"sdk_version": "6.1.1", "consumer_id": "app", "deployment_id": "app@abc", "request_id": "request-1",
            "requested_label": "production", "request_started_at_utc": "2026-11-11T12:00:00+00:00",
            "observed_at_utc": "2026-11-11T12:00:01+00:00", "cache_ttl_seconds": 0}


def running_capture():
    return {"completed_at_utc": None, "completion": {"local": "running"},
            "targets": [{"name": "root", "version": 2, "case_ids": ["case-1"]}], "items": []}


class CaptureBridgeTests(unittest.TestCase):
    def dispatch(self, operation, arguments):
        return capture.dispatch({"operation": operation, "arguments": arguments})

    def command(self, raw):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("LANGFUSE_") and key not in ("GH_TOKEN", "GITHUB_TOKEN")}
        env["PYTHONPATH"] = str(SOURCE)
        return subprocess.run([sys.executable, "-m", "langfuse_release_capture"], input=raw,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=SOURCE,
                              check=False, timeout=20)

    def prompt_arguments(self, **updates):
        arguments = {"capture": running_capture(), "item_id": "case-1", "requested_name": "root",
                     "requested_version": 2, "prompt": snapshot()}
        arguments.update(updates)
        return arguments

    def test_hash_preserves_python_fractional_unicode_and_nested_json_contract(self):
        value = {"floats": [0.2, 1e-7, 1e-12, 123456.789, -0.25], "unicode": "café e\u0301 漢字 🌍", "nested": [None, True, {"x": False}]}
        self.assertEqual(fingerprint(value), self.dispatch("hash", {"value": value}))
        result = self.command(json.dumps({"operation": "hash", "arguments": {"value": value}}).encode())
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(fingerprint(value), json.loads(result.stdout))

    def test_js_adoption_records_exact_method_sdk_and_prompt_hash(self):
        value = snapshot()
        observed = self.dispatch("adoption", {"prompt_snapshot": value, **metadata()})
        self.assertEqual("langfuse-js", observed["sdk"]["name"])
        self.assertEqual("6.1.1", observed["sdk"]["version"])
        self.assertEqual("langfuse_release_adoption.capture_js_sdk_observation/v1", observed["capture_method"])
        self.assertEqual(prompt_sha256(value), observed["returned"]["resolved_sha256"])
        self.assertEqual(observed, adoption._observations([observed])[0])
        value["config"]["temperature"] = 0.9
        self.assertNotEqual(prompt_sha256(value), observed["returned"]["resolved_sha256"])

    def test_chat_placeholder_hash_matches_existing_python_canonicalization(self):
        content = [{"type": "chatmessage", "role": "system", "content": "Hello 🌍"},
                   {"type": "placeholder", "name": "history"}, {"role": "user", "content": "{{question}}"}]
        value = snapshot(type="chat", prompt=content)
        observed = self.dispatch("adoption", {"prompt_snapshot": value, **metadata()})
        normalized = copy.deepcopy(value); del normalized["prompt"][0]["type"]
        self.assertEqual(prompt_sha256(normalized), observed["returned"]["resolved_sha256"])
        normalized["prompt"][1]["name"] = "other"
        self.assertNotEqual(prompt_sha256(normalized), observed["returned"]["resolved_sha256"])

    def test_js_observation_type_and_fallback_flags_are_strict(self):
        for bad in ("false", 0, None):
            with self.subTest(fallback=bad), self.assertRaises(ReleaseError):
                self.dispatch("adoption", {"prompt_snapshot": snapshot(is_fallback=bad), **metadata()})
        for bad in (True, "2", -1, 0):
            with self.subTest(version=bad), self.assertRaises(ReleaseError):
                self.dispatch("adoption", {"prompt_snapshot": snapshot(version=bad), **metadata()})
        for bad in (snapshot(type="unknown"), {**snapshot(), "extra": "untrusted"}):
            with self.assertRaises(ReleaseError):
                self.dispatch("adoption", {"prompt_snapshot": bad, **metadata()})
        observed = self.dispatch("adoption", {"prompt_snapshot": snapshot(version=0, is_fallback=True), **metadata()})
        self.assertTrue(observed["is_fallback"])
        self.assertEqual(0, observed["returned"]["version"])

    def test_normalization_rejects_mismatched_method_and_sdk_identity(self):
        observed = self.dispatch("adoption", {"prompt_snapshot": snapshot(), **metadata()})
        for method, sdk in ((adoption.CAPTURE_METHOD, "langfuse-js"), (adoption.JS_CAPTURE_METHOD, "langfuse-python"),
                            ("fake.capture/v1", "langfuse-js"), (adoption.JS_CAPTURE_METHOD, "unknown-sdk")):
            row = copy.deepcopy(observed); row["capture_method"] = method; row["sdk"]["name"] = sdk
            with self.subTest(method=method, sdk=sdk), self.assertRaises(ReleaseError):
                adoption._observations([row])

    def test_prompt_operation_records_returned_identity_and_rejects_wrong_request(self):
        result = self.dispatch("prompt", self.prompt_arguments())
        self.assertEqual(2, result["items"][0]["prompts"][0]["returned_version"])
        for updates in ({"requested_name": "other"}, {"requested_version": 1}, {"requested_version": True},
                        {"item_id": "other-case"}, {"prompt": snapshot(version=1)}, {"prompt": snapshot(version=True)}):
            with self.subTest(updates=updates), self.assertRaises(ReleaseError):
                self.dispatch("prompt", self.prompt_arguments(**updates))

    def test_direct_prompt_operation_rejects_fallback_and_unknown_flag_before_mutation(self):
        for bad in (True, None, "false", 0):
            arguments = self.prompt_arguments(prompt=snapshot(is_fallback=bad))
            with self.subTest(flag=bad), self.assertRaises(ReleaseError):
                self.dispatch("prompt", arguments)
            self.assertEqual([], arguments["capture"]["items"])
        value = snapshot(); del value["is_fallback"]
        with self.assertRaises(ReleaseError):
            self.dispatch("prompt", self.prompt_arguments(prompt=value))

    def test_prompt_duplicate_and_finalized_capture_are_rejected(self):
        arguments = self.prompt_arguments()
        self.dispatch("prompt", arguments)
        with self.assertRaises(ReleaseError):
            self.dispatch("prompt", arguments)
        arguments = self.prompt_arguments(); arguments["capture"]["completion"]["local"] = "completed"
        with self.assertRaises(ReleaseError):
            self.dispatch("prompt", arguments)

    def test_only_allowlisted_operations_dispatch_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            for operation in (f"touch {marker}", f"$(touch {marker})", "exec", "eval", "__import__('os').system('id')"):
                with self.subTest(operation=operation), self.assertRaises(ReleaseError):
                    self.dispatch(operation, {})
            result = self.command(json.dumps({"operation": "exec", "arguments": {"command": f"touch {marker}"}}).encode())
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(marker.exists())

    def test_dispatch_schema_and_extra_arguments_fail(self):
        for request in ([], {}, {"operation": "hash", "arguments": {}, "command": "anything"},
                        {"operation": "hash", "arguments": []}):
            with self.subTest(request=request), self.assertRaises(ReleaseError):
                capture.dispatch(request)
        with self.assertRaises(TypeError):
            self.dispatch("hash", {"value": 1, "command": "echo invalid"})

    def test_start_and_finish_route_only_to_existing_engine_functions(self):
        with patch.object(capture, "start_capture", return_value={"started": True}) as start:
            self.assertEqual({"started": True}, self.dispatch("start", {"manifest_path": "snapshot", "stage_report_path": "stage", "dataset": {}, "execution": {}}))
            start.assert_called_once_with(manifest_path="snapshot", stage_report_path="stage", dataset={}, execution={})
        with patch.object(capture, "finalize_capture", return_value={"finished": True}) as finish:
            self.assertEqual({"finished": True}, self.dispatch("finish", {"capture": {}, "result_path": "results", "remote_export": "unconfirmed"}))
            finish.assert_called_once_with(capture={}, result_path="results", remote_export="unconfirmed")

    def test_cli_rejects_duplicate_json_keys_nan_malformed_utf8_and_invalid_json(self):
        raw_inputs = [b'{"operation":"hash","operation":"exec","arguments":{"value":0}}',
                      b'{"operation":"hash","arguments":{"value":NaN}}',
                      b'{"operation":"hash","arguments":{"value":Infinity}}',
                      b'\xff', b'not-json-PRIVATE-PROMPT-CONTENT', b'[]']
        for raw in raw_inputs:
            with self.subTest(raw=raw):
                result = self.command(raw)
                self.assertNotEqual(0, result.returncode)
                self.assertTrue(result.stderr.startswith(b"Capture rejected:"), result.stderr)
                self.assertNotIn(b"PRIVATE-PROMPT-CONTENT", result.stderr)
                self.assertEqual(b"", result.stdout)

    def test_cli_enforces_input_size_bound(self):
        result = self.command(b" " * 8_000_001)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"8 MB bound", result.stderr)

    def test_cli_does_not_accept_positional_shell_commands(self):
        result = subprocess.run([sys.executable, "-m", "langfuse_release_capture", "--command", "echo sentinel"],
                                env={**os.environ, "PYTHONPATH": str(SOURCE)}, cwd=SOURCE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn(b"sentinel\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
