"""JSON bridge for JS capture using the release engine's existing validators.

No shell commands are accepted or executed. Keeping hashing in Python avoids
cross-language float/exponent serialization changing evidence identities.
"""
import argparse
import copy
import json
import sys
from datetime import datetime, timezone

from import_langfuse_experiment import start_capture, capture_prompt, finalize_capture
from langfuse_release_adoption import capture_js_observation
from langfuse_release_common import ReleaseError, fingerprint, validate_prompt
from langfuse_release_policy import timestamp
from langfuse_release_results import project_javascript_result


MAX_MESSAGE_BYTES = 8_000_000


def _decode(raw):
    if len(raw) > MAX_MESSAGE_BYTES:
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
    return request


def _encode(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ReleaseError("Capture response exceeds the 8 MB bound.")
    return raw


def dispatch(request):
    if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
        raise ReleaseError("Capture bridge requires operation and arguments.")
    arguments = request["arguments"]
    if not isinstance(arguments, dict):
        raise ReleaseError("Capture arguments must be an object.")
    operations = {"start": start_capture, "finish": finalize_capture,
                  "adoption": capture_js_observation, "hash": fingerprint,
                  "project-result": project_javascript_result}
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


class CollectorSession:
    """Single-process, serial capture session; no files or network listener.

    Each successful response is a validation acknowledgment, not a durable
    receipt. A failed request taints the session and prevents finalization.
    All state changes are committed only after bounded response serialization.
    """

    def __init__(self):
        self.state = None
        self.last_id = 0
        self.accepted = 0
        self.rejected = 0

    def respond(self, request):
        if not isinstance(request, dict) or set(request) != {"id", "submitted_at_utc", "operation", "arguments"}:
            raise ReleaseError("Invalid collector frame.")
        request_id = request["id"]
        if type(request_id) is not int or request_id != self.last_id + 1:
            raise ReleaseError("Collector request IDs must be contiguous and ordered.")
        timestamp(request["submitted_at_utc"], "collector source capture time")
        self.last_id = request_id
        response = {"id": request_id, "submitted_at_utc": request["submitted_at_utc"],
                    "processed_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            operation, arguments = request["operation"], request["arguments"]
            if not isinstance(arguments, dict):
                raise ReleaseError("Collector arguments must be an object.")
            candidate = None
            if operation == "barrier":
                if arguments:
                    raise ReleaseError("Collector barrier has no arguments.")
                result = {"accepted": self.accepted, "rejected": self.rejected,
                          "complete": self.rejected == 0}
            else:
                if operation in ("prompt", "finish"):
                    if self.state is None or "capture" in arguments:
                        raise ReleaseError("Start the collector capture before this operation.")
                    if operation == "finish" and self.rejected:
                        raise ReleaseError("Rejected collector requests prevent finalization.")
                    if operation == "finish":
                        expected = {(case, target["name"]) for target in self.state["targets"] for case in target["case_ids"]}
                        observed = {(item["id"], prompt["requested_name"]) for item in self.state["items"] for prompt in item["prompts"]}
                        if expected != observed:
                            raise ReleaseError("Collector is missing required case/prompt captures.")
                    arguments = {**arguments, "capture": copy.deepcopy(self.state)}
                elif operation == "start" and self.state is not None:
                    raise ReleaseError("Collector capture already started.")
                result = dispatch({"operation": operation, "arguments": arguments})
                if operation in ("start", "prompt", "finish"):
                    candidate = result
            response.update(ok=True, result=result)
            raw = _encode(response)
            if candidate is not None:
                self.state = candidate
            self.accepted += 1
            return raw
        except (ReleaseError, OSError, ValueError, TypeError, RecursionError):
            self.rejected += 1
            # Do not copy prompt/output data from arbitrary exception messages.
            response.pop("result", None)
            response.update(ok=False, error={"code": "CAPTURE_REJECTED", "message": "Capture operation rejected."})
            return _encode(response)


def serve(input_stream, output_stream):
    session = CollectorSession()
    while True:
        raw = input_stream.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            return
        if not raw.endswith(b"\n"):
            raise ReleaseError("Collector frame is oversized or incomplete.")
        response = session.respond(_decode(raw))
        output_stream.write(response)
        output_stream.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Opt-in bounded JSON-lines collector over stdin/stdout")
    args = parser.parse_args()
    try:
        if args.serve:
            serve(sys.stdin.buffer, sys.stdout.buffer)
        else:
            result = dispatch(_decode(sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)))
            sys.stdout.buffer.write(_encode(result))
    except (ReleaseError, OSError, ValueError, TypeError, RecursionError) as exc:
        parser.exit(1, f"Capture rejected: {exc}\n")


if __name__ == "__main__":
    main()
