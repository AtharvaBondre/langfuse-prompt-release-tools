# Validation record

Recorded **11 November 2026** for **0.6.0a1**, after all four planned additions passed their implementation gates. [Machine-readable evidence](validation-v06.json). The [0.5 record](validation-v05.md) and [0.4 record](validation-v04.md) remain historical.

| Check | Observed result |
|---|---|
| Source suite, macOS ARM64 / Python 3.14.7 | 383 tests passed, including the official-schema check in the review environment. |
| Clean installed Python wheel outside checkout | 367 tests passed; one optional schema check skipped because the stdlib-only environment has no jsonschema. All fifteen command help checks passed. |
| Unpacked source distribution | 383 tests passed; original fixture bytes and upstream license preserved; no credentials, virtual environments or node_modules included. |
| JavaScript capture | 18 source tests passed. The same 18 passed with the packaged JS helper and installed Python wheel in a separate temporary application directory. |
| Official result schema | Two actual synthetic SDK result artifacts passed unchanged Action v1.0.12 schema validation with format checking. A dedicated CI job installs review dependencies. |
| Cross-platform CI | Nine jobs passed: Python 3.10/3.11/3.14 on Linux, Python 3.14 on macOS/Windows, installed wheel, Node 22/24 and official schema. |
| Portable signed evidence | Two jobs passed: Linux built and signed synthetic bundle/rollout artifacts; a separate macOS machine verified original bytes, externally expected signer identity and seven rejection controls. |
| Real SDK/server matrix | Langfuse 4.59.0 passed 24 live assertions; 4.78.0 passed those plus 14 recovery/adoption assertions. Both ran on Linux AMD64 with official Python SDK 5.2.0 and JS SDK 6.1.1. Cleanup completed; zero model calls. |

All thirteen implementation CI jobs are green at `4117f43`: [tests](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43025012317), [signed evidence](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43025012383), [live SDKs](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43025012274). Counts overlap across environments; they are not independent production scenarios. Check workflow runs for the release commit when upgrading.

The maintainer explicitly deferred the operational pilot on 11 November 2026 because no existing application/test environment is available yet. The implementation/integration gates above are complete, but synthetic examples do not supply production-consumer feedback.

## Tested compatibility

| Server | Architecture | SDK/runtime | Result |
|---|---|---|---|
| 4.59.0 | Linux AMD64 | Python SDK 5.2.0 / Python 3.14; JS SDK 6.1.1 / Node 22 | Complete new workflow passed. Lowest tested server. |
| 4.78.0 | Linux AMD64 | Same SDK/runtime pair | Complete new and prior recovery workflows passed. |
| 4.78.0 | Linux ARM64 on local macOS | Python SDK 5.2.0; JS SDK 6.1.1 | Prior 14 checks and Python remote flow passed; JS failure case persisted and was blocked. A subsequent local export timed out amid Redis/database disruption; retained as incomplete, not full ARM64 proof. |
| Other servers/SDKs/architectures | — | — | Not validated by this release. Missing API or resolver evidence fails explicitly. |

Exact AMD64 image digests and memory limits are recorded in the JSON evidence and `tools/run_live_ci.py`. The Compose source remains unchanged; CI supplies architecture-specific images and disposable credentials. Only the two listed server versions were tested, not every intervening version.

## What the live checks establish

Actual SDK tasks fetched pinned prompts, compiled server-resolved nested dependencies and evaluated deterministic outputs. The verifier read every case and required typed score from Langfuse. Persisted failing JavaScript scores still blocked promotion. Moving a nested dependency invalidated passing evidence; a new lock and actual rerun restored it. A warmed JS cache returned the old version; uncached access returned the release, and fallback remained explicit.

The rollout test captured four actual accesses across Python and JS consumers, covering both buckets of a predeclared eight-second window. Replaying a request did not increase accepted coverage. Recovery tests retained an injected lost PATCH response, reconciled by GET and avoided repeating the mutation.

Local completion, remote persistence, portable integrity, signer identity and application observations are separate facts. Signed CI uses explicitly synthetic historical fixtures; it does not authenticate a customer runtime or prove model quality. Observations describe supplied instrumented samples, not an entire fleet. API reads cannot provide an atomic remote release or compare-and-swap guarantee.

## Defects caught before release

Review and actual runs caught incompatible GitHub CLI identity flags, freshness measured before slow signature verification, untyped Python BOOLEAN values persisted as NUMERIC, JS BOOLEAN scores dropped when supplied as JS booleans, historical timestamp conversion, missing API probe time bounds, SDK/runtime mismatches and Windows line-ending changes to original fixtures. Each was corrected and checked. Pending exports and infrastructure timeouts were preserved; none was relabeled successful.

Private runtime/config files never enter CI artifacts. Live tests upload only sanitized logs and summaries. Provenance artifacts contain fixed public synthetic fixtures. POSIX mode checks do not establish Windows ACL privacy.
