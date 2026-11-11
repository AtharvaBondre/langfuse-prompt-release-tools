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

## Outstanding operational step from the original plan

The original real-application pilot remains open. The current integration proof uses synthetic applications and does not include feedback from an existing deployed consumer. Select the application/repository and test environment, instrument its actual accesses, measure overhead and choose rollout thresholds from that evidence. No customer or production deployment was performed as part of this personal repository release.

## Next implementation order

These are **new, unimplemented work items**, beyond the shipped capabilities. The pilot above is carried forward, not counted as completed.

### 1. Enforce signer policy at the promotion boundary

Connect the verified bundle/provenance path to a reviewed promotion policy. Materialize validated evidence in an immutable release workspace without the producer's original paths. Keep host/project/label and signer policy independently selected.

Acceptance: unsigned or wrong-signer bundles cannot satisfy a signer-required release; historical evidence cannot bypass fresh dependency/remote checks; dry-run and apply enforce identical policy; inspection never executes bundled code. Preserve explicit unsigned-local compatibility.

### 2. Add a bounded asynchronous capture collector

The JS sidecar starts Python synchronously per capture. Add an optional persistent collector for a real application's low-volume pilot, retaining the common validator and original access times. Measure overhead before recommending request-path use.

Acceptance: bounded queue/memory, explicit backpressure/drop status, deduplication, restart-safe acknowledgments, safe shutdown and measured latency. Missing/delayed samples reduce coverage rather than disappear. Select the application and deployment within its own authorization before a live pilot.

### 3. Expand hosted-dataset and upgrade compatibility tests

The verifier implements hosted dataset identity/version checks, while the current live matrix uses local synthetic datasets. Add a disposable hosted-dataset scenario, schema/version changes and run-level scores. Extend the matrix one reviewed SDK/server pair at a time.

Acceptance: changed dataset version/schema or wrong run cannot pass; required scores are read across pagination; sampling/export loss stays incomplete; upgrade proposals include recorded official artifacts and exact image/SDK versions. Do not infer support for untested intermediate versions.

### 4. Harden concurrent and interrupted local operations

Add a release-folder operation lock and crash-injection tests around durable journal writes. Build explicit resume proposals from reconciliation, with fresh preflight before an authorized mutation.

Acceptance: two local writers cannot corrupt one folder; process death retains enough evidence to classify the attempt; uncertain creation is never automatically retried. A local lock is not distributed exclusion or a server transaction. Automatic rollback remains outside this plan.

An upstream contribution should start with a small reproduction and current maintainer rules. This roadmap does not authorize outreach or customer deployment. A release-specific MCP wrapper remains conditional on a consumer need the existing commands cannot meet.
