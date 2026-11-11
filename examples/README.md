# Check captured application responses

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

Use the resulting evaluation receipt in the normal [promotion workflow](../README.md). A failed receipt blocks promotion by default. A passing receipt establishes only that these two checks passed for the trusted captured strings bound to the stage.

`response_json` requires parseable JSON with finite numbers and unique object members. `nonempty_answer` requires a JSON object whose `answer` field is a string containing at least one non-whitespace character. Prose fails both. `{"answer":""}` parses but fails the answer check. A true but incorrect answer can pass both.

Exit codes are `0` when all computed checks pass, `1` when valid captures produce failed checks, and `2` for invalid bindings, types or paths. Code `1` still writes app-results so the recorder can retain failed evidence; codes caused by input validation create no output. Never chain these commands assuming that a failed check means the results file is absent.

Stale snapshot/stage hashes, wrong versions, missing/duplicate/extra targets and non-string outputs are rejected before writing. The output's parent directory must already exist. The checker creates a new output with mode `0600` on POSIX and exclusive creation; it refuses to overwrite any existing file, including the capture input. POSIX modes do not establish Windows ACL privacy. A filesystem failure can leave a partial output; inspect it and choose a new filename rather than overwriting evidence.

The output retains `captured_source_sha256`, the canonical hash of the captured input object, as provenance. It omits the response strings. The evaluation recorder accepts this optional field and includes it in the source-results hash; it does **not** reopen or authenticate the original capture file. Keep captures and results privately, and do not put sensitive response text into check details.

## Offline regression checks

```bash
python -m unittest -v test_captured_outputs
```

Only the test module imports mocked stage fixtures. The example itself imports runtime helpers and reads local files. Tests demonstrate failed output blocking the gate, passing output allowing promotion in the in-memory service, rejection of stale/malformed captures, and preservation of existing evidence. They do not call Langfuse or a model.
