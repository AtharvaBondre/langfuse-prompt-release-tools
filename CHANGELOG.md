# Changelog

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
