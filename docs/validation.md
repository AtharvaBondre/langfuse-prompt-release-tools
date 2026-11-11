# Validation record

Recorded **11 November 2026** for 0.4.0a1. The alpha contains a standard-library release engine and a source-checkout captured-output example. Validation levels below are separate; their counts must not be added into a claim about independent integration scenarios.

## Current local checks

| Check | Observed result |
|---|---|
| Full source suite, macOS ARM64 / Python 3.14.7 | 118 tests passed: 110 release/runtime tests and eight captured-output tests. |
| Python 3.11.15 regression checks | 110 runtime tests and the eight example tests passed in separate runs. |
| Clean wheel build/install, Python 3.14.7 | Built 0.4.0a1, installed without runtime dependencies into a fresh virtual environment. |
| Installed wheel outside the checkout | 110 portable tests passed; all five installed CLI help commands passed. The example is source-only and is not part of the wheel. |
| Pinned OpenAPI fixtures | 18 expectations passed against the original pinned schema `e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3`, SHA-256 `301fc5d38a5d4bc2dc97f517e205ea2b0c145a993eec93f490e3a958408de6f6`. Includes intentional schema/runtime discrepancies, not 18 accepted server operations. |
| Repository skill | Frontmatter, naming and scaffold validation passed; reviewed against missing-evidence, uncertain-POST and intended-direct-mode scenarios. No claim of marketplace installation testing. |

The source tests block real network access and use synthetic prompt state. The schema checker fetched only the pinned public schema. No Langfuse tenant, Docker deployment or model was called in this validation session.

The [CI workflow](../.github/workflows/ci.yml) runs source tests on Ubuntu Python 3.10, 3.11 and 3.14, plus macOS/Windows Python 3.14, and builds/tests an installed wheel. Check the [actual workflow results](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/ci.yml) for the commit you use; a configured matrix alone is not a passing result. POSIX permission tests do not establish Windows ACL privacy.

## Existing deployment evidence

[DEMO-RESULTS.md](../DEMO-RESULTS.md) records the **17 October 2026** Gist validation: two executions of the same 15 assertions against one disposable Langfuse v4 ARM64 image set. Each execution used six POSTs, two PATCHes and zero model calls. It demonstrated a failed synthetic output blocking promotion, staged versions, label movement, injected post-commit client failure and recognition of an already-completed promotion.

Those results remain historical. The current integration runner's explicit staging/evaluation calls are compatible with the new defaults, but a fresh live integration run was not performed for this alpha. Source review of Langfuse v4.78.0 is not a substitute for that test.

## What remains unproven

Cross-version live compatibility, model quality, application cache adoption, transitive child-dependency stability, multiple-writer safety, remote transactions/CAS and authenticated result provenance. See [the roadmap](roadmap.md) for bounded work on these areas.
