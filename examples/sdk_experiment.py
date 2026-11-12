"""Real Langfuse SDK experiment with task-time prompt capture; no model calls.

The runner in sdk_release_demo.py creates a synthetic release and calls this
module. Applications should replace the deterministic task and cases, then
review and pin their policy before staging. Never copy passing scores.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from langfuse_release_common import fingerprint
from langfuse_release_results import project_python_result
from publish_langfuse_prompts import write_report


CASES = [
    {"id": "one", "input": {"answer": "A pinned prompt version is immutable."},
     "expected_output": {"answer": "A pinned prompt version is immutable."}},
    {"id": "two", "input": {"answer": "A label can move between versions."},
     "expected_output": {"answer": "A label can move between versions."}},
]


def identities():
    """Derive identity from the actual local dataset and application source."""
    revision = fingerprint(Path(__file__).read_text(encoding="utf-8"))
    dataset = {"id": "local-release-demo", "name": "local-release-demo",
               "version": fingerprint(CASES)}
    execution = {"task_revision": revision, "evaluator_revision": revision,
                 "model": {"provider": "local", "name": "deterministic-json-renderer",
                           "config_sha256": fingerprint({})}}
    return dataset, execution


def policy_for(name):
    dataset, execution = identities()
    return {"format_version": 1, "id": "demo-answer-contract-v1",
            "producer": {"kind": "langfuse-python-sdk", "schema_version": "v1",
                         "script_path": "examples/sdk_experiment.py"},
            "dataset": {**dataset, "cases": [
                {"id": row["id"], "input_sha256": fingerprint(row["input"]),
                 "expected_output_sha256": fingerprint(row["expected_output"])}
                for row in CASES]},
            "execution": execution,
            "targets": [{"name": name, "case_ids": [row["id"] for row in CASES]}],
            "checks": [{"id": "answer-exact", "kind": "boolean", "operator": "eq",
                        "threshold": True}],
            # Local complete results gate this demo. flush() alone is not proof
            # that every remote trace/score has persisted.
            "remote_export": "optional"}


def run_experiment(sdk, manifest, stage_path, result_path, capture_path, *, dependency_lock_path):
    from langfuse import Evaluation, RunnerContext
    from import_langfuse_experiment import start_capture, capture_prompt, finalize_capture

    dataset, execution = identities()
    capture = start_capture(manifest, stage_path, dataset=dataset, execution=execution,
                            dependency_lock_path=dependency_lock_path)
    targets = capture["targets"]
    if len(targets) != 1:
        raise ValueError("This small example expects one changed prompt.")
    target = targets[0]

    def task(*, item, **kwargs):
        prompt = sdk.get_prompt(target["name"], version=target["version"], cache_ttl_seconds=0)
        capture_prompt(capture, item_id=item["id"], requested_name=target["name"],
                       requested_version=target["version"], prompt=prompt)
        # This is the application under test. It compiles the real fetched
        # candidate and parses it, without asking an LLM to generate anything.
        return json.loads(prompt.compile(**item["input"]))

    def answer_exact(*, output, expected_output, **kwargs):
        return Evaluation(name="answer-exact", value=output == expected_output, data_type="BOOLEAN")

    started = time.monotonic()
    result = RunnerContext(client=sdk).run_experiment(
        name="release-contract-demo", data=CASES, task=task,
        evaluators=[answer_exact], max_concurrency=1)

    artifact = project_python_result(result, script_path="examples/sdk_experiment.py",
                                     duration_ms=int((time.monotonic() - started) * 1000))
    write_report(Path(result_path), artifact, create=True)
    sdk.flush()
    final = finalize_capture(capture, Path(result_path), remote_export="unconfirmed")
    write_report(Path(capture_path), final, create=True)
    return artifact, final
