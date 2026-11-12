# Build a policy from your cases and source files

`langfuse-release-policy-draft` turns reviewed case data and release requirements into the policy JSON used by the existing release commands. You choose the prompt targets, score types and thresholds. The helper computes case, source and configuration fingerprints so you do not have to copy hashes by hand.

It reads local files and creates a new draft. It does not run your task, inspect candidate scores, contact Langfuse or pin the policy into a snapshot.

## Try it with the working example

From the repository root, after [installing the commands](getting-started.md):

```bash
langfuse-release-policy-draft \
  --spec examples/policy-inputs/spec.json \
  --cases examples/policy-inputs/cases.json \
  --source-root . \
  --out policy-draft.json

langfuse-release-policy-draft --validate policy-draft.json
```

The selected cases match the synthetic cases in [`sdk_experiment.py`](../examples/sdk_experiment.py). The spec selects its deterministic task/evaluator, the `answer-exact` boolean check and the `example/answer` prompt. The generated policy matches that example's `policy_for("example/answer")` result. This command does not run an SDK experiment or evaluate a model.

Open the output and review the identities, prompt targets and checks. Once you have exported the intended prompt snapshot, pin the policy explicitly:

```bash
langfuse-release-policy --manifest release-review/prompt-index.json \
  --policy policy-draft.json
```

Pin before staging. A different policy needs a fresh export; generating another draft does not replace an earlier pinned policy. Existing output files are never overwritten.

## Replace the example with your inputs

The [example spec](../examples/policy-inputs/spec.json) contains the choices you review. The [cases file](../examples/policy-inputs/cases.json) contains an array of records with `id`, `input` and `expected_output`. A wrapper object with a single `cases` field is also accepted. An explicit `null` input or expected output is valid; an omitted field is an error.

| Input | Your choice | What the helper records |
|---|---|---|
| `dataset.id` and `dataset.name` | Identities your task will capture. | The selected strings. |
| `dataset.version` | An actual hosted/versioned dataset identity, when needed. | Your explicit version; if omitted for a local dataset, the fingerprint of the selected case array. A declared hosted remote policy requires its captured timezone-aware timestamp. |
| Case input and expected output | The reviewed data your task will run. | Separate fingerprints for each case, plus its ID. |
| `dataset.schema` | Optional producer schema evidence; see the hosted shape below. | Its `schema_sha256` fingerprint. This does not validate the case values against the schema. |
| `execution.task_file` and `evaluator_file` | Actual UTF-8 source files within `--source-root`. | A canonical JSON fingerprint of each file's text. The helper never imports or executes them. |
| `execution.model` | Provider, model name and full execution configuration. | Provider/name and `config_sha256`. Use the same configuration in capture. |
| `producer` | Action, Python SDK or JavaScript SDK, result schema `v1`, and result `script_path`. | The identity that experiment import must match. |
| `targets` | Explicit prompt names and their selected `case_ids`. | Those assignments. Every selected case must belong to a target. |
| `checks` | Score name (`id`), kind, operator and threshold selected before the candidate run. | Those exact requirements, without using candidate results. |

Source paths resolve relative to `--source-root`, which defaults to the current directory. Absolute paths, parent traversal and symlinks are rejected. The source revision is the repository's existing `fingerprint(source_text)` convention, not a raw-file SHA-256 or a Git commit ID. Capture must use the same identity convention. Imported helpers, dependencies and environment state are not included automatically; select source boundaries and execution configuration that represent your actual task.

Your cases file should contain selected inputs and expected outputs only. Candidate `output`, `evaluations`, aggregate counts and inferred thresholds are not accepted as substitutes. Choose the cases and requirements before you evaluate a candidate. The helper cannot determine whether an author selected them independently.

Boolean checks require `operator: "eq"` and a JSON `true` or `false` threshold. Numeric checks accept `eq`, `gte` or `lte` and a finite JSON number. Duplicate IDs, uncovered cases, unknown target case IDs and malformed fields stop generation with a field-specific error. The file reader also rejects duplicate JSON keys and non-finite numbers.

## Require remote persistence

Set `format_version` to `2`, keep `remote_export: "required"`, and include the independently selected destination and freshness requirement:

```json
{
  "remote_verification": {
    "project_id": "your-reviewed-project-id",
    "dataset_kind": "local",
    "max_age_seconds": 300
  }
}
```

This fragment belongs in the full spec. Choose `local` or `langfuse` to match the task's dataset. The allowed maximum age is 1–3600 seconds. The helper preserves these requirements; [remote verification](remote-persistence.md) performs the actual server reads later.

For `dataset_kind: "langfuse"`, supply the actual captured dataset version as a timezone-aware timestamp. A local hash is not a hosted dataset version. If you pin hosted schema evidence, `dataset.schema` must contain exactly the two fields returned by the Langfuse dataset API:

```json
{
  "schema": {
    "inputSchema": {"type": "object"},
    "expectedOutputSchema": null
  }
}
```

Put this fragment inside `dataset`, using the actual selected schemas. The remote verifier fingerprints that same two-field object; a bare schema such as `{"type": "object"}` is not compatible. The helper validates this wrapper, not JSON Schema semantics. For local datasets, an optional schema object remains producer identity evidence; remote verification does not independently fetch or verify a local schema.

Output uses the existing policy v1/v2 schema. Version 1 remains supported for compatibility, including its older remote-export semantics; choose v2 when you need the built-in remote-persistence gate. The helper does not upgrade a policy or infer a project ID from credentials.

## Validate a policy someone else prepared

```bash
langfuse-release-policy-draft --validate path/to/policy.json
```

Validation checks the policy structure, types, case coverage and supported requirements. It does not confirm source provenance, recompute source fingerprints, verify the dataset against a server or establish that an evaluation passed. No file is changed. Review a policy before using the separate pin command.

The raw spec and cases may contain private task data or execution configuration. Keep them in your usual private workspace. The generated policy contains hashes for those values, while dataset names, target names and producer paths remain visible.
