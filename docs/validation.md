# Validation record

Recorded **12 November 2026** for **0.8.0a1**. All five implementation items passed the release checks. The code was verified at [`d705b07`](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/commit/d705b072457171d31d37be012927fbba0c427c71). The [release notes](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/releases/tag/v0.8.0a1) identify the final release commit and its CI runs. [Machine-readable record](validation-v08.json). The earlier [0.7 record](validation-v07.md) is preserved.

| Check | Observed result |
|---|---|
| Source distribution, macOS ARM64 / Python 3.14.7 with review dependencies | 530 tests run: 529 passed, one optional SDK-object test skipped. |
| Clean installed wheel outside the checkout | 514 tests run: 512 passed, optional SDK-object and schema checks skipped in the dependency-free environment. All 17 command help checks passed. |
| JavaScript source and installed package | 28 tests passed in each environment. Installed tests used the packaged Python wheel and a separate consumer directory. |
| Cross-platform CI | All nine jobs passed: Python 3.10/3.11/3.14 on Linux, Python 3.14 on Windows/macOS, wheel installation, Node 22/24, and the official artifact/schema contract. |
| Signed evidence transfer and promotion | Both jobs passed. Linux created and attested synthetic evidence; macOS verified the original bytes and used the actual promotion engine for preview, apply and a fresh no-op attempt. Unsigned and wrong-signer attempts were rejected. |
| Langfuse 4.59.0, Linux AMD64 | 40 live assertions passed. |
| Langfuse 4.78.0, Linux AMD64 | 54 live assertions passed, including the prior recovery/adoption scenario. |
| Package inventory | All 21 runtime modules present. Required source fixtures/examples present. No private runtime data, keys, environments, node_modules or unsafe archive paths detected. |

Test counts overlap across environments; they are not separate production scenarios. Optional dependency checks omitted from the clean wheel environment are covered by the dedicated schema job, pinned SDK helper tests and real SDK runs.

## Reproducible CI evidence

- [Cross-platform and package checks](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43196790713)
- [Real signatures and promotion](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43196790482)
- [Live SDK/server matrix and sanitized summaries](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/runs/43196790635)

The matrix pins official Langfuse **4.59.0 and 4.78.0 Linux AMD64** images, Python SDK **5.2.0**, JavaScript SDK **6.1.1**, Python **3.14** and Node **22**. Exact image digests and resource limits are recorded in `tools/run_live_ci.py` and the run summaries. Each job uses disposable synthetic data, makes **zero model-provider calls**, and verifies cleanup. Untested intermediate versions and ARM64 server images are not inferred to be compatible.

The hosted scenario checks actual dataset IDs, version timestamps, schemas, typed case/run scores and pagination. Missing score export stays pending; changed identity, version or schema cannot pass. The JavaScript experiment uses the persistent collector and verifies that its acknowledged queue closes without loss. The existing cases also cover failed evaluations, nested dependency changes, cache/fallback behavior and an observed rollout window.

## Failures found and corrected

The first CI pass exposed a Windows difference between path and descriptor timestamp representations. The reader now compares each API's before/after metadata separately while retaining file identity, byte-count, symlink and change checks. Two tests also needed portable JSON-path comparison and explicit clearing of the Windows read-only attribute for tamper injection.

The newer-server run caught an older demo client that did not accept the new project argument. Both disposable demo clients were corrected and a constructor regression was added. Review also found and fixed duplicate JSON ambiguity, a misleading project in the status view, missing materialization synchronization, and a cross-project remote-policy mismatch. Final CI passed after these corrections; failed runs are not counted as compatibility evidence.

## Limits of this evidence

Signed promotion is tested against a synthetic HTTP fixture with real CI signatures. Real Langfuse server tests exercise the same release engine using explicitly selected unsigned local evidence. These are distinct test environments; no production deployment is implied.

Process-crash tests preserve durable journals and unknown outcomes without retrying writes. They do not simulate power loss or provide distributed exclusion. File synchronization remains subject to storage guarantees; Windows has no portable directory-sync API.

The existing-application pilot remains deferred. No production adoption, complete fleet coverage, model-quality result, customer deployment or production capture-overhead measurement is claimed. The persistent collector is opt-in until an application pilot supplies those measurements.
