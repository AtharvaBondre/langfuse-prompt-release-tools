---
name: langfuse-prompt-release
description: Review and stage Langfuse prompt edits, bind actual application-check results to exact versions, and promote a deployment label with drift checks and write reconciliation.
---

# Release reviewed Langfuse prompts

Use the commands from [langfuse-prompt-release-tools](https://github.com/AtharvaBondre/langfuse-prompt-release-tools). Version 0.4.0a1 stages by default and requires evaluation evidence for promotion. This skill is independent of Langfuse's official general-purpose `langfuse` skill and can be used alongside it.

## Select the release

Use the host, project, prompt names and deployment label the user selected. Continue within their existing authorization; do not infer authority to change another target. Read project keys from `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, keeping their values out of commands, reports and messages.

Confirm which installed commands are available with `--help`. Source scripts and the five `langfuse-release-*` entrypoints run the same functions. Installation and full examples are in the [README](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/blob/main/README.md).

Creation always advances `latest`. Account for consumers of that label before staging. Explicit tags affect all versions. A stage is not invisible to all applications.

## Review, stage, evaluate, promote

1. Export the selected label and explicit names into an empty private directory with `langfuse-release-pull`. Keep `prompt-index.json` intact; edit the prompt files.
2. Preview with `langfuse-release-publish --manifest <manifest> --report <new-name>`. Review the diff, baseline and contract changes. `--allow-contract-change` is for an intended reviewed interface change, not a way to bypass unexplained drift.
3. Apply the reviewed stage with `--apply`, optionally selecting the first prompt with `--canary`. Review per-item versions and status before proceeding. Canary readback checks stored content, not model quality.
4. Run the application's checks against the **exact versions in the completed stage report**. Use the real consuming SDK where compilation, child dependencies or caching matter. Preserve its source results.
5. Record those results with `langfuse-release-evaluate --manifest <manifest> --stage-report <stage> --results <source-results> --require-check <id> --report <new-name>`, repeating required check IDs. See the [result schema](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/blob/main/EVALUATION.md). The recorder runs no commands or models. Never invent a check, replace a failure with a fixture pass, or call a JSON-shape check an answer-quality evaluation.
6. Preview `langfuse-release-promote` with the manifest, stage report, chosen `--label` and `--evaluation-report`. Apply within the selected scope with `--apply`. Use a fresh report filename for each attempt, then run `langfuse-release-verify` against the completed promotion report.

Keep the snapshot, edited prompts, stage report and original result file unchanged through promotion. Receipts bind their hashes, versions and required checks; they are unsigned attestations from a trusted runner, not proof of execution or an authorization boundary.

The contract guard compares variable sets, placeholders and `OUTPUT_SCHEMA` blocks. It does not validate JSON Schema or freeze dependencies referenced by movable labels. Label readback does not establish cache refresh or application adoption.

## A stopped write

A batch can stop after earlier items succeeded. Read the per-item outcomes and reconcile remote state before another write. A timeout or malformed response can follow a completed mutation.

For an uncertain promotion, an unchanged valid receipt and a matching remote target let the promoter recognize prior completion without issuing that PATCH again. For an uncertain creation, inspect remote versions first; POST is not idempotent and rerunning can create a duplicate. Do not automatically roll back, delete or continue a conflicting batch.

Coordinate a single writer. Preflight and per-item checks cannot eliminate the remote GET-to-write race. A disk error can also leave the newest outcome unrecorded. Stop on unresolved outcomes instead of interpreting a missing receipt as proof that nothing happened.

## Explicit compatibility modes

`--allow-direct-release` creates with baseline non-`latest` labels, bypassing separate evaluation and promotion. `--allow-structural-only` allows promotion without evaluation; any supplied receipt is still validated. Do not add either option to get past failed checks. Use one only when the user intends that release behavior and the target scope is already authorized. [Migration details](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/blob/main/docs/migration-v03.md).

Report created versions, promoted labels, unchanged/skipped items, required checks and unresolved outcomes. Distinguish stored-content verification, application tests and model-quality evaluation. Keep prompts and receipts private.
