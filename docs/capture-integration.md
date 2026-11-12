# Capture an application's experiment results

The supported projection helpers replace the result-mapping code previously
embedded in the examples. They reuse the official SDK runner and the release
engine's Action v1 validator. They do not run evaluators, choose a dataset,
invent IDs, infer evaluator success from script completion, or confirm remote
trace/score persistence.

The recipe below assumes an independently reviewed policy is already pinned,
the candidate is staged, and a dependency lock was captured before execution.
Dataset and task/evaluator/model identities must describe the application that
actually runs. Use synthetic inputs for this repository's disposable examples;
the official SDK sends experiment inputs, outputs and scores to its configured
Langfuse project.

## Python

```python
from pathlib import Path
from time import monotonic
from langfuse import RunnerContext
from import_langfuse_experiment import start_capture, capture_prompt, finalize_capture
from langfuse_release_results import project_python_result
from publish_langfuse_prompts import write_report

capture = start_capture(manifest_path, stage_path, dataset=dataset_identity,
                        execution=execution_identity, dependency_lock_path=lock_path)
if len(capture["targets"]) != 1:
    raise ValueError("This recipe expects one changed prompt")
target = capture["targets"][0]

def task(*, item, **kwargs):
    case_id = item["id"] if isinstance(item, dict) else item.id
    prompt = langfuse.get_prompt(target["name"], version=target["version"], cache_ttl_seconds=0)
    capture_prompt(capture, item_id=case_id, requested_name=target["name"],
                   requested_version=target["version"], prompt=prompt)
    return application_task(item, prompt)  # Existing application behavior.

started = monotonic()
result = RunnerContext(client=langfuse).run_experiment(
    name=run_name, data=actual_cases, task=task,
    evaluators=actual_evaluators, max_concurrency=1)
artifact = project_python_result(result, script_path="checks/application.py",
                                 duration_ms=int((monotonic() - started) * 1000))
write_report(Path(result_path), artifact, create=True)
langfuse.flush()
write_report(Path(capture_path), finalize_capture(capture, result_path,
             remote_export="unconfirmed"), create=True)
```

Use the actual policy producer script path. The capture functions above mutate
one Python capture object; this recipe serializes its task accesses. Projecting
a result does not make concurrent mutations of that object safe. Apply the
normal SDK shutdown lifecycle in the owning application as well as flushing a
short-lived run. Import the resulting files with the existing experiment
importer and, where policy requires it, obtain a separate remote verification
receipt before promotion.

`project_python_result(result, *, script_path, duration_ms, status="passed",
error=None, metadata=None)` returns detached JSON-compatible data. It supports
local dictionary items and official hosted `DatasetItem` objects. The generated
SDK serializer supplies hosted item timestamps and metadata; the helper maps
the expected-output alias into the common `expected_output` field. It leaves
other hosted metadata in the SDK serializer's public format.

## JavaScript

The snippets below assume an application `.mjs` file in this checkout's root.
After installing the release's JavaScript tarball into another application,
import from `langfuse-release-capture` instead of the checkout-relative path.
Use a Python interpreter with the core package installed.

```js
import { ReleaseCapture, projectExperimentResult } from './js/capture.mjs';

const capture = new ReleaseCapture({ python: '/trusted/venv/bin/python' });
const state = capture.start({ manifestPath, stageReportPath, dependencyLockPath,
  dataset: datasetIdentity, execution: executionIdentity });
if (state.targets.length !== 1) throw new Error('This recipe expects one changed prompt');
const target = state.targets[0];

// Inside the existing SDK experiment task, immediately after the real fetch:
const prompt = await langfuse.prompt.get(target.name, {
  version: target.version, type: 'text', cacheTtlSeconds: 0,
});
capture.capturePrompt(item.id, prompt, { name: target.name, version: target.version });
// Return the actual application task output; let its actual evaluators run.

const artifact = projectExperimentResult(actualSdkResult, {
  scriptPath: 'checks/application.mjs', durationMs: measuredDuration,
}, capture);
// Write artifact to a new private resultPath, then flush the SDK exporter.
const finished = capture.finish(resultPath, { remoteExport: 'unconfirmed' });
// Persist finished to a new private capturePath and run the existing importer.
```

`projectExperimentResult(result, options, bridge)` uses the supplied bridge;
the default is a synchronous `ReleaseCapture`. `bridge.projectResult(result,
options)` is equivalent. Options are `scriptPath`, `durationMs`, optional
`status`, `error`, and string-valued `metadata`. The persistent bridge below
returns a Promise, so use `await` with it. The SDK's display/format functions
are never executed.

Both projections preserve the actual experiment/run identities, optional trace
and dataset run IDs, every supplied item, and item/run evaluations including
`data_type`, `config_id`, comment and metadata. Missing IDs are never generated.
The SDK may expose a deprecated dataset-run alias of an experiment ID even for
local data; retaining that alias does not establish hosted dataset execution.
Python BOOLEAN values must be booleans. JS BOOLEAN wire values must be numeric
0/1 and are mapped to false/true; other typed values are rejected. Unspecified
score types are numeric. Duplicate case IDs, duplicate scores, absent case IDs,
empty result sets, non-finite/non-JSON data and results over 8 MB fail. Extra SDK
convenience fields outside the supported result subset are not exported.

