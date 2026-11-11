# Upgrading to 0.6

The stdlib Python core remains compatible with Python 3.10+. The official Python
SDK example needs Python 3.11+. JavaScript capture needs Node 22+ and its separately
installed official SDK dependency. Existing 0.5 policy and receipt formats remain
readable; their old remote completion flags remain producer statements.

## Require independently checked remote persistence

For new releases, pin a format 2 policy **before** staging. Keep the existing
producer, dataset, execution, target and check fields, and add:

```json
{
  "format_version": 2,
  "remote_export": "required",
  "remote_verification": {
    "project_id": "your-independently-selected-project",
    "dataset_kind": "local",
    "max_age_seconds": 300
  }
}
```

This is an excerpt, not a complete policy. Use `dataset_kind: "langfuse"` only
for a captured hosted dataset. Finish the actual SDK experiment, retain its
source/capture bytes, and run `langfuse-release-remote`. Pass the confirmed report
to `langfuse-release-import-experiment --remote-report PATH`. Both import and
promotion check that report's identity, contents and freshness. A `flush()` or
caller-supplied `confirmed` cannot satisfy this policy. A persisted failing score
still fails evaluation. Explicitly type BOOLEAN evaluators; NUMERIC 1 is not a
substitute for BOOLEAN true.

Do not upgrade an old capture by rewriting it or claiming a different project.
Review a new policy and rerun the actual task. Policy version 1 is retained for
compatibility; its `remote_export: required` does not provide this built-in audit.

## Preserve and transfer evidence

`langfuse-release-bundle` stores original prompt/source/receipt/lock bytes plus a
relative inventory. It does not rewrite the original absolute references or read
them on the destination machine. Verification rederives case/check semantics
from bundled objects. Historical remote evidence is checked at its recorded
evaluation time; this does not turn an old receipt into fresh promotion authority.
Live promotion continues to require the original sources and a fresh report.

Keep bundles private. They contain prompts, dataset values and outputs. The CI
workflow in this repository uploads only a hardcoded, explicitly synthetic
fixture. It has no input for a private release directory. Unsigned local bundles
are classified as such. Configure trusted repository, workflow, commit, ref,
issuer and runner independently before verifying a signature. The verifier uses
the official `gh attestation` implementation and an independent trusted root.

## Capture application use

The JavaScript helper snapshots actual official SDK prompt objects and delegates
hashing/contract checks to the Python sidecar. Install both components and choose
a trusted Python interpreter. This synchronous bridge is for experiments and
bounded probes, not automatic instrumentation of every high-volume request.

Pin expected consumer/deployment/SDK identities and a rollout window before
collecting observations. Bind each actual access promptly. The report counts
distinct requests per target and time bucket, rejects replay, and separates
fallbacks, stale caches and content/version mismatches. Its recovery output is a
read-only proposal. It does not roll labels back or retry uncertain creations.

See the [compatibility and validation record](validation.md) for tested SDK/server
pairs. A server missing required APIs fails explicitly; an SDK's advertised
minimum version alone is not compatibility proof.
