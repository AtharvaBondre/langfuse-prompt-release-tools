# Trusted promotion and 0.8 migration

Promotion requires trusted CI evidence by default in 0.8; unsigned local
workflows need an explicit opt-out. A trusted promotion verifies
the portable release bundle with the official GitHub attestation verifier for
every attempt, including preview and an already-promoted retry. A prior
`verified_ci` report is useful evidence of an earlier check; it cannot authorize
a new promotion.

The destination host, Langfuse project ID, deployment label, expected release ID
and signer policy must come from the deployment owner's reviewed configuration.
Do not fill these expectations by reading them out of the downloaded bundle.
Select the expected release ID through the release-review process. The signed
snapshot binds the project and label; the bundle's release identity binds the
snapshot, stage, evaluation policy and dependency lock.

## Trusted CI workflow

1. Re-export the selected destination with its independently selected project
   ID. Snapshot format 2 records that project ID. Review edits, stage, capture
   execution with the dependency lock, and import passing evaluation evidence.
2. Create the portable bundle. Review its private-content inventory and contents
   before sharing it. Obtain its release ID through the reviewed release process.
3. Have the approved CI workflow attest the exact ZIP bytes. Obtain the artifact
   and signed attestation through the intended CI/artifact channel.
4. Preview promotion with the independent destination, release ID and trust
   policy. Apply uses the same gate and fresh remote checks.

For the signer-policy schema and official verifier requirements, see
[portable evidence and provenance](portable-evidence.md#independently-trusted-ci-provenance).
The policy must pin the repository, workflow, exact source commit/ref, GitHub
Actions issuer and GitHub-hosted runner, with `allow_unsigned: false`. An
administrator-controlled Sigstore root can be supplied separately when required.
No policy or trust root is accepted from the bundle.

```sh
langfuse-release-promote \
  --host https://cloud.langfuse.com \
  --project-id REVIEWED_PROJECT_ID \
  --label production \
  --bundle private-release.zip \
  --release-id REVIEWED_RELEASE_ID \
  --trust-policy deployment-policy.json \
  --attestation attestation.jsonl \
  --require-trusted-ci \
  --materialize-dir ./promotion-preview
```

Use a new, absent materialization directory for each attempt. Apply the reviewed
release with the same expectations and `--apply`, using another new directory.
This avoids overwriting earlier reports and reruns signature verification.
Promotion still requires the current project identity, target versions, label
baseline, dependency closure and evaluation freshness to pass. Signing cannot
make stale remote evidence current or replace those checks.

Ordinary bundle verification validates a remote receipt at its original
evaluation time. Promotion checks that same preserved receipt against the
current time and the pinned maximum age. If it has expired, verify remote
persistence again, import a new evaluation report, rebuild the bundle and obtain
a new attestation for its new bytes. Do not replace a file inside the signed
materialization. Refreshing only evaluation/remote receipts may leave the
release ID unchanged; the ZIP digest and its required signature still change.
Promotion checks receipt age and integrity, while the separate remote verifier
performs the trace/score readback.

## Materialization and file boundaries

The helper copies the explicit external inputs into a private temporary
directory before invoking the official verifier. It verifies the inventory,
original file bytes, release identity and signature together, then writes only
the payload bytes returned by that same verification. It never reopens the
untrusted ZIP for extraction after authentication.

Only the documented snapshot, prompt and evidence roles are materialized.
Archive paths cannot escape the new directory. Materialization never merges
into an existing tree, follows a symlink, uses an original receipt locator as a
destination, or executes bundled files. On POSIX, directories use mode 0700
and materialized files use mode 0400 without executable bits. Windows deployments
must provide appropriate private-directory ACLs.

Copied verifier inputs and materialized evidence are flushed and synchronized
before promotion can begin. POSIX also synchronizes their new directory entries;
Windows has no portable directory synchronization API. A synchronization failure
stops the gate before it yields evidence to the promoter. These filesystem
operations remain subject to the storage device's durability guarantees.

Original receipts and their absolute file locators are preserved byte-for-byte.
Those locators are historical data. Evaluation reads the verified role objects
directly, preserving their original fingerprints, and applies current freshness
rules. It does not rewrite a receipt and claim the rewritten bytes were signed.
The old producer filesystem can be absent.

The gate checks the external inputs, its private copies and every materialized
evidence file for changes. The promoter repeats this check before writes and
on final verification. A supplied local file outside the materialized role paths
cannot substitute for signed evidence, even when its contents happen to match.
Independent policy or evidence changes during an attempt stop that attempt.
Restart with reviewed inputs to rerun provenance verification.

With `--materialize-dir`, evidence and new promotion reports remain after the
attempt for inspection. The temporary verifier inputs are removed. Retained
materialization and saved trust claims do not themselves authorize another
attempt; enter the gate again with the original bundle and external policy.

## Explicit local compatibility

For a local workflow without signed CI evidence, explicitly select
`--allow-unsigned-local`. This grants no CI identity claim. An unsigned bundle
still requires the expected release ID, destination binding, structural evidence
and current promotion checks. For unsigned bundles, library callers set
`require_trusted_ci=False, allow_unsigned_local=True` on `trusted_promotion`,
then pass `allow_unsigned_local=True` to `promote`. Direct local-file promotion
uses `promote(..., allow_unsigned_local=True)` without a bundle context.

Required CI and unsigned compatibility are mutually exclusive. A supplied
invalid signature or unauthorized signer always fails, including in local
compatibility mode. There is no fallback from a failed signature to unsigned
execution. `--allow-structural-only`, where supported for local evidence, is a
separate evaluation decision and cannot remove a pinned policy or bypass CI trust.

Existing format 1 snapshots and bundles remain readable for inspection.
Promotion and other writes require re-export with an explicit project ID and
new stage/evaluation evidence. Do not add `project_id` to an old file by hand:
that changes its snapshot identity and invalidates the evidence chain. The
portable ZIP envelope remains bundle format 1; it can contain the new snapshot
format 2 without rewriting older artifacts.

## Python integration contract

```python
from langfuse_release_trust import trusted_promotion
from promote_langfuse_prompts import promote

with trusted_promotion(
    bundle_zip,
    expected_host=reviewed_host,
    expected_project_id=reviewed_project_id,
    expected_label=reviewed_label,
    expected_release_id=reviewed_release_id,
    trust_policy_path=independent_policy,
    attestation_path=signed_attestation,
    materialize_dir=new_private_directory,
) as evidence:
    report = promote(
        client, evidence.manifest_path, evidence.stage_report_path,
        label=reviewed_label, project_id=reviewed_project_id,
        evaluation_report=evidence.evaluation_report_path,
        dependency_lock=evidence.dependency_lock_path,
        trusted_evidence=evidence,
    )
```

`trusted_promotion` defaults to required signed CI. It accepts no saved verifier
result argument. `evidence.report` returns a copy of the current verification
observations and independent destination expectations. The object authorizes
only one promoter call and expires at context exit. A separate preview or apply
needs a new context and verifier invocation. Without `materialize_dir`, all materialization is temporary;
persist the resulting promotion report externally before exiting if needed.

The promoter calls `authorize_attempt` once, checks the exact materialized role
paths with `assert_paths`, calls `validate_evaluation` for fresh evaluation checks,
and calls `check_integrity` before label writes and final verification. Custom
integrations must preserve the same ordering and live checks.

The signature establishes that the selected workflow attested those artifact
bytes. It does not prove evaluator quality, honest model outputs, secure CI code,
application adoption or a successful current deployment.
