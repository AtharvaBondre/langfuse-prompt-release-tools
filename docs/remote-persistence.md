# Verify remote experiment persistence

A completed local experiment and `flush()` do not prove that Langfuse received every required case and score. This read-only verifier compares the expected results with Langfuse's public experiment and score APIs. Missing exported or sampled-out data remains `pending`; it never becomes confirmed merely because a wait expired.

The audited combinations are the Python SDK 5.2 and JavaScript SDK 6.1.1 stored-IO contracts with public APIs in Langfuse 4.78.0. Older servers without these endpoints fail explicitly. The verifier uses the standard library; it does not run an experiment, submit scores, change a prompt or call a model.

## API

```python
from langfuse_release_remote import (
    RemoteClient, make_remote_expectation, verify_remote,
    validate_remote_receipt,
)

expected = make_remote_expectation(
    source_results, finalized_capture, reviewed_policy,
    host="https://cloud.langfuse.com",
    project_id="independently-configured-project-id",
    dataset_kind="local",  # or "langfuse" for an actual hosted dataset
)
receipt = verify_remote(
    RemoteClient(expected["host"], timeout=10), expected,
    wait_seconds=60, poll_interval_seconds=2,
    report_path="review/reports/remote-001.json",
)
validate_remote_receipt(receipt, expected, max_age_seconds=300)
```

