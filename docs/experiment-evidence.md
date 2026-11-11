# Experiment evidence and required checks

A release policy is reviewed **before staging and execution**. It specifies the
prompt targets, dataset cases, required evaluator names and thresholds. Importing
results cannot reduce these requirements. Promotion reads the policy from the
snapshot again and recomputes every gate from the preserved source artifacts.

This is a release-evidence adapter around Langfuse. Langfuse owns experiment
execution, datasets, scores and prompt resolution. The adapter does not run an
LLM, implement another evaluator, or infer runtime identity from an experiment
URL.

## Workflow

1. Export prompts and review the local edits.
2. Write the policy JSON and run
   `langfuse-release-policy --manifest export/manifest.json --policy policy.json`.
   This adds the full policy and its digest to the manifest. It is a one-time
   binding; changing it requires a fresh export. Staging must happen afterward.
3. Stage the edited prompts. Its snapshot digest now includes the policy.
4. Capture a dependency lock for those exact staged versions. The lock must exist
   before the experiment starts, including for roots with no dependencies.
5. Run the application's task through the official SDK or Experiment Action.
   Start the capture before execution, fetch each prompt by immutable version,
   and record the returned prompt immediately. Preserve actual outputs and
   per-item evaluator results.
6. Import the Action v1 result file and finalized capture:

   ```sh
   langfuse-release-import-experiment \
     --manifest export/manifest.json \
     --stage-report export/reports/stage.json \
     --results result.json --capture capture.json
   ```

7. Supply the resulting receipt to promotion along with the same dependency
   lock. Promotion revalidates evidence before each label PATCH. A later
   dependency change invalidates the lock; rerun the evaluation against a newly
   reviewed release instead of binding an old run to a new lock.

The equivalent Python entry points are `langfuse_release_policy.pin_policy` and
`import_langfuse_experiment.import_experiment`. Both return dictionaries. The
imported receipt lives at `manifest.parent / 'reports' / report['report_file']`.
Source result, capture and lock files must remain available; receipts contain
absolute local references and fingerprints. Copying a receipt alone is not a
portable evidence bundle.

## Policy v1

Required top-level fields are `format_version: 1`, a nonempty `id`, `producer`,
`dataset`, `execution`, `targets`, `checks` and `remote_export`.

| Field | Meaning |
| --- | --- |
| `producer.kind` | `langfuse-experiment-action` or `langfuse-python-sdk`; records what actually produced the evidence |
| `producer.schema_version` | `v1`, the official Experiment Action JSON format |
| `producer.script_path` | Exact script identity selected before the run |
| `dataset.id`, `.name`, `.version` | Actual dataset identity and immutable item-version selection; local examples must identify themselves as local |
| `dataset.schema_sha256` | Optional canonical fingerprint of the actual dataset schema; include when schemas are used, because a dataset item version does not freeze its schema |
| `dataset.cases` | Nonempty list of `{id, input_sha256, expected_output_sha256}`; hashes use the core canonical JSON `fingerprint`, including explicit `null` |
| `execution.task_revision`, `.evaluator_revision` | Revisions actually executed, independently selected before execution |
| `execution.model` | `{provider, name, config_sha256}` for the configuration actually used; deterministic local examples explicitly use no model |
| `targets` | Nonempty `{name, case_ids}` entries exactly covering changed staged prompts; every pinned case belongs to at least one target |
| `checks` | Nonempty `{id, kind, operator, threshold}` entries required for **every case**, where `id` is the official score name |
| `remote_export` | `required` or `optional`, independently selected before execution |

Boolean checks accept only `kind: boolean`, `operator: eq` and an actual boolean
threshold. Numeric checks accept finite numbers with `eq`, `gte` or `lte`.
Strings, booleans-as-numbers, missing values, duplicate IDs and non-finite numbers
are rejected. For example, `{"id":"valid_json","kind":"boolean","operator":"eq","threshold":true}`
and `{"id":"accuracy","kind":"numeric","operator":"gte","threshold":0.95}`
are separate required checks. Categorical scores need an explicitly designed
mapping in a future schema; this adapter does not guess what a string means.

All policy fields are validated. Unknown fields fail rather than silently
changing expectations. Dataset cases and required checks cannot be empty.

## Execution capture helpers

