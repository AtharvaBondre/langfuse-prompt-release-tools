# Validation record

Recorded **12 November 2026** for **0.7.0a1**. This page records the local source and package checks. The [release notes](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/releases/tag/v0.7.0a1) link the CI runs for the release commit. [Machine-readable local evidence](validation-v07.json). The [0.6 record](validation-v06.md) preserves its earlier live matrix and signatures; those historical results are not substituted for a new release check.

| Check | Observed result |
|---|---|
| Source suite, macOS ARM64 / Python 3.14.7 | 443 tests passed, including the official-schema check in the review environment. |
| Clean installed Python wheel outside checkout | 428 tests run: 427 passed, one optional schema check skipped in the dependency-free environment. All seventeen command help checks passed. |
| Unpacked source distribution | 443 tests passed. Required policy fixtures and source tests are included; no runtime credentials, virtual environments or node_modules are packaged. |
| JavaScript source | 18 tests passed; helper behavior is unchanged in this release. |
| Installed JavaScript helper + installed Python wheel | The same 18 tests passed in a separate temporary consumer directory. |
| New policy helper | 29 tests cover explicit requirements, source/case fingerprints, invalid inputs, no overwrite, example compatibility and hosted-policy/verifier interoperability. |
| New status inspector | 31 tests cover evidence recomputation, missing/ambiguous inputs, path containment, stale/uncertain evidence, malformed metadata, subset staging and unchanged stages. |

Test counts overlap across packages and environments. They are not separate production scenarios.

## What changed in 0.7

The policy helper uses the existing policy v1/v2 formats. It computes identities from selected cases and source files without executing them, selecting thresholds from results or pinning the output. A hosted remote policy requires its actual timezone-aware dataset version and the API's two-field schema wrapper when schema evidence is supplied.

The status inspector reads local release files and recomputes supported evidence checks. It distinguishes a passing evaluation, a recorded label move and observed SDK use. It handles selected prompt subsets and reports a valid no-op as `recorded_unchanged` with `no_changes_to_promote`. Malformed nested metadata marks the affected section invalid instead of crashing the whole inspection.

Inspection does not contact Langfuse, verify CI signatures or authenticate the author of a saved file. A historical label result does not prove its current server value. Missing, stale, failed and ambiguous evidence remains visible. Exit 0 means the inspection completed without invalid/ambiguous inputs; it is not release approval.

## Release CI and compatibility

The release is checked by the repository's [CI](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/ci.yml), [portable signed evidence](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/evidence.yml), and [live SDK compatibility](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/live-sdk.yml) workflows. Follow the release-note links for the exact commit, job conclusions and sanitized live summaries.

The configured live matrix uses Langfuse **4.59.0 and 4.78.0 on Linux AMD64**, Python SDK **5.2.0**, JS SDK **6.1.1**, Python **3.14** and Node **22**. It uses disposable servers, synthetic tasks and zero model calls. The older server runs 24 assertions; the newer server adds 14 prior recovery/adoption assertions. Exact image digests and limits remain in `tools/run_live_ci.py`. This is not validation of every intervening version or of ARM64 servers.

The two new commands are tested with local fixtures, including GET-only remote-verifier interoperability. The live demo exercises the existing SDK/release path; it does not pretend the new inspector performs a live read.

## Remaining evidence limits

The maintainer deferred the real-application pilot because no existing application/test environment is available yet. Synthetic demos and signed CI artifacts do not establish customer adoption, production reliability, complete fleet coverage or model quality. No customer deployment was performed for this release.

Hosted-dataset API behavior has local verifier coverage, but the current live matrix uses local synthetic datasets. Hosted-dataset live validation, project-ID checks before every mutation, signer-required promotion and capture integration improvements remain on the [roadmap](roadmap.md).
