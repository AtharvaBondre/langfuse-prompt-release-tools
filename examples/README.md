# Release examples

Start with [getting started](../docs/getting-started.md) and the
[current 0.6 SDK walkthrough](../docs/sdk-release-example.md). Choose the example
that matches the behavior you want to inspect:

| Example | Purpose |
| --- | --- |
| `verified_release_demo.py` | Current workflow: policy v2, Python/JavaScript experiments, independent remote persistence, dependency locks, promotion, bundles and rollout observations |
| `sdk_release_demo.py` | Older policy-v1 Python baseline, including interrupted-release recovery; remote export remains unconfirmed |
| `check_captured_outputs.py` | Legacy unpinned snapshots: check response strings that an application already captured |

## Current 0.6: remote evidence, JavaScript and rollout windows

`verified_release_demo.py` runs actual official SDK experiments against a
disposable loopback Langfuse project. It retains real synthetic task results,
checks persisted cases and typed scores, and demonstrates both a passing release
and a failing JavaScript evaluator that stays blocked. It also observes
cached/fresh/fallback accesses and a two-consumer time window. No model is called.

From the repository root, use Python 3.11+, Node 22+ and a separately prepared
disposable local server. Install the Python example extra into your environment
and the locked JavaScript dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[example]'
npm ci --prefix js --ignore-scripts

# Supply the disposable project's keys in the environment and independently
# select its project ID before running this example.
.venv/bin/python examples/verified_release_demo.py \
  --host http://127.0.0.1:13830 --project-id "$LANGFUSE_PROJECT_ID" \
  --out-dir ./verified-demo-evidence --disposable-local
```

The output directory must be new. A complete run writes `summary.json` with
`status: passed`; per-run evidence remains available for inspection. The script
writes synthetic prompts and scores to the disposable project. The
[walkthrough](../docs/sdk-release-example.md) explains expected failures and the
[validation record](../docs/validation.md#tested-compatibility) lists tested
SDK/server/architecture combinations. The local Compose template is ARM64-specific;
the complete recorded 0.6 matrix ran on Linux AMD64.

To adapt the task, capture the actual SDK prompt immediately after fetching it,
before compiling or converting it for a framework. Follow the
[evidence guide](../docs/experiment-evidence.md) for reviewed policy v2, result
projection and remote receipts; use the [JavaScript helper](../js/README.md) for
that SDK's capture methods. The examples are in the source checkout and source
archive, not installed as commands by the Python wheel.

## Legacy: check captured application responses

This small source-checkout example turns **already-captured response strings** into the application-results format consumed by the release gate. It computes two checks: `response_json` and `nonempty_answer`. It makes no model, API or shell calls, and is not an evaluator engine or an installed wheel command.

Your application or trusted capture process must record the snapshot hash, stage-report hash, exact prompt name/version and response string when it obtains that response. The example validates these existing bindings; it never attaches current version IDs to unrelated outputs after the fact.

## Input contract

Use a private working directory. A capture file has one row for **every changed staged target**, with no duplicates or extra names:

```json
{
  "format_version": 1,
  "snapshot_sha256": "<hash recorded by your capture process>",
  "stage_report_sha256": "<hash recorded by your capture process>",
  "outputs": [
    {
      "name": "example/answer",
      "version": 8,
      "output": "{\"answer\":\"A captured application response.\"}"
    }
  ]
}
```

The placeholder hashes and example version are illustrative, not runnable evidence. Use the runtime helpers' canonical `fingerprint` function for the parsed stage report and the **loaded** snapshot returned by `load_snapshot`; that snapshot includes reviewed local prompt content. See [the binding format](../EVALUATION.md). Capture those identities before collecting the response, and retain them with its exact version. Never replace old hashes merely to make this checker accept a stale capture.

Captured target identity is trusted, not attested. A source author can lie about which prompt produced an output or recompute the hashes. These checks neither prove an application/model execution happened nor assess correctness, groundedness or model quality.

## Run from the source checkout

With a completed stage report and a genuine captured-output file:

```bash
python -m examples.check_captured_outputs \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json \
  --captured ./release-review/captured-outputs.json \
  --out ./release-review/app-results.json

python evaluate_langfuse_release.py \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json \
  --results ./release-review/app-results.json \
  --require-check response_json --require-check nonempty_answer \
  --report evaluation.json
```

Use the resulting evaluation receipt only for a legacy unpinned snapshot's
promotion workflow. It cannot satisfy a snapshot with a pinned experiment policy.
A failed receipt blocks promotion by default. A passing receipt establishes only
that these two checks passed for the trusted captured strings bound to the stage.

`response_json` requires parseable JSON with finite numbers and unique object members. `nonempty_answer` requires a JSON object whose `answer` field is a string containing at least one non-whitespace character. Prose fails both. `{"answer":""}` parses but fails the answer check. A true but incorrect answer can pass both.

Exit codes are `0` when all computed checks pass, `1` when valid captures produce failed checks, and `2` for invalid bindings, types or paths. Code `1` still writes app-results so the recorder can retain failed evidence; codes caused by input validation create no output. Never chain these commands assuming that a failed check means the results file is absent.

Stale snapshot/stage hashes, wrong versions, missing/duplicate/extra targets and non-string outputs are rejected before writing. The output's parent directory must already exist. The checker creates a new output with mode `0600` on POSIX and exclusive creation; it refuses to overwrite any existing file, including the capture input. POSIX modes do not establish Windows ACL privacy. A filesystem failure can leave a partial output; inspect it and choose a new filename rather than overwriting evidence.

The output retains `captured_source_sha256`, the canonical hash of the captured input object, as provenance. It omits the response strings. The evaluation recorder accepts this optional field and includes it in the source-results hash; it does **not** reopen or authenticate the original capture file. Keep captures and results privately, and do not put sensitive response text into check details.

## Offline regression checks

```bash
python -m unittest -v test_captured_outputs
```

Only the test module imports mocked stage fixtures. The example itself imports runtime helpers and reads local files. Tests demonstrate failed output blocking the gate, passing output allowing promotion in the in-memory service, rejection of stale/malformed captures, and preservation of existing evidence. They do not call Langfuse or a model.
