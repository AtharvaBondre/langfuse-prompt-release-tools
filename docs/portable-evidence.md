# Portable evidence and CI provenance

A portable bundle carries the original release files together. It can be moved
to another runner and verified without the producer's filesystem. Signature
verification is optional and uses an independent trust policy plus the official
GitHub CLI. No command in this workflow uploads evidence.

## Create and inspect a private bundle

After a passing experiment import:

```sh
langfuse-release-bundle create \
  --manifest release/snapshot.json \
  --stage-report release/reports/stage.json \
  --evaluation-report release/reports/experiment.json \
  --out private-release.zip

langfuse-release-bundle inventory --bundle private-release.zip
```

The creator returns an inventory, total size, byte digest and `release_id`.
Review the inventory **and actual contents** before sharing. Prompts,
configuration, dataset inputs, expected/actual outputs and original local file
locators can be private. Creation uses mode 0600 on POSIX and never overwrites
an existing bundle. Windows users must also use a private directory with suitable
ACLs; POSIX mode bits do not establish Windows permissions.

The creator collects only the snapshot's prompt files and the explicit release
evidence graph. It does not scan the workspace or collect `.env`, keychains,
credential directories or arbitrary attachments. Recognizable credential fields,
GitHub tokens, Langfuse secret keys and private-key PEM data block creation.
This is an additional guard, not an exhaustive secret detector. Do not put real
credentials in prompts, metadata or experiment outputs.

## Bundle format 1

The ZIP contains `bundle.json`, a snapshot under `snapshot/`, its original
relative prompt files, and these fixed evidence roles:

- `stage`
- `dependency_lock`
- `capture`
- `results`
- `evaluation`
- Optional `remote_verification`, only when referenced by evaluation

The reviewed policy remains embedded, byte-for-byte, in the original snapshot.
Every evidence file retains its original bytes, including existing absolute file
locators. `bundle.json` records safe **relative** role and JSON-pointer mappings;
verification resolves those mappings without consulting the old locators.
Original receipts are not rewritten or re-signed during packing.

Inventory entries contain `path`, `role`, raw-file `sha256` and `size`. Prompt
entries also contain `snapshot_file`, their original snapshot-relative path.
`references` describes the evidence edges. `release` contains the host and
canonical snapshot, stage, policy and dependency-lock digests. `release_id` is the
canonical fingerprint of that object. `manifest_sha256` binds the inventory;
the outer ZIP's raw-byte digest is the subject for signed provenance.

The ZIP uses sorted files, fixed archive timestamps and stored bytes. Identical
inputs produce identical ZIP bytes. There are no extraction commands or scripts
inside the bundle. Verification accepts only bounded regular entries: at most
10,000 evidence files and 128 MiB including inventory. Path traversal, absolute
paths, symlinks/devices, encrypted files, case-colliding names, duplicate entries
or JSON keys, unsupported roles, unlisted/missing files and changed bytes fail.
ASCII portable paths avoid Windows device names and trailing-dot ambiguities.

## Verify on another machine

```sh
langfuse-release-bundle verify \
  --bundle private-release.zip \
  --expected-release-id REVIEWED_RELEASE_ID
```

Use a release ID selected through the review/deployment process. Taking the
expected ID from the untrusted bundle itself does not prevent replay. Without an
external ID the result explicitly says `release_binding: self_consistent`;
with a matching expected ID it says `expected_release`. A bundle from another
snapshot, host, stage, policy or dependency lock fails that comparison.

The verifier checks the original receipt digests, policy, dataset cases, typed
checks, runtime prompt/version/content observations and dependency-lock closure.
It reconstructs only a private temporary snapshot to reuse the existing snapshot
validator. Original files may be absent. A dependency lock is always required in
format 1; a passing receipt without it is insufficient.

Optional remote verification is preserved as historical experiment evidence.
It does not claim that an old result is freshly observed now. Promotion must
apply its current freshness policy separately.

This version bundles passing policy-based experiment receipts. Legacy structural
receipts and failed experiments remain available as local diagnostics, but do
not form a passing portable release bundle. Verification performs no Langfuse
reads/writes and says nothing about current remote labels or application adoption.

Python entry points:

```python
created = create_bundle(manifest, stage, evaluation, output_zip,
                        dependency_lock_path=lock)  # lock optional if capture references it
checked = verify_bundle(output_zip, expected_release_id=reviewed_release_id)
```

## Independently trusted CI provenance

Store this policy outside the artifact, under the verifier/deployment owner's
control. Substitute the actual reviewed repository, workflow, commit and ref:

```json
{
  "format_version": 1,
  "repository": "AtharvaBondre/langfuse-prompt-release-tools",
  "workflow": ".github/workflows/validate.yml",
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "source_ref": "refs/heads/main",
  "oidc_issuer": "https://token.actions.githubusercontent.com",
  "runner": "github-hosted",
  "allow_unsigned": false
}
```

A publishing workflow can attest the ZIP with the official
[`actions/attest-build-provenance`](https://github.com/actions/attest-build-provenance)
action. Attestation creation and upload are separate, explicitly configured CI
operations. Never attest or publish a bundle containing private customer evidence
to a public repository. Repository examples use synthetic data.

Download the signed attestation bundle through a trusted CI/artifact channel,
then verify:

```sh
langfuse-release-provenance \
  --artifact private-release.zip \
  --trust-policy trusted-policy.json \
  --attestation attestation.jsonl
```

Or pass `--trust-policy` and `--attestation` to bundle verification, which binds
the signature check to the same bytes whose release semantics passed. A separately
supplied `--trusted-root` can point to administrator-controlled Sigstore trust
material for offline verification. No root, issuer or trust policy is loaded from
the release bundle. Default trust-material refresh by `gh` may use the network;
full offline signature verification requires pre-provisioned official trust
material. Ordinary bundle semantics verification is fully offline.

`verify_provenance(artifact_path, trust_policy_path, *, attestation_path=None,
trusted_root_path=None, timeout=60)` executes `gh attestation verify`. It never
accepts saved verifier-result JSON as authentication and never implements its own
cryptography. It pins the repository, workflow, exact certificate SAN, source
commit/ref, signer commit, issuer, predicate type and GitHub-hosted runner.
The verifier then checks the authenticated certificate fields, trusted timestamps
and artifact subject from the official verifier's JSON output. Workflow-controlled
`statement.predicate` fields are **not** used to establish signer identity.

Format 1 deliberately supports same-repository workflows on GitHub-hosted runners.
Reusable workflows in another repository, self-hosted runners and enterprise
issuers require a separately designed policy version; widening a regex is not an
implicit fallback. The implementation was developed against the documented
GitHub CLI 2.89 attestation command/output contract. Incompatible CLI output or
missing required flags fails closed.

Results distinguish:

- `verified_ci`: the official verifier accepted the signature and every external
  policy expectation matched.
- `unsigned_local`: no attestation was supplied and external policy explicitly
  allowed unsigned input. It does not mean authenticated execution.

A supplied bad signature or unauthorized signer always fails, even when unsigned
local compatibility is allowed. With no policy/signature options, ordinary bundle
verification explicitly classifies its trust as `unsigned_local`.

`artifact_sha256` hashes original file bytes. `trust_policy_sha256` fingerprints
the parsed policy using the core canonical JSON routine. Returned identity claims
are observations from the actual verifier invocation. They must not be replaced
with caller-provided `authenticated: true` or a previously saved result blob.

A valid signature establishes which authorized workflow attested those bytes.
It does not prove evaluator quality, honest model outputs, secure workflow code,
complete traffic coverage or that the release is currently deployed.
