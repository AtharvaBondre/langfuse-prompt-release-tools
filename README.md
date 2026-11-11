# Langfuse prompt release tools

Test a staged prompt, promote its label, then check what the application actually used.

**Independent project by Atharva Bondre.** Version **0.5.0a1** adds a release verification workflow around Langfuse's official SDK, experiments and prompt resolver. It grows the [original Gist](https://gist.github.com/AtharvaBondre/3ad9d0654b5450d1e007728f67047fa0); it is not an official Langfuse product.

## What this adds

Langfuse supplies prompt management, experiments, SDKs, CLI, MCP and skills. This project connects those capabilities to a reviewed release:

- Pin required cases, scores and thresholds **before** staging and execution.
- Import official Experiment Action v1 results, or SDK results projected into that format, with task-time prompt identity and content capture.
- Bind the complete resolved dependency graph to the experiment; reject nested label drift before promotion writes.
- Reconcile interrupted operations by GET, retaining unknown creation outcomes without automatically retrying them.
- Record actual application SDK accesses after promotion, distinguishing a stale cached prompt or fallback from an observed adoption.

These are unsigned records from a trusted runner. They do not authenticate execution, guarantee better answers or prove that an entire fleet has adopted a release.

## Install

The ten CLI commands require Python 3.10+ and have no mandatory runtime dependencies. The optional SDK example requires Python 3.11+.

```bash
git clone https://github.com/AtharvaBondre/langfuse-prompt-release-tools.git
cd langfuse-prompt-release-tools
python3 -m venv .venv
.venv/bin/python -m pip install .
# Optional real SDK example:
.venv/bin/python -m pip install '.[example]'
```

Windows virtual environments use `Scripts` instead of `bin`. See [all commands](PACKAGING.md).

## Release with a pinned policy

Select the host, project, prompt names and deployment label. Supply `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` through the environment or a secret manager. Keep exports and execution evidence private.

**Staging advances `latest`.** Other deployment labels remain unchanged during staging. Explicit tags apply to all versions.

```bash
langfuse-release-pull --host "$LANGFUSE_HOST" --label production \
  --name example/answer --out-dir ./release-review

# Edit the exported prompt. Write and review policy.json for the actual app.
langfuse-release-policy --manifest ./release-review/prompt-index.json \
  --policy ./policy.json

langfuse-release-publish --host "$LANGFUSE_HOST" \
  --manifest ./release-review/prompt-index.json --report preview.json
langfuse-release-publish --host "$LANGFUSE_HOST" \
  --manifest ./release-review/prompt-index.json --apply --report stage.json

langfuse-release-dependencies --host "$LANGFUSE_HOST" \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json \
  --sdk 'langfuse-python/5.2.0' --compiler 'your-app/reviewed-revision' \
  --report dependencies.json
```

Run the actual application experiment now. Initialize its capture with the manifest, stage and dependency lock; fetch each exact staged version inside the task; record the returned object before scoring the output. The [evidence guide](docs/experiment-evidence.md) defines the policy and capture contract. The [working SDK example](docs/sdk-release-example.md) demonstrates a complete local run without model calls.

```bash
langfuse-release-import-experiment \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json \
  --results ./result.json --capture ./capture.json --report evaluation.json

# Preview first. Add --apply to perform the selected label move.
langfuse-release-promote --host "$LANGFUSE_HOST" \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json --label production \
  --evaluation-report ./release-review/reports/evaluation.json \
  --dependency-lock ./release-review/reports/dependencies.json \
  --report promotion-preview.json
```

Keep all source files unchanged through promotion. Policy-pinned snapshots reject direct publication, structural-only promotion, legacy evaluation receipts and a missing or substituted dependency lock. Every changed target and required case must pass. A failed partial batch can retain earlier label moves; inspect its per-item report.

After promotion, instrument the actual consumer with `capture_sdk_observation()` and record/check its accesses with `langfuse-release-adoption`. Required consumer/deployment identities and maximum observation age are supplied independently. [Dependency and adoption guide](docs/dependencies-and-adoption.md).

## Interrupted operations

```bash
langfuse-release-reconcile --host "$LANGFUSE_HOST" \
  --manifest ./release-review/prompt-index.json \
  --release-report ./release-review/reports/stopped.json
```

Reconciliation makes no mutation. It reports completed, pending, conflicting or unknown outcomes. A matching version after a lost POST is only a candidate, not proof that the attempt created it. [Recovery details](docs/reconciliation.md).

## Validation and limits

The [validation record](docs/validation.md) separates offline tests, installed-package tests and the real local Langfuse 4.78.0 / Python SDK 5.2.0 integration. The local example checks failed evaluation, nested dependency drift, response-loss recovery and actual cached/fresh SDK accesses. It makes no model-quality or remote trace-persistence claim.

Langfuse's API has no release transaction or compare-and-swap across this workflow. Fresh checks cannot eliminate concurrent-writer races. Coordinate writers. Dependency tags outside the supported documented syntax fail closed; this tool uses the server resolver and does not implement another prompt renderer. An adoption receipt covers the instrumented accesses supplied to it.

Legacy unpinned exports remain supported through the [legacy result format](EVALUATION.md). See [0.5 migration](docs/migration-v05.md) before upgrading automation.

- [Roadmap and implemented scope](docs/roadmap.md)
- [Official platform contracts](docs/official-platform-contracts.md) and [official tools](docs/official-agent-tools.md)
- [Agent skill](skills/langfuse-prompt-release/SKILL.md)
- [Security](SECURITY.md), [contributing](CONTRIBUTING.md), [upstream notices](THIRD-PARTY-NOTICES.md)
