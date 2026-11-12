# Hosted dataset validation

`examples/hosted_dataset_demo.py` exercises a dataset stored by the disposable
Langfuse server. It uses the official Python SDK `5.2.0`, three synthetic cases
and a deterministic JSON prompt renderer. It makes no model calls and does not
use a customer or cloud Langfuse tenant.

The demo targets the same pinned AMD64 server matrix as `tools/run_live_ci.py`:
`4.59.0` and `4.78.0`. A completed live matrix is required for a compatibility
claim. Offline tests and source inspection alone do not establish live support.
An authenticated server missing the required experiment APIs remains unsupported
under the runner's existing classification.

## Reproduce

Use the isolated live CI runner, which creates the stack, supplies newly generated
project credentials, verifies its identity and removes its volumes afterward.
The demo's invocation contract is:

```sh
python examples/hosted_dataset_demo.py \
  --host http://127.0.0.1:13830 \
  --project-id "$LANGFUSE_INIT_PROJECT_ID" \
  --out-dir /private/runner-evidence/hosted_dataset_demo \
  --disposable-local
```

`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` must be the disposable project's
keys. The runner also supplies `LANGFUSE_BASE_URL`, `LANGFUSE_HOST` and
`LANGFUSE_TRACING_ENABLED=true` in its cleaned child environment. Never copy
customer credentials into this scenario. `--project-id` is required independently
of the connected key; project verification precedes dataset or prompt writes.
The output directory must be new. A `360` second outer subprocess timeout is
appropriate for this small scenario; individual HTTP requests use ten seconds,
remote completion polling allows sixty seconds, and the deliberately missing
export polls for twenty seconds. Remote pagination uses a one-item page size and
a ten-page limit. The official SDK dataset paginator is bounded by the runner's
outer timeout and is expected to fetch exactly three initial pages.

Run the focused offline contracts with:

```sh
python -m unittest -v test_hosted_dataset_demo
```

## Evidence and checks

The scenario creates a schema-bearing dataset and three items through the SDK.
It chooses the newest server-returned item `version` timestamp, fetches the
dataset at that explicit timestamp, and pins its real dataset ID, name, item IDs,
input/expected-output hashes and schema hash into a required-remote policy.
The schema digest covers both `inputSchema` and `expectedOutputSchema`; it is
independent of the dataset timestamp. A freshly created item's server-returned
`updated_at` is the fallback point-in-time selector if a projection omits
`version`. No local content hash is presented as a hosted dataset version.

The public `DatasetClient.run_experiment` executes a staged prompt with task-time
prompt capture. Hosted tasks receive SDK `DatasetItem` objects. The Action v1
projection preserves their evaluated values, actual trace IDs and experiment ID.
Each case emits an explicit `BOOLEAN` exact-answer score and `NUMERIC` quality
score. The run emits a boolean completeness/exactness score and numeric mean.
Run scores are verified for persistence and preserved; release thresholds remain
the independently pinned per-case checks.

The demo then checks:

- Complete live dataset/version/schema/case evidence passes the release importer.
- Actual SDK dataset pages and remote item, case-score and run-score cursor pages
  are traversed; a one-page sample cannot establish completion.
- A second real run intentionally withholds exactly one SDK run-score write while
  retaining that evaluation in its local result. Missing persisted evidence stays
  `pending` and cannot create a passing evaluation receipt after `flush()`.
- Another real run name or another real hosted dataset cannot substitute for the
  original captured run and dataset association.
- An actual item edit produces a changed dataset version/content that is rejected
  before execution under the existing policy. It cannot relabel the earlier run.
  Fetching the original timestamp must still reproduce the original cases.
- A real schema update causes independent schema verification to fail, even with
  the original experiment and dataset timestamp.

Negative expectation variants are explicitly test inputs. They are never imported
as successful release evidence. Dataset edits affect only uniquely named synthetic
objects in the disposable project. The runner deletes that stack and its volumes.

`summary.json` and stdout contain only sanitized check descriptions, counts, score
types, bounds and outcome classifications. Success requires `status: "passed"`
and `model_calls: 0`; `checks` is an array of strings, compatible with the live
runner's summary collector. `remote_statuses` reports `complete: "confirmed"`,
`missing_export: "pending"`, and `changed_schema: "failed"` for a successful
scenario. These negative outcomes are expected assertions, not compatibility
failures. Any unexpected condition exits nonzero and leaves no successful summary.

Raw result/capture/policy/remote receipts and `pagination.json` stay in the private
evidence directory. They are unsigned local runtime evidence, not an assertion
about production datasets or future SDK versions.

## Primary API sources

The implementation was checked against the installed `langfuse==5.2.0` SDK's
`_client/datasets.py`, `get_dataset`, `_run_experiment_async`, hosted-item processing
and `ExperimentItemResult` definitions. The public dataset method forwards the
explicit dataset version and associates run-level scores through the experiment
ID. Official server sources inspected for the target versions are:

- [Dataset API types, 4.59.0](https://github.com/langfuse/langfuse/blob/v4.59.0/web/src/features/public-api/types/datasets.ts)
- [Dataset API types, 4.78.0](https://github.com/langfuse/langfuse/blob/v4.78.0/web/src/features/public-api/types/datasets.ts)
- [Dataset creation, 4.78.0](https://github.com/langfuse/langfuse/blob/v4.78.0/web/src/pages/api/public/v2/datasets/index.ts)
- [Dataset retrieval, 4.78.0](https://github.com/langfuse/langfuse/blob/v4.78.0/web/src/pages/api/public/v2/datasets/%5BdatasetName%5D/index.ts)
- [Dataset item service, 4.78.0](https://github.com/langfuse/langfuse/blob/v4.78.0/web/src/features/datasets/server/publicDatasetService.ts)

The server's dataset creation endpoint upserts by name, allowing the synthetic
schema-drift probe. Dataset item creation upserts the same item ID and records a
new version. Hosted history preservation is an explicit live assertion; a server
configured to update dataset items in place cannot silently pass it.
