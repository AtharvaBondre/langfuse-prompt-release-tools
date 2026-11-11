"""Offline contract tests: no Langfuse tenant or SDK is required."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from langfuse_release_common import ReleaseError, fingerprint
import langfuse_release_dependencies as dependencies


HOST = "https://example.com"
CONSUMER = {"sdk": "langfuse-python==5.2.0", "compiler": "app@abc123"}


def prompt(name, version, content, *, labels=None, kind="text"):
    return {"id": f"{name}-{version}", "name": name, "version": version, "type": kind,
            "prompt": content, "config": {}, "tags": [], "labels": labels or []}


def node(value):
    return {key: value[key] for key in ("id", "name", "version")}


class ResolverFixture:
    """Captured graph shape from the official resolver contract, not a renderer."""
    host = HOST

    def __init__(self):
        baseline = prompt("root", 1, "old", labels=["production"])
        root = prompt("root", 2, "R @@@langfusePrompt:name=child|version=3@@@")
        child = prompt("child", 3, "C @@@langfusePrompt:name=leaf|label=production@@@", labels=["production"])
        leaf = prompt("leaf", 4, "L", labels=["production"])
        self.raw = {(p["name"], p["version"]): p for p in (baseline, root, child, leaf)}
        self.resolved = {
            ("root", 2): {**root, "prompt": "R C L", "resolutionGraph": {
                "root": node(root), "dependencies": {root["id"]: [node(child)], child["id"]: [node(leaf)]}}},
            ("child", 3): {**child, "prompt": "C L", "resolutionGraph": {
                "root": node(child), "dependencies": {child["id"]: [node(leaf)]}}},
            ("leaf", 4): {**leaf, "resolutionGraph": None},
        }
        self.calls = []
        self.fail = None
        self.snapshot = {"format_version": 1, "host": HOST, "selector": {"label": "production", "version": None},
                         "prompts": [{"name": "root", "file": "prompts/root.txt", "baseline": baseline,
                                      "local_content": root["prompt"]}]}
        self.stage = {"format_version": 2, "mode": "stage", "status": "verified", "release_strategy": "stage",
                      "host": HOST, "snapshot_sha256": fingerprint(self.snapshot), "results": [
                          {"name": "root", "baseline_version": 1, "changed": True, "created_version": 2, "status": "verified"}]}

    def get(self, name, label=None, version=None):
        self.calls.append(("raw", name, label, version))
        if name == self.fail:
            raise ReleaseError("Synthetic timeout")
        if label is not None:
            matches = [p for (n, _), p in self.raw.items() if n == name and label in p["labels"]]
            if len(matches) != 1:
                raise ReleaseError("Synthetic missing or ambiguous selector")
            return copy.deepcopy(matches[0])
        if (name, version) not in self.raw:
            raise ReleaseError("Synthetic missing prompt")
        return copy.deepcopy(self.raw[(name, version)])

    def get_resolved(self, name, *, version):
        self.calls.append(("resolved", name, version))
        if name == self.fail or (name, version) not in self.resolved:
            raise ReleaseError("Synthetic resolution failure")
        return copy.deepcopy(self.resolved[(name, version)])

    def lock(self):
        return dependencies.build_dependency_lock(self, self.snapshot, self.stage, consumer=CONSUMER)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.api = ResolverFixture()

    def test_transitive_lock_and_fresh_validation(self):
        lock = self.api.lock()
        self.assertEqual("verified", lock["status"])
        self.assertEqual([("child", 3), ("leaf", 4), ("root", 2)],
                         [(n["name"], n["version"]) for n in lock["roots"][0]["nodes"]])
        self.assertIn(("raw", "leaf", "production", None), self.api.calls)
        self.assertEqual(lock, dependencies.check_dependency_lock(self.api, lock, self.api.snapshot, self.api.stage))
        self.assertNotEqual(lock["roots"][0]["raw_sha256"], lock["roots"][0]["resolved_sha256"])

    def test_nested_label_drift_even_when_resolved_server_cache_is_stale(self):
        lock = self.api.lock()
        self.api.raw[("leaf", 4)]["labels"] = []
        self.api.raw[("leaf", 5)] = prompt("leaf", 5, "new", labels=["production"])
        with self.assertRaisesRegex(ReleaseError, "drift"):
            dependencies.check_dependency_lock(self.api, lock, self.api.snapshot, self.api.stage)

    def test_new_graph_and_resolved_content_drift_blocks_existing_lock(self):
        lock = self.api.lock()
        for key in (("root", 2), ("child", 3)):
            response = self.api.resolved[key]
            response["resolutionGraph"]["dependencies"]["child-3"][0] = {"id": "leaf-5", "name": "leaf", "version": 5}
            response["prompt"] = response["prompt"].replace("L", "new")
        self.api.raw[("leaf", 4)]["labels"] = []
        new = prompt("leaf", 5, "new", labels=["production"])
        self.api.raw[("leaf", 5)], self.api.resolved[("leaf", 5)] = new, {**new, "resolutionGraph": None}
        with self.assertRaisesRegex(ReleaseError, "no longer matches"):
            dependencies.check_dependency_lock(self.api, lock, self.api.snapshot, self.api.stage)

    def test_missing_graph_is_not_assumed_to_be_leaf(self):
        for key in list(self.api.resolved):
            with self.subTest(key=key):
                saved = self.api.resolved[key].pop("resolutionGraph")
                with self.assertRaisesRegex(ReleaseError, "omitted resolutionGraph"):
                    self.api.lock()
                self.api.resolved[key]["resolutionGraph"] = saved

    def test_graph_null_with_dependency_tags_is_incomplete(self):
        self.api.resolved[("root", 2)]["resolutionGraph"] = None
        with self.assertRaisesRegex(ReleaseError, "incomplete"):
            self.api.lock()

    def test_cycle_is_rejected(self):
        graph = self.api.resolved[("root", 2)]["resolutionGraph"]
        graph["dependencies"]["leaf-4"] = [graph["root"]]
        with self.assertRaisesRegex(ReleaseError, "Cycle"):
            self.api.lock()

    def test_conflicting_node_identity_is_rejected(self):
        graph = self.api.resolved[("root", 2)]["resolutionGraph"]
        graph["dependencies"]["root-2"].append({"id": "child-3", "name": "other", "version": 3})
        with self.assertRaisesRegex(ReleaseError, "conflicting node"):
            self.api.lock()

    def test_orphan_adjacency_is_rejected(self):
        self.api.resolved[("root", 2)]["resolutionGraph"]["dependencies"]["orphan"] = [node(self.api.raw[("leaf", 4)])]
        with self.assertRaisesRegex(ReleaseError, "unreachable"):
            self.api.lock()

    def test_nested_subgraph_cannot_omit_transitive_edge(self):
        self.api.resolved[("child", 3)]["resolutionGraph"] = None
        with self.assertRaisesRegex(ReleaseError, "incomplete"):
            self.api.lock()

    def test_missing_child_and_nontext_child(self):
        child = self.api.raw.pop(("leaf", 4))
        with self.assertRaises(ReleaseError):
            self.api.lock()
        self.api.raw[("leaf", 4)] = {**child, "type": "chat", "prompt": [{"role": "user", "content": "L"}]}
        with self.assertRaisesRegex(ReleaseError, "non-text|text prompts"):
            self.api.lock()

    def test_partial_read_failure_never_returns_a_lock(self):
        self.api.fail = "leaf"
        with self.assertRaisesRegex(ReleaseError, "Synthetic"):
            self.api.lock()
        self.assertTrue(all(row[0] in ("raw", "resolved") for row in self.api.calls))

    def test_raw_staged_content_mismatch(self):
        self.api.raw[("root", 2)]["prompt"] = "unexpected"
        with self.assertRaisesRegex(ReleaseError, "reviewed snapshot"):
            self.api.lock()

    def test_leaf_content_and_config_mismatch(self):
        self.api.resolved[("leaf", 4)]["prompt"] = "invented resolution"
        with self.assertRaisesRegex(ReleaseError, "leaf prompt"):
            self.api.lock()
        self.api.resolved[("leaf", 4)]["config"] = {"changed": True}
        with self.assertRaisesRegex(ReleaseError, "config disagree"):
            self.api.lock()

    def test_tampered_lock_and_snapshot_binding(self):
        lock = self.api.lock()
        lock["consumer"]["compiler"] = "other"
        with self.assertRaisesRegex(ReleaseError, "changed since capture"):
            dependencies.check_dependency_lock(self.api, lock, self.api.snapshot, self.api.stage)
        lock = self.api.lock()
        self.api.snapshot["prompts"][0]["local_content"] += "changed"
        with self.assertRaises(ReleaseError):
            dependencies.check_dependency_lock(self.api, lock, self.api.snapshot, self.api.stage)

    def test_unsupported_reference_fails_instead_of_local_rendering(self):
        self.api.raw[("child", 3)]["prompt"] = "@@@langfusePrompt:name=leaf|version=4|extra=yes@@@"
        with self.assertRaisesRegex(ReleaseError, "documented"):
            self.api.lock()

    def test_chat_root_uses_official_resolved_content_and_normalized_hash(self):
        raw = self.api.raw[("root", 2)]
        raw.update(type="chat", prompt=[{"role": "system", "content": raw["prompt"]}])
        self.api.snapshot["prompts"][0]["baseline"].update(type="chat", prompt=[{"role": "system", "content": "old"}])
        self.api.snapshot["prompts"][0]["local_content"] = raw["prompt"]
        self.api.stage["snapshot_sha256"] = fingerprint(self.api.snapshot)
        self.api.resolved[("root", 2)].update(type="chat", prompt=[{"type": "chatmessage", "role": "system", "content": "R C L"}])
        lock = self.api.lock()
        expected = {**raw, "prompt": [{"role": "system", "content": "R C L"}]}
        self.assertEqual(dependencies.prompt_sha256(expected), lock["roots"][0]["resolved_sha256"])

    def test_immutable_disk_capture_and_no_artifact_on_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts/root.txt").write_text(self.api.snapshot["prompts"][0]["local_content"])
            manifest = root / "manifest.json"
            data = copy.deepcopy(self.api.snapshot)
            del data["prompts"][0]["local_content"]
            manifest.write_text(json.dumps(data))
            stage = root / "stage.json"
            stage.write_text(json.dumps(self.api.stage))
            self.api.fail = "leaf"
            with self.assertRaises(ReleaseError):
                dependencies.capture_dependency_lock(self.api, manifest, stage, consumer=CONSUMER, report_name="locked.json")
            self.assertFalse((root / "reports/locked.json").exists())
            self.api.fail = None
            dependencies.capture_dependency_lock(self.api, manifest, stage, consumer=CONSUMER, report_name="locked.json")
            with self.assertRaisesRegex(ReleaseError, "already exists"):
                dependencies.capture_dependency_lock(self.api, manifest, stage, consumer=CONSUMER, report_name="locked.json")

    def test_lock_schema_rejects_rehashed_missing_nodes_and_spoofed_types(self):
        lock = self.api.lock()
        for mutation in (lambda value: value["roots"][0]["nodes"].pop(),
                         lambda value: value["roots"][0]["nodes"][0].update(type="chat"),
                         lambda value: value["roots"][0]["nodes"][0].update(selectors=[])):
            changed = copy.deepcopy(lock)
            mutation(changed)
            changed["evidence_sha256"] = fingerprint({key: value for key, value in changed.items() if key != "evidence_sha256"})
            with self.assertRaises(ReleaseError):
                dependencies.validate_lock_integrity(changed)

    def test_selector_hash_must_equal_its_concrete_child(self):
        original = self.api.get
        def inconsistent(name, label=None, version=None):
            value = original(name, label=label, version=version)
            if label is not None:
                value["prompt"] = "changed same-version content"
            return value
        self.api.get = inconsistent
        with self.assertRaisesRegex(ReleaseError, "content changed between selector"):
            self.api.lock()

    def test_second_root_failure_does_not_return_partial_batch(self):
        baseline = prompt("z-root", 1, "old", labels=["production"])
        self.api.snapshot["prompts"].append({"name": "z-root", "file": "prompts/z-root.txt",
            "baseline": baseline, "local_content": "new"})
        self.api.stage["snapshot_sha256"] = fingerprint(self.api.snapshot)
        self.api.stage["results"].append({"name": "z-root", "baseline_version": 1, "changed": True,
                                        "created_version": 2, "status": "verified"})
        self.api.fail = "z-root"
        with self.assertRaisesRegex(ReleaseError, "Synthetic timeout"):
            self.api.lock()
        self.assertIn(("resolved", "root", 2), self.api.calls)
        self.assertIn(("raw", "z-root", None, 2), self.api.calls)

    def test_resolved_transport_is_explicit_version_get_and_encodes_folder(self):
        client = dependencies.DependencyClient.__new__(dependencies.DependencyClient)
        calls = []
        def request(method, path, query):
            calls.append((method, path, query))
            return {**prompt("folder/root", 2, "x"), "resolutionGraph": None}
        client._request = request
        client.get_resolved("folder/root", version=2)
        self.assertEqual([("GET", "/api/public/v2/prompts/folder%2Froot", {"version": 2, "resolve": "true"})], calls)
        with self.assertRaises(ReleaseError):
            client.get_resolved("folder/root", version=True)


if __name__ == "__main__":
    unittest.main()
