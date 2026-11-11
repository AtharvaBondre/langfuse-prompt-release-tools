#!/usr/bin/env python3
"""Exercise release helpers against a disposable, loopback-only Langfuse project.

Creates synthetic prompts and retains them for inspection. No model calls.
The lost-response case injects a client exception after a real successful PATCH.
"""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from langfuse_release_common import Client, ReleaseError, fingerprint, inside
from pull_langfuse_prompts import export
from publish_langfuse_prompts import load_snapshot, release
from evaluate_langfuse_release import record_evaluation
from promote_langfuse_prompts import promote
from verify_langfuse_prompts import verify


class ObservedClient(Client):
    def __init__(self, host):
        super().__init__(host, timeout=30)
        self.calls = []
        self.lose_next_patch_response = False

    def _request(self, method, path, query=None, payload=None):
        # Record only operation identity; no headers, credentials or payloads.
        self.calls.append({"method": method, "path": path})
        return super()._request(method, path, query=query, payload=payload)

    def add_labels(self, name, version, labels):
        response = super().add_labels(name, version, labels)
        if self.lose_next_patch_response:
            self.lose_next_patch_response = False
            raise ReleaseError("Injected lost response after successful real PATCH", unknown_write=True)
        return response

    def writes(self, method):
        return sum(call["method"] == method for call in self.calls)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def report_path(manifest, result):
    return manifest.parent / "reports" / result["report_file"]


def edit(manifest, transform):
    for entry in json.loads(manifest.read_text())["prompts"]:
        path = inside(manifest.parent, entry["file"])
        baseline = entry["baseline"]
        content = transform(baseline["prompt"], baseline["type"])
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def add_instruction(content, kind):
    suffix = "\nReturn a JSON object with a non-empty answer string."
    if kind == "text":
        return content + suffix
    return [{**row, "content": row["content"] + suffix} if row.get("role") == "system" else row
            for row in content]


def application_checks(client, manifest, stage_path, output_fixture):
    """Run deterministic caller checks; fixture output does not come from a model."""
    stage = json.loads(stage_path.read_text())
    snapshot = load_snapshot(manifest)[1]
    results = []
    for row in stage["results"]:
        if not row["changed"]:
            continue
        actual = client.get(row["name"], version=row["created_version"])
        text = actual["prompt"] if actual["type"] == "text" else "\n".join(
            message.get("content", "") for message in actual["prompt"])
        # A small application's input/render check plus its output parser.
        rendered = text.replace("{{question}}", "What is a prompt version?")
        template_ok = not re.search(r"\{\{[^}]+\}\}", rendered)
        try:
            output = json.loads(output_fixture)
            output_ok = isinstance(output, dict) and isinstance(output.get("answer"), str) and bool(output["answer"].strip())
        except (ValueError, TypeError):
            output_ok = False
        results.append({"name": row["name"], "version": row["created_version"], "checks": [
            {"id": "render-input", "passed": template_ok,
             "detail": "Rendered the fetched exact version with the caller's synthetic question input."},
            {"id": "output-contract", "passed": output_ok,
             "detail": "Parsed a synthetic application-output fixture; no model was called."},
        ]})
    raw = {"format_version": 1, "snapshot_sha256": fingerprint(snapshot),
           "stage_report_sha256": fingerprint(stage), "results": results}
    target = manifest.parent / "application-results.json"
    save(target, raw)
    result = record_evaluation(manifest, stage_path, target,
                              required_checks=["render-input", "output-contract"], report_name="evaluation.json")
    return result, report_path(manifest, result)


