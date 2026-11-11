#!/usr/bin/env python3
"""Lock server-resolved prompt dependencies; all remote operations are GETs.

The selector scanner audits the documented reference syntax. It never renders
prompts: Langfuse's resolved response is the authority for runtime content.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from evaluate_langfuse_release import _read_object, _stage_targets
from langfuse_release_common import (
    Client, ReleaseError, _canonical_content, _label, _name, _positive_int,
    fingerprint, inside, make_payload, validate_prompt,
)
from publish_langfuse_prompts import load_snapshot, write_report


class DependencyClient(Client):
    """The existing raw client plus one explicit-version, resolved read."""

    def get_resolved(self, name, *, version):
        _name(name)
        if not _positive_int(version):
            raise ReleaseError("Resolved reads require an explicit positive version.")
        data = self._request("GET", "/api/public/v2/prompts/" + urllib.parse.quote(name, safe=""),
                             {"version": version, "resolve": "true"})
        return validate_prompt(data, name, version)


def resolved_read(client, name, version):
    """Use a client's resolved reader or its existing audited HTTP transport."""
    reader = getattr(client, "get_resolved", None)
    if reader is not None:
        result = reader(name, version=version)
    elif isinstance(client, Client):
        result = DependencyClient.get_resolved(client, name, version=version)
    else:
        raise ReleaseError("Dependency checks require an explicit server-resolved reader.")
    result = validate_prompt(result, name, version)
    if "resolutionGraph" not in result:
        raise ReleaseError("Resolved response omitted resolutionGraph; dependency closure is unknown.")
    return result


def prompt_value(prompt):
    """Fields actually consumed; labels/tags are mutable routing metadata."""
    return {"name": prompt["name"], "version": prompt["version"], "type": prompt["type"],
            "prompt": _canonical_content(prompt["prompt"], prompt["type"]),
            "config": copy.deepcopy(prompt["config"])}


def prompt_sha256(prompt):
    return fingerprint(prompt_value(prompt))


def _selectors(raw):
    # Intentionally accept only unambiguous documented tags. Unsupported syntax
    # is blocked, not silently interpreted differently from the upstream parser.
    content = raw["prompt"]
    texts = [content] if raw["type"] == "text" else [m.get("content", "") for m in content]
    selectors = {}
    for text in texts:
        matches = list(re.finditer(r"@@@langfusePrompt:(.*?)@@@", text))
        if text.count("@@@langfusePrompt:") != len(matches):
            raise ReleaseError("Incomplete or unsupported prompt dependency tag.")
        for match in matches:
            parts = match.group(1).split("|")
            if len(parts) != 2 or not parts[0].startswith("name="):
                raise ReleaseError("Dependency locks accept only documented name-first dependency tags.")
            name = _name(parts[0][5:])
            if "=" in name or "\\" in name:
                raise ReleaseError("Ambiguous dependency name in a reference tag.")
            field, sep, value = parts[1].partition("=")
            if not sep:
                raise ReleaseError("Dependency tag is missing its selector.")
            if field == "version" and re.fullmatch(r"[1-9][0-9]*", value):
                selector = {"name": name, "version": int(value)}
            elif field == "label":
                selector = {"name": name, "label": _label(value)}
            else:
                raise ReleaseError("Unsupported dependency selector; use an explicit positive version or label.")
            selectors[fingerprint(selector)] = selector
    return sorted(selectors.values(), key=lambda row: (row["name"], str(row)))


def _descriptor(value):
    if not isinstance(value, dict) or set(value) != {"id", "name", "version"}:
        raise ReleaseError("Malformed node in server resolutionGraph.")
    if not isinstance(value["id"], str) or not value["id"].strip():
        raise ReleaseError("Resolution graph nodes require non-empty IDs.")
    _name(value["name"])
    if not _positive_int(value["version"]):
        raise ReleaseError("Resolution graph node version must be a positive integer.")
    return copy.deepcopy(value)


