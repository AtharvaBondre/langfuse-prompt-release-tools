#!/usr/bin/env python3
"""Preview a prompt release, then explicitly apply with pinned read-back checks."""
import argparse
import difflib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from langfuse_release_common import (Client, ReleaseError, inside, validate_prompt,
                                     contract_signature, fingerprint, make_payload, verify_exact)


def load_snapshot(manifest_path):
    source = Path(manifest_path)
    if source.is_symlink() or source.parent.is_symlink():
        raise ReleaseError("Snapshot file and directory must not be symlinks.")
    path = Path(manifest_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise ReleaseError("Expected a format_version=1 snapshot from the bundled exporter.")
    selector = data.get("selector")
    if not isinstance(selector, dict) or (selector.get("label") is None) == (selector.get("version") is None):
        raise ReleaseError("Snapshot must record exactly one label or version selector.")
    entries = data.get("prompts")
    if not isinstance(entries, list) or not entries:
        raise ReleaseError("Snapshot has no prompts.")
    seen_names, seen_paths = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ReleaseError("Invalid snapshot prompt entry.")
        name = entry["name"]
        baseline = validate_prompt(entry.get("baseline"), expected_name=name)
        local_path = inside(path.parent, entry.get("file"))
        key = str(local_path).casefold()
        if name in seen_names or key in seen_paths or local_path == path:
            raise ReleaseError("Snapshot contains duplicate names or colliding file paths.")
        seen_names.add(name)
        seen_paths.add(key)
        if baseline["type"] == "text":
            content = local_path.read_text(encoding="utf-8")
        else:
            content = json.loads(local_path.read_text(encoding="utf-8"))
        validate_prompt({**baseline, "prompt": content}, expected_name=name)
        entry["local_content"] = content
    return path, data


def observed_state(prompt):
    return {"name": prompt["name"], "version": prompt["version"], "type": prompt["type"],
            "prompt": prompt["prompt"], "config": prompt["config"],
            "labels": sorted(prompt["labels"]), "tags": sorted(prompt["tags"])}


def assert_unchanged(current, baseline):
    if fingerprint(observed_state(current)) != fingerprint(observed_state(baseline)):
        raise ReleaseError("Remote version/content/metadata changed since export; pull a new snapshot and review again.")


def preview_diff(before, after):
    def lines(content):
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
        return text.splitlines(keepends=True)
    diff = difflib.unified_diff(lines(before), lines(after),
                                fromfile="exported baseline", tofile="local edit")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in diff)


