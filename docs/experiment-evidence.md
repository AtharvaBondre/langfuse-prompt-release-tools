# Experiment evidence and required checks

For a first run, start with [getting started](getting-started.md) and the
[current 0.6 SDK demo](sdk-release-example.md). The demo uses real Python and
JavaScript SDK experiment runners on a disposable server, with deterministic
tasks and no model calls. This guide explains how to connect your own task and
evaluators to the same evidence contract.

A release policy is reviewed **before staging and execution**. It specifies the
prompt targets, dataset cases, required evaluator names and thresholds. Importing
results cannot reduce these requirements. Promotion reads the policy from the
snapshot again and recomputes every gate from the preserved source artifacts.

This is a release-evidence adapter around Langfuse. Langfuse owns experiment
execution, datasets, scores and prompt resolution. The adapter does not run an
LLM, implement another evaluator, or infer runtime identity from an experiment
URL.

## Current workflow: policy v2 with remote verification

Use `format_version: 2` and `remote_export: required` when a release must prove
that its experiment cases and scores reached Langfuse. The independent
`remote_verification` policy pins the project, dataset kind and receipt freshness.
Completing a task or calling `flush()` cannot satisfy that requirement.

The commands below assume the installed environment is activated as described
in [getting started](getting-started.md). Keep the selected host, project and
project-scoped keys together in a dedicated environment for this release.

1. Export prompts and review the local edits.
2. Write the policy JSON and run
   `langfuse-release-policy --manifest release-review/prompt-index.json --policy policy.json`.
   This adds the full policy and its digest to the manifest. It is a one-time
   binding; changing it requires a fresh export. Staging must happen afterward.
3. Stage the edited prompts. Its snapshot digest now includes the policy.
4. Capture a dependency lock for those exact staged versions. The lock must exist
   before the experiment starts, including for roots with no dependencies.
5. Run the application's task through the official SDK or Experiment Action.
   Start the capture before execution, fetch each prompt by immutable version,
   and record the returned prompt immediately, before compiling it or passing it
   into a framework that transforms the SDK object. Preserve actual outputs and
   per-item evaluator results. Write `result.json` in the supported Action v1
   format and finalize `capture.json`; see the helpers below.
6. Verify remote persistence using the same plain policy object that was pinned.
   Select the project ID independently; do not derive it from a result URL:

   ```sh
   langfuse-release-remote --host "$LANGFUSE_HOST" \
     --project-id "$LANGFUSE_PROJECT_ID" --dataset-kind local \
     --policy policy.json --results result.json --capture capture.json \
     --report release-review/reports/remote.json
   ```

   Use `--dataset-kind langfuse` only for an actual hosted dataset whose identity
   is pinned in the policy. A `pending` or `failed` remote receipt blocks the
   required gate. Keep the finalized capture unchanged; do not rewrite its export
   flag after this check. See [remote persistence](remote-persistence.md).
7. Import the Action v1 result file, finalized capture and confirmed remote receipt:

   ```sh
   langfuse-release-import-experiment \
     --manifest release-review/prompt-index.json \
     --stage-report release-review/reports/stage.json \
     --results result.json --capture capture.json \
     --remote-report release-review/reports/remote.json --report evaluation.json
   ```

8. Supply the resulting receipt to promotion along with the same dependency
   lock. Promotion revalidates evidence before each label PATCH. A later
   dependency change invalidates the lock; rerun the evaluation against a newly
   reviewed release instead of binding an old run to a new lock.

   ```sh
   langfuse-release-promote --host "$LANGFUSE_HOST" \
     --manifest release-review/prompt-index.json \
     --stage-report release-review/reports/stage.json --label production \
     --evaluation-report release-review/reports/evaluation.json \
     --dependency-lock release-review/reports/dependencies.json \
     --report promotion-preview.json

   # After reviewing the preview, apply using a new report filename.
   langfuse-release-promote --host "$LANGFUSE_HOST" \
     --manifest release-review/prompt-index.json \
     --stage-report release-review/reports/stage.json --label production \
     --evaluation-report release-review/reports/evaluation.json \
     --dependency-lock release-review/reports/dependencies.json \
     --apply --report promotion-apply.json
   ```

   Select the actual reviewed deployment label. Reports never overwrite an old
   attempt. If the remote receipt expires during review, perform a new remote
   check and import it into a new evaluation receipt before promotion.

The equivalent Python entry points are `langfuse_release_policy.pin_policy` and
`import_langfuse_experiment.import_experiment`. Both return dictionaries. The
imported receipt lives at `manifest.parent / 'reports' / report['report_file']`.
Source result, capture and lock files must remain available; receipts contain
absolute local references and fingerprints. Copying a receipt alone is not a
portable evidence bundle.

