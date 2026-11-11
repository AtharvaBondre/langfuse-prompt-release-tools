# Release roadmap

Updated **11 November 2026**, after the 0.5 implementation passed local source,
installed-package and live SDK/server checks plus all six GitHub CI jobs at
[53e2eae](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43014213742).

## Completed in 0.5.0a1

| Previously planned capability | Implemented behavior | Evidence |
|---|---|---|
| Official experiment result import | Action v1 / normalized Python SDK results, exact item coverage, typed thresholds, task-time returned prompt capture | Actual SDK artifacts pass the official v1 schema; malformed/partial/changed inputs fail closed |
| Independent required-test policy | Review and pin before staging; no legacy receipt, direct-publication, structural-only or missing-lock bypass | Policy and integration regression tests |
| Working application example | Real official SDK runner compiles fetched candidates, scores actual output and produces both failing and passing receipts | 14 live assertions on SDK 5.2.0 + Langfuse 4.78.0; no LLM calls |
| Interrupted-release recovery | Separate GET-only reconciliation; completed/pending/conflicting/unknown classifications; unknown POST remains unknown | Lost response, disk failure, partial batch and concurrent-change tests; real injected lost PATCH recovered without another PATCH |
| Transitive dependency checks | Server graph plus concrete raw/resolved hashes; task-time binding; fresh checks before/after writes and final completion | Nested child-label movement blocks previously passing evidence |
| Application adoption | Actual SDK objects, independent consumer/deployment expectations, freshness, version/content/fallback checks | Actual cached old prompt reports not_adopted; fresh access matches release |

The current gates are green: **232 source tests, 224 installed-wheel tests,
ten installed commands, 14 live assertions and six CI jobs**. These are distinct
validation levels; do not add their counts as independent production scenarios.
See [validation](validation.md) and [migration](migration-v05.md).

This adds a release evidence chain around official Langfuse capabilities. It
does not claim broader feature superiority, official endorsement, authenticated
execution, better model answers or atomic multi-prompt releases.

## Next implementation order

### 1. Portable evidence bundles and CI provenance — target 0.6

**Problem:** receipts reference absolute local files and trust their producer.
Moving a job artifact to another runner currently needs the original paths;
a hash alone does not identify who produced it.

Implement a versioned bundle containing policy, snapshot, stage, dependency
lock, capture and source-result manifests with relative references. Verify the
bundle offline before resolving any remote target. Add an optional signed CI
attestation binding the bundle digest to repository, workflow, commit and
runner identity. Keep trusted signer/workflow expectations outside the bundle.

Acceptance:

- Validate an unchanged bundle on a second machine without original paths.
- Reject path traversal, symlinks, duplicate files/keys, missing files, modified
  data, wrong signatures, unauthorized workflows and replay against another
  release. A supplied bundle must not choose its own trusted issuer.
- Keep prompts/results private by default; produce an explicit content inventory
  before any public upload. Never bundle credentials.
- Preserve unsigned local compatibility with an explicit trust classification.

### 2. Remote experiment persistence verification — target 0.7

**Problem:** local evaluation may finish while trace/score export fails, is
sampled out or has not yet persisted. The current remote completion value is a
trusted caller statement, not a built-in server audit.

Use official read APIs to verify the expected experiment, cases, traces and
scores against the captured run identity. Handle pagination and eventual
consistency with bounded retries and a recorded deadline.

Acceptance:

- Confirm every required case/score and its typed value before declaring remote
  persistence verified. A URL, aggregate average or successful flush is insufficient.
- Fail clearly on missing/duplicate results, exporter failure, sampling,
  unauthorized reads, timeout and a run from another project or dataset version.
- Keep local completion and remote persistence as separate statuses. Never
  replace a failed check with remote metadata or perform model calls implicitly.

### 3. Compatibility fixtures and JavaScript capture — target 0.8

**Problem:** the live proof currently covers one Python SDK/server combination.

Add a task-time capture helper for the official JavaScript SDK, using the same
policy/evidence contract. Add recorded official artifact fixtures and disposable
server tests for explicitly supported SDK/server pairs and architectures.

Acceptance:

- Exercise JS 6.x on Node 22+ and supported Python SDKs with the same passing,
  failing, nested-dependency, cache and fallback cases.
- Test actual minimum supported server behavior rather than deriving it from an
  SDK's advertised minimum. Missing resolver evidence must fail closed.
- Publish the tested matrix and unsupported combinations. Keep core dependencies
  optional; do not build a second experiment engine or prompt compiler.

### 4. Rollout observation over a time window — after consumer pilot

**Problem:** one instrumented access proves only that access, not sustained
adoption across applications.

Aggregate observations over a reviewed window with expected consumer/deployment
identities, freshness and sample-count requirements. Show missing consumers,
stale caches, fallback rates and dependency mismatches separately. Start with
one real consuming application and use its feedback to choose thresholds.

Acceptance:

- Mixed old/new versions, missing consumers and replayed observations cannot
  produce a complete rollout result.
- State measured coverage and sampling limits; do not describe samples as all
  traffic. Reuse CI provenance where observations can be authenticated.
- Produce a reviewed recovery/rollback proposal when useful. No automatic
  rollback or retry of an uncertain POST.

## Conditional work

A local operation lock could prevent two processes from sharing one release
folder; it would not provide distributed exclusion against other clients.
An explicit recovery plan may build on reconciliation, but unknown creations
must still require review unless the server supplies a real idempotency contract.

A narrow release-only MCP interface is worthwhile only when an identified
consumer cannot invoke these commands. Official prompt CRUD, experiment UI,
general CLI/MCP and coding-agent telemetry already exist upstream; duplicating
them would add maintenance without solving the release problems above.

An upstream contribution should start with a small reproduction or documentation
clarification and the maintainers' current contribution rules. No external
submission or outreach is implied by this roadmap.
