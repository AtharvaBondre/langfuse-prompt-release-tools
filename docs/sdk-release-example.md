# Run the complete SDK example

This example uses Langfuse's Python SDK 5.2.0 and its real experiment runner.
The application compiles a fetched prompt and parses JSON. It calls no model.
The scores come from comparing actual task output with two fixed expected
answers. This tests release controls; it is not an LLM quality benchmark.

Use Python 3.11 or later and a new disposable, loopback Langfuse project:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[example]'
# Start the disposable server following LOCAL-DEMO.md, then supply its newly
# generated LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in the environment.
.venv/bin/python examples/sdk_release_demo.py \
  --host http://127.0.0.1:13830 \
  --out-dir ./local-demo-evidence \
  --disposable-local
```

The output directory must not already exist. Generated prompts, captures and
receipts stay private. The script creates uniquely named synthetic prompts and
retains them in the disposable project for inspection.

The sequence is:

1. Pin case identities, input/expected-output hashes, task/evaluator revisions,
   model/config identity and required checks before staging.
2. Capture the server's nested dependency graph before running the experiment.
3. Fetch each staged version through the official SDK inside the task. Capture
   its returned identity and resolved content before scoring the output.
4. Import the SDK result in the Action v1 JSON format. Preserve typed scores,
   complete case coverage and the original artifact. The example explicitly
   identifies its producer as the SDK; it does not claim a hosted Action ran.
5. Demonstrate a failed experiment blocking promotion without a PATCH.
6. Demonstrate a nested child-label change blocking a previously passing
   release, then run a new real evaluation against a new dependency lock.
7. Inject response loss after a real successful PATCH. Reconcile by GET, then
   recognize the already-promoted version without another PATCH.
8. Observe an actual SDK cache returning the old prompt and record
   `not_adopted`. Fetch again with SDK caching disabled and verify the observed
   consumer against the promoted version and resolved hash.

The SDK's `flush()` is not proof that every remote trace and score persisted.
This demo's policy gates on complete local results and records remote export as
`unconfirmed`. A policy with `remote_export: required` rejects that outcome.
The instrumented consumer establishes this application's observed accesses,
not fleet-wide adoption. Hashes are unsigned and require a trusted runner.

Replace the cases, task and evaluator with your actual application's work
before using this pattern for a real release. Do not turn this deterministic
example into a fabricated model benchmark or copy its passing scores.
