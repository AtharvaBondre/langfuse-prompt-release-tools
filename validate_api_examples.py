#!/usr/bin/env python3
"""Optional development check of synthetic examples against pinned OpenAPI.

Install requirements-review.txt separately. The prompt helpers need none of
these dependencies. This checker never calls a tenant API or reads credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

SCHEMA_REVISION = "e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3"
SCHEMA_URL = (
    "https://raw.githubusercontent.com/langfuse/langfuse/"
    + SCHEMA_REVISION + "/web/public/generated/api/openapi.yml"
)
SCHEMA_SHA256 = "301fc5d38a5d4bc2dc97f517e205ea2b0c145a993eec93f490e3a958408de6f6"
MAX_SCHEMA_BYTES = 5_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_schema(path: Path | None) -> bytes:
    if path is not None:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SCHEMA_BYTES + 1)
    else:
        # Fixed public source only; no user-supplied host or authorization.
        request = urllib.request.Request(
            SCHEMA_URL, headers={"User-Agent": "langfuse-prompt-example-check/2"}
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as stream:
            if stream.geturl() != SCHEMA_URL:
                raise ValueError("Unexpected schema source URL")
            raw = stream.read(MAX_SCHEMA_BYTES + 1)
    if len(raw) > MAX_SCHEMA_BYTES:
        raise ValueError("Schema exceeds the size limit")
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
        raise ValueError("Schema SHA-256 differs from the pinned official artifact")
    return raw


def local_references_only(value):
    if isinstance(value, dict):
        if "$ref" in value and not (
            isinstance(value["$ref"], str) and value["$ref"].startswith("#/")
        ):
            raise ValueError("External schema references are not permitted")
        for child in value.values():
            local_references_only(child)
    elif isinstance(value, list):
        for child in value:
            local_references_only(child)


def resolve_pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        raise ValueError("Each case needs a document-local schema_ref")
    node = document
    for part in pointer[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    if not isinstance(node, dict):
        raise ValueError("schema_ref must identify a schema object")


def check_cases(document, fixture, validator_class):
    if not isinstance(fixture, dict) or fixture.get("format_version") != 1:
        raise ValueError("Unsupported examples format")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Examples must contain at least one case")
    results, seen = [], set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Every example must be an object")
        name = case.get("id")
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError("Example ids must be unique non-empty strings")
        seen.add(name)
        if type(case.get("expected_valid")) is not bool or "instance" not in case:
            raise ValueError("Each example needs instance and boolean expected_valid")
        resolve_pointer(document, case.get("schema_ref"))
        # Keep the complete OpenAPI document as the reference root. Adding a
        # local entry-point $ref does not rewrite nullable, oneOf or allOf.
        validation_root = dict(document, **{"$ref": case["schema_ref"]})
        validator = validator_class(
            validation_root, format_checker=validator_class.FORMAT_CHECKER
        )
        errors = list(validator.iter_errors(case["instance"]))
        actual = not errors
        row = {
            "id": name,
            "schema_ref": case["schema_ref"],
            "expected_valid": case["expected_valid"],
            "actual_valid": actual,
            "passed": actual == case["expected_valid"],
            "note": case.get("note"),
            "errors": [
                {"path": list(error.path), "message": error.message}
                for error in errors[:3]
            ],
        }
        results.append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'} {name}: "
              f"{'accepted' if actual else 'rejected'}")
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path,
                        help="Use a local copy; the same pinned SHA-256 is required")
    parser.add_argument("--examples", type=Path,
                        default=Path(__file__).with_name("api-examples.json"))
    parser.add_argument("--report", type=Path,
                        help="Write optional JSON evidence to this explicit path")
    args = parser.parse_args(argv)
    try:
        import yaml
        from openapi_schema_validator import OAS30Validator
    except ImportError:
        print("Install the optional requirements-review.txt dependencies first.", file=sys.stderr)
        return 2
    try:
        raw = read_schema(args.schema)
        document = yaml.safe_load(raw)
        if not isinstance(document, dict) or document.get("openapi") != "3.0.1":
            raise ValueError("Expected the pinned OpenAPI 3.0.1 document")
        local_references_only(document)
        examples_raw = args.examples.read_bytes()
        fixture = json.loads(examples_raw)
        results = check_cases(document, fixture, OAS30Validator)
        passed = sum(row["passed"] for row in results)
        report = {
            "status": "pass" if passed == len(results) else "fail",
            "schema_revision": SCHEMA_REVISION,
            "schema_url": SCHEMA_URL,
            "schema_sha256": SCHEMA_SHA256,
            "schema_input": "explicit local file" if args.schema else "pinned public URL",
            "examples_sha256": hashlib.sha256(examples_raw).hexdigest(),
            "validator": {
                "class": "OAS30Validator",
                "openapi-schema-validator": importlib.metadata.version("openapi-schema-validator"),
                "jsonschema": importlib.metadata.version("jsonschema"),
                "PyYAML": yaml.__version__,
                "semantics": "Native OAS 3.0, no nullable conversion, full document reference root",
            },
            "passed": passed,
            "total": len(results),
            "results": results,
            "limits": fixture.get("limits", []),
            "tenant_api_calls": 0,
        }
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{passed}/{len(results)} expectations matched. "
              "Negative controls pass when rejected.")
        print("Schema checks do not test runtime labels, authorization, concurrency or model quality.")
        return 0 if passed == len(results) else 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