def _graph(value, name, version):
    if value is None:
        return {"root": {"id": None, "name": name, "version": version}, "dependencies": {}}
    if not isinstance(value, dict) or set(value) != {"root", "dependencies"}:
        raise ReleaseError("Malformed server resolutionGraph.")
    root = _descriptor(value["root"])
    if (root["name"], root["version"]) != (name, version):
        raise ReleaseError("Resolution graph root differs from the requested prompt.")
    edges = value["dependencies"]
    if not isinstance(edges, dict):
        raise ReleaseError("Resolution graph dependencies must be an adjacency object.")
    nodes, normalized, identities = {root["id"]: root}, {}, {(name, version): root["id"]}
    for parent, children in edges.items():
        if not isinstance(parent, str) or not parent or not isinstance(children, list) or not children:
            raise ReleaseError("Resolution graph adjacency entries must contain children.")
        unique = {}
        for child in children:
            child = _descriptor(child)
            child_id = child["id"]
            identity = (child["name"], child["version"])
            if (child_id in nodes and nodes[child_id] != child) or (
                    identity in identities and identities[identity] != child_id):
                raise ReleaseError("Resolution graph contains conflicting node identities.")
            nodes[child_id], identities[identity], unique[child_id] = child, child_id, child
        normalized[parent] = sorted(unique.values(), key=lambda node: node["id"])
    reached = set()

    def visit(node_id, active, names):
        if node_id in active or nodes[node_id]["name"] in names:
            raise ReleaseError("Cycle in server prompt dependency graph.")
        if len(active) >= 64:
            raise ReleaseError("Dependency graph exceeds the local safety bound.")
        reached.add(node_id)
        for child in normalized.get(node_id, []):
            visit(child["id"], active | {node_id}, names | {nodes[node_id]["name"]})

    visit(root["id"], set(), set())
    if set(nodes) != reached or set(normalized) - reached:
        raise ReleaseError("Resolution graph contains unreachable or incomplete nodes.")
    if not normalized:
        raise ReleaseError("Dependency-free server responses must return resolutionGraph=null.")
    return {"root": root, "dependencies": {key: normalized[key] for key in sorted(normalized)}}


def _nodes(graph):
    result = {graph["root"]["id"]: graph["root"]}
    for children in graph["dependencies"].values():
        result.update({child["id"]: child for child in children})
    return result


def _subgraph(graph, node):
    edges = graph["dependencies"]
    selected = {}

    def visit(node_id):
        if node_id in selected or node_id not in edges:
            return
        selected[node_id] = edges[node_id]
        for child in edges[node_id]:
            visit(child["id"])

    visit(node["id"])
    if not selected:
        return {"root": {**node, "id": None}, "dependencies": {}}
    return {"root": node, "dependencies": {key: selected[key] for key in sorted(selected)}}


