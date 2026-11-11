# Release checks and trade-offs

## From local edits to a deployment label

A prompt edit can change the inputs a caller must supply. Creating its next version can move several labels at once. The workflow gives you a diff to review, a version to test and a separate command for moving one deployment label.

Langfuse already supports staging and label promotion. These helpers use those APIs to check a local release and record the result of each write.

| Step | Checks and effects |
|---|---|
| Export | Fetch the chosen label or version with `resolve=false`; save raw text/chat content and its baseline. |
| Preview | Compare local edits and current remote state across the selected batch. Check contract drift before any POST. |
| Stage | Recheck each pending item, create without deployment labels and fetch the returned version. Creation advances `latest`. |
| Application test | Run your checks against the recorded version and record their results. The recorder executes no test commands or models. |
| Promote | Require passing evidence, check the baseline label and target, then move one label and read it back. |

Direct release requires `--allow-direct-release`. It creates a version with the baseline's non-`latest` labels, moving those labels during POST. Choose it with that effect in mind.

## Bind promotion to the reviewed files

Format-2 reports contain a canonical JSON hash of the loaded snapshot, including the local prompt content. The promoter accepts a completed stage report and rejects a different snapshot, local edit or target host. It records the stage report's hash in the promotion report.

The chosen label must have appeared on each original baseline. Before PATCH, it must still resolve to that baseline with the same content, config, tags and non-`latest` labels. The staged target must match the intended content and carry no conflicting deployment labels. An already-promoted target must carry the chosen label and pass both version and label reads; a repeat run then skips its PATCH.

Keep the files unchanged between these steps. Hashes detect changed inputs; they do not authenticate the author of a snapshot or report. Use a private working directory without concurrent edits. Start with a new export for this version; v01 reports do not provide the required binding.

## Gate promotion on recorded application checks

`evaluate_langfuse_release.py` consumes results from a trusted local application or CI runner. The caller supplies a non-empty, unique set of required check IDs. Results must cover each changed staged target at its recorded version, with those check IDs and literal JSON booleans. Valid failing results produce a failed receipt; missing cases, duplicate IDs or malformed values stop recording.

A receipt binds the source results, stage report, snapshot and versions by canonical JSON hash. The promoter validates these bindings and rereads the source files before each PATCH. Changed evidence blocks the next write; the report retains earlier outcomes. `EVALUATION.md` describes the format and failure cases.

This is an unsigned local attestation. A person with access to replace the files and hashes can forge it. Use a runner you trust and keep its original results. The recorder proves neither execution nor model quality. A deterministic check of JSON shape measures that property; it cannot establish that an answer is correct.

Promotion requires evaluation by default. The CLI permits structural-only promotion only with `--allow-structural-only`. A supplied receipt is always validated. Direct publication requires `--allow-direct-release` and remains a separate path without this gate. Reports record the selected opt-outs.

## Read back the version you wrote

A label can move between requests. Fetch the exact version returned by POST to check its stored content, type, config and metadata. Use `resolve=false` so dependency expansion cannot hide the raw text you uploaded. After promotion, fetch the label too and check that it selects the intended version.

Comparison preserves text whitespace and JSON values. It accounts for the runtime's removal of the optional normal-chat `type:"chatmessage"` discriminator. The runtime also changes root-null config to `{}` on creation; reports identify that normalization. Other JSON config values retain their types.

Template-variable sets, chat-placeholder names and `OUTPUT_SCHEMA` blocks form the local contract guard. It detects additions and removals and rejects malformed markers. It does not parse the full prompt language or validate JSON Schema. The helper checks variable/placeholder name collisions before writing; the API also rejects them.

## Writes can stop halfway through

The API has no compare-and-swap or idempotency contract for these operations. Preflight and per-item rechecks reduce stale writes, but another writer can act between GET and POST/PATCH. A later failure can leave earlier versions created or labels moved. The helpers stop, preserve per-item status and avoid write retries; they do not roll back the batch.

A connection failure or server error can follow a successful mutation. Inspect the report and remote state before trying again. Promotion can recognize a matching prior success, including one whose response was lost. Inspect other unknown outcomes before issuing another request.

Staging advances `latest`, and explicit tags apply across a prompt's versions. Test and account for those consumers before staging. The helpers preserve raw dependency references but do not validate, order or update a dependency graph as one transaction. Langfuse checks dependencies during creation, so a dependency error can still stop a batch after earlier writes.

## Local data and credentials

Supply credentials through environment variables. The HTTP client requires HTTPS except for loopback testing, rejects redirects and omits response bodies from errors. Snapshots and reports contain prompt data, hostnames and local paths; keep them out of public repositories.

Exports require an empty directory and use 0700 directories and 0600 files on POSIX; receipts use 0600. These bits do not enforce Windows ACL privacy. Path checks reject traversal and symlink entries, while fresh report filenames protect previous evidence. Files remain trusted local inputs: the workflow provides no multi-process lock or signed audit trail. A filesystem failure can prevent the last status update from reaching disk; reconcile a pending submission with remote state.

## Validate the deployment and the application

The 17 October 2026 Gist passed 97 offline tests using synthetic prompts and mocked HTTP with network access blocked. Current alpha validation is recorded separately in [docs/validation.md](docs/validation.md). They exercise state checks, release order, write uncertainty, receipt validation and reconciliation.

On 17 October 2026, a separate disposable Langfuse v4 ARM64 deployment passed 15 integration assertions using six POSTs and two PATCHes. Failed synthetic application output blocked promotion and left production on version 1. Text and chat candidates staged as version 2 while production stayed on version 1 and latest advanced. The test then injected a client exception after one real PATCH succeeded; reconciliation recognized that completed move, promoted the remaining item and verified both label targets. Repeating the completed promotion issued no PATCH. This was a controlled client-side injection, not an observed network timeout or server bug.

`LOCAL-DEMO.md` provides the runnable setup. `DEMO-RESULTS.md` records the observed results and image digests. This establishes the tested workflow on one v4 ARM64 deployment. Other versions, architectures, SDK cache behavior and application adoption remain unverified. The test called no model and establishes no model-quality improvement.

The optional `validate_api_examples.py` passed 18 expectations for positive and negative fixtures using the pinned OpenAPI 3.0 schema. Install its separate `requirements-review.txt` dependencies to use it. It reads a hash-checked public schema, or the same artifact supplied with `--schema`, and makes no tenant API calls. See `API-NOTES.md` for the runtime differences the generated schema misses.

Use a disposable project on your deployment's version before a release. Run application fixtures and evaluations against the staged version, then check label consumers and SDK caches after promotion.

## Implementation history

The March 2026 toolkit used a lifecycle skill, Python pull/publish/verify helpers and a Windows round-trip wrapper. The first public copy refreshed the API handling and added batch preflight and exact-version checks. Version 2 added staging, single-label promotion and report binding. Version 3 added application-result receipts and an opt-in promotion gate. The 0.4.0a1 repository release makes staging and the evaluation gate default, requires explicit compatibility opt-outs and tightens local artifact permissions. Portable verification replaces the Windows wrapper.

The CLI and report formats have changed; these files are a maintained implementation of that workflow. Private prompts, hosts and execution records remain outside the gist. Pinned public-source references in `API-NOTES.md` explain the API choices. Treat offline, deployment-integration and model-quality tests as separate evidence.
