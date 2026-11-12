# Read a release without opening every report

Use `langfuse-release-status` to answer three separate questions: did the evaluation pass, did a promotion report record the label move, and what did the instrumented application accesses return?

The command reads the saved release directory. It does not change release files, run scripts from reports, contact Langfuse or verify CI signatures. It cannot tell you that a historical label observation still describes the server now.

## Inspect a release directory

```bash
langfuse-release-status --workspace ./release-review
```

The directory must contain `prompt-index.json` and its prompt files. The command looks for recognized JSON reports in that directory and its immediate `reports/` subdirectory. You can also start with an explicit snapshot:

```bash
langfuse-release-status --manifest ./release-review/prompt-index.json
```

Each section includes its source path and any missing evidence or failed consistency check. For automation, use the versioned JSON output:

```bash
langfuse-release-status --workspace ./release-review --format json
```

`--json` is an alias. JSON goes to stdout; the command has no output-file option. If you use shell redirection, choose a file outside the release inputs.

## Select the intended attempt

A directory can contain several previews, failed runs or completed releases. The inspector reports an ambiguous selection rather than choosing whichever file has the newest timestamp. Select the intended files:

```bash
langfuse-release-status --workspace ./release-review \
  --stage-report reports/stage.json \
  --evaluation-report reports/evaluation.json \
  --dependency-lock reports/dependencies.json \
  --promotion-report reports/promote.json \
  --remote-report remote.json \
  --adoption-report adoption.json
```

Use the paths from your run. Relative paths are relative to the selected workspace, including references inside reports. Absolute paths are allowed within that workspace. Files and directories must not use symlinks. Referenced source results, capture and receipts must also remain inside the workspace; the command does not follow an artifact to unrelated files or rewrite old locators. It is not a replacement for [portable bundle verification](portable-evidence.md).

If you have window-based rollout evidence, select its original policy and window as well:

```bash
langfuse-release-status --workspace ./release-review \
  --rollout-report rollout.json \
  --rollout-window rollout-window.json \
  --rollout-policy rollout-policy.json
```

The inspector can recompute unsigned local rollout coverage. A report requiring provenance verification remains `unverified` here, with a direction to use the separate rollout verifier and its trust policy. This command never starts `gh` or trusts a saved `verified_ci` string as proof.

## Understand the result

| Section | What the inspector checks | How to read it |
|---|---|---|
| Project, host and label | Snapshot selector and independently pinned remote-policy project ID, when present. | `not recorded` means the project ID is absent. Displaying it does not establish project binding before every mutation. |
| Stage | Current local prompt content, snapshot identity, selected targets and stage result shape. | `recorded_verified` is a consistent saved stage report, not a fresh server check. A supported subset stays valid; `unselected_snapshot_prompts` lists exported prompts outside that stage attempt. An unchanged selection reports `recorded_unchanged` and `no_changes_to_promote`. |
| Evaluation | Report digest, original result/capture files, policy and dependency bindings; recomputed required checks. | `passed` and `failed` describe the saved execution. An edited passing flag cannot substitute for the original results. |
| Remote persistence | Expected project/execution/cases, saved receipt consistency and age against the pinned limit. | A previously `confirmed` receipt can be `stale` now. Rerun remote verification before relying on it for another promotion. |
| Dependencies | Lock integrity and exact snapshot/stage/target bindings. | `recorded_locked` means the saved lock is consistent. `current_remote_drift` remains `not_checked`. |
| Promotion | Exact staged targets, label, evaluation/lock references and completion state. | `recorded_promoted` describes the saved attempt. `preview_only` is no label move; `uncertain_do_not_retry` needs reconciliation. |
| Adoption | Recomputed SDK observations, release bindings, required consumers, content/version and age. | `observed_adopted` covers supplied instrumented accesses. A cached old version or missing consumer produces `not_adopted`. |
| Rollout | Saved policy/window/sample bindings and recomputed consumer/time coverage. | `complete` applies to the chosen window and supplied samples; stale evidence stays visible. |

`recorded_status` preserves what the artifact said. `state` reports the inspector's current local assessment. For example, an adoption receipt may retain `recorded_status: verified` while inspection reports `state: stale`. Evaluation can remain historically passed after a remote receipt ages out; the next step then asks for fresh remote verification.

Digests detect changed content and mismatched records. They do not authenticate who created the files or prove an application ran honestly. Each selected artifact has a `canonical_sha256` for its canonical JSON content, not its original byte encoding.

## Missing data and exit codes

- Missing reports remain `missing`; a changed label never fills in an absent adoption result.
- Several matching artifacts produce `ambiguous`, with candidate paths. Select the intended attempt explicitly.
- Malformed JSON, duplicate keys, mismatched evidence or future timestamps produce `invalid` or a visible candidate warning.
- A valid failed evaluation stays `failed`, with the failed check IDs, rather than being treated as malformed evidence.

Exit **0** means the command inspected the available inputs without malformed or ambiguous evidence. A valid failure or missing report can still produce exit 0. It is **not a release approval**. Exit **1** means an input/consistency error, ambiguous artifact selection or unreadable candidate needs attention. Argument errors use exit **2**.

The top-level JSON `status` is `attention` when the inspector has a next action, otherwise `inspected`; neither value means production approval. Use the existing evaluation, remote, promotion and adoption commands for their release decisions.

## Integration path

Run the inspector after the [guided release example](getting-started.md#3-run-a-release-with-your-own-app). Keep its source paths with your release notes so another reviewer can inspect the original evidence. For the checks and expected limits behind those files, see [experiment evidence](experiment-evidence.md), [remote persistence](remote-persistence.md), [recovery](reconciliation.md) and [rollout observation](rollout-observation.md).
