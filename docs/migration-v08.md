# Move to 0.8

0.8 adds project checks before release operations and makes trusted CI evidence the default for promotion. Existing evidence is kept intact. Start a new release folder when moving an older release to this version.

## Select the project separately

Get the project ID from your intended Langfuse project settings. Supply it with `--project-id` to pull, publish, promote and reconcile. A matching hostname is insufficient: credentials for another project on that server are rejected, even if its prompt content happens to match.

```bash
langfuse-release-pull --host "$LANGFUSE_HOST" \
  --project-id "$LANGFUSE_PROJECT_ID" --label production \
  --name example/answer --out-dir ./release-v08
```

The new snapshot has `format_version: 2` and records `project_id`. Format 1 snapshots remain readable by local inspection and historical evidence tools. Remote release operations require a fresh export; adding an ID or changing a format number by hand cannot establish where old evidence came from. Review your edits again, pin the policy, stage and rerun the experiment.

Library callers can set `Client(host, project_id=expected_id)` or pass `project_id=expected_id` to an operation. The expected value must come from your deployment configuration, independently of the snapshot. Custom clients must implement `get_project_id()`. Never store API keys in reports.

## Choose how promotion is authorized

For CI, use a signed bundle and an independently selected host, project, label, release ID and signer policy. Follow [trusted promotion](trusted-promotion.md). Preview and apply each verify the original bytes and signature anew. Use a new materialization directory for each attempt.

For a local workflow where you deliberately accept unsigned evidence, pass `--allow-unsigned-local` or `allow_unsigned_local=True`. This choice is recorded. It keeps required evaluation, dependency, project and remote-evidence checks in place. Invalid signatures never fall back to local mode.

## Recover without replaying a write

The snapshot's `.release.lock` coordinates local toolkit operations. A busy folder fails immediately. Leave the file in place; the operating system releases its lock when the process exits, including after a crash. Do not delete it to bypass an active process.

Reports are written as complete files, flushed before replacement, and synchronized before a remote write. POSIX also synchronizes directory entries; portable Python does not provide directory synchronization on Windows. Storage hardware and network filesystems have their own durability guarantees. Use a local filesystem for these operation locks.

Reconciliation now includes a `resume_proposal`: preserve a completed result, review a new attempt, or stop and inspect an uncertain outcome. It contains no automatic retry command. A lost POST remains uncertain even if matching content appears later. A local lock cannot prevent another machine or the Langfuse UI from changing the server.

## Use the supported result helpers

Replace copied projection code with `project_python_result()` or JavaScript `projectExperimentResult()`. See the [integration recipe](capture-integration.md). JavaScript also has an optional persistent collector with bounded queues, acknowledgments and an explicit shutdown receipt. Check rejected requests and dropped samples; incomplete capture cannot establish full coverage.

The collector remains opt-in. Its application overhead will be measured during the deferred real-application pilot before recommending it on a production request path.