def write_report(path, report, *, create=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if create:
        # Reserve new evidence exclusively; a later preview must not erase a release.
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except FileExistsError:
            raise ReleaseError("Report already exists; choose a new --report filename.") from None
        return
    fd, temp_name = tempfile.mkstemp(prefix=".release-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def release(client, manifest_path, *, apply=False, names=None, canary=None,
            allow_contract_change=False, commit_message="Reviewed prompt update",
            report_name=None, stage=True, allow_direct_release=False):
    for field, value in (("apply", apply), ("stage", stage),
                         ("allow_direct_release", allow_direct_release),
                         ("allow_contract_change", allow_contract_change)):
        if type(value) is not bool:
            raise ReleaseError(f"{field} must be a boolean.")
    if stage and allow_direct_release:
        raise ReleaseError("Staging and allow_direct_release are contradictory; choose one release strategy.")
    if not stage and not allow_direct_release:
        raise ReleaseError("Direct publication moves deployment labels; explicitly set allow_direct_release=True.")
    manifest_path, snapshot = load_snapshot(manifest_path)
    root = manifest_path.parent
    if snapshot.get("host") != client.host:
        raise ReleaseError("Snapshot host does not match the requested target.")
    label = snapshot["selector"].get("label")
    if not isinstance(label, str) or not label:
        raise ReleaseError("Publishing requires a label-based snapshot; immutable-version exports are review-only.")
    if not isinstance(commit_message, str) or len(commit_message) > 500:
        raise ReleaseError("Commit message must be at most 500 characters.")
    entries = snapshot["prompts"]
    selected = set(names or [entry["name"] for entry in entries])
    if not selected or selected - {entry["name"] for entry in entries}:
        raise ReleaseError("Selected prompt names must exist in the snapshot.")
    entries = [entry for entry in entries if entry["name"] in selected]
    if canary:
        if canary not in selected:
            raise ReleaseError("Canary must be among the selected names.")
        entries.sort(key=lambda entry: entry["name"] != canary)
    if report_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        report_name = f"{'apply' if apply else 'preview'}-{stamp}-{uuid.uuid4().hex[:8]}.json"
    report_path = inside(root / "reports", report_name)
    if report_path.suffix.lower() != ".json":
        raise ReleaseError("Report filename must end in .json.")
    if any(report_path == inside(root, entry["file"]) for entry in snapshot["prompts"]):
        raise ReleaseError("Report would overwrite prompt content.")
    report = {"format_version": 2, "host": client.host,
              "mode": ("stage" if stage else "apply") if apply else "dry_run",
              "release_strategy": "stage" if stage else "direct",
              "allow_direct_release": allow_direct_release,
              "snapshot_sha256": fingerprint(snapshot),
              "status": "preflight", "report_file": report_name, "results": []}
    # Validate the report destination before any write to Langfuse.
    write_report(report_path, report, create=True)
    phase, pending = "preflight", None
    try:
        plans = []
        contract_approval_needed = False
        for entry in entries:
            baseline, content = entry["baseline"], entry["local_content"]
            assert_unchanged(client.get(entry["name"], label=label), baseline)
            changed = content != baseline["prompt"]
            contract_changed = contract_signature(content) != contract_signature(baseline["prompt"])
            if changed and contract_changed and not allow_contract_change:
                contract_approval_needed = True
            payload = make_payload(baseline, content, commit_message)
            if stage:
                # Creation still advances server-managed latest; deployment labels stay put.
                payload["labels"] = []
            result = {"name": entry["name"], "baseline_version": baseline["version"],
                      "changed": changed, "contract_changed": contract_changed,
                      "diff": preview_diff(baseline["prompt"], content),
                      "labels_to_move": payload["labels"] if changed else [],
                      "latest_assigned_automatically": changed,
                      "tags_preserved": payload["tags"],
                      "config_normalized_from_null": changed and baseline["config"] is None,
                      "status": "planned" if changed else "unchanged"}
            report["results"].append(result)
            plans.append((entry, payload, result))
        if contract_approval_needed:
            raise ReleaseError("Template variables, placeholders or OUTPUT_SCHEMA changed; review the saved diffs before using --allow-contract-change.")
        report["status"] = "preflight_passed"
        write_report(report_path, report)
        if not apply:
            report["status"] = "dry_run_complete"
            write_report(report_path, report)
            return report
        for entry, payload, result in plans:
            if not result["changed"]:
                continue
            pending = result
            phase = "recheck"
            # Best-effort optimistic check, not an atomic compare-and-swap.
            assert_unchanged(client.get(entry["name"], label=label), entry["baseline"])
            phase = "write"
            result["status"] = "submitting"
            write_report(report_path, report)
            created = client.create(payload)
            result["created_version"] = created["version"]
            phase = "readback"
            result["status"] = "created_pending_verification"
            write_report(report_path, report)
            if created["version"] <= entry["baseline"]["version"]:
                raise ReleaseError("Create did not return a newer version; inspect remote versions before proceeding.")
            actual = client.get(entry["name"], version=created["version"])
            verify_exact(actual, payload, created["version"])
            result["status"] = "verified"
            write_report(report_path, report)
            # The next item is not submitted until this version passes read-back.
            pending = None
        report["status"] = "verified"
        write_report(report_path, report)
        return report
    except (ReleaseError, OSError, ValueError) as exc:
        report["status"] = "stopped"
        report["phase"] = phase
        report["error"] = str(exc)
        if pending is not None:
            if phase == "write" and getattr(exc, "unknown_write", False):
                pending["status"] = "write_outcome_unknown_do_not_retry"
            elif phase == "readback":
                pending["status"] = "created_but_verification_failed"
            else:
                pending["status"] = "stopped"
        write_report(report_path, report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--name", action="append")
    parser.add_argument("--canary", help="Process this selected prompt first; each version is verified before continuing.")
    parser.add_argument("--apply", action="store_true")
    strategy = parser.add_mutually_exclusive_group()
    strategy.add_argument("--stage", action="store_true", help="Create without deployment labels (the default); latest still advances. Combine with --apply to write.")
    strategy.add_argument("--allow-direct-release", action="store_true", help="Explicitly allow legacy direct creation that moves all baseline deployment labels without evaluation.")
    parser.add_argument("--allow-contract-change", action="store_true")
    parser.add_argument("--commit-message", default="Reviewed prompt update")
    parser.add_argument("--report", help="New filename within the snapshot's reports directory; defaults to a unique name.")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        report = release(Client(args.host, args.timeout), args.manifest, apply=args.apply,
                         names=args.name, canary=args.canary, allow_contract_change=args.allow_contract_change,
                         commit_message=args.commit_message, report_name=args.report,
                         stage=not args.allow_direct_release, allow_direct_release=args.allow_direct_release)
        print(f"Release status: {report['status']}")
        print(f"Report: {Path(args.manifest).resolve().parent / 'reports' / report['report_file']}")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Release stopped: {exc}\n")


if __name__ == "__main__":
    main()
