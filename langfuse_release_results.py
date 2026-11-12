"""Project official SDK experiment results into the supported Action v1 subset.

No evaluator runs here. IDs and scores come from the supplied result; a
completed script is not a passing release or proof of remote persistence.
The Python SDK is optional: only hosted DatasetItem serialization imports it.
"""
from __future__ import annotations

import copy
import json
from pathlib import PurePosixPath

from import_langfuse_experiment import _action_result
from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_policy import text


MAX_RESULT_BYTES = 8_000_000
_MISSING = object()


def _field(value, name, default=_MISSING):
    result = value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)
    if result is _MISSING:
        raise ReleaseError(f"SDK result is missing required field {name}.")
    return result


def _array(value, where):
    if not isinstance(value, list) or len(value) > 100_000:
        raise ReleaseError(f"{where} must be an array of at most 100000 entries.")
    return value


def _score(row, *, javascript):
    value = _field(row, "value")
    data_type = _field(row, "dataType" if javascript else "data_type", None)
    if data_type not in (None, "NUMERIC", "BOOLEAN", "CATEGORICAL"):
        raise ReleaseError("Unsupported SDK score data type.")
    if data_type == "BOOLEAN":
        if javascript:
            if type(value) not in (int, float) or value not in (0, 1):
                raise ReleaseError("JavaScript BOOLEAN scores must contain numeric 0 or 1.")
            value = value == 1
        elif type(value) is not bool:
            raise ReleaseError("Python BOOLEAN scores must contain booleans.")
    elif data_type == "CATEGORICAL":
        if not isinstance(value, str):
            raise ReleaseError("CATEGORICAL scores must contain strings.")
    elif type(value) not in (int, float):
        raise ReleaseError("NUMERIC or untyped SDK scores must contain numbers.")
    score = {"name": _field(row, "name"), "value": value}
    for field, source in (("data_type", "dataType"), ("config_id", "configId"),
                          ("comment", "comment"), ("metadata", "metadata")):
        optional = _field(row, source if javascript else field, None)
        if optional is not None:
            score[field] = copy.deepcopy(optional)
    return score


def _item(value, *, javascript):
    sdk_model = not isinstance(value, dict)
    if not isinstance(value, dict):
        # The SDK's hosted item is a generated Pydantic model. Use its JSON
        # serializer for timestamps; do not stringify arbitrary task objects.
        try:
            from langfuse.api.commons.types.dataset_item import DatasetItem
        except ImportError as exc:
            raise ReleaseError("Hosted Python items require the optional Langfuse SDK.") from exc
        if not isinstance(value, DatasetItem):
            raise ReleaseError("SDK item must be a JSON object or an official DatasetItem.")
        value = value.model_dump(mode="json", by_alias=False)
    value = copy.deepcopy(value)
    # Fern's SDK serializer emits its wire alias even with by_alias=False in
    # Python 5.2.0. Normalize only the input contract's expected-output field;
    # retain the remaining hosted-item metadata exactly as serialized.
    if (javascript or sdk_model) and "expectedOutput" in value:
        if "expected_output" in value:
            raise ReleaseError("SDK item contains ambiguous expected-output fields.")
        value["expected_output"] = value.pop("expectedOutput")
    text(value.get("id"), "actual SDK case id")
    return value


def _project(result, *, script_path, duration_ms, status, error, metadata, javascript):
    text(script_path, "script_path")
    key = lambda snake, camel: camel if javascript else snake
    rows = []
    seen = set()
    for row in _array(_field(result, key("item_results", "itemResults")), "SDK item results"):
        item = _item(_field(row, "item"), javascript=javascript)
        if item["id"] in seen:
            raise ReleaseError("SDK result contains duplicate case IDs.")
        seen.add(item["id"])
        projected = {"item": item, "output": copy.deepcopy(_field(row, "output")),
                     "evaluations": [_score(score, javascript=javascript) for score in
                                     _array(_field(row, "evaluations"), "SDK evaluations")]}
        # Python item results retain inputs in item; JS additionally exposes
        # top-level inputs. Preserve either layout and let the common importer
        # detect a contradictory duplicate value.
        for field, camel in (("input", "input"), ("expected_output", "expectedOutput")):
            source = key(field, camel)
            value = row.get(source, _MISSING) if isinstance(row, dict) else getattr(row, source, _MISSING)
            if value is not _MISSING:
                projected[field] = copy.deepcopy(value)
        for field, camel in (("trace_id", "traceId"), ("dataset_run_id", "datasetRunId")):
            value = _field(row, key(field, camel), None)
            if value is not None:
                projected[field] = text(value, field)
        rows.append(projected)
    experiment = {"experiment_id": text(_field(result, key("experiment_id", "experimentId")), "actual experiment ID"),
                  "run_name": text(_field(result, key("run_name", "runName")), "actual run name"),
                  "item_results": rows,
                  "run_evaluations": [_score(score, javascript=javascript) for score in
                                      _array(_field(result, key("run_evaluations", "runEvaluations")), "SDK run evaluations")]}
    dataset_run_id = _field(result, key("dataset_run_id", "datasetRunId"), None)
    if dataset_run_id is not None:
        experiment["dataset_run_id"] = text(dataset_run_id, "actual dataset run ID")
    runtime = "node" if javascript else "python"
    producer = "langfuse-javascript-sdk" if javascript else "langfuse-python-sdk"
    artifact = {"schema_version": "v1", "action_metadata": {"producer": producer, **(metadata or {})},
                "results": [{"script_path": script_path, "script_name": PurePosixPath(script_path).name,
                             "runtime": runtime, "duration_ms": duration_ms, "status": status,
                             "error": copy.deepcopy(error),
                             "langfuse_experiment_url": _field(result, key("experiment_url", "experimentUrl"), None),
                             "experiment_result": experiment}]}
    # One shared validator owns the evidence envelope; the pinned release policy
    # is still checked independently when the application imports this result.
    _action_result(artifact, {"producer": {"kind": producer, "script_path": script_path}})
    fingerprint(artifact)
    if len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ReleaseError("Projected result exceeds the 8 MB bound.")
    return copy.deepcopy(artifact)


def project_python_result(result, *, script_path, duration_ms, status="passed", error=None, metadata=None):
    """Return detached Action v1 JSON from a Python SDK ExperimentResult.

    ``status`` describes script completion, not evaluator success. Preserve the
    actual result and separately import it under the pinned release policy.
    """
    return _project(result, script_path=script_path, duration_ms=duration_ms,
                    status=status, error=error, metadata=metadata, javascript=False)


def project_javascript_result(result, *, script_path, duration_ms, status="passed", error=None, metadata=None):
    """Bridge entry point for the JSON fields of a JS SDK ExperimentResult."""
    if not isinstance(result, dict):
        raise ReleaseError("JavaScript SDK result must be a JSON object.")
    return _project(result, script_path=script_path, duration_ms=duration_ms,
                    status=status, error=error, metadata=metadata, javascript=True)
