# Upgrade to 0.5.0a1

The runtime remains Python 3.10+ with no required third-party dependencies.
Existing unpinned 0.4 exports, evaluation receipts and explicit compatibility
modes still work. Five new commands add policy, experiment import, dependency
locking, read-only reconciliation and SDK adoption checks.

For a new strict release, export again, review its policy and pin that policy
before staging. An old stage cannot be retrofitted: pinning changes the snapshot
hash, so export/stage again instead of rewriting an old report.

A policy-pinned snapshot requires an imported experiment receipt and the same
pre-execution dependency lock during promotion. It rejects legacy receipts,
structural-only promotion and direct publication, even when their explicit
legacy flags are supplied. Required cases/checks come from the pinned policy.
An experiment result or URL without task-time prompt capture is insufficient.

Dependency-free prompts still get a lock. A changed nested dependency requires
reviewing a new lock and rerunning the actual experiment; old passing scores
cannot be attached to the new closure. Keep source results, capture, lock,
snapshot and stage available at their recorded paths throughout promotion.
These unsigned files are not an authenticated or portable evidence bundle.

Successful applied promotion reports now include `completed_at_utc` and, when
locked, `dependency_lock_sha256`. Adoption requires that exact lock, captured
before completion, plus fresh observations from the actual SDK consumer.
Historical reports without these fields cannot be upgraded by stamping a
current timestamp or a post-hoc lock onto them.

The optional example uses Python SDK 5.2.0 (Python 3.11+) and has been run against
local Langfuse 4.78.0 on ARM64. This is a tested combination, not a guarantee for
every Langfuse/SDK release. Core receipt validation does not install the SDK.