def run(host, out_dir):
    url = urlsplit(host)
    if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ReleaseError("This demo accepts a loopback-only disposable project.")
    root = Path(out_dir)
    if root.is_symlink() or (root.exists() and any(root.iterdir())):
        raise ReleaseError("Choose a fresh empty output directory.")
    root.mkdir(parents=True, exist_ok=True)
    client = ObservedClient(host)
    prefix = "release-demo-" + uuid.uuid4().hex
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "fixture_prefix": prefix, "model_calls": 0, "checks": [],
              "fault_injection": "Client-side exception after a real successful PATCH; not an observed server failure"}

    def check(name, condition, **evidence):
        result["checks"].append({"name": name, "passed": bool(condition), **evidence})
        save(root / "integration-result.json", result)
        if not condition:
            raise ReleaseError("Integration assertion failed: " + name)

    def seed(suffix, kind="text"):
        name = prefix + "/" + suffix
        prompt = "Answer {{question}}."
        if kind == "chat":
            prompt = [{"role": "system", "content": "Answer the question."},
                      {"role": "user", "content": "{{question}}"}]
        created = client.create({"name": name, "type": kind, "prompt": prompt,
                                 "labels": ["production"], "tags": ["disposable-integration"], "config": {}})
        check("fresh_baseline_" + suffix, created["version"] == 1, version=created["version"])
        return name

    try:
        bad_name = seed("bad-output")
        bad_manifest = export(client, root / "bad-candidate", label="production", names=[bad_name])
        edit(bad_manifest, add_instruction)
        staged = release(client, bad_manifest, stage=True, apply=True, report_name="stage.json")
        bad_stage = report_path(bad_manifest, staged)
        evaluation, evidence_path = application_checks(client, bad_manifest, bad_stage, '{"answer":""}')
        check("bad_output_produces_failed_evaluation", evaluation["status"] == "failed")
        before = client.writes("PATCH")
        blocked = False
        try:
            promote(client, bad_manifest, bad_stage, label="production", apply=True,
                    require_evaluation=True, evaluation_report=evidence_path, report_name="blocked-promotion.json")
        except ReleaseError:
            blocked = True
        blocked_report = json.loads((bad_manifest.parent / "reports" / "blocked-promotion.json").read_text())
        check("bad_output_blocked_before_patch", blocked and client.writes("PATCH") == before and
              blocked_report.get("phase") == "evaluation" and
              blocked_report.get("evaluation", {}).get("status") == "blocked" and
              "failed checks" in blocked_report.get("error", ""))
        check("bad_candidate_did_not_move_production", client.get(bad_name, label="production")["version"] == 1)

        names = [seed("good-text"), seed("good-chat", "chat")]
        manifest = export(client, root / "good-candidates", label="production", names=names)
        edit(manifest, add_instruction)
        staged = release(client, manifest, stage=True, apply=True, report_name="stage.json")
        stage_path = report_path(manifest, staged)
        versions = {row["name"]: row["created_version"] for row in staged["results"]}
        check("stage_keeps_production_on_baseline", all(client.get(n, label="production")["version"] == 1 for n in names))
        check("stage_advances_latest", all(client.get(n, label="latest")["version"] == versions[n] for n in names))
        evaluation, evidence_path = application_checks(client, manifest, stage_path,
                                                       '{"answer":"A stored revision of the prompt."}')
        check("good_application_fixture_passed", evaluation["status"] == "passed")

        client.lose_next_patch_response = True
        before = client.writes("PATCH")
        uncertain = False
        try:
            promote(client, manifest, stage_path, label="production", apply=True,
                    require_evaluation=True, evaluation_report=evidence_path, report_name="lost-response.json")
        except ReleaseError as exc:
            uncertain = exc.unknown_write
        check("lost_response_stops_after_one_patch", uncertain and client.writes("PATCH") == before + 1)
        check("real_label_moved_before_injected_failure", client.get(names[0], label="production")["version"] == versions[names[0]])
        recovery = promote(client, manifest, stage_path, label="production", apply=True,
                           require_evaluation=True, evaluation_report=evidence_path, report_name="reconciled.json")
        check("recovery_skips_completed_patch", client.writes("PATCH") == before + len(names) and
              [row["status"] for row in recovery["results"]] == ["already_promoted", "verified"],
              statuses=[row["status"] for row in recovery["results"]])
        check("exact_versions_and_labels_verified", verify(client, manifest, report_path(manifest, recovery)) == len(names))
        before = client.writes("PATCH")
        repeat = promote(client, manifest, stage_path, label="production", apply=True,
                         require_evaluation=True, evaluation_report=evidence_path, report_name="repeat.json")
        check("repeat_promotion_has_zero_writes", client.writes("PATCH") == before and
              all(row["status"] == "already_promoted" for row in repeat["results"]))
        listed = client.list_names("production")
        check("list_finds_all_demo_prompts", all(name in listed for name in [bad_name, *names]))
        result["status"] = "passed"
    except (ReleaseError, OSError, ValueError) as exc:
        result["status"], result["error"] = "failed", str(exc)
        raise
    finally:
        result.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                      requests=client.calls, write_counts={method: client.writes(method) for method in ("POST", "PATCH")})
        save(root / "integration-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--confirm-disposable-project", action="store_true", required=True)
    args = parser.parse_args()
    try:
        result = run(args.host, args.out_dir)
        print(json.dumps({"status": result["status"], "checks": len(result["checks"]),
                          "write_counts": result["write_counts"], "model_calls": 0}, indent=2))
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, "Disposable integration stopped: " + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
