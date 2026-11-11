# JavaScript capture bridge

This local helper records actual official Langfuse JavaScript SDK prompt objects
into the Python release evidence contract. It reuses Python's canonical hashing,
policy checks and capture lifecycle. It does not implement another experiment
runner, prompt compiler or signing scheme.

The package is private and lives in this repository. Use **Node 22+**, the pinned
`@langfuse/client` dependency and a trusted Python environment with this release
package installed. The Python core needs Python 3.10+; the separate official
Python SDK example has its own Python version requirement.

```sh
# From the repository root, install the Python package into your chosen environment.
python3 -m pip install -e .
cd js
npm ci
npm test
```

The verified offline test combination is Node 22.23.2, JS SDK 6.1.1 and the Python
release engine. These tests construct official SDK objects without calling a
server. Actual server compatibility is a separate runnable-example/CI check;
see the repository validation and compatibility documentation.

## Record a versioned experiment access

```js
import { ReleaseCapture } from './capture.mjs';

const capture = new ReleaseCapture({
  python: '/absolute/path/to/trusted/venv/bin/python',
});

capture.start({
  manifestPath: 'snapshot/manifest.json',
  stageReportPath: 'snapshot/reports/stage.json',
  dependencyLockPath: 'snapshot/reports/dependencies.json',
  dataset: actualDatasetIdentity,
  execution: actualTaskEvaluatorAndModelIdentity,
});

// Inside the application's actual experiment task:
const prompt = await langfuse.prompt.get('support-answer', {
  version: stagedVersion,
  type: 'text',
  cacheTtlSeconds: 0,
});
capture.capturePrompt(caseId, prompt, {
  name: 'support-answer',
  version: stagedVersion,
});
const compiled = prompt.compile({ question: caseInput.question });
// Run the existing application task/evaluators with compiled.

// After writing the actual normalized experiment result artifact:
const completedCapture = capture.finish('experiment-results.json', {
  remoteExport: 'unconfirmed',
});
```

Dataset/execution identities must match the separately reviewed policy. Pass the
actual request's explicit version and the SDK-returned object. A different
requested/returned version, case outside the policy, duplicate capture or
fallback fails. The pre-execution dependency lock checks the actual resolved
prompt/config hash. Preserve the normalized result artifact and returned capture
for the existing experiment importer.

`finish` hashes the supplied result file and records local completion. It does
not prove remote trace/score persistence. Leave `remoteExport` unconfirmed unless
the application established the stated result; use the separate remote evidence
verifier for server-side coverage.

## Observe a consumer after promotion

```js
const started = new Date().toISOString();
const prompt = await langfuse.prompt.get('support-answer', {
  label: 'production',
  type: 'text',
  cacheTtlSeconds: 0,
});
const observation = capture.observation(prompt, {
  consumerId: 'support-api-js',
  deploymentId: actualDeploymentRevision,
  requestId: actualRequestId,
  requestedLabel: 'production',
  requestStartedAtUtc: started,
  cacheTtlSeconds: 0,
});
```

The observation records `sdk.name=langfuse-js`, the installed SDK version and the
explicit JavaScript capture method. It records the actual fallback flag; a
fallback can be observed but cannot become passing experiment evidence. Adoption
and rollout policy decide whether the observations satisfy their own required
versions, deployments, freshness and coverage.

The cache field describes the supplied SDK settings. With a nonzero TTL it says
`sdk_cache_possible`, not a measured cache hit/miss. The actual returned
version/content are what the release checks compare. One SDK access proves only
that instrumented access, not all application traffic.

## Fidelity and execution boundaries

- Only exact official `TextPromptClient` / `ChatPromptClient` prototypes are
  accepted. Plain mappings, subclasses and proxies are rejected. Even an official
  SDK object can be constructed by application code; trusted instrumentation and
  authenticated artifact provenance remain separate requirements.
- Snapshots and exposed capture state are detached copies. Later mutation of the
  SDK prompt, nested config or returned state cannot rewrite an earlier snapshot.
- Finite JSON numbers, Unicode, chat messages and placeholders pass through the
  Python canonical hashing implementation. Unicode is not normalized. Optional
  normal `chatmessage` discriminators are normalized by the shared prompt hash;
  placeholder identities remain significant.
- Unsafe JavaScript integers, NaN/infinity, sparse arrays, extra array keys,
  symbols, hidden properties, getters, proxies, cycles and non-JSON objects are
  rejected. The helper does not silently drop or coerce these values into
  different JSON evidence. JavaScript's numeric representation still applies;
  this helper cannot recover precision already lost before capture.
- The bridge calls a fixed Python module with `execFileSync` and JSON stdin,
  without a shell. Evidence cannot choose an executable or arbitrary operation.
  The interpreter/source path are trusted application configuration. The Python
  module allowlists capture operations, rejects duplicate JSON keys and limits
  messages to 8 MB.
- Langfuse environment variables and GitHub token variables are stripped from the
  sidecar environment. The application's actual SDK access remains in the app
  process. This helper is not an operating-system sandbox.

The sidecar is synchronous and starts Python per call. Use it for experiments
and low-volume rollout probes, not an unmeasured high-throughput request hook.
The default timeout is 30 seconds, with an enforced maximum of 60 seconds and
bounded output. Captured prompt content is not intentionally echoed in bridge
errors.

For source development only, `pythonPath` can point at this repository. Python's
normal import precedence includes the current working directory, so use the
trusted installed environment or run from the `js/` directory; a stale checkout
containing older Python modules can otherwise shadow the configured source.

## Independent test execution

The Node suite uses the built-in test runner. A separate worktree can test a
parent's bridge without copying production code:

```sh
cd js
RELEASE_CAPTURE_MODULE=file:///absolute/path/to/repo/js/capture.mjs \
RELEASE_PYTHON_PATH=/absolute/path/to/repo \
node --test test/capture.test.mjs
```

`RELEASE_PYTHON` optionally selects the trusted interpreter for Node tests.
Defaults use the adjacent capture module and repository root. Python bridge
checks run with `python -m unittest test_release_capture`; set
`RELEASE_PYTHON_PATH` to select the parent source during independent review.

Tests cover actual SDK class handling, fallback enforcement through both wrapper
and direct dispatcher, copy isolation, invalid JavaScript values, Python hash
parity, wrong versions, method/SDK mismatches, credential-variable stripping and
command-injection rejection. They make no browser, network or model calls.
