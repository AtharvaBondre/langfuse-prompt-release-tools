# Implementation roadmap

Updated **11 November 2026**, after the known roadmap passed source, installed-package, signed-CI and real SDK/server gates. See [validation](validation.md) for scope and [migration](migration-v06.md) for adoption.

## Completed together in 0.6.0a1

| Previous item | Shipped behavior | Verification |
|---|---|---|
| Portable evidence and signed CI records | Original-byte bundle, safe relative inventory, offline semantic checks; independent GitHub repository/workflow/commit/ref/issuer/runner expectations | Linux-to-macOS transfer without original paths; actual signatures; wrong identity, tampering and release replay rejected |
| Remote experiment persistence | Project-bound, paginated GET checks for complete cases and typed scores; bounded waiting; policy v2 freshness gate | Actual Python/JS experiments; missing export stays pending; persisted failing scores block release; stale/substituted evidence rejected |
| JavaScript and compatibility | Actual SDK capture with common Python validation/hashing; official-schema fixtures; packaged helper | Node 22/24; installed JS + installed wheel; real 4.59.0/4.78.0 matrix including failure, nested drift, cache and fallback |
| Rollout observation windows | Reviewed consumer/deployment/SDK identities, distinct requests, time buckets, freshness, fallback and mixed-version detection; recovery proposal | Actual two-consumer/eight-second probe; replay/missing/mixed/stale tests; signed synthetic CI observations |

The six 0.5 capabilities remain shipped: independent required-test policy, official result import, working application example, interrupted-release reconciliation, transitive dependency locking and individual SDK adoption checks.

This adds release evidence and checks around official Langfuse services. It does not duplicate their compiler, experiment engine, general CLI, MCP or skills. Tests do not establish broader feature superiority or production adoption.

## Deferred real-application pilot

The maintainer explicitly deferred the real-application pilot on 11 November 2026 because no application/test environment is available yet. The current integration proof uses synthetic applications and does not include feedback from an existing deployed consumer. Select the application/repository and test environment, instrument its actual accesses, measure overhead and choose rollout thresholds from that evidence. No customer or production deployment was performed as part of this personal repository release.

## Next implementation order

The [product review](product-improvements.md) explains the user benefit and acceptance criteria for this order. The first three items address setup and release interpretation gaps found during the README review.

These are **new, unimplemented work items**, beyond the shipped capabilities. The pilot above is explicitly deferred, not counted as completed.

### 1. Bind project identity before mutations

Add the independently selected project ID to the export and validate the project associated with the current key before staging or promotion. The remote verifier already checks it; the earlier mutation path currently binds the host and prompt baseline. Define explicit compatibility for older snapshots.

Acceptance: a same-host wrong-project key produces zero writes even when prompt baselines match; preview and apply enforce the same identity; credentials stay out of reports.

### 2. Generate and validate reviewed policy drafts

Create a policy draft from the selected dataset cases and actual execution configuration. Compute the fingerprints, show missing fields and validate the structure before pinning. Users choose evaluator names and thresholds before seeing candidate results.

Acceptance: a new user can build the example policy without hand-written hashes; malformed or duplicated cases/checks produce field-specific errors; generation performs no remote writes and never weakens criteria to match a run.

### 3. Explain release status in one read-only view

Combine stage, evaluation, remote freshness, dependency state, promotion and observed adoption in a concise terminal view with stable JSON output. Link each conclusion to its source report and print the next safe action. Standardize output-path reporting across commands.

Acceptance: missing or stale evidence stays visible; label movement cannot stand in for application adoption; inspection performs no writes and distinguishes historical reports from fresh reads.

### 4. Enforce signer policy at the promotion boundary

Connect the verified bundle/provenance path to a reviewed promotion policy. Materialize validated evidence in an immutable release workspace without the producer's original paths. Keep host/project/label and signer policy independently selected.

Acceptance: unsigned or wrong-signer bundles cannot satisfy a signer-required release; historical evidence cannot bypass fresh dependency/remote checks; dry-run and apply enforce identical policy; inspection never executes bundled code. Preserve explicit unsigned-local compatibility.

### 5. Harden concurrent and interrupted local operations

Add a release-folder operation lock and crash-injection tests around durable journal writes. Build explicit resume proposals from reconciliation, with fresh preflight before an authorized mutation.

Acceptance: two local writers cannot corrupt one folder; process death retains enough evidence to classify the attempt; uncertain creation is never automatically retried. A local lock is not distributed exclusion or a server transaction. Automatic rollback remains outside this plan.

### 6. Reduce capture integration work

Move the Python/JavaScript result projections from examples into supported helpers. Add a small application recipe. Prototype an optional persistent collector for JavaScript, which starts Python synchronously per capture today; retain the common validator and original access times.

Acceptance: helpers preserve real result identities and typed scores; packaged examples use those helpers; queues/memory are bounded; backpressure, dropped samples, acknowledgments and shutdown outcomes remain visible. Measure overhead in the deferred application pilot before recommending request-path use. Missing or delayed samples reduce coverage rather than disappear.

### 7. Expand hosted-dataset and upgrade compatibility tests

The verifier implements hosted dataset identity/version checks, while the current live matrix uses local synthetic datasets. Add a disposable hosted-dataset scenario, schema/version changes and run-level scores. Extend the matrix one reviewed SDK/server pair at a time.

Acceptance: changed dataset version/schema or wrong run cannot pass; required scores are read across pagination; sampling/export loss stays incomplete; upgrade proposals include recorded official artifacts and exact image/SDK versions. Do not infer support for untested intermediate versions.

An upstream contribution should start with a small reproduction and current maintainer rules. This roadmap does not authorize outreach or customer deployment. A release-specific MCP wrapper remains conditional on a consumer need the existing commands cannot meet.
