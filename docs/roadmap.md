# Implementation roadmap

Updated **12 November 2026** for the 0.8 reliability and integration work. See [validation](validation.md) for dated evidence and [migration](migration-v08.md) for adoption.

## Added in 0.7.0a1

- Generate a reviewable policy from selected cases, source files and explicit score requirements with `langfuse-release-policy-draft`. Validate before separately pinning; candidate results never select the thresholds.
- Inspect a local release folder with `langfuse-release-status`: readable terminal output and structured JSON, original evidence paths, missing/ambiguous records and freshness limits. It performs no remote reads or writes.

See [policy setup](policy-setup.md) and [release status](release-status.md). The existing release authorization and evidence formats remain unchanged.

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

## Implemented for 0.8, undergoing release validation

| Work item | Delivered behavior | Guide |
|---|---|---|
| Independent project identity | Format 2 exports bind the selected project; current keys are checked before remote release operations, including preview. Legacy snapshots need a fresh export. | [Migration](migration-v08.md) |
| Trusted promotion | Original bytes and external CI signer expectations are verified at each promotion attempt. Private materialization preserves evidence; fresh evaluation/dependency/project checks still apply. | [Trusted promotion](trusted-promotion.md) |
| Local concurrency and recovery | OS operation locks, durable atomic journals, subprocess crash tests and read-only resume proposals. Unknown writes are not retried. | [Recovery](local-recovery.md) |
| Easier capture integration | Supported Python/JS result projections, application recipe, opt-in persistent JS collector with bounded queues and explicit acknowledgment/shutdown/loss receipts. | [Capture integration](capture-integration.md) |
| Hosted dataset compatibility | Real hosted dataset versions/schemas, typed case/run scores, paging and negative controls in the disposable matrix. | [Hosted validation](hosted-dataset-validation.md) |

The [validation record](validation.md) distinguishes completed checks from checks still running. The deferred application pilot above is not counted as finished.

## Next decisions after this release

1. **Use adopter feedback to fix the first integration obstacles.** Collect a redacted report and a minimal reproduction for confusing setup, incomplete capture or SDK incompatibility. Turn observed problems into focused tests before expanding features.
2. **Run the deferred application pilot when an app exists.** Measure the persistent collector's CPU/latency/memory overhead, choose coverage thresholds from actual traffic and compare results with direct SDK access.
3. **Review the next official SDK/server pair.** Pin official artifacts and run this same acceptance matrix before claiming compatibility. A dependency bump alone is not evidence.
4. **Prepare a small upstream proposal if the workflow earns adoption.** Use a reproducible failure and measured benefit. An MCP wrapper or broader framework integrations should answer a concrete consumer need first.

These are follow-up decisions, not unimplemented parts of the five-item 0.8 scope. Outreach, customer deployment and new platform support require their own concrete target.
