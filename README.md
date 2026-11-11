# Langfuse prompt release tools

Test a staged prompt version before moving a deployment label.

These Python commands export a prompt baseline, preview local changes, create staged versions, record application-check results and promote one label with readback. A receipt binds the reviewed files, exact versions and supplied results so changed or failed evidence blocks promotion.

**Independent project by Atharva Bondre. Not an official Langfuse product.** This repository grows the [original public Gist](https://gist.github.com/AtharvaBondre/3ad9d0654b5450d1e007728f67047fa0). The first repository release is **0.4.0a1**, an alpha with stricter defaults.

## Why use it alongside Langfuse?

Langfuse already provides prompt versions and labels, datasets and experiments, SDKs, a CLI, MCP tools and agent skills. This project uses its prompt APIs for a narrower workflow:

- Review a local change against an exported baseline before creating anything.
- Check application results for the exact staged versions before moving a label.
- Refuse stale snapshots, changed evidence and conflicting label targets.
- Retain per-item outcomes when a batch stops, and recognize an already-completed promotion after an uncertain response.

It does not run your application or judge answers. Results are unsigned attestations from a runner you trust. A passing JSON-format check does not establish answer quality.

The [research and expansion plan](docs/roadmap.md) explains what to reuse from Langfuse and what to build here. See the [platform contracts](docs/official-platform-contracts.md) and [official agent tools](docs/official-agent-tools.md) for dated source references.

## Install

Python 3.10 or later; no required runtime dependencies.

```bash
git clone https://github.com/AtharvaBondre/langfuse-prompt-release-tools.git
cd langfuse-prompt-release-tools
python3 -m venv .venv
.venv/bin/python -m pip install .
```

The five installed commands are `langfuse-release-pull`, `langfuse-release-publish`, `langfuse-release-evaluate`, `langfuse-release-promote` and `langfuse-release-verify`. The examples below use the equivalent source scripts. Windows virtual environments use `Scripts` instead of `bin`.

## Release a prompt

Start in a disposable Langfuse project. Supply `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` through your environment or secret manager, and set `LANGFUSE_HOST` to that project's deployment URL. Keep generated prompts, outputs and receipts private.

**Staging advances `latest`.** Other deployment labels stay on their current versions, but an application using `latest` can see the staged change. Explicit tags affect all versions.

```bash
# Export one prompt baseline into an empty private directory.
python3 pull_langfuse_prompts.py --host "$LANGFUSE_HOST"   --label production --name example/answer --out-dir ./release-review

# Edit the prompt file, preserving prompt-index.json. Preview first.
python3 publish_langfuse_prompts.py --host "$LANGFUSE_HOST"   --manifest ./release-review/prompt-index.json --report stage-preview.json

# Create staged versions; no deployment-label move.
python3 publish_langfuse_prompts.py --host "$LANGFUSE_HOST"   --manifest ./release-review/prompt-index.json --apply   --canary example/answer --report stage.json
```

Run your application's checks against the exact versions recorded in `stage.json`. Use the [result format](EVALUATION.md) to capture actual results in `app-results.json`. Required check IDs belong to your application; choose checks that cover the intended change. A [small captured-output example](examples/README.md) computes JSON and nonempty-answer checks from already recorded outputs; it makes no model or tenant calls.

```bash
python3 evaluate_langfuse_release.py   --manifest ./release-review/prompt-index.json   --stage-report ./release-review/reports/stage.json   --results ./app-results.json   --require-check response_json --require-check required_fields   --report evaluation.json

# Evidence is required by default, including for previews.
python3 promote_langfuse_prompts.py --host "$LANGFUSE_HOST"   --manifest ./release-review/prompt-index.json   --stage-report ./release-review/reports/stage.json --label production   --evaluation-report ./release-review/reports/evaluation.json   --report promotion-preview.json

python3 promote_langfuse_prompts.py --host "$LANGFUSE_HOST"   --manifest ./release-review/prompt-index.json   --stage-report ./release-review/reports/stage.json --label production --apply   --evaluation-report ./release-review/reports/evaluation.json   --report promotion.json

python3 verify_langfuse_prompts.py --host "$LANGFUSE_HOST"   --manifest ./release-review/prompt-index.json   --report ./release-review/reports/promotion.json
```

Keep the snapshot, prompt files, stage report and source results unchanged through promotion. Review interface changes before using `--allow-contract-change`. The contract guard compares variables, placeholders and `OUTPUT_SCHEMA` blocks; it does not validate JSON Schema or run a prompt compiler.

## Limits to account for

- There is no remote transaction, compare-and-swap or distributed lock. Coordinate writers; another process can change a label between a read and a write.
- A timeout can follow a successful write. Inspect the saved report and remote state before retrying. POST is not automatically retried; an unknown creation can otherwise produce a duplicate version.
- Raw prompt-version binding does not freeze child dependencies referenced by movable labels. Test the resolved prompt with the SDK your application uses.
- Label readback does not prove that application caches have refreshed or that a new answer is better.
- The checks are workflow controls, not a server-side authorization boundary. An authorized client can use Langfuse's API directly. Explicit legacy opt-outs also exist; see [migration notes](docs/migration-v03.md).

## Validate and contribute

```bash
python3 -m unittest discover -v -p 'test_*.py'
```

The portable suite uses synthetic prompts and mocked HTTP. [Validation notes](docs/validation.md) separate current offline/build checks from the **17 October 2026** disposable integration evidence. No current live-project or model-quality claim is implied.

- [Design and failure handling](DESIGN-NOTES.md)
- [Installable agent skill](skills/langfuse-prompt-release/SKILL.md)
- [Packaging](PACKAGING.md) and [contributing](CONTRIBUTING.md)
- [Disposable local demo](LOCAL-DEMO.md) and [dated demo results](DEMO-RESULTS.md)
- [Security and data handling](SECURITY.md), [license](LICENSE), [upstream notices](THIRD-PARTY-NOTICES.md)
