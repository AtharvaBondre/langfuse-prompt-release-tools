# Get a prompt release working

Start with the demo to see the reports. Then add capture to one application task and release one prompt. You can add more prompts, evaluators and consumers after that first run.

## 1. Run the working example

The current example exercises Python and JavaScript against a real, disposable Langfuse server. It creates synthetic prompts, runs deterministic tasks, verifies stored results, checks dependency changes and observes app accesses. You need no model-provider account.

### Use GitHub Actions

This is the shortest path to the complete tested example:

1. Fork [this repository](https://github.com/AtharvaBondre/langfuse-prompt-release-tools).
2. Enable Actions in your fork and open **Actions → Live SDK compatibility**.
3. Choose **Run workflow** on `main`.
4. Open the completed run and download `live-sdk-4.59.0-linux-amd64` and `live-sdk-4.78.0-linux-amd64` under **Artifacts**.
5. Read `summary.json` for versions and outcomes, and `sanitized.log` for the execution details.

The workflow creates credentials for its disposable servers and cleans up its containers. You do not add existing Langfuse credentials or model keys. GitHub Actions usage follows your account's runner limits. Artifacts expire after seven days; download a run you want to keep.

You can inspect a [recorded successful run](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43026237620) first. The [committed validation record](validation.md) retains the results after downloadable artifacts expire.

| Demo event | Result to look for | Use in your application |
|---|---|---|
| JavaScript returns the wrong answer. | The score persists, the evaluation fails, and promotion leaves the baseline label in place. | Keep a failed quality check separate from a successful export. |
| A nested prompt changes after evaluation. | Promotion refuses the old dependency evidence. | Review the new dependency and rerun the evaluation. |
| The app reads a cached prompt after promotion. | The observation differs from the released version. | Check cache behavior before declaring a consumer updated. |
| Two consumers fetch the released version across the chosen window. | The rollout report meets the demo's coverage policy. | Choose consumers and thresholds that reflect your deployment. |

The recorded matrix passed 24 checks on 4.59.0 and 38 on 4.78.0. The latter includes 14 earlier release/recovery checks. Both run on Linux AMD64 with Python SDK 5.2.0 and JavaScript SDK 6.1.1. These synthetic checks demonstrate release behavior; you still supply the quality tests for your own task.

### Use a local Docker instance

Follow [LOCAL-DEMO.md](../LOCAL-DEMO.md) to generate a new demo directory, check your local Docker context and start the stack. Its pinned images target Linux ARM64. The recorded complete 0.6 matrix uses the GitHub AMD64 workflow above; the local ARM64 run did not complete that matrix because of database/Redis timeouts.

From the repository root, install the example dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[example]'
npm ci --prefix js --ignore-scripts
```

Use Python 3.11+ and Node 22+. Once the stack is ready, keep the `DEMO_DIR` from the local setup and run:

```bash
env -i HOME="$HOME" PATH="$PATH" sh -c '
  set -a
  . "$1/runtime.env"
  set +a
  exec "$2/bin/python" examples/verified_release_demo.py \
    --host http://127.0.0.1:13830 \
    --project-id "$LANGFUSE_INIT_PROJECT_ID" \
    --out-dir "$1/verified-run" --disposable-local
' sh "$DEMO_DIR" "$(pwd)/.venv"
```

Choose a new output directory for each run. The script creates prompts and experiments in that disposable project. Follow the local guide's stop/cleanup steps when you finish.

## 2. Choose your first application check

Pick one prompt and a failure you can evaluate. For a support assistant, you might require valid JSON and a correct account identifier. For RAG, you might test whether the answer cites a retrieved source. You choose the evaluator and threshold; this project does not supply those application judgments.

Prepare these inputs before staging:

| Input | Where you get it |
|---|---|
| Prompt name and starting deployment label | The existing prompt in your Langfuse project. |
| Dataset cases and expected outputs | Your reviewed local cases or selected Langfuse dataset version. |
| Task and evaluator revisions | The actual source revision or digest you will execute. |
| Model/provider configuration | The actual configuration your task will use; deterministic tasks can record that they use no model. |
| Required checks and thresholds | Your acceptance criteria, chosen before seeing the candidate's scores. |
| Remote persistence requirement | Policy v2, your project ID, dataset kind and maximum receipt age. |

The small [`policy_for()` example](../examples/sdk_experiment.py) builds a complete policy from its real cases and source fingerprints. [`verified_release_demo.py`](../examples/verified_release_demo.py) upgrades it to policy v2 and requires remote persistence. Use those as construction examples: replace the task, cases, revisions and checks together. Copying the demo's passing scores or identities would describe its task, not yours.

Read the [policy fields and capture contract](experiment-evidence.md) while adapting your producer. Use the [policy-draft helper](policy-setup.md) to compute fingerprints from your selected cases, source files and explicit requirements. After step 3 exports the prompts, generate and review the plain policy object as `release-review/policy.json`. The helper does not choose acceptance thresholds or pin the draft for you.

## 3. Run a release with your own app

These commands use one existing prompt called `example/answer` and the `production` label. Substitute your selections. Run from the repository root with the virtual environment active. Configure `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and your independently selected `LANGFUSE_PROJECT_ID` through your secret manager or private shell environment.

Select the project independently from its settings. Export, preview, stage and promotion verify the current key against that project. Native Langfuse permissions still apply. Format 1 snapshots need a fresh export; see [0.8 migration](migration-v08.md).

### Export and review

```bash
langfuse-release-pull --host "$LANGFUSE_HOST" --project-id "$LANGFUSE_PROJECT_ID" --label production \
  --name example/answer --out-dir ./release-review
```

Open `release-review/prompt-index.json`. Edit the prompt file named in its `prompts[].file` entry. Keep the recorded baseline unchanged. A fresh export needs a new directory. Use a label-based export for this release workflow; exports made with `--version` support review only.

Create and review `release-review/policy.json` using the inputs from step 2. Then pin it:

```bash
langfuse-release-policy --manifest release-review/prompt-index.json \
  --policy release-review/policy.json
```

Pinning adds the policy and its digest to the snapshot. Do this before staging. Choose a fresh export if you need to change the pinned requirements.

### Preview, then stage the candidate

```bash
langfuse-release-publish --host "$LANGFUSE_HOST" --project-id "$LANGFUSE_PROJECT_ID" \
  --manifest release-review/prompt-index.json --report stage-preview.json
```

Review the preview. To create the candidate, run:

```bash
langfuse-release-publish --host "$LANGFUSE_HOST" --project-id "$LANGFUSE_PROJECT_ID" \
  --manifest release-review/prompt-index.json --apply --report stage.json
```

Staging creates versions without moving your deployment labels. Langfuse's `latest` version advances, so apps using `latest` can see staged changes. Use an explicit deployment label for apps that must wait for promotion.

### Record dependencies before running the task

```bash
langfuse-release-dependencies --host "$LANGFUSE_HOST" \
  --manifest release-review/prompt-index.json \
  --stage-report release-review/reports/stage.json \
  --sdk langfuse-python/5.2.0 --compiler official \
  --report dependencies.json
```

Record the SDK/compiler your task uses. Create the lock even if the root has no nested prompts. Use that same file throughout capture, import and promotion.

### Add capture to your experiment

Adapt the [Python task](../examples/sdk_experiment.py) or [JavaScript example](../js/README.md). The integration points are:

1. Call `start_capture` before the experiment with the manifest, stage report, dependency lock and actual dataset/execution identities.
2. Inside each task, fetch the staged prompt by version through the official SDK.
3. Call `capture_prompt` with that returned SDK object before compilation or framework conversion. Then execute your task and evaluator.
4. Preserve the actual SDK results in the supported Action v1 JSON format, including case IDs, trace IDs, outputs and typed scores. Use the supported `project_python_result()` or `projectExperimentResult()` helper; see [capture integration](capture-integration.md).
5. Call `finalize_capture` after writing the result, and save the returned object.

Save those two files as `release-review/results.json` and `release-review/capture.json`. A `flush()` call leaves remote persistence unconfirmed until you read the exported data back. The official Experiment Action result format is also supported; it still needs capture from the task that fetched the candidate.

### Verify remote results and import the evaluation

For a v2 policy that requires remote persistence:

```bash
langfuse-release-remote --host "$LANGFUSE_HOST" \
  --project-id "$LANGFUSE_PROJECT_ID" --dataset-kind local \
  --results release-review/results.json --capture release-review/capture.json \
  --policy release-review/policy.json --report release-review/remote.json

langfuse-release-import-experiment --manifest release-review/prompt-index.json \
  --stage-report release-review/reports/stage.json \
  --results release-review/results.json --capture release-review/capture.json \
  --remote-report release-review/remote.json --report evaluation.json
```

Use `--dataset-kind langfuse` for a hosted dataset when that matches the pinned policy and actual experiment. The [hosted-dataset scenario](hosted-dataset-validation.md) exercises pinned versions, schemas, pagination and typed scores on a disposable server. See [remote verification](remote-persistence.md) for the API contract.

The remote command exits with `0` for confirmed, `2` for pending and `1` for failure. Stop on a nonzero result and read the report before continuing. A confirmed export can contain failing scores; the importer evaluates those against your policy. Missing cases, failed checks or stale evidence must be resolved before promotion.

### Preview and apply the label move

The following command explicitly accepts unsigned local evidence. For a CI release, use [trusted promotion](trusted-promotion.md), which verifies a signed bundle and external signer policy again at this boundary.

```bash
langfuse-release-promote --allow-unsigned-local --host "$LANGFUSE_HOST" --project-id "$LANGFUSE_PROJECT_ID" \
  --manifest release-review/prompt-index.json \
  --stage-report release-review/reports/stage.json --label production \
  --evaluation-report release-review/reports/evaluation.json \
  --dependency-lock release-review/reports/dependencies.json \
  --report promotion-preview.json
```

Read the preview. To apply it, repeat the command with `--apply` and `--report promotion.json`. Keep the two filenames distinct: reports refuse overwrites. The command rechecks evidence and reads the label back after moving it. If your remote receipt has expired, obtain a fresh receipt and import it into a new evaluation report before retrying promotion.

### Observe app use and share evidence

Capture the SDK object from real app accesses with the [adoption helper](dependencies-and-adoption.md), then compare it with the release. For several consumers, choose identities and coverage thresholds in a [rollout window](rollout-observation.md). A label readback alone does not tell you which version an app cache returned.

Create a bundle for a reviewer or another CI job:

```bash
langfuse-release-bundle create --manifest release-review/prompt-index.json \
  --stage-report release-review/reports/stage.json \
  --evaluation-report release-review/reports/evaluation.json \
  --dependency-lock release-review/reports/dependencies.json \
  --out release-review/release.zip
langfuse-release-bundle inventory --bundle release-review/release.zip
langfuse-release-bundle verify --bundle release-review/release.zip
```

This bundle carries the original test evidence. Keep promotion and adoption reports beside it in your release record. For CI handoffs, select the expected release ID and signer policy outside the bundle and follow [signature verification](portable-evidence.md). Hash checks alone do not authenticate a producer.

## 4. Find the output you need

The commands above produce this layout, alongside the prompt files listed in the manifest:

```text
release-review/
├── prompt-index.json       reviewed snapshot and pinned policy
├── policy.json             plain policy supplied to the remote verifier
├── results.json            actual experiment results
├── capture.json            prompt accesses during that experiment
├── remote.json             remote persistence receipt
├── release.zip             portable test evidence
└── reports/
    ├── stage-preview.json
    ├── stage.json
    ├── dependencies.json
    ├── evaluation.json
    ├── promotion-preview.json
    └── promotion.json
```

`--report` takes a **filename inside `reports/`** for publish, dependencies, import and promote. The remote verifier takes a **full output path**. Use a new name for each attempt and preserve prior evidence.

| You see | Read next |
|---|---|
| A failed evaluator or missing case | The evaluation report and source result; fix the task or test coverage. |
| A dependency mismatch | The lock and changed nested prompt; review and rerun capture/evaluation against the new graph. |
| Remote `pending` | The remote report's issues; allow export to finish or investigate the missing trace/score. |
| A connection loss during a write | [Reconciliation](reconciliation.md); inspect before retrying an uncertain creation. |
| A promoted label with `not_adopted` observations | The consumer version, caching, fallback and observation window. |

For format changes, use the [0.8 migration guide](migration-v08.md). For all command names and installation options, see [packaging](../PACKAGING.md).