```python
capture = start_capture(
    manifest_path, stage_report_path,
    dataset=actual_dataset_identity,  # id/name/version; schema_sha256 when used
    execution=actual_execution_identity,
    dependency_lock_path=lock_path,
)

# Inside the task, for each required dataset case:
prompt = langfuse.get_prompt(prompt_name, version=staged_version)
capture_prompt(
    capture, item_id=item_id,
    requested_name=prompt_name, requested_version=staged_version,
    prompt=prompt,
)
# Compile and execute this returned prompt; preserve the actual result.

# After the official runner has completed and result.json has been written:
finished = finalize_capture(capture, result_path, remote_export="not_attempted")
write_report(capture_path, finished, create=True)
```

`start_capture` checks actual dataset/task/evaluator/model identities against the
already pinned policy. It derives target versions from the stage report, never
from a caller's label. `capture_prompt` accepts an SDK prompt object or dictionary
and checks its returned name/version. With a dependency lock, it also fingerprints
actual resolved content and config using the same canonicalization as dependency
validation. The importer requires each case/target observation and independently
checks its digest against the original lock.

`finalize_capture` returns a new dictionary and must be assigned as shown. It
records local completion and the canonical digest of the result file. A local
completion does not prove remote export. Use `confirmed` only after the producer
has established remote persistence; a URL or a queued flush alone is insufficient.
Use `unconfirmed` when export/flush was attempted but persistence has not been verified.
When export is optional, a local passing run with `failed` export stays visibly
`failed` in the receipt's separate `completion.remote_export` field. When export
is required, failed, unattempted and unconfirmed export all block import.

Capture helpers do not execute SDK calls. The producer must call them at the
indicated points using the actual objects and execution configuration. Supplying
policy metadata after an unrelated run is not runtime evidence.

## Supported result subset

The importer validates the official
[Experiment Action result_json v1 schema](https://github.com/langfuse/experiment-action/blob/c7c4efe432e37d4e3693a018de469233c0018de4/schemas/result-json.v1.schema.json)
without adding a runtime dependency. It supports exactly one pinned script per
receipt. Each `item_results[]` entry needs `item.id`, actual output, input and
expected output (the latter two may be on the result or its nested `item`). When
both locations are present, they must agree. Actual input/expected-output hashes
must match the policy.

Per-item `evaluations[].name` must exactly match pinned check IDs. Missing, extra
or duplicated cases/checks block import. Every changed staged prompt must have
all its required cases. Run-level evaluations are preserved as summaries; they
are not release gates and cannot substitute for per-item coverage. A script
regression produces a failed receipt even if item scores pass. Unknown schema
versions and incomplete/error-only artifacts cannot produce passing evidence.

An official SDK runner can serialize its real results into this format using
`producer.kind: langfuse-python-sdk`. Its string-valued `action_metadata` should
identify the actual adapter and SDK version. Using the envelope does not mean a
hosted GitHub Action ran.

## Trust and compatibility

This is trusted local release tooling. Fingerprints detect accidental drift and
bind artifacts; they are not signatures, trusted timestamps or protection
against a producer fabricating outputs. Review and control the producer, task,
evaluators and policy in CI. Never present synthetic fixture results as evidence
of a live model, tenant or production release.

Legacy format-1 application-check receipts remain supported for snapshots with
**no** pinned policy. Their required-check selection comes from the legacy
recorder and does not have the stronger pre-execution policy guarantee. Once a
snapshot contains `evaluation_policy`, the legacy recorder, format-1 receipt and
structural-only promotion option cannot satisfy that policy. Removing or changing
the policy invalidates the original stage fingerprint. The importer also refuses
format-2 experiment receipts without their independently pinned snapshot policy.

Standalone import allows an absent dependency lock for explicit older capture
compatibility; such a receipt has `dependency_lock_sha256: null`. It cannot claim
resolved-content verification. The integrated pinned-policy promotion workflow
requires a bound dependency lock.

Policy pinning, capture helpers and import perform no remote writes. A failed
promotion can have already moved earlier labels in its batch; its report
preserves those outcomes and does not retry automatically. Passing evidence
proves the configured cases against their recorded environment;
it does not establish application adoption after a release. That is checked
separately using runtime adoption observations.
