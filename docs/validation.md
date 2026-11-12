# Validation record

The 0.8 release validation is in progress. This version adds project-bound snapshots, trusted promotion, durable local recovery, supported result helpers, a persistent JavaScript collector and a hosted-dataset scenario.

The earlier [0.7 record](validation-v07.md) is preserved. Previous green runs do not establish that this update passed; this page will record the new source, installed-package, signed CI and real SDK/server checks before release.

## Scope

The reviewed server matrix uses official Langfuse **4.59.0 and 4.78.0 Linux AMD64** images, Python SDK **5.2.0** and JavaScript SDK **6.1.1**. Exact digests are pinned in `tools/run_live_ci.py`. Runs use disposable synthetic data with zero model-provider calls.

The real-application pilot remains deferred. No production adoption, customer deployment, model-quality result or application-overhead measurement is claimed.