Project identity must come from independently reviewed configuration. The experiment URL is not used as authorization to select a project or host. Authentication uses only `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. The client rejects non-GET methods before opening a connection. It never falls back to an organization key, another project or another host.

The CLI module accepts `--host`, `--project-id`, `--results`, `--capture`, `--policy`, `--dataset-kind` and a new `--report` path. `--policy` is the plain reviewed policy object, not the snapshot wrapper. It also accepts `--wait-seconds`, `--poll-interval-seconds`, `--max-pages` and `--timeout`. From a checkout, run `python langfuse_release_remote.py --help`.

The finalized execution capture stays immutable after verification. Its digest is part of the receipt. Do not rewrite `remote_export` to `confirmed` afterward: that would invalidate the binding. A strict release gate must consume the bound receipt, not a producer's standalone `confirmed` statement. The normal experiment importer remains responsible for staged prompt identities and threshold checks; persistence alone can correctly confirm that a **failing** score was stored.

## Required evidence

The source must preserve one supported Python or Node experiment result with its actual `experiment_id`, `run_name`, every expected case's unique 32-character trace ID, actual output and all typed scores. The pinned `langfuse-python-sdk` producer requires `runtime: python`; `langfuse-javascript-sdk` requires `runtime: node`. The Action producer can use either. The capture must preserve the completed execution time window, dataset identity, execution identity and complete case IDs. Missing cases, duplicate traces, changed hashes or weaker score coverage fail before network access.

The verifier reads:

1. `GET /api/public/projects` and matches the single project associated with the project-scoped key.
2. `GET /api/public/experiments`, filtered by the exact experiment ID and capture time window. The run name, dataset association and complete item count must agree.
3. All cursor pages of `GET /api/public/experiment-items?fields=core,dataset,io`. Trace IDs map these rows back to local case IDs, and stored input/output/expected-output values must agree.
4. All cursor pages of `GET /api/public/v3/scores?fields=subject&traceId=...&observationId=...` for each item. The official endpoint requires trace ID with observation ID because observation IDs are scoped to traces. Expected run-level scores use the experiment ID filter.
5. The experiment summary again, rejecting changes during the inspection round.

Embedded experiment-item scores are deliberately not used: their `scoreLimit` tops out at 50. The separate score API provides cursor pagination. Pagination validates the endpoint's metadata, repeated IDs/cursors and the configured maximum pages. Reaching a page bound is a failure, not a partial success. Other score names are outside the receipt's required-score scope; duplicate required names are rejected instead of choosing a convenient value.

Every required score must have the expected project, API source, observation/experiment subject, name, data type and value. BOOLEAN scores can be normalized from 0/1 **only when the remote score explicitly says `dataType: BOOLEAN`**. A NUMERIC score of 1 cannot satisfy a BOOLEAN check. Producers should create `Evaluation(..., value=..., data_type="BOOLEAN")` for boolean checks and retain that type in the source artifact. The checked SDK/server pair otherwise persisted an untyped Python boolean as NUMERIC 1.

JavaScript SDK 6.1.1's score writer accepts numbers or strings, so its BOOLEAN evaluator must send `value: 1` or `value: 0` with `dataType: "BOOLEAN"`. Normalize that explicitly typed result into a boolean in the local evidence artifact; do not normalize untyped numeric scores. Returning a raw JavaScript boolean can produce a locally passing result with no persisted score. Such a run remains `pending` here.

## Stored IO and dataset limits

The checked public API returns stored IO strings. Python SDK 5.2 and JavaScript SDK 6.1.1 leave plain strings unchanged and JSON-serialize other values. Each expected field therefore records `text`, `json` or `null` storage:

- Text is compared verbatim, even when it looks like JSON.
- JSON is decoded exactly once with duplicate-key and non-finite-value rejection, then compared by canonical value hash.
- Null requires null.

This verifies the stored SDK representation. It cannot independently recover type distinctions already lost during serialization: a JSON-looking string and a serialized object can produce identical stored text. The receipt does not claim independent proof of original runtime types or authenticated producer execution. Unsupported projections fail instead of trying multiple coercions until one passes.

JavaScript's experiment runner omits an empty expected-output string from span attributes. If the API consequently returns null, verification fails; the verifier does not pretend null and empty text are equivalent. Non-JSON values such as undefined, functions, BigInt and non-finite numbers are outside the supported evidence contract. Node expectations explicitly bind `runtime: node` and use a JavaScript receipt scope; older Python expectation hashes and receipt scopes remain unchanged.

For local datasets, Langfuse reports a null dataset ID/version. Python derives `experimentItemId` from input data instead of the local case dictionary's `id`; JavaScript uses a supplied item `id` or derives a hash when absent. The verifier maps cases by their unique actual trace IDs and compares stored values. Local dataset name/version/schema identity remains bound producer evidence, not a claim that a hosted Langfuse dataset exists.

For hosted datasets, the verifier additionally fetches `GET /api/public/v2/datasets/{encoded name}`, matches ID/name, requires each remote case ID and compares `experimentItemVersion` with the captured aware timestamp. That is evidence of the dataset version attached to the experiment, not an independent replay of historical dataset storage. If `schema_sha256` is pinned, it must be the canonical SHA-256 of `{"inputSchema": ..., "expectedOutputSchema": ...}`; the current dataset response must expose and match both fields. Schema and item version are separate checks.

## Outcomes, time and receipt storage

- `confirmed`: every expected case and typed score was observed and matched.
- `pending`: incomplete export, missing experiment/cases/scores, incomplete remote spans, transient transport/429/5xx, or a summary that changed during inspection. Wait exhaustion preserves pending.
- `failed`: conflicting project/run/dataset/content/score identity, malformed or incomplete pagination, ambiguous duplicates, authentication failure or unsupported endpoint.

The default polling horizon is 60 seconds, with a two-second delay. `wait_seconds=0` performs one complete inspection. Each HTTP request also has a timeout; an inspection already underway may finish after the polling horizon. Each scan is bounded by expected cases and `max_pages`, and no new polling round begins once the horizon expires. The command sends no write or retry of a write.

A receipt records project/host/experiment identity, source/capture/policy/expectation digests, complete observed case/score evidence, status, issues, attempts and check time. It reserves a new output file exclusively before HTTP and uses POSIX mode `0600`. Existing source artifacts or reports cannot be overwritten. Storage failure raises an error; a partial reserved report may remain pending. Windows ACL privacy is not established by POSIX mode bits.

Saved-receipt validation checks digests, exact case/score coverage, identity bindings, scope and age. The default five-minute freshness limit is explicit and configurable. Unsigned hashes are not runner authentication. A malicious writer able to fabricate a whole receipt must be addressed by the separately configured provenance verifier or by fresh authenticated GETs.

CLI exits are 0 for confirmed, 2 for pending and 1 for failure. No outcome asserts model quality, successful application adoption or an official Langfuse endorsement.

## Source and test evidence

Primary sources were checked at Langfuse 4.78.0 commit `138beceb728b998003059981892ad09e04f154b1`:

- [Experiment API definitions](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/fern/apis/server/definition/experiments.yml): exact experiment and item fields, cursor filters and embedded score limits.
- [Project API definitions](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/fern/apis/server/definition/projects.yml): project-scoped identity endpoint.
- [Experiment response service](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/features/experiments/server/public/service.ts): stored IO projection and item/run score scope.
- [Score v3 handler](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/pages/api/public/v3/scores/index.ts): trace/observation filter requirement and cursor-limit metadata.
- [Score v3 schema](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/packages/shared/src/features/scores/interfaces/api/v3/schemas.ts): explicit polymorphic score types.

Python SDK 5.2.0's `_client/client.py` experiment runner and `_client/attributes.py:_serialize` were also inspected. JavaScript SDK 6.1.1's published packages were checked: `@langfuse/core` (`src/utils.ts:serializeValue`), `@langfuse/tracing` (`src/attributes.ts:_serialize`) and `@langfuse/client` (experiment `runItem` and score writer). Local read-only API checks confirmed stored IO strings, observation-scoped score queries, the Python untyped-boolean mismatch and the JavaScript missing-score outcome for raw boolean values. Those checks do not establish all tenant/SDK combinations or claim a confirmed complete JavaScript run.

Run `python -m unittest -v test_release_remote` for offline tests of typed values, exact coverage, local/hosted dataset identity, schema/version drift, pagination, eventual consistency, timeout, sampling/export gaps, wrong projects, private output and receipt validation. Live end-to-end results belong in the release's validation record rather than being inferred from these fixtures.
