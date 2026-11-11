# Inspecting an interrupted release

`langfuse-release-reconcile` reads a release receipt and its matching edited snapshot, then checks the current Langfuse versions and deployment labels. It explains the outcome of each recorded item without submitting, retrying or rolling back a write.

```bash
langfuse-release-reconcile \
  --host https://cloud.langfuse.com \
  --manifest review/prompt-index.json \
  --release-report review/reports/stage-interrupted.json
```

From a source checkout, use `python reconcile_langfuse_release.py` with the same arguments. Authentication uses `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in the environment. The command uses a client that rejects every non-GET request before opening a connection, including inherited POST and PATCH methods. It does not execute commands found in reports.

The input must be a format-version-2 publication or promotion receipt produced by this project, with its original edited prompt files and manifest. The snapshot hash and host must match. Missing, corrupt, unsupported or mismatched input fails before HTTP. Recover the preserved files instead of manufacturing a success receipt. A receipt can be inspected whether it stopped, completed or only previewed a release.

## What the classifications mean

| Classification | Evidence and next decision |
|---|---|
| `completed` | An acknowledged created version matches the intended content/config/tags and expected labels, or an exact promotion target and its deployment label agree. An unchanged item can also be complete. This describes the state observed now, not who performed the write or whether applications have adopted it. |
| `pending` | A publication plan records no submitted create and its baseline still matches, or a known staged promotion target matches while the deployment label still selects the baseline. A label may have moved and been reverted between sessions; this is not a historical claim. Review separately before any future write. |
| `conflicting` | Successful reads show a different content/config/tag or deployment-label state. For example, another version now holds production. No corrective write is attempted. |
| `unknown` | A create has no saved acknowledged version, reads fail/change, local evidence changes, or a preflight crash left selection scope unrecorded. Preserve the uncertainty; absence of a matching version does not authorize retry. |

Known targets and their deployment labels are read twice using explicit version/label selectors and `resolve=false`. If relevant state differs between rounds, the item becomes unknown. Two matching rounds are **not an atomic snapshot**: another client can write afterward, or move and restore a label between reads. This command provides no distributed lock, compare-and-swap or transaction.

For staging, completion requires the exact created version to remain unlabeled except for server-managed `latest`, and the baseline deployment labels to remain on the baseline. If the version was subsequently promoted, reconciliation of that older stage receipt can report a conflict with its original staging state. Inspect the promotion receipt to assess that later operation. A newer unrelated `latest` version is not mistaken for the acknowledged created version.

Publication receipts contain all selected plans before the first write. Reconciliation respects a completed `--name` subset; it does not infer releases for excluded snapshot prompts. During an incomplete publication preflight, missing records are shown as unknown because the original selected scope cannot be recovered reliably.

## The lost-POST boundary

A server can create version 8 while the POST response is lost. If the receipt has no `created_version`, reconciliation lists all versions for the exact prompt name, then fetches concrete versions newer than the recorded baseline. Content, type and config matches appear under `candidate_versions`.

**One exact candidate still does not prove request identity.** Another caller may have submitted identical content. Tags are mutable across prompt versions, and labels can move later, so neither is used as proof of the original creator. Their current values and whether they match the intended state are recorded with each candidate.

The default limit is 100 newer versions per uncertain item. Change it with `--max-versions`, between 1 and 10000. The listing is read again after inspection. A limit hit or changed version listing sets `candidate_scan_complete` to false. Whether the scan finds zero, one or several matches, an unacknowledged POST remains unknown and is never retried automatically.

By contrast, a promotion receipt already pins its PATCH target. After a lost PATCH response, exact-version and label reads can establish that the intended state exists now. They do not prove that the original request, rather than another actor, caused it.

## Evidence and failures

The original release report is never updated. A separate timestamped report is reserved exclusively under the snapshot's `reports/` directory before HTTP. Supply `--report inspection-001.json` to choose a new filename. Existing receipts, manifests and prompt files cannot be overwritten. Files use mode `0600` on POSIX; POSIX mode bits do not establish Windows ACL privacy.

The reconciliation report records:

- The exact source-report SHA-256, snapshot hash, host and timestamps.
- Each original status, acknowledged target, classification and explanation.
- Observed version/label/tag metadata and state hashes, without copying prompt bodies or credentials.
- Candidate versions, scan completeness, four classification counts and source-evidence stability.

The source report and snapshot are rechecked at the end. Changed or unavailable evidence invalidates all completion conclusions. If an output write fails, the command fails rather than reporting success; a previously reserved report may remain with `status: inspecting`. Fix local storage, preserve the source and choose a fresh report filename to inspect again. A disk failure after a server POST may leave only `submitting` in the original receipt; this correctly follows the unknown-POST path.

Exit codes are `0` when all inspected items currently match, `2` when any item is pending/conflicting/unknown, and `1` for invalid input, authentication setup or output-storage failure. An HTTP inspection failure is recorded as an unknown item and exits `2` when the report can be saved.

This is read-only operational evidence. It does not authenticate unsigned input reports, re-approve evaluation policies, prove runtime adoption, reconstruct missing release intent, or promise rollback safety. Technical API assumptions follow [the pinned official platform contracts](official-platform-contracts.md).

## Validation

Run `python -m unittest -v test_reconciliation`. Tests use synthetic in-memory HTTP and block real network access. They cover committed POST/PATCH responses lost in transport, partial batches, unknown identical candidates, newer unrelated versions, concurrent label movement, bounded/inconsistent listings, changed local evidence, missing/corrupt receipts, post-commit disk failure, output-storage failure, private output, CLI status and transport rejection of all mutations.

These tests do not establish compatibility with a live tenant or server permissions.
