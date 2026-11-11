# Resolved dependencies and consuming applications

A released parent version can keep the same raw text while a nested child label
moves. A dependency lock records the concrete transitive graph, raw/resolved
content hashes and reviewed consumer SDK/compiler identity. The separate adoption
receipt records what instrumented application SDK accesses returned after a
verified label promotion.

These controls use Langfuse's resolver and SDK. They do not implement another
prompt renderer, invalidate server caches or infer application traffic from a
release API read-back.

## Capture before running the application checks

Stage the reviewed snapshot, then capture the lock before evaluating it:

```sh
python -m langfuse_release_dependencies \
  --host https://cloud.langfuse.com \
  --manifest snapshot/manifest.json \
  --stage-report snapshot/reports/stage.json \
  --sdk 'langfuse-python==5.2.0' --compiler 'support-api@<commit>' \
  --report dependencies.json
```

`--host` must match the snapshot. Credentials use the existing environment-based
client. No operation here sends POST, PATCH or DELETE. The optional official SDK
is not needed for this command.

Each resolved response must explicitly contain `resolutionGraph`, including null
for a leaf. Capture reads every concrete node and each node's raw selectors.
Raw selector reads use `resolve=false`, which bypasses the inspected server
prompt cache. This catches a moved nested label even if a resolved response
still comes from a stale cache. Independent child resolution must match the
parent's complete subgraph. Missing nodes, cycles, non-text children,
contradictory graph identities and incomplete reads stop capture. No successful
artifact is written for a partial batch.

Reference tags are inspected only to check their selectors against fresh raw
reads. The accepted subset is the documented name-first two-part form:

```text
@@@langfusePrompt:name=shared/guidelines|version=3@@@
@@@langfusePrompt:name=shared/guidelines|label=production@@@
```

Malformed/ambiguous tags, non-canonical numeric versions and unknown selectors
fail closed. This is intentionally narrower than permissive upstream parsing.
Resolved content comes from the official server, never local substitution.

The lock contains `roots`, each with `name`, `version`, `raw_sha256`,
`resolved_sha256`, canonical `graph` and every concrete `nodes` entry. Node
selectors retain the selected version and raw hash. Each prompt digest hashes
canonical JSON containing **name, version, type, prompt and config**. It strips
only the optional `type: chatmessage` field from normal chat messages to match
the SDK; placeholder type/name and all actual text/JSON values are preserved.
Mutable labels/tags are not the runtime content digest. The root's tags are
separately checked against the staged snapshot.

A fresh pre-promotion check is:

```sh
python -m langfuse_release_dependencies \
  --host https://cloud.langfuse.com \
  --manifest snapshot/manifest.json \
  --stage-report snapshot/reports/stage.json \
  --check snapshot/reports/dependencies.json
```

The integration hooks are `build_dependency_lock(client, snapshot, stage,
consumer=...)`, `check_dependency_lock(client, lock, snapshot, stage)` and disk
wrappers `capture_dependency_lock(...)` / `validate_dependency_lock(...)`.
`validate_lock_integrity(lock)` checks the stored structure and digest without
network access; it **does not** establish that current selectors still match.
`prompt_sha256(prompt_dict)` is shared with experiment capture so the actual
SDK-resolved prompt can be compared with the pre-execution lock.

A lock does not rewrite floating references into immutable content. A child label
can move after the final check; Langfuse provides no multi-object compare-and-swap
here. Freeze child labels for the release window or use explicit child versions
when a stronger operational boundary is required. A multi-prompt release that
moves a child label and changes its parent resolution must be restaged and
reevaluated against the intended closure; do not bypass a lock rejection.

## Observe the real consuming SDK path

Instrument the application immediately around its existing `get_prompt` call.
The helper accepts the official installed Python SDK's exact `TextPromptClient`
or `ChatPromptClient` type. A dictionary, lookalike class, subclass or unknown
fallback flag is rejected. The SDK is imported only when recording an object, so
the rest of the CLI remains standard-library-only on Python 3.10+.

