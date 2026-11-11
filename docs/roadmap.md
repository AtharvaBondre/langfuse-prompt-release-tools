# Research and expansion plan

Decision date: **11 November 2026**. Build a release workflow that makes application evidence and interrupted writes inspectable. Reuse Langfuse for prompt storage, label movement, experiment execution and observability.

This continues the public Gist's design: v2 separated staging and promotion; v3 bound application results to a release. The repository alpha makes those controls default and gives the project a tested distribution path. It does not claim to invent staging, evaluations, CI gates or MCP access.

## What exists upstream, and what belongs here

| Need | What Langfuse already supplies | Value this project can add |
|---|---|---|
| Store, fetch and version prompts | Prompt API, Python/JS SDKs, official CLI and native MCP | Review local content against a captured baseline; preserve exact JSON/text values and read back returned version IDs. |
| Test application behavior | Datasets, SDK experiments, evaluators and `experiment-action` | Bind results from those runners to every changed staged version and the intended release checks. |
| Move deployment labels | Label API, SDK/CLI/MCP tools, protected labels where available | Require unchanged evidence, detect baseline drift, record each outcome and reconcile a completed move after a lost response. |
| Guide an agent | Official Langfuse skills and Claude/Codex/Cursor skill manifests | A small complementary release skill that follows this project's receipts and stopping conditions. |
| Observe the agent | Six official telemetry plugins, Halo and gateway integrations | No replacement. Tracing an agent does not prove it evaluated the prompt version being released. |
| Recover from partial failure | API reads and mutable labels | A release-specific explanation of what completed, what conflicts and what remains uncertain. No automatic rollback claim. |

Sources and exact versions are in [official-agent-tools.md](official-agent-tools.md) and [official-platform-contracts.md](official-platform-contracts.md). The audit covered the complete generated MCP catalogue, CLI resource inventory, official skills/plugin manifests and selected platform/SDK implementation. It was not a whole-platform security audit or tenant execution test.

## Alpha: implemented

- Default to staging; require an explicit, recorded direct-release opt-out.
- Default to evaluation-required promotion; require an explicit, recorded structural-only opt-out.
- Retain complete changed-target coverage, source/stage/snapshot hashes, stale-state checks and already-promoted reconciliation.
- Create private exports/receipts on POSIX, avoid clobbering new export files and classify deeply nested JSON after a write as an uncertain outcome.
- Include a captured-output example that computes JSON/answer-shape checks instead of asking a user to handwrite passing booleans. It uses trusted captured metadata, not authenticated execution proof.
- Provide migration notes, an Agent Skill, source/wheel tests and a Python/OS CI matrix. Preserve the original Gist and attribution.

See [validation](validation.md) for completed checks and their limits. These are workflow controls, not a replacement for server permissions.

## Next: official experiment-result adapter and expected check policy

This is the highest-value expansion. Use the official SDK or experiment action to run the application. Import its results into a versioned release-evidence contract.

Acceptance criteria:

1. Every changed prompt has an explicit staged version in the task's execution metadata. Reject a latest/production selector or a run with no verifiable mapping; an experiment URL alone is insufficient.
2. Capture dataset identity/version, case coverage, evaluator/task revision, model/config identity and original artifact hash. Hash dataset schema separately if relevant because schema changes are not dataset-versioned.
3. Bind required checks and thresholds to a reviewed project policy, so a command cannot accidentally choose a weaker check set for promotion. Missing, failed, NaN, sampled-out or partially exported results must not silently pass.
4. Preserve the source artifact and distinguish local evaluation completion from remote trace/score persistence. Test partial success, exporter failure, changed bindings and rejected promotion with fixtures from supported official versions.
5. Keep SDK dependencies optional. Python v5 requires Python 3.11+; JS v6 requires Node 22+ and both need self-hosted Langfuse 4.59.0+ or Cloud. Publish actual tested combinations rather than inheriting these as a compatibility guarantee.

Design details: [experiment adapter](experiment-adapter-design.md). This adapter and policy are **planned, not implemented in 0.4.0a1**.

## Next: read-only reconciliation

Add a command that reports a stopped release as completed, pending, conflicting or still unknown without making a write. It should retain the original report, inspect exact versions and labels, and identify candidate creations by reviewed content/config/metadata without treating similarity as proof of identity.

Acceptance criteria: simulate a committed POST with a lost response, a committed PATCH with a lost response, an unrelated newer version, a concurrent label move and a disk failure. Inspection sends no POST/PATCH/DELETE. A separate intentional resume may use the reviewed evidence; it must not retry an unknown POST automatically.

A local operation lock may prevent two processes sharing one release directory from overlapping. It must not be described as a distributed lock or compare-and-swap across other API clients.

## Later: resolved dependencies and consumer adoption

A parent version can reference child labels that move. An optional dependency lock should record transitive concrete versions, raw and resolved hashes, and the consuming SDK/compiler; reject changed dependencies before promotion. Test nested labels, cycles, missing/non-text children and partial batch failure. Use Langfuse's resolver rather than inventing another prompt language.

Separately add an application-side adoption probe that observes the actual prompt version used after a label move. Account for SDK stale-while-revalidate caches, fallbacks, server caches and timeouts. Do not equate API readback with traffic adoption. Rollback remains an explicit label operation with current-state review.

Signed CI attestations may then authenticate the runner/policy/artifact chain. Unsigned hashes alone do not provide that property. Choose this after identifying a consumer and trust boundary.

## Conditional distribution and upstream contribution

A release-only MCP interface is justified if a real consuming client cannot run the CLI. Its tools should expose this workflow's previews, evidence and reconciliation; broad prompt CRUD already exists upstream. Add a plugin manifest only after testing installation and invocation in the target client. The current skill has no telemetry hooks and requires no MCP server.

A small upstream documentation contribution about ambiguous-write reconciliation remains useful if the issue still exists and maintainers want it. Recheck current contribution rules and present a minimal reproduction. Keep the Python engine in this repository; do not submit a large workflow or code bundle to the official skills repository by assumption.

## Deliberate exclusions

No general Langfuse CLI/MCP clone, hosted evaluation service, dataset UI, coding-agent tracing plugin, automatic model-judged promotions, unreviewed rollback or production deployment bundle. No promise of higher model quality, atomic batches or official endorsement. Expand when a concrete release problem and a testable improvement justify the maintenance.
