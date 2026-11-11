#!/usr/bin/env python3
"""Read back exact published versions from a release report and compare content/metadata."""
import argparse
import json
import re
from pathlib import Path
from langfuse_release_common import Client, ReleaseError, fingerprint, make_payload, verify_exact
from publish_langfuse_prompts import load_snapshot


def verify(client, manifest_path, report_path):
    _, snapshot = load_snapshot(manifest_path)
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("format_version") != 2:
        raise ReleaseError("Expected a format_version=2 release report object; use v01 helpers for old reports.")
    if snapshot.get("host") != client.host or report.get("host") != client.host:
        raise ReleaseError("Snapshot/report host does not match the requested target.")
    if report.get("mode") not in {"apply", "stage", "promote"} or report.get("status") != "verified":
        raise ReleaseError("Verification requires a completed apply/stage/promotion report; inspect partial/uncertain writes manually.")
    promotion = report["mode"] == "promote"
    label = report.get("label")
    if promotion and (report.get("apply") is not True or not isinstance(label, str) or
                      not re.fullmatch(r"[a-z0-9_.-]{1,36}", label) or label == "latest"):
        raise ReleaseError("Promotion report must record an applied, explicit non-latest label.")
    if report.get("snapshot_sha256") != fingerprint(snapshot):
        raise ReleaseError("Snapshot or local edits changed since release; preserve the reviewed files.")
    entries = {entry["name"]: entry for entry in snapshot["prompts"]}
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise ReleaseError("Release report contains no results.")
    seen = set()
    for result in results:
        if not isinstance(result, dict):
            raise ReleaseError("Release report entries must be objects.")
        name = result.get("name")
        if not isinstance(name, str) or name not in entries or name in seen:
            raise ReleaseError("Report name is absent from snapshot or duplicated.")
        seen.add(name)
        valid_statuses = {"verified", "already_promoted"} if promotion else {"verified", "unchanged"}
        if result.get("status") not in valid_statuses:
            raise ReleaseError("Report contains an unverified item.")
        entry = entries[name]
        if result.get("baseline_version") != entry["baseline"]["version"]:
            raise ReleaseError("Report baseline version does not match the snapshot.")
        version = (result.get("target_version") if promotion else
                   result.get("created_version") if result["status"] == "verified" else result.get("baseline_version"))
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ReleaseError("Report is missing a valid exact version.")
        if result["status"] != "unchanged" and version <= entry["baseline"]["version"]:
            raise ReleaseError("Created/promoted version must be newer than its baseline.")
        if promotion and label not in entry["baseline"]["labels"]:
            raise ReleaseError("Promotion label was not carried by the original baseline.")
        actual = client.get(name, version=version)
        if result["status"] == "unchanged":
            # No POST occurred, so do not apply creation-only null-config normalization.
            expected = {**entry["baseline"], "prompt": entry["local_content"]}
        else:
            expected = make_payload(entry["baseline"], entry["local_content"], None)
            if report["mode"] == "stage":
                expected["labels"] = []
            elif promotion:
                expected["labels"] = [label]
        verify_exact(actual, expected, version)
        if promotion:
            verify_exact(client.get(name, label=label), expected, version)
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        count = verify(Client(args.host, args.timeout), args.manifest, args.report)
        print(f"Verified {count} selected exact prompt version(s).")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Verification stopped: {exc}\n")


if __name__ == "__main__":
    main()