```python
from datetime import datetime, timezone
from langfuse_release_adoption import capture_sdk_observation

started = datetime.now(timezone.utc)
prompt = langfuse.get_prompt("support-answer", label="production", cache_ttl_seconds=0)
observation = capture_sdk_observation(
    prompt,
    consumer_id="support-api",
    deployment_id="app@<commit>",
    request_id="<application-request-id>",
    requested_label="production",
    request_started_at_utc=started,
    cache_ttl_seconds=0,
)
```

Place this at the application's actual prompt-access point. A separate test
script only establishes that the script observed the version. The helper does
not execute models, send telemetry, run applications or invent a request ID.
Record the real deployment revision and request ID supplied by the application.
It captures name/version/content/config hashes and the SDK's `is_fallback`
flag. It does not save the prompt content itself.

With caching enabled, the recorder reports `sdk_cache_possible`: it does not
claim to know whether that access was a cache hit or a background refresh. A
cached object may pass if its actual version and resolved content match the
released lock. A stale version or changed resolved content cannot pass. Setting
the TTL to zero disables the SDK cache only; server caching remains possible.

Provide one current observation for every required `(consumer, promoted prompt)`
pair. `required_consumers` maps each consumer to its expected deployment revision.
The promotion must be an applied, verified format-version-2 report with
`completed_at_utc`, the exact `dependency_lock_sha256`, and matching
snapshot/stage/target bindings. The lock must have been captured no later than
promotion completion. A legacy unbound promotion or a lock created afterwards
cannot establish adoption of the evaluated closure. All timestamps
must be timezone-aware UTC. Requests must start after promotion completion;
observations must be recent, non-future, and after the request started.

```python
from langfuse_release_adoption import write_adoption_receipt, check_adoption_receipt

receipt = write_adoption_receipt(
    "adoption.json", observations, promotion_report, dependency_lock,
    required_consumers={"support-api": "app@<commit>"},
    max_age_seconds=300,
)
check_adoption_receipt(
    receipt, promotion_report, dependency_lock,
    required_consumers={"support-api": "app@<commit>"},
    max_age_seconds=300,
)
```

Missing observations, stale/wrong versions, fallbacks, changed content, wrong
labels/revisions or invalid timing produce a persisted `not_adopted` receipt
with explicit reasons. Malformed inputs and incomplete/unknown/conflicting
promotion reports raise an error. Validation rejects changed receipts and
reevaluates their freshness; a receipt that passed yesterday cannot pass today
under a five-minute freshness policy. Existing files are never overwritten.

The equivalent recording CLI reads a JSON object containing an `observations`
array and a JSON object mapping consumers to deployment IDs:

```sh
python -m langfuse_release_adoption \
  --promotion snapshot/reports/promote.json \
  --dependency-lock snapshot/reports/dependencies.json \
  --consumers consumers.json --observations observations.json --report adoption.json
```

Use `--check adoption.json` instead of `--observations`/`--report` to validate an
existing receipt. An unverified result exits nonzero.

## Evidence and test boundaries

Hashes detect accidental edits and bind artifacts; these are unsigned local
attestations, not authenticated CI provenance. Even an official SDK object can
be constructed by application code. Trusted instrumentation and runner logs are
still necessary. The receipt describes its instrumented accesses only, not all
pods, all requests, fleet rollout, LLM completion quality or durable telemetry.
It does not prove the compiler revision string was executed.

Offline tests use captured official graph shapes and SDK-shaped classes. They
exercise nested label drift (including stale resolved cache responses), malformed
and incomplete graphs, missing/non-text children, partial batches, stale and
fallback observations, timestamp checks, missing consumers, changed evidence and
immutable report writes. They do not call a tenant. The repository's optional
SDK example is the separate place for an actual pinned SDK/server execution.

Official behavior was checked against Langfuse
[`PromptService`](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/packages/shared/src/server/services/PromptService/index.ts),
[`parsePromptDependencyTags`](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/packages/shared/src/features/prompts/parsePromptDependencyTags.ts),
and Python SDK
[`model.py`](https://github.com/langfuse/langfuse-python/blob/8e6c60db5f76f8cfacb536a7564a3f42f078efdc/langfuse/model.py).
No upstream source implementation was copied; the scanner and validation here
are deliberately limited release controls.
