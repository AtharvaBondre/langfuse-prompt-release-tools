# Validation record

Recorded **11 November 2026** for **0.5.0a1**. These are separate checks, not a sum of independent integration scenarios. The [machine-readable result](validation-v05.json) contains the exact local demo assertions. The [0.4 record](validation-v04.md) and [October demo](../DEMO-RESULTS.md) remain historical evidence.

| Check | Observed result |
|---|---|
| Source suite, macOS ARM64 / Python 3.14.7 | 232 tests passed, including policy downgrade, missing/changed evidence, typed scores, nested graph drift, interrupted writes, adoption freshness and final verification. |
| Fresh installed wheel outside checkout | 224 portable tests passed; all ten installed command help checks passed with no runtime dependencies. |
| Actual official SDK/server workflow | All 14 assertions passed with Python SDK 5.2.0 and local Langfuse 4.78.0 on Linux ARM64. One real label PATCH, zero model calls. |
| Official Experiment Action result schema | Three actual SDK-generated artifacts (failed, passing before drift, passing after drift) validated against the official v1 schema from Action v1.0.12. |
| Optional SDK environment | `pip check` passed. |
| Independent review | Fixed and regression-tested acceptance of post-hoc adoption locks and stale final verification on already-promoted paths. No remaining concrete findings from that review. |

The live scenario staged a failing candidate, imported its real failed scores and blocked promotion; staged a passing candidate; moved a nested child label and blocked old evidence; evaluated again against a new lock; injected response loss **after a real successful PATCH**; reconciled by GET; and completed without repeating the PATCH. An actual SDK cache returned the old version and produced `not_adopted`; the subsequent uncached SDK access matched the released version and resolved hash.

This is a synthetic deterministic application using actual Langfuse services and SDK objects. The response-loss injection is not an observed server bug. Local evaluation results were complete, while remote trace persistence was explicitly `unconfirmed`; the demo policy allowed that. No LLM or customer/cloud tenant was used.

The current Compose template pins Langfuse 4.78.0 images:

- Web: `sha256:037188385ac6612d3494319fa060052da407b235502874c7ea79ade583c29002`
- Worker: `sha256:175a32200b9908b283104e67b84d080381e841daa8ee6692f5bb26538df1a59e`

Infrastructure image digests remain listed in `compose.demo.json`. These identities describe the local test, not other architectures or versions.

The [CI workflow](../.github/workflows/ci.yml) tests Ubuntu Python 3.10, 3.11 and 3.14, macOS/Windows Python 3.14, and the installed wheel. Check the [actual run](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/ci.yml) for your commit. POSIX file mode checks do not establish Windows ACL privacy.

Still outside the proven scope: model quality, every SDK/server combination, authenticated execution attestations, fleet-wide traffic adoption and a remote atomic release/CAS guarantee. Current receipts are unsigned and require trusted execution and independently reviewed policy. See [the roadmap](roadmap.md).
