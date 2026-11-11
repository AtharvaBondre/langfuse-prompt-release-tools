---
name: langfuse-prompt-release
description: Pin release policy, stage Langfuse prompts, bind official experiment results and resolved dependencies, promote a label, reconcile interruptions and verify actual SDK consumer adoption.
---

# Release reviewed Langfuse prompts

Use [langfuse-prompt-release-tools](https://github.com/AtharvaBondre/langfuse-prompt-release-tools) 0.5.0a1. This independent release workflow complements Langfuse's official SDK, experiments, resolver and general-purpose skill.

## Select the release

Use the host, project, prompt names and deployment label already selected by the user. Continue within existing authorization. Read project credentials from the environment or secret manager; never print or save them in prompts, reports or commands.

Confirm installed CLI arguments with `--help`. The ten commands and their source modules are listed in the repository's PACKAGING.md. Core commands require Python 3.10+; the optional official SDK example requires 3.11+.

Staging advances `latest`. Account for consumers of that label. Explicit tags affect every version.

## Execute the release

1. Export the explicit baseline with `langfuse-release-pull` into a new private directory. Edit the prompt files and review their diff.
2. Review the application's independent policy: exact dataset cases and hashes, task/evaluator revisions, model/config identity, typed score thresholds and whether remote persistence is required. Bind it before staging with `langfuse-release-policy`. Never weaken policy to make a result pass.
3. Preview and apply staging with `langfuse-release-publish`. Review every changed target and created version. The interface-change guard is not a model-quality or JSON Schema test.
4. Capture a dependency lock with `langfuse-release-dependencies`, including SDK/compiler identity. Use the server resolver; do not invent another prompt language. Lock even dependency-free roots.
5. Run the application's actual task with the official SDK or Experiment Action. Initialize execution capture before the run with the manifest, stage and lock. Fetch exact staged versions inside the task, record returned objects and resolved hashes before evaluating their outputs. Never reconstruct a supposed execution by adding version numbers afterward.
6. Import original results and capture with `langfuse-release-import-experiment`. Require every pinned case/check and preserve typed scores. Distinguish complete local results from remote trace/score persistence; `flush()` alone does not establish persistence.
7. Preview `langfuse-release-promote` with the evaluation receipt, same dependency lock and explicit label. Apply within the selected authorization. Policy-pinned snapshots cannot bypass gates through direct publication, legacy receipts, structural-only mode or a missing lock. Preserve all input evidence through the final verification.
8. Capture real post-promotion SDK accesses using `capture_sdk_observation`, with consumer/deployment/request identity and actual cache/fallback metadata. Record/check adoption using `langfuse-release-adoption` and an independently supplied expected consumer set. A label GET does not prove consumer adoption. A receipt covers observed accesses, not an entire fleet.

See docs/experiment-evidence.md, docs/dependencies-and-adoption.md and docs/sdk-release-example.md for schemas and runnable examples. The deterministic example makes no LLM calls and must not be presented as model evaluation.

## Handle interruptions

Run `langfuse-release-reconcile` against the saved attempt. It performs only GETs and writes a separate private report. A lost POST with matching remote content remains unknown; do not retry the creation automatically. A known label target can be observed as completed without proving which writer moved it.

A partial batch can retain earlier changes. Read per-item outcomes and preserve unknown-write boundaries. Do not automatically roll back, delete, or resume conflicting operations. Explicitly reviewed promotion recovery can recognize already-promoted targets without repeating PATCH; final evidence and dependency checks still run.

Coordinate writers. The server API has no transaction or compare-and-swap for this workflow, and local reads cannot eliminate concurrent changes. Hashes are unsigned evidence from trusted producers, not authenticated attestations or an authorization boundary.

## Legacy compatibility

Unpinned legacy exports still support the old recorder and explicit direct/structural modes. Never use those modes to bypass a selected policy or failed experiment. A legacy promotion without its own pre-promotion lock binding cannot establish verified adoption. See docs/migration-v05.md.

Report created versions, promoted labels, failed checks, reconciliation classifications and observed consumer outcomes separately. Include remaining unknowns; never equate stored content, application contract checks, model quality and audience outcomes.