def _capture_root(client, name, version, expected):
    raw = validate_prompt(client.get(name, version=version), name, version)
    if prompt_sha256(raw) != prompt_sha256({**expected, "version": version}):
        raise ReleaseError("Staged raw prompt no longer matches the reviewed snapshot.")
    if set(raw["tags"]) != set(expected["tags"]):
        raise ReleaseError("Staged prompt tags no longer match the snapshot.")
    resolved = resolved_read(client, name, version)
    graph = _graph(resolved["resolutionGraph"], name, version)
    captured = []
    for node in sorted(_nodes(graph).values(), key=lambda n: (n["name"], n["version"])):
        node_name, node_version = node["name"], node["version"]
        is_root = (node_name, node_version) == (name, version)
        node_raw = raw if is_root else validate_prompt(client.get(node_name, version=node_version), node_name, node_version)
        node_resolved = resolved if is_root else resolved_read(client, node_name, node_version)
        if not is_root and node_raw["type"] != "text":
            raise ReleaseError("Langfuse composition dependencies must be text prompts.")
        if node_raw["type"] != node_resolved["type"] or fingerprint(node_raw["config"]) != fingerprint(node_resolved["config"]):
            raise ReleaseError("Raw and resolved prompt identity/config disagree.")
        if node_raw.get("id") is not None and node["id"] is not None and node_raw["id"] != node["id"]:
            raise ReleaseError("Raw prompt ID differs from the resolution graph.")
        node_graph = _graph(node_resolved["resolutionGraph"], node_name, node_version)
        if node_graph != _subgraph(graph, node):
            raise ReleaseError("Dependency graph changed or is incomplete across resolved reads.")
        selectors, direct, bindings = _selectors(node_raw), [], []
        for selector in selectors:
            # Raw GET bypasses Langfuse's server prompt cache. This check catches
            # nested label movement even while a resolved cache entry is stale.
            selected = validate_prompt(client.get(**selector), selector["name"], selector.get("version"))
            if selected["type"] != "text":
                raise ReleaseError("Prompt dependency selector returned a non-text prompt.")
            if "label" in selector and selector["label"] not in selected["labels"]:
                raise ReleaseError("Dependency selector did not carry the requested label.")
            direct.append((selected["name"], selected["version"]))
            bindings.append({**selector, "selected_version": selected["version"],
                             "selected_raw_sha256": prompt_sha256(selected)})
        graph_direct = {(child["name"], child["version"]) for child in graph["dependencies"].get(node["id"], [])}
        if set(direct) != graph_direct:
            raise ReleaseError("Dependency selector drift or incomplete server resolution graph.")
        if not selectors and prompt_sha256(node_raw) != prompt_sha256(node_resolved):
            raise ReleaseError("A leaf prompt resolved differently from its raw content.")
        captured.append({"id": node["id"], "name": node_name, "version": node_version,
                         "type": node_raw["type"], "raw_sha256": prompt_sha256(node_raw),
                         "resolved_sha256": prompt_sha256(node_resolved), "selectors": bindings})
    captured_by_identity = {(n["name"], n["version"]): n for n in captured}
    for captured_node in captured:
        for binding in captured_node["selectors"]:
            child = captured_by_identity[(binding["name"], binding["selected_version"])]
            if child["raw_sha256"] != binding["selected_raw_sha256"]:
                raise ReleaseError("Dependency raw content changed between selector and concrete reads.")
    return {"name": name, "version": version, "raw_sha256": prompt_sha256(raw),
            "resolved_sha256": prompt_sha256(resolved), "graph": graph, "nodes": captured}


def _consumer(value):
    if not isinstance(value, dict) or set(value) != {"sdk", "compiler"} or any(
            not isinstance(item, str) or not item.strip() for item in value.values()):
        raise ReleaseError("Dependency lock requires explicit consumer sdk and compiler identities.")
    return copy.deepcopy(value)


