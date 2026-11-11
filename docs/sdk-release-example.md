# Run the SDK release examples

Start with the **current 0.6 example**, `examples/verified_release_demo.py`. It
runs actual Python and JavaScript SDK experiments, verifies stored cases and
typed scores, promotes passing candidates, and observes cached/fresh application
accesses. Tasks render two synthetic JSON answers locally; no model provider is
called. This demonstrates release controls, not LLM quality or production use.

For installation and choosing your first path, see [getting started](getting-started.md).

## Current 0.6 verified release demo

Run from a source checkout with Python 3.11+, Node 22+, and a new disposable
Langfuse project on a loopback server. The example uses Python SDK 5.2.0 and
JavaScript SDK 6.1.1. The [validation record](validation.md#tested-compatibility)
lists the exact tested server/runtime combinations. The
[local Compose guide](../LOCAL-DEMO.md) describes an ARM64 setup; do not assume
that its pinned images work on another architecture. The complete recorded
0.6 matrix ran on Linux AMD64; the recorded ARM64 run was incomplete.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[example]'
npm ci --prefix js --ignore-scripts

# Start the disposable server and supply only its project-scoped
# LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in this environment.
# Set LANGFUSE_PROJECT_ID to that independently selected disposable project ID.
.venv/bin/python examples/verified_release_demo.py \
  --host http://127.0.0.1:13830 \
  --project-id "$LANGFUSE_PROJECT_ID" \
  --out-dir ./verified-demo-evidence \
  --disposable-local
```

The output directory must not already exist. Generated prompts, captures and
receipts stay private. The script creates uniquely named synthetic prompts and
retains them in the disposable project for inspection. It writes prompts and
scores to that local project; use the documented disposable setup rather than
an existing application deployment.

The current example checks this sequence:

1. Pin a policy-v2 requirement for exact cases, checks, task identities, project
   identity and remote-receipt freshness, then stage a candidate.
2. Lock the server-resolved transitive dependencies before the task runs. Fetch
   each exact staged version through the official SDK and capture the returned
   object before compiling or passing it into another framework.
3. Run the deterministic task and retain its actual results. Show that `flush()`
   without an independently verified remote receipt cannot satisfy the policy.
4. Read persisted experiments, complete cases and typed scores from Langfuse.
   The intentionally failing JavaScript candidate has stored BOOLEAN `false`
   scores and remains blocked; script completion alone does not mean it passed.
5. Promote passing candidates with matching evidence. Move a nested child label
   and demonstrate that the old lock blocks promotion, then evaluate again
   against a fresh lock.
6. Create and verify a portable evidence bundle. These local bundles are
   unsigned; signed CI provenance is a separate workflow.
7. Observe the old version in a warmed JavaScript SDK cache, the released version
   after bypassing that cache, and an explicit fallback object. Measure four
   real SDK accesses across two consumers in a predeclared eight-second window;
   show that replaying a request cannot increase accepted coverage.

On a fully successful run, stdout and `verified-demo-evidence/summary.json`
report `status: passed` and `model_calls: 0`. The directory retains per-run
policy, capture, results, remote receipt, evaluation, promotion and rollout
artifacts. The failing candidate and replay report are deliberate negative
checks. Missing remote data, an unsupported API or infrastructure failure must
remain incomplete; inspect the retained artifacts instead of treating a partial
run as a pass.

The eight-second window is a test setting, not a recommended rollout policy.
The checks cover the supplied synthetic SDK accesses, not an application's
entire traffic. The maintainer's existing-application pilot remains deferred.

## Use this pattern in your application

Keep your actual task and evaluator. Replace the synthetic cases and execution
identities, review the required scores/thresholds, and pin that policy before
staging. The capture hook belongs immediately after the official SDK fetch and
before a framework converts its prompt object. Keep the actual outputs and
result projection; do not copy the demo's passing scores.

See [experiment evidence](experiment-evidence.md) for the v2 policy, remote
verification and distinct preview/apply commands. Use the
[JavaScript helper](../js/README.md) for its capture API and the
[adoption guide](dependencies-and-adoption.md) for post-promotion access checks.
These examples remain source-checkout files; the Python wheel installs the
release commands, not this demo.

## Older 0.5 Python baseline

`examples/sdk_release_demo.py` remains available for the earlier Python-only
workflow, including interrupted-PATCH recovery. It uses Python SDK 5.2.0 and
policy v1 with optional remote export. Its complete local results do not establish
the independent remote persistence required by the current v2 example.

With the same Python environment and a disposable loopback project:

```bash
.venv/bin/python examples/sdk_release_demo.py \
  --host http://127.0.0.1:13830 \
  --out-dir ./python-baseline-evidence \
  --disposable-local
```

Its sequence is:

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
This older demo's policy gates on complete local results and records remote
export as `unconfirmed`. Policy v1 with `remote_export: required` rejects that
producer flag; policy v2 requires a separate verified receipt when remote export
is required.
The instrumented consumer establishes this application's observed accesses,
not fleet-wide adoption. Hashes are unsigned and require a trusted runner.

Keep this baseline for compatibility and recovery checks. Use the current 0.6
example above to explore remote persistence, JavaScript and rollout windows.
