# Changelog

## 0.8.0a1 — 2026-11-12

- Bind snapshots to an independently selected project; check current credentials before export, preview, staging, promotion and reconciliation. Format 1 evidence remains inspectable; remote operations require a fresh export.
- Require trusted CI evidence by default at promotion. Verify original bundle bytes and external signer expectations, materialize a private workspace, and retain fresh evaluation, dependency and project checks. Unsigned local workflows require an explicit option.
- Coordinate local snapshot operations with OS locks. Flush atomic journals before writes and add read-only resume proposals, concurrent-process and crash tests.
- Provide supported Python/JavaScript result projections and an optional bounded persistent JavaScript collector with acknowledgments, backpressure and shutdown receipts.
- Exercise hosted dataset versions, schemas, typed case/run scores, pagination and missing exports in the disposable SDK/server workflow.
- Keep the real-application pilot and its capture-overhead measurements explicitly deferred.

## 0.7.0a1 — 2026-11-12

- Generate reviewable policy drafts from selected cases, UTF-8 source files and explicit score requirements; compute the existing identity fingerprints and validate before separate pinning.
- Inspect local release evidence with a readable terminal status view or stable JSON, retaining source references, freshness, ambiguity and the difference between promotion and observed app use.
- Add example policy inputs, focused failure-mode tests, installation coverage for seventeen commands and first-use guides.
- Preserve existing policy/capture/report contracts and remote mutation behavior. Both new commands operate without contacting Langfuse.

See [migration](docs/migration-v07.md), [policy setup](docs/policy-setup.md) and [release status](docs/release-status.md).

## 0.6.0a1 — 2026-11-11

- Transfer original release evidence in path-safe portable bundles and verify independently pinned GitHub CI provenance.
- Require fresh, complete, typed remote experiment evidence under format 2 policies; retain explicit legacy behavior.
- Capture actual Python and JavaScript SDK objects through one validation and hashing contract.
- Check rollout samples across reviewed consumer/deployment identities and time buckets; reject replay and mixed releases.
- Add real SDK/server CI on Langfuse 4.59.0 and 4.78.0, Node 22/24 capture tests, official-schema fixtures and fifteen installed commands.
- Fix JavaScript BOOLEAN score wire values, preserve recorded fixture bytes on Windows, and count signature-verification time toward freshness.

See [migration](docs/migration-v06.md), [validation](docs/validation.md) and the [next roadmap](docs/roadmap.md).

## 0.5.0a1 — 2026-11-11

- Pin independently reviewed experiment policy before staging; block all legacy release bypasses for pinned snapshots.
- Import official Action v1 or normalized SDK results with exact cases, typed thresholds, execution identity and task-time returned prompt capture.
- Bind server-resolved transitive dependencies to evaluation and recheck before/after promotion writes and final completion.
- Reconcile interrupted creates/promotions with GET-only reports; retain unknown POST outcomes.
- Verify actual SDK consumer observations against the release lock, consumer revisions, freshness and fallback state.
- Add a runnable official SDK experiment with failing/passing cases, nested dependency drift, injected response loss and cached/fresh adoption.
- Package ten commands; retain the stdlib core and optional SDK dependency.

See [migration](docs/migration-v05.md) and [validation](docs/validation.md) for compatibility and tested scope.

## 0.4.0a1 — 2026-11-11

First independent repository release, based on the public 0.3 Gist.

- Stage prompt versions by default. Direct release requires `--allow-direct-release`.
- Require evaluation evidence for promotion by default. Structural-only promotion requires `--allow-structural-only`.
- Record both opt-outs in reports; reject contradictory or non-boolean Python API options.
- Create prompt exports and receipts with private POSIX permissions and exclusive file creation.
- Classify excessive JSON nesting in a response after a write as an uncertain outcome.
- Add a captured-output example with computed JSON/answer checks, binding validation and private result files.
- Add regression checks, packaging CI, a repository skill, migration guidance and a source-backed expansion plan.
- Preserve required Windows OS paths in isolated offline test environments so SSL context setup can run.

These defaults change behavior. See [migration from the Gist](docs/migration-v03.md). There are no new runtime dependencies.

## 0.3 Gist — 2026-10-17

Application-result receipts bind source results to the snapshot, stage report and exact versions. The evaluation gate was opt-in. [Original source](https://gist.github.com/AtharvaBondre/3ad9d0654b5450d1e007728f67047fa0/824e13d8ec01bdee3c3717652ae6852c9aa06dda).