def build_dependency_lock(client, snapshot, stage, *, consumer):
    """Read all concrete nodes and selectors; return a lock only after full success."""
    consumer = _consumer(consumer)
    if type(stage.get("format_version")) is not int or snapshot.get("host") != client.host:
        raise ReleaseError("Dependency capture host/stage format mismatch.")
    targets = _stage_targets(snapshot, stage)
    entries = {entry["name"]: entry for entry in snapshot["prompts"]}
    roots = []
    for name, version in sorted(targets.items()):
        entry = entries[name]
        expected = make_payload(entry["baseline"], entry["local_content"])
        roots.append(_capture_root(client, name, version, expected))
    # A batch cannot attest two different states of the same child/selector.
    node_values, selector_values = {}, {}
    for root in roots:
        for node in root["nodes"]:
            key = node["name"], node["version"]
            value = node["raw_sha256"], node["resolved_sha256"]
            if key in node_values and node_values[key] != value:
                raise ReleaseError("Dependency content changed between batch roots.")
            node_values[key] = value
            for binding in node["selectors"]:
                selector = {k: v for k, v in binding.items() if k not in ("selected_version", "selected_raw_sha256")}
                key = fingerprint(selector)
                value = binding["selected_version"], binding["selected_raw_sha256"]
                if key in selector_values and selector_values[key] != value:
                    raise ReleaseError("Dependency selector changed between batch roots.")
                selector_values[key] = value
    lock = {"format_version": 1, "mode": "dependency_lock", "status": "verified",
            "host": client.host, "snapshot_sha256": fingerprint(snapshot),
            "stage_report_sha256": fingerprint(stage), "consumer": consumer, "roots": roots,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
    lock["evidence_sha256"] = fingerprint(lock)
    return lock


def validate_lock_integrity(lock):
    if (not isinstance(lock, dict) or type(lock.get("format_version")) is not int or lock["format_version"] != 1 or
            lock.get("mode") != "dependency_lock" or lock.get("status") != "verified"):
        raise ReleaseError("A verified format_version=1 dependency lock is required.")
    payload = {key: value for key, value in lock.items() if key != "evidence_sha256"}
    if lock.get("evidence_sha256") != fingerprint(payload):
        raise ReleaseError("Dependency lock changed since capture.")
    _consumer(lock.get("consumer"))
    roots = lock.get("roots")
    if not isinstance(roots, list) or not roots:
        raise ReleaseError("Dependency lock contains no roots.")
    identities = set()
    for root in roots:
        if not isinstance(root, dict) or not _positive_int(root.get("version")):
            raise ReleaseError("Invalid dependency lock root.")
        name = _name(root.get("name"))
        if name in identities:
            raise ReleaseError("Duplicate dependency lock root.")
        identities.add(name)
        for key in ("raw_sha256", "resolved_sha256"):
            if not isinstance(root.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", root[key]):
                raise ReleaseError("Invalid dependency lock root hash.")
    for root in roots:
        graph_value = root.get("graph")
        if not isinstance(graph_value, dict):
            raise ReleaseError("Dependency lock is missing its graph.")
        if graph_value == {"root": {"id": None, "name": root["name"], "version": root["version"]}, "dependencies": {}}:
            graph = graph_value
        else:
            graph = _graph(graph_value, root["name"], root["version"])
            if graph != graph_value:
                raise ReleaseError("Dependency lock graph is not canonical.")
        expected = _nodes(graph)
        nodes = root.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(expected):
            raise ReleaseError("Dependency lock is missing concrete node hashes.")
        concrete = {}
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"id", "name", "version", "type", "raw_sha256", "resolved_sha256", "selectors"}:
                raise ReleaseError("Invalid concrete dependency node.")
            node_id = node["id"]
            if not isinstance(node_id, (str, type(None))) or node_id in concrete or node_id not in expected:
                raise ReleaseError("Invalid or duplicate concrete dependency identity.")
            if {key: node[key] for key in ("id", "name", "version")} != expected[node_id] or type(node["version"]) is not int:
                raise ReleaseError("Dependency node differs from graph identity.")
            if node["type"] not in ("text", "chat") or (node_id != graph["root"]["id"] and node["type"] != "text"):
                raise ReleaseError("Invalid dependency node prompt type.")
            for key in ("raw_sha256", "resolved_sha256"):
                if not isinstance(node[key], str) or not re.fullmatch(r"[a-f0-9]{64}", node[key]):
                    raise ReleaseError("Invalid concrete dependency hash.")
            if not isinstance(node["selectors"], list):
                raise ReleaseError("Dependency node selectors must be a list.")
            concrete[node_id] = node
        by_identity = {(node["name"], node["version"]): node for node in concrete.values()}
        for node_id, node in concrete.items():
            selected, seen_selectors = set(), set()
            for binding in node["selectors"]:
                if not isinstance(binding, dict):
                    raise ReleaseError("Invalid dependency selector binding.")
                kind = "label" if "label" in binding else "version"
                if set(binding) != {"name", kind, "selected_version", "selected_raw_sha256"}:
                    raise ReleaseError("Incomplete dependency selector binding.")
                _name(binding["name"])
                if kind == "label":
                    _label(binding["label"])
                elif not _positive_int(binding["version"]) or binding["version"] != binding["selected_version"]:
                    raise ReleaseError("Dependency version selector binding differs from its target.")
                if not _positive_int(binding["selected_version"]):
                    raise ReleaseError("Invalid dependency selector target version.")
                selector_key = fingerprint({"name": binding["name"], kind: binding[kind]})
                if selector_key in seen_selectors:
                    raise ReleaseError("Duplicate dependency selector binding.")
                seen_selectors.add(selector_key)
                identity = binding["name"], binding["selected_version"]
                child = by_identity.get(identity)
                if child is None or child["raw_sha256"] != binding["selected_raw_sha256"]:
                    raise ReleaseError("Dependency selector hash does not match its concrete child.")
                selected.add(identity)
            direct = {(child["name"], child["version"]) for child in graph["dependencies"].get(node_id, [])}
            if direct != selected:
                raise ReleaseError("Dependency lock selectors do not cover the graph.")
        root_node = concrete[graph["root"]["id"]]
        if any(root_node[key] != root[key] for key in ("raw_sha256", "resolved_sha256")):
            raise ReleaseError("Dependency lock root hashes differ from its concrete root.")
    return lock


def check_dependency_lock(client, lock, snapshot, stage):
    """Fresh GETs validate the complete lock before any release mutation."""
    validate_lock_integrity(lock)
    fresh = build_dependency_lock(client, snapshot, stage, consumer=lock["consumer"])
    for field in ("host", "snapshot_sha256", "stage_report_sha256", "consumer", "roots"):
        if fresh[field] != lock.get(field):
            raise ReleaseError("Dependency lock no longer matches the release or resolved dependency closure.")
    return lock


def capture_dependency_lock(client, manifest_path, stage_report_path, *, consumer, report_name=None):
    manifest, snapshot = load_snapshot(manifest_path)
    _, stage = _read_object(stage_report_path, "stage report")
    lock = build_dependency_lock(client, snapshot, stage, consumer=consumer)
    name = report_name or f"dependencies-{uuid.uuid4().hex}.json"
    if not name.endswith(".json"):
        raise ReleaseError("Dependency report filename must end in .json.")
    path = inside(manifest.parent / "reports", name)
    write_report(path, lock, create=True)
    return lock


def validate_dependency_lock(client, lock_path, snapshot, stage):
    _, lock = _read_object(lock_path, "dependency lock")
    return check_dependency_lock(client, lock, snapshot, stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage-report", required=True)
    parser.add_argument("--sdk", help="Exact consuming SDK/version identity")
    parser.add_argument("--compiler", help="Exact application compiler/revision identity")
    parser.add_argument("--report")
    parser.add_argument("--check", help="Existing lock to check instead of capture")
    args = parser.parse_args()
    try:
        client = DependencyClient(args.host)
        if args.check:
            _, snapshot = load_snapshot(args.manifest)
            _, stage = _read_object(args.stage_report, "stage report")
            validate_dependency_lock(client, args.check, snapshot, stage)
            print("Dependency lock verified against fresh raw selectors and server resolution.")
        else:
            if not args.sdk or not args.compiler:
                raise ReleaseError("Capture requires --sdk and --compiler.")
            lock = capture_dependency_lock(client, args.manifest, args.stage_report,
                                           consumer={"sdk": args.sdk, "compiler": args.compiler}, report_name=args.report)
            print(json.dumps({"status": lock["status"], "roots": len(lock["roots"])}))
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Dependency check failed: {exc}\n")


if __name__ == "__main__":
    main()
