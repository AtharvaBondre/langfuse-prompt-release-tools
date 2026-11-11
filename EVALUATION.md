# Supply application-check results

Run the application's checks against the exact versions in `stage.json`. Capture those results before invoking `evaluate_langfuse_release.py`. The command records supplied results; it executes no shell commands, tests or models.

Choose required case IDs for the behavior you intend to release. For example, `response_json` can check whether a captured response parses, while `required_fields` can check its field names and types. Those deterministic checks measure output structure. Use separate evaluations for answer quality, groundedness or task success.

## Result file

Use this shape for `app-results.json`. This example illustrates a failed check; it is not a run result. Replace the hashes, name and version with your stage's values, and use your runner's actual checks.

```json
{
  "format_version": 1,
  "snapshot_sha256": "<computed snapshot hash>",
  "stage_report_sha256": "<computed stage report hash>",
  "results": [
    {
      "name": "example/answer",
      "version": 8,
      "checks": [
        {"id": "response_json", "passed": true},
        {
          "id": "required_fields",
          "passed": false,
          "detail": "Parsed response omitted the required summary field."
        }
      ]
    }
  ]
}
```

Compute the bindings using the same canonical JSON hash as the release helpers:

```python
import json
from pathlib import Path
from langfuse_release_common import fingerprint
from publish_langfuse_prompts import load_snapshot

_, snapshot = load_snapshot("release-review/prompt-index.json")
stage = json.loads(Path("release-review/reports/stage.json").read_text())
snapshot_sha256 = fingerprint(snapshot)
stage_report_sha256 = fingerprint(stage)
targets = {
    row["name"]: row["created_version"]
    for row in stage["results"] if row["changed"]
}
# Your application runner uses these targets and hashes when writing its results.
```

Include one result per changed staged prompt. Each `version` must match its `created_version`. Supply non-empty check lists with unique IDs and JSON booleans; strings such as `"true"` and numeric values such as `1` fail validation. Optional `detail` must be a string. Extra checks are welcome, but a failing extra check also fails the receipt.

## Record the evidence

```bash
python3 evaluate_langfuse_release.py \
  --manifest ./release-review/prompt-index.json \
  --stage-report ./release-review/reports/stage.json \
  --results ./app-results.json \
  --require-check response_json --require-check required_fields \
  --report evaluation.json
```

Repeat `--require-check` for the cases the release requires. The set must be non-empty and unique. A missing target or required case, a duplicate ID or an incorrect version stops recording. Valid failed checks produce a receipt with `status: "failed"` and exit code 1 so you can inspect the failure.

The receipt records required IDs, results and hashes of the snapshot, stage report and source result file. Keep those inputs unchanged and retain the source file; promotion reads it again. Use a fresh report filename for a new attempt.

## Enforce it during promotion

Pass `--evaluation-report ./release-review/reports/evaluation.json` to the promoter, including for the preview. Evaluation is required by default; `--require-evaluation` remains supported. It rejects failed evidence, missing source files and changed bindings before PATCH. It also checks the live staged target and baseline label. Changing local files during a batch can stop a later item after earlier promotions succeeded.

Structural-only promotion requires the explicit `--allow-structural-only` opt-out. Any supplied receipt is checked, even on that path. See [migration notes](docs/migration-v03.md) for the changed defaults.

These receipts are unsigned attestations from a runner you trust. Hashes detect changed inputs but do not prove that tests ran or authenticate their author. Someone who can replace the files and recompute their hashes can forge a receipt. Use trusted application or CI results; never substitute a synthetic fixture pass for a production-model evaluation.
