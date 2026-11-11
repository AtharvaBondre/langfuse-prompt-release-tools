# Recorded SDK result fixtures

These are byte-for-byte result files from two actual official SDK experiment
runs against a disposable **Langfuse 4.78.0** instance on 11 November 2026. Each
task rendered two synthetic JSON answers locally; no model provider or customer
dataset was used. Repository example adapters projected the returned SDK
objects into the official Experiment Action v1 JSON format. The GitHub Action
itself did not run these experiments.

| Fixture | SDK | Script outcome | `answer-exact` evaluations |
| --- | --- | --- | --- |
| `python-sdk-5.2.0-passing.action-v1.json` | Python `langfuse==5.2.0` | Completed | Both `true` |
| `javascript-sdk-6.1.1-failing.action-v1.json` | JavaScript `@langfuse/client==6.1.1` | Completed | Both `false` |

The JavaScript runner sends BOOLEAN scores as numeric `0`/`1` through the SDK.
The example adapter explicitly converts those typed values to JSON booleans.
The failing fixture catches accidental truthiness conversion or treating script
`status: "passed"` as evaluator success. The Python fixture also records
top-level input/expected-output fields, while the JavaScript fixture records
them inside `item`; the importer must preserve both supported layouts.

`provenance.json` records each source run label, original SHA-256, SDK version,
server version and validation scope. No result bytes were redacted or rewritten.
The retained loopback URLs and project/experiment/trace IDs are disposable local
identifiers, not credentials. No private filesystem paths, captures, credentials
or complete release bundles are included.

This is an **unsigned compatibility record**. Fixture tests do not establish
remote persistence, authenticated CI origin, application adoption, model quality
or a broader SDK-version support range. Remote persistence requires separate
API readback evidence; the presence of a URL or trace ID cannot supply it.

The vendored `result-json.v1.schema.json` is unchanged from official
[Experiment Action v1.0.12](https://github.com/langfuse/experiment-action/blob/c7c4efe432e37d4e3693a018de469233c0018de4/schemas/result-json.v1.schema.json),
commit `c7c4efe432e37d4e3693a018de469233c0018de4`. Its MIT notice is in
`LICENSE.experiment-action`. The recorded schema validation uses
`jsonschema==4.26.0`, Draft 2020-12 and format checking.

From the repository root:

```sh
python -m unittest test_sdk_result_fixtures -v
```

All importer and BOOLEAN checks use the standard library. The upstream schema
test is skipped when `jsonschema` is absent; install the optional `review` extra
to run it. `provenance.json` records the validation performed when these files
were captured, including the exact schema and fixture hashes. Its record is
descriptive, not an authenticated attestation.

When updating fixtures, record a new real synthetic SDK run and its reviewed
source bytes. Keep passing and failing evaluations, re-run upstream schema and
importer checks, and update the source hashes and version labels together.
Do not edit a recorded result to make a changed projection pass.
