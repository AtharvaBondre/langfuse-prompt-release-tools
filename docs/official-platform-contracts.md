# Official Langfuse contracts and integration boundaries

Reviewed and refreshed **11 November 2026** against official repositories and documentation. The contract notes below retain their exact source pins; the integration sections describe shipped **0.6.0a1** behavior. This source review is separate from the [tested SDK/server matrix](validation.md).

Langfuse already provides prompt storage, numbered versions, deployment labels, diffs, rollback by label movement, prompt composition, SDK caching, datasets and evaluation runners. This toolkit connects reviewed files, exact staged versions, required checks, dependency locks and explicit label promotion, then records instrumented application accesses. See [how it works alongside Langfuse](langfuse-comparison.md).

## Source pins

| Source | Released version observed | Inspected source |
|---|---|---|
| Langfuse | [v4.78.0](https://github.com/langfuse/langfuse/releases/tag/v4.78.0), 10 November | Release [`138beceb728b998003059981892ad09e04f154b1`](https://github.com/langfuse/langfuse/commit/138beceb728b998003059981892ad09e04f154b1); refreshed main [`3781307833fc079426f6330fe6be5054cabbf3a3`](https://github.com/langfuse/langfuse/commit/3781307833fc079426f6330fe6be5054cabbf3a3) |
| Python SDK | [v5.2.0](https://github.com/langfuse/langfuse-python/releases/tag/v5.2.0), 5 November | Release [`8e6c60db5f76f8cfacb536a7564a3f42f078efdc`](https://github.com/langfuse/langfuse-python/commit/8e6c60db5f76f8cfacb536a7564a3f42f078efdc); main [`e3ffb853847777f544cdfe82da80151e4a9d7c3c`](https://github.com/langfuse/langfuse-python/commit/e3ffb853847777f544cdfe82da80151e4a9d7c3c) |
| JS/TS SDK | [v6.1.1](https://github.com/langfuse/langfuse-js/releases/tag/v6.1.1), 2 November | Release [`7ba77b7c8517829b1e5dc2985de851fa3e6f89ec`](https://github.com/langfuse/langfuse-js/commit/7ba77b7c8517829b1e5dc2985de851fa3e6f89ec); main [`be7e61f9c80652ca438089637939425cff33050e`](https://github.com/langfuse/langfuse-js/commit/be7e61f9c80652ca438089637939425cff33050e) |
| Documentation | Refreshed snapshot | [`3c7670a175810c67d530c89be2c0fdae17ca2761`](https://github.com/langfuse/langfuse-docs/commit/3c7670a175810c67d530c89be2c0fdae17ca2761) |

The initial 11 November audit used core main [`47a7034`](https://github.com/langfuse/langfuse/commit/47a70342f1736561dfa157a654d011aac29db79d) and docs [`7e4f0a7`](https://github.com/langfuse/langfuse-docs/commit/7e4f0a7e031828632a11b7ae9decf6aeab57dff7). At those pins, the inspected create/update/PromptService implementations and prompt endpoint definitions matched the release; the retained links below identify that audited code. Selected SDK prompt/experiment implementations also matched their released tags.

The refreshed core main has since added batched dependency fetching in `PromptService` and default-evaluation-model MCP tools. Those main-branch additions are not attributed to released **4.78.0**. The [live validation](validation.md) covers released 4.59.0 and 4.78.0, not arbitrary main revisions or every version between them.

## Prompt API used by the release workflow

Definitions: [prompt API](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/fern/apis/server/definition/prompts.yml), [label update API](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/fern/apis/server/definition/prompt-version.yml), [runtime validation](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/packages/shared/src/features/prompts/types.ts).

| Method and path | Contract |
|---|---|
| `GET /api/public/v2/prompts` | Lists metadata in `{data,meta}`. Pagination starts at 1, defaults to 10, and allows 100 items per page. Rows contain `name,type,versions,labels,tags,lastUpdatedAt,lastConfig`. |
| `GET /api/public/v2/prompts/{promptName}` | Select either `version` or `label`. With neither, selects `production`. A missing target returns 404; both selectors return 400. `resolve` defaults to true. URL-encode the complete folder-qualified name. |
| `POST /api/public/v2/prompts` | Creates a version. Text requires `name` and string `prompt`; chat requires `name,type:"chat"` and an array `prompt`. Optional fields are `config,labels,tags,commitMessage`. Returns the created Prompt. |
| `PATCH /api/public/v2/prompts/{name}/versions/{version}` | Accepts `{newLabels:[...]}`. Adds/moves supplied labels to the chosen version, preserving other labels already on it. An empty array changes nothing. `latest` is reserved. Returns the updated Prompt. |

A Prompt includes `type,name,version,prompt,config,labels,tags`, with optional `commitMessage` and `resolutionGraph`; runtime responses can contain extra metadata. A resolved graph has `root:{id,name,version}` and `dependencies:{[promptId]:[{id,name,version},...]}`. It is null for raw reads or prompts without dependencies.

Important details from the runtime:

- Creation with `labels:[]` still advances **latest**. Consumers of latest can see staged content.
- Explicit tags apply across all versions of a prompt. Omitted/null tags inherit from the latest version. Use release reports or version commit messages for release provenance.
- `newLabels` is additive. It does not replace the full label set, remove all labels, or provide a compare-and-swap condition.
- Create labels are arrays or omitted; null fails runtime validation. Each create label is 1–36 lowercase letters, digits, underscores, hyphens or dots. Commit messages are limited to 500 characters.
- Normal stored chat messages have string `role` and `content`. Runtime validation strips extra fields, including the generated optional normal-message `type:"chatmessage"`. Placeholders require `type:"placeholder"` and a name matching `^[A-Za-z][A-Za-z0-9_]*$`.
- The public create service normalizes omitted/root-null config to `{}` and preserves other JSON values.
- POST success is **201** in the runtime; the generated OpenAPI advertises 200. Generated schemas/examples must not replace runtime checks.
- List filters apply before version aggregation. Returned labels, versions and `lastConfig` can represent only matching versions. The JSON `filter` parameter takes precedence over individual filters, and an empty array removes those individual filters.

These behaviors are implemented in the [API service](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/web/src/features/prompts/server/prompt-api-service.ts), [create action](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/features/prompts/server/actions/createPrompt.ts) and [label action](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/features/prompts/server/actions/updatePrompts.ts).

## Raw verification, dependency resolution and caches

Use `resolve=false` for release read-back. It returns stored content with dependency tags intact and [bypasses the server prompt cache](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/packages/shared/src/server/services/PromptService/index.ts#L53). Fetch the exact version returned by POST, then separately confirm that the deployment label points to that version.

[Prompt composition](https://langfuse.com/docs/prompt-management/features/composability) supports references such as:

```text
@@@langfusePrompt:name=shared-guidelines|version=3@@@
@@@langfusePrompt:name=shared-guidelines|label=production@@@
```

Dependencies must be text prompts, even when the parent is chat. The inspected resolver rejects missing/non-text children, cycles and excessive nesting. A parent version that references a child label can resolve to different text after that label moves. A child version may itself contain a label reference. Raw parent equality therefore does not prove equality of the complete resolved prompt used by an application.

Shipped [dependency locks](dependencies-and-adoption.md) bind concrete transitive versions, raw/resolved content hashes and the server graph to the task capture and evaluation. Promotion rechecks the dependency closure around its label writes; a moved nested label invalidates the earlier evidence. Langfuse remains the resolver and compiler authority. This verification does not provide dependency-aware batch staging, automatic ordering or a remote transaction.

The [official SDK cache](https://langfuse.com/docs/prompt-management/features/caching) defaults to 60 seconds. An expired entry can be returned immediately while it refreshes in the background. `cache_ttl_seconds=0` in Python or `cacheTtlSeconds:0` in JS disables the SDK cache; it does not bypass the server cache. A successful label read-back does not establish immediate application adoption. Shipped [adoption checks](dependencies-and-adoption.md) and [rollout windows](rollout-observation.md) evaluate actual Python/JS SDK observations for version/content, cache, fallback and declared consumer coverage. These observations cover supplied instrumented accesses; a production application pilot remains future work.

## Concurrency and uncertain writes

The four release operations expose no expected-baseline condition or idempotency key in the inspected schema. Preflight reduces stale writes but cannot eliminate a concurrent change between GET and POST/PATCH, nor make a multi-prompt batch atomic.

The audited 4.78.0 create action catches cache-invalidation and event-preparation failures after its transaction commits. The [label action](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/features/prompts/server/actions/updatePrompts.ts#L134) still awaits those steps after committing its label change. Dependency resolution can fail at that point. An error or lost response is not proof that nothing changed.

The shipped [reconciliation command](reconciliation.md) retains per-item results and reads exact version/label state after an interrupted operation. An uncertain creation remains unknown: a matching version alone does not prove that the interrupted POST created it. Reconciliation does not mutate state or automatically retry/roll back the write.

## Using current SDKs

| Purpose | Python | JS/TS v6 |
|---|---|---|
| Runtime prompt fetch | `langfuse.get_prompt(...)` | `langfuse.prompt.get(...)` |
| Create prompt | `langfuse.create_prompt(...)` | `langfuse.prompt.create(...)` |
| Move labels | `langfuse.update_prompt(...)` | `langfuse.prompt.update(...)` |
| Run experiment | `langfuse.run_experiment(...)` | `langfuse.experiment.run(...)` |
| Fetch versioned dataset | `langfuse.get_dataset(name, version=timestamp)` | `langfuse.dataset.get(name, {version: timestamp})` |

High-level prompt getters supply caching, retries and fallbacks. The generated low-level API exposes `resolve`; use the raw endpoint contract for release verification. JS v6 removed the top-level `getPrompt`, `createPrompt` and `updatePrompt` aliases.

Two adapter pitfalls deserve explicit tests:

1. The Python [convenience create method](https://github.com/langfuse/langfuse-python/blob/8e6c60db5f76f8cfacb536a7564a3f42f078efdc/langfuse/_client/client.py#L4200) uses `config or {}`. It changes false, zero, empty string and empty list to `{}`, unlike the REST service's null-only normalization. An SDK transport must preserve the project's JSON fidelity.
2. Python uses its own [sequential template parser](https://github.com/langfuse/langfuse-python/blob/8e6c60db5f76f8cfacb536a7564a3f42f078efdc/langfuse/model.py#L49); JS uses [Mustache with escaping disabled](https://github.com/langfuse/langfuse-js/blob/7ba77b7c8517829b1e5dc2985de851fa3e6f89ec/packages/client/src/prompt/promptClients.ts#L215). Local variable-set checks are not a complete specification of either language. Test compilation through the application's actual SDK.

Python SDK v5 requires **Python 3.11+**; JS SDK v6 requires **Node 22+**. Both require Langfuse Cloud or self-hosted **v4.59.0+**. See the [Python migration](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5) and [JS migration](https://langfuse.com/docs/observability/sdk/upgrade-path/js-v5-to-v6) guides. These are upstream SDK requirements, not an automatic compatibility claim for every combination. The toolkit keeps its standard-library CLI at Python 3.10+ and the SDK adapters optional. Its [published matrix](validation.md) records actual Python 5.2.0 / JS 6.1.1 runs on the two tested server versions and the narrower ARM64 evidence.

## Evaluation and evidence integration in 0.6.0a1

[Official experiment runners](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk) execute local/hosted datasets, sync/async tasks and evaluators, item-level/run-level scores, concurrency limits and tracing. Official [CI guidance](https://github.com/langfuse/langfuse-docs/blob/3c7670a175810c67d530c89be2c0fdae17ca2761/content/docs/evaluation/experiments/experiments-ci-cd.mdx) includes reviewed baseline artifacts, per-case regression gates, dataset/evaluator version checks and an every-required-case-must-pass policy. Those are upstream capabilities and documented recipes.

The shipped [result importer and policy](experiment-evidence.md) package a specific release workflow around them: pin requirements before staging, capture the exact prompt returned inside the actual task, and validate every required case/check against the staged target and dependency lock. Python and [JavaScript](../js/README.md) producers preserve their actual outputs in the Action v1 result format. The toolkit uses the official runners rather than providing another evaluator engine.

Policy and captures bind the staged prompt, declared execution identities, dataset identity, required thresholds and item coverage. These bindings detect changed evidence; unsigned producer claims are not authenticated execution. [Dataset versions](https://langfuse.com/docs/evaluation/experiments/datasets#versioning) are timestamps: item add/update/delete/archive operations are versioned, but dataset schema changes are not. The toolkit supports a separately pinned schema hash when schema identity matters. Hosted-dataset live coverage is a [remaining compatibility test](roadmap.md); the current live matrix uses local synthetic datasets.

The integration preserves these distinctions:

- Local dataset inputs still get sent to the configured Langfuse instance by the official experiment SDK. Adding an SDK runner adds an external data path.
- Current experiments use OpenTelemetry. Sampling can drop item traces while run-level scores remain stored; do not infer complete coverage from aggregate scores.
- Score helpers may log/drop invalid values. Batch APIs can partially accept scores with HTTP 207. Local evaluation success and persisted remote evidence need separate outcomes.
- Use the SDK's flush/shutdown lifecycle for short-lived runners. Policy v2 can require a [remote verification receipt](remote-persistence.md): bounded, project-scoped reads must confirm every expected case and typed score. Flush alone is insufficient. Legacy policy v1 completion remains a producer statement.
- Local receipts remain unsigned unless a separate [CI provenance check](provenance.md) verifies the artifact against independently supplied trust requirements. [Portable bundles](portable-evidence.md) preserve original bytes and support offline semantic validation without the original paths. A signer-required promotion boundary is still [planned](roadmap.md); successful bundle/signature inspection is not automatically a promotion authorization.

Langfuse also provides [code evaluators](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators), LLM judges and schema-enforced datasets. Self-hosted code evaluators need a configured dispatcher. Reuse those capabilities when appropriate; a deterministic JSON-shape check still does not measure answer quality.

## Authentication, data and attribution

The release API uses HTTP Basic with a project public/secret key pair. Read routes require `prompts:read`; writes require `prompts:CUD` and enforce protected-label permissions. Current [API-key scopes](https://langfuse.com/faq/all/read-only-api-keys) support Viewer, Ingestion, Scores ingestion and Full access, plus expiry. Viewer keys fit export/verify. Ingestion-only keys cannot read prompts. Keep credentials out of snapshots, reports and logs.

[Protected labels](https://langfuse.com/docs/prompt-management/features/prompt-version-control#protected-prompt-labels) have paid/Enterprise availability restrictions. A local release gate does not replace server authorization. SDK [masking](https://langfuse.com/docs/observability/features/masking) controls telemetry exported through the configured processor; it does not sanitize local release artifacts or every other exporter.

The [Langfuse core license](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/LICENSE) permits MIT Expat use outside the excluded `ee/`, `web/src/ee/`, `worker/src/ee/` and `packages/shared/src/server/ee/` directories. Those excluded directories have a commercial [Enterprise license](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/ee/LICENSE); do not copy their implementation into this MIT project. The official [Python](https://github.com/langfuse/langfuse-python/blob/8e6c60db5f76f8cfacb536a7564a3f42f078efdc/LICENSE), [JS](https://github.com/langfuse/langfuse-js/blob/7ba77b7c8517829b1e5dc2985de851fa3e6f89ec/LICENSE) and [documentation](https://github.com/langfuse/langfuse-docs/blob/7e4f0a7e031828632a11b7ae9decf6aeab57dff7/LICENSE) repositories carry MIT notices. Preserve original copyright/license notices when copying or substantially adapting their material, and record the source commit. Referenced container images keep their own applicable licenses.

This public-source review covered selected implementation, schema, documentation, tests and release notes. It did not audit the full platform or run upstream tests. Separate [toolkit validation](validation.md) exercised disposable synthetic SDK/server workflows; it does not establish customer-runtime adoption or model-quality improvements.