## Policy fields

Required top-level fields are `format_version`, a nonempty `id`, `producer`,
`dataset`, `execution`, `targets`, `checks` and `remote_export`.
For the current remote-required workflow, use version 2 and include
`remote_verification`. Policy versions and the Action result's `schema_version:
v1` are separate contracts.

| Field | Meaning |
| --- | --- |
| `producer.kind` | `langfuse-experiment-action`, `langfuse-python-sdk` or `langfuse-javascript-sdk`; records what actually produced the evidence |
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
| `remote_verification` | Required with policy v2 and `remote_export: required`: `{project_id, dataset_kind, max_age_seconds}`; dataset kind is `local` or `langfuse`, and freshness is an integer from 1 to 3600 seconds |

Python SDK producers require `runtime: python` in results; JavaScript SDK
producers require `runtime: node`. The Action producer may run either runtime.
For a complete generated synthetic policy, see `policy_for()` in
[`examples/sdk_experiment.py`](../examples/sdk_experiment.py) and the format-2
extension in [`verified_release_demo.py`](../examples/verified_release_demo.py).
Replace the demo's cases, identities and checks with reviewed application values;
the demo's passing scores are not evidence for another application.

Policy v2 with `remote_export: optional` omits `remote_verification`. This is an
explicit local-evidence choice and does not establish remote persistence. Policy
v1 remains readable for older workflows; see its limitations below and the
[0.6 migration guide](migration-v06.md).

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
from pathlib import Path
from import_langfuse_experiment import start_capture, capture_prompt, finalize_capture
from publish_langfuse_prompts import write_report

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
# Now compile or hand this prompt to the framework and run the real task.
# Preserve the actual result and the official runner's per-item evaluations.

# After the official runner has completed and result.json has been written:
finished = finalize_capture(capture, result_path, remote_export="unconfirmed")
write_report(Path(capture_path), finished, create=True)
```

`start_capture` checks actual dataset/task/evaluator/model identities against the
already pinned policy. It derives target versions from the stage report, never
from a caller's label. `capture_prompt` accepts an SDK prompt object or dictionary
and checks its returned name/version. With a dependency lock, it also fingerprints
actual resolved content and config using the same canonicalization as dependency
validation. The importer requires each case/target observation and independently
checks its digest against the original lock.

`finalize_capture` returns a new dictionary and must be assigned as shown. It
records local completion and the canonical digest of the result file. Use
`unconfirmed` when export/flush was attempted but persistence has not yet been
verified, or `not_attempted` if no export was attempted. For a policy-v2
remote-required release, the separate confirmed remote receipt establishes
persistence; the finalized capture stays unchanged. A caller-supplied `confirmed`
flag, result URL or queued flush cannot replace that receipt.

For a JavaScript task, use the [JavaScript capture bridge](../js/README.md) at the
same point immediately after `langfuse.prompt.get()`. It requires the actual
official SDK prompt object. Capture before compiling or converting that object
into a framework-specific representation. The bridge delegates validation to
the installed Python core; it does not run your task or normalize arbitrary
experiment results for you.

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
`producer.kind: langfuse-python-sdk` or `langfuse-javascript-sdk`. Its
string-valued `action_metadata` should
identify the actual adapter and SDK version. Using the envelope does not mean a
hosted GitHub Action ran.

The working projections are in [`examples/sdk_experiment.py`](../examples/sdk_experiment.py)
and [`js/sdk-experiment.mjs`](../js/sdk-experiment.mjs). Preserve actual item IDs,
outputs, trace IDs and typed evaluations. In the checked JavaScript SDK 6.1.1,
send BOOLEAN scores as numeric `0`/`1` with `dataType: "BOOLEAN"`, then explicitly
project those typed values to JSON booleans. Do not convert an untyped numeric
score or use general truthiness. A script can complete with `status: "passed"`
while its evaluator returns `false`; that failed check still blocks promotion.

## Older policy-v1 workflows

Policy v1 with `remote_export: required` requires a producer-reported
`completion.remote_export: confirmed`. It does not perform the built-in
independent persistence check introduced for policy v2. Failed, unattempted or
unconfirmed export blocks that older required-export gate. Do not treat its
producer flag as equivalent to a verified remote receipt.

Both policy versions retain optional-export workflows. A locally passing run
can retain a visible failed or unconfirmed export outcome in
`completion.remote_export` when export was explicitly optional. The
[older Python baseline](sdk-release-example.md#older-05-python-baseline) uses
that local-evidence mode. Start a new export and reviewed policy to adopt v2;
do not relabel an old run as stronger evidence.

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
