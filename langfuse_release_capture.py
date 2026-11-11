"""JSON bridge for JS capture using the release engine's existing validators.

No shell commands are accepted or executed. Keeping hashing in Python avoids
cross-language float/exponent serialization changing evidence identities.
"""
import argparse
import json
import sys

from import_langfuse_experiment import start_capture, capture_prompt, finalize_capture
from langfuse_release_adoption import capture_js_observation
from langfuse_release_common import ReleaseError, fingerprint, validate_prompt


def dispatch(request):
    if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
        raise ReleaseError("Capture bridge requires operation and arguments.")
    arguments = request["arguments"]
    if not isinstance(arguments, dict):
        raise ReleaseError("Capture arguments must be an object.")
    operations = {"start": start_capture, "finish": finalize_capture,
                  "adoption": capture_js_observation, "hash": fingerprint}
    if request["operation"] == "prompt":
        prompt = arguments.get("prompt")
        if (not isinstance(prompt, dict) or set(prompt) != {"name", "version", "type", "prompt", "config", "is_fallback"} or
                type(prompt.get("is_fallback")) is not bool or prompt["is_fallback"]):
            raise ReleaseError("JavaScript task capture requires a complete non-fallback SDK snapshot.")
        validate_prompt({**prompt, "labels": [], "tags": []},
                        expected_name=arguments.get("requested_name"), expected_version=arguments.get("requested_version"))
        capture_prompt(**arguments)
        return arguments["capture"]
    function = operations.get(request["operation"])
    if function is None:
        raise ReleaseError("Unsupported capture bridge operation.")
    return function(**arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ReleaseError("Capture message exceeds the 8 MB bound.")

        def pairs(rows):
            result = {}
            for key, value in rows:
                if key in result:
                    raise ReleaseError("Duplicate capture JSON key.")
                result[key] = value
            return result

        request = json.loads(raw, object_pairs_hook=pairs)
        fingerprint(request)
        result = dispatch(request)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
    except (ReleaseError, OSError, ValueError, TypeError, RecursionError) as exc:
        parser.exit(1, f"Capture rejected: {exc}\n")


if __name__ == "__main__":
    main()
