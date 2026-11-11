"""Check already-captured, stage-bound responses without model or API calls.

Run from the source checkout with ``python -m examples.check_captured_outputs``.
Captured identities are trusted input, not authenticated proof of execution.
"""
import argparse
import json
import os
from pathlib import Path

from evaluate_langfuse_release import _stage_targets
from langfuse_release_common import ReleaseError, fingerprint
from publish_langfuse_prompts import load_snapshot


REQUIRED_CHECKS = ("response_json", "nonempty_answer")


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object member.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number.")


def _parse_json(text):
    value = json.loads(text, object_pairs_hook=_unique_members,
                       parse_constant=_reject_constant)
    fingerprint(value)
    return value


def _read_object(path, description):
    path = Path(path)
    if path.is_symlink():
        raise ReleaseError(f"{description} must not be a symlink.")
    try:
        value = _parse_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError, ReleaseError):
        raise ReleaseError(f"Cannot read {description} as valid JSON.") from None
    if not isinstance(value, dict):
        raise ReleaseError(f"{description} must be a JSON object.")
    return value


def _checks(output):
    try:
        parsed = _parse_json(output)
        response_json = True
    except (ValueError, RecursionError, ReleaseError):
        parsed, response_json = None, False
    nonempty_answer = (isinstance(parsed, dict) and
                       isinstance(parsed.get("answer"), str) and
                       bool(parsed["answer"].strip()))
    return [
        {"id": "response_json", "passed": response_json,
         "detail": "Requires JSON with finite numbers and unique object members."},
        {"id": "nonempty_answer", "passed": nonempty_answer,
         "detail": "Requires a JSON object with a nonempty answer string."},
    ]


def check_captured_outputs(manifest_path, stage_report_path, captured_path, output_path):
    """Validate every existing binding, compute checks, then create one new file."""
    _, snapshot = load_snapshot(manifest_path)
    stage = _read_object(stage_report_path, "stage report")
    targets = _stage_targets(snapshot, stage)
    captured = _read_object(captured_path, "captured outputs")
    if type(captured.get("format_version")) is not int or captured["format_version"] != 1:
        raise ReleaseError("Captured outputs require format_version=1.")
    if (captured.get("snapshot_sha256") != fingerprint(snapshot) or
            captured.get("stage_report_sha256") != fingerprint(stage)):
        raise ReleaseError("Captured outputs belong to a different snapshot or stage report.")
    rows = captured.get("outputs")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("Captured outputs require a nonempty outputs list.")
    by_name = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseError("Each captured output must be an object.")
        name = row.get("name")
        if not isinstance(name, str) or name not in targets or name in by_name:
            raise ReleaseError("Captured names must uniquely match the changed staged targets.")
        if type(row.get("version")) is not int or row["version"] != targets[name]:
            raise ReleaseError("Captured output version differs from the staged target.")
        if not isinstance(row.get("output"), str):
            raise ReleaseError("Each captured output must contain its original response as a string.")
        by_name[name] = row
    if set(by_name) != set(targets):
        raise ReleaseError("Captured outputs are missing one or more changed staged targets.")

    result = {
        "format_version": 1,
        # Retain the supplied bindings only after validating them; never rebind outputs.
        "snapshot_sha256": captured["snapshot_sha256"],
        "stage_report_sha256": captured["stage_report_sha256"],
        "captured_source_sha256": fingerprint(captured),
        "results": [{"name": name, "version": by_name[name]["version"],
                     "checks": _checks(by_name[name]["output"])} for name in sorted(by_name)],
    }
    path = Path(output_path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ReleaseError("Output file and parent must not be symlinks.")
    if not path.parent.is_dir():
        raise ReleaseError("Output parent must already exist; use a private working directory.")
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ReleaseError("Output already exists; choose a new filename.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage-report", required=True)
    parser.add_argument("--captured", required=True, help="Already-bound captured response strings JSON.")
    parser.add_argument("--out", required=True, help="New private app-results JSON file; never overwritten.")
    args = parser.parse_args(argv)
    try:
        result = check_captured_outputs(args.manifest, args.stage_report, args.captured, args.out)
    except (ReleaseError, OSError, ValueError, RecursionError) as exc:
        parser.exit(2, f"Captured-output check stopped: {exc}\n")
    passed = all(check["passed"] for row in result["results"] for check in row["checks"])
    print(f"Captured-output checks: {'passed' if passed else 'failed'}")
    print(f"Results: {Path(args.out).resolve()}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
