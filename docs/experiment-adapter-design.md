# Proposed official experiment adapter

This research design was implemented in 0.5.0a1. Use [the current evidence contract](experiment-evidence.md), [working SDK example](sdk-release-example.md) and [roadmap](roadmap.md) for current behavior and remaining scope. The design discussion below is retained as rationale.

**Design only; not implemented in 0.4.0a1.** The alpha accepts trusted application results and includes a deterministic captured-output checker. This proposal maps results from official Langfuse experiments into that release contract without creating a second experiment engine.

## User flow

1. Export, review and stage the prompt change using the existing release commands.
2. The application's official SDK experiment task fetches each relevant prompt by the exact stage-report version, records that identity, and runs the task/evaluators. The user selects dataset, model and destination because this step can transmit data and incur cost.
3. A small adapter reads the experiment output and a reviewed project check policy. It verifies target coverage and check thresholds, then writes bound source results and provenance.
4. The existing recorder and promoter check unchanged evidence and current label state. They do not infer that a successful experiment ran the intended prompt merely from its URL or title.

## Evidence to preserve

| Field | Why it matters |
|---|---|
| Snapshot and stage-report hashes | Ties results to the reviewed local release. |
| Prompt name and exact version for each changed target | Prevents an evaluation of a different version from approving this one. |
| Task/evaluator revision and code revision | Identifies what ran and how scores were produced. |
| Dataset ID/name, version timestamp and case IDs/counts | Makes missing or different cases visible. Schema hash is separate where relevant. |
| Model/provider and non-secret config identity | Distinguishes different execution conditions. |
| Required check IDs, thresholds and policy hash | Prevents an accidental weaker check set from approving a release. |
| Original result artifact hash, run ID/URL and item outcomes | Allows review of the actual evidence and per-target coverage. |
| Local completion and remote export status | Avoids treating dropped traces or partial score writes as complete evidence. |

Do not include project keys or private model-provider credentials in these records. Keep outputs and detailed results private unless explicitly sanitized for sharing.

## Failure behavior to test first

- A run evaluated `latest`, omitted a target, or used a different staged version: reject the mapping.
- Aggregate score passed but one required case/target failed or was missing: fail the release.
- A result contains non-finite values, invalid score types or unknown required checks: reject it.
- Policy, dataset schema, source result or stage report changed after capture: reject the binding.
- Evaluator completed locally but export was sampled, dropped or partially accepted: retain both outcomes; apply the declared evidence policy without inventing persisted results.
- An evaluation passes but a deployment label moved concurrently: the existing promoter still stops on drift.

## Integration choice

Start with one official result shape and a documented SDK/action version. The official [`experiment-action`](https://github.com/langfuse/experiment-action) exposes `result_json`; SDK runners expose richer item results. Inspect actual outputs and build real fixtures before promising compatibility. Do not silently parse human-readable CLI summaries or infer target identity from names.

A future SDK execution example should pin exact versions in its task, use an appropriate flush/shutdown lifecycle, and state its data destination and model costs. Its optional dependencies must not force a new runtime requirement on the standard-library release CLI.

The adapter does not make local files unforgeable. Authenticated CI provenance requires a separate signing and verification design. See the [roadmap](roadmap.md) and [official contracts](official-platform-contracts.md).