## Optional persistent JavaScript collector

`PersistentReleaseCapture` is an opt-in prototype. It keeps one Python process
alive and validates serial requests over stdin/stdout. It avoids Python startup
for every access, but has **no production overhead measurement** and is not
recommended for a request path until the deferred application pilot measures it.

```js
import { PersistentReleaseCapture } from './js/capture.mjs';

const capture = new PersistentReleaseCapture({
  python: '/trusted/venv/bin/python',
  maxPending: 32, maxQueuedBytes: 8_000_000, maxMessageBytes: 8_000_000,
  timeout: 30_000, shutdownTimeout: 5_000,
});
try {
  // The same start/projectResult/finish methods are asynchronous here.
  const state = await capture.start({ manifestPath, stageReportPath, dependencyLockPath,
    dataset: datasetIdentity, execution: executionIdentity });
  if (state.targets.length !== 1) throw new Error('This recipe expects one changed prompt');
  const target = state.targets[0];
  // Within the task, snapshot the actual SDK object at the access time.
  const ack = await capture.capturePrompt(item.id, actualPrompt, {
    name: target.name, version: target.version, capturedAtUtc: new Date().toISOString(),
  });
  // ack carries id, submittedAtUtc and processedAtUtc; retain it if useful.
  // Await every task and evaluator, then project/write results and await finish.
} finally {
  const receipt = await capture.close();
  // Persist this receipt, including on failure. Do not hide an incomplete run.
  if (!receipt.complete) throw new Error('Capture collector evidence is incomplete');
}
```

The runnable [JS SDK example](../js/sdk-experiment.mjs) uses this path when its
configuration has `"collector": "persistent"`; set `collector_receipt` to a new
private output path to retain its shutdown receipt. The default remains the
synchronous bridge. The [Python example](../examples/sdk_experiment.py) and JS
example both use the supported projection helpers.

| Behavior | Contract |
| --- | --- |
| Admission | At most 32 queued/in-flight requests and 8 MB of serialized request bytes by default. Configurable maxima are 1,024 requests, 16 MB total, and 8 MB per message. There is one in-flight request. |
| Backpressure | A full queue rejects the new call with `CAPTURE_BACKPRESSURE`; it is not silently dropped or retried. Oversized/invalid requests also reject and increment `dropped`. |
| Validation | Python uses the same prompt, adoption, hashing and result validators as the synchronous bridge. Capture state changes commit only after the response fits the 8 MB bound. |
| Acknowledgment | Each Promise resolves only after Python validates its matching ordered request. `capturePrompt` returns an acknowledgment; low-level `request` returns `{result, acknowledgment}`. An acknowledgment is in-memory processing, not durable storage or remote ingestion. |
| Source times | Adoption keeps the original `requestStartedAtUtc` and `observedAtUtc` instants, normalized by the existing validator. Enqueue/processing does not replace them. Prompt acknowledgments retain `capturedAtUtc`; the legacy execution capture retains its existing run start/end schema, so keep acknowledgments separately when per-access timing is needed. |
| Deadline | `timeout` covers time from admission through acknowledgment, including queue time: default 30 seconds, maximum 60. A timeout kills the collector and rejects all remaining requests as unacknowledged; no automatic replay occurs. |
| Completion | `drain()` waits for admitted requests and returns a receipt. `finish()` rejects if any request was dropped, rejected or left unacknowledged. The independent importer still requires every policy case/prompt. |
| Shutdown | `close()` refuses new work, drains admitted work, closes stdin and waits for exit. Its separate deadline is 5 seconds by default, maximum 60. Timeout or nonzero/unexpected exit yields `complete:false`. Call it in `finally`; the worker keeps Node alive until closed. |
| Receipt | Counts submitted, acknowledged, rejected, dropped, unacknowledged and pending requests, serialized queue bytes/high-water values, backpressure events, child exit code/signal and a sanitized failure code. A rejection is acknowledged but unsuccessful. `complete` requires no pending work or failure/loss. |

Limits bound serialized queues, responses and capture state; they are not an
operating-system memory cap. Runtime overhead, temporary JSON copies and the
caller's retained SDK results/observations consume additional memory. The
collector does not accumulate adoption history: the caller must retain resolved
observations and the final receipt. Capture state and acknowledgments are lost
if the parent process or host dies. There is no disk spool, crash recovery or
distributed ordering guarantee. Missing or delayed observations cannot satisfy
the independent adoption/rollout coverage and freshness policies.

The subprocess uses a fixed Python module and no shell. Its environment allows
only path/platform/locale/temp settings and a configured Python source path;
provider tokens, cloud credentials, proxy variables and Python startup hooks are
not inherited. Python/user filesystem access remains trusted application
configuration, not an OS sandbox. Prompt or output bodies are omitted from
collector diagnostics. Receipts and captured results themselves can contain
application data and should be stored privately.

Offline tests construct official SDK objects and exercise typed failures,
hosted-item serialization, queue saturation, response bounds, source timestamps,
process death, timeouts, credential stripping and clean/incomplete shutdown.
The real existing-application pilot, request overhead measurement and thresholds
chosen from actual production traffic remain deferred.
