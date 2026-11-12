import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const captureUrl = process.env.RELEASE_CAPTURE_MODULE
  ? (process.env.RELEASE_CAPTURE_MODULE.startsWith('file:') ? process.env.RELEASE_CAPTURE_MODULE : pathToFileURL(resolve(process.env.RELEASE_CAPTURE_MODULE)).href)
  : new URL('../capture.mjs', import.meta.url).href;
const { ReleaseCapture, PersistentReleaseCapture, projectExperimentResult } = await import(captureUrl);
const sdkPackageUrl = new URL('../package.json', pathToFileURL(createRequire(captureUrl).resolve('@langfuse/client')));
const sdkPackage = JSON.parse(readFileSync(sdkPackageUrl, 'utf8'));
const { TextPromptClient } = await import(new URL(sdkPackage.exports['.'].import.default, sdkPackageUrl));
const options = { python: process.env.RELEASE_PYTHON || 'python3',
  pythonPath: process.env.RELEASE_PYTHON_PATH || resolve(dirname(fileURLToPath(captureUrl)), '..') };
const prompt = () => new TextPromptClient({ name: 'root', version: 2, type: 'text', prompt: 'Hello {{name}}', config: {}, labels: [], tags: [] });
const metadata = { consumerId: 'synthetic-app', deploymentId: 'synthetic-revision', requestId: 'actual-request',
  requestedLabel: 'production', requestStartedAtUtc: '2026-11-11T12:00:00Z', observedAtUtc: '2026-11-11T12:00:01Z', cacheTtlSeconds: 0 };
function sdkResult() {
  return { experimentId: 'actual-experiment', runName: 'actual-run', datasetRunId: 'actual-experiment',
    experimentUrl: 'https://langfuse.example/project/demo/experiments/actual-experiment',
    itemResults: [{ item: { id: 'actual-case', input: { x: 'café' }, expectedOutput: false, version: '2026-11-11T00:00:00Z' },
      output: false, traceId: 'actual-trace', datasetRunId: 'actual-experiment',
      evaluations: [{ name: 'gate', value: 0, dataType: 'BOOLEAN', configId: 'actual-config', comment: 'actual failure', metadata: { x: 0.2 } }] }],
    runEvaluations: [{ name: 'category', value: 'needs-work', dataType: 'CATEGORICAL' }],
    format() { throw new Error('The projection must not run formatting helpers'); } };
}
function fakePython(t, code) {
  const root = mkdtempSync(join(tmpdir(), 'release-collector-test-'));
  t.after(() => rmSync(root, { force: true, recursive: true }));
  writeFileSync(join(root, 'langfuse_release_capture.py'), code);
  return root;
}

test('result projection keeps real IDs, typed failure, run scores and hosted item fields', async () => {
  const bridge = new ReleaseCapture(options);
  const result = sdkResult();
  const artifact = projectExperimentResult(result, { scriptPath: 'checks/task.mjs', durationMs: 1 }, bridge);
  const projected = artifact.results[0].experiment_result;
  assert.equal(projected.experiment_id, result.experimentId);
  assert.equal(projected.dataset_run_id, result.datasetRunId);
  assert.equal(projected.item_results[0].trace_id, 'actual-trace');
  assert.equal(projected.item_results[0].item.version, '2026-11-11T00:00:00Z');
  assert.deepEqual(projected.item_results[0].evaluations[0], { name: 'gate', value: false, data_type: 'BOOLEAN',
    config_id: 'actual-config', comment: 'actual failure', metadata: { x: 0.2 } });
  assert.deepEqual(projected.run_evaluations, [{ name: 'category', value: 'needs-work', data_type: 'CATEGORICAL' }]);
  result.itemResults[0].evaluations[0].metadata.x = 0.5;
  assert.equal(projected.item_results[0].evaluations[0].metadata.x, 0.2);
  const collector = new PersistentReleaseCapture(options);
  try {
    assert.deepEqual(await projectExperimentResult(sdkResult(), { scriptPath: 'checks/task.mjs', durationMs: 1 }, collector), artifact);
  } finally { assert.equal((await collector.close()).complete, true); }
});

test('projection rejects missing identities, invalid scores and getters', () => {
  const bridge = new ReleaseCapture(options);
  for (const value of [true, '0', 2]) {
    const result = sdkResult(); result.itemResults[0].evaluations[0].value = value;
    assert.throws(() => bridge.projectResult(result, { scriptPath: 'task.mjs', durationMs: 0 }), /BOOLEAN/);
  }
  const result = sdkResult(); delete result.experimentId;
  assert.throws(() => bridge.projectResult(result, { scriptPath: 'task.mjs', durationMs: 0 }), /missing/);
  Object.defineProperty(result, 'experimentId', { get() { throw new Error('must not execute'); } });
  assert.throws(() => bridge.projectResult(result, { scriptPath: 'task.mjs', durationMs: 0 }), /getters/);
  for (const field of ['itemResults', 'runEvaluations']) {
    const proxyResult = sdkResult();
    proxyResult[field] = new Proxy([], { get() { throw new Error('proxy trap executed'); } });
    assert.throws(() => bridge.projectResult(proxyResult, { scriptPath: 'task.mjs', durationMs: 0 }), /plain arrays/);
  }
  const arrayGetter = sdkResult();
  Object.defineProperty(arrayGetter.itemResults, '0', { get() { throw new Error('array getter executed'); } });
  assert.throws(() => bridge.projectResult(arrayGetter, { scriptPath: 'task.mjs', durationMs: 0 }), /getters/);
});

test('persistent requests retain source times, snapshots and ordered acknowledgments', async () => {
  const collector = new PersistentReleaseCapture(options);
  try {
    const value = prompt();
    const observedPromise = collector.observation(value, metadata);
    value.prompt = 'changed after enqueue';
    const ackPromise = collector.request('hash', { value: [0.2, '漢字'] }, { submittedAtUtc: metadata.observedAtUtc });
    assert.equal(collector.receipt.complete, false);
    const observed = await observedPromise;
    assert.equal(Date.parse(observed.observed_at_utc), Date.parse(metadata.observedAtUtc));
    assert.equal(Date.parse(observed.request_started_at_utc), Date.parse(metadata.requestStartedAtUtc));
    const expected = new ReleaseCapture(options).observation(prompt(), metadata);
    assert.deepEqual(observed, expected);
    const ack = await ackPromise;
    assert.equal(ack.acknowledgment.id, 2);
    assert.equal(ack.acknowledgment.submittedAtUtc, metadata.observedAtUtc);
    assert.equal((await collector.drain()).acknowledged, 2);
  } finally {
    const receipt = await collector.close();
    assert.equal(receipt.closed, true); assert.equal(receipt.complete, true); assert.equal(receipt.exitCode, 0);
    assert.equal(await collector.close(), receipt);
  }
});

test('count and byte backpressure reject admission and prevent successful finalization', async () => {
  const collector = new PersistentReleaseCapture({ ...options, maxPending: 1, maxQueuedBytes: 512, maxMessageBytes: 512 });
  try {
    const first = collector.fingerprint('first');
    await assert.rejects(collector.fingerprint('second'), { code: 'CAPTURE_BACKPRESSURE' });
    await first;
    await assert.rejects(collector.fingerprint('x'.repeat(513)), { code: 'CAPTURE_TOO_LARGE' });
    const receipt = await collector.drain();
    assert.equal(receipt.dropped, 2); assert.equal(receipt.submitted, 1); assert.equal(receipt.acknowledged, 1);
    assert.equal(receipt.complete, false); assert.ok(receipt.highWaterBytes <= 512); assert.equal(receipt.highWaterPending, 1);
    await assert.rejects(collector.finish('unused.json'), { code: 'CAPTURE_INCOMPLETE' });
  } finally { assert.equal((await collector.close()).complete, false); }
});

test('shared validator rejection is acknowledged and never disappears from receipt', async () => {
  const collector = new PersistentReleaseCapture(options);
  try {
    await assert.rejects(collector.capturePrompt('case', prompt(), { name: 'root', version: 2 }), { code: 'CAPTURE_REJECTED' });
    await assert.rejects(collector.observation(prompt(), { ...metadata, requestStartedAtUtc: 'bad-time' }), { code: 'CAPTURE_REJECTED' });
    assert.equal(collector.receipt.acknowledged, 2); assert.equal(collector.receipt.rejected, 2);
    await assert.rejects(collector.finish('unused.json'), { code: 'CAPTURE_INCOMPLETE' });
  } finally { assert.equal((await collector.close()).complete, false); }
});

test('queue acknowledgment timeout makes pending outcomes explicit and kills the child', async t => {
  const pythonPath = fakePython(t, 'import time\ntime.sleep(20)\n');
  const collector = new PersistentReleaseCapture({ ...options, pythonPath, timeout: 150 });
  const outcomes = await Promise.allSettled([collector.fingerprint(1), collector.fingerprint(2)]);
  assert.ok(outcomes.every(row => row.status === 'rejected' && row.reason.code === 'COLLECTOR_TIMEOUT'));
  const receipt = await collector.close();
  assert.equal(receipt.complete, false); assert.equal(receipt.unacknowledged, 2); assert.equal(receipt.closed, true);
  assert.equal(receipt.failure.code, 'COLLECTOR_TIMEOUT'); assert.equal(receipt.pending, 0);
});

test('process death and response-size failures remain unknown rather than retried', async t => {
  for (const [code, expected] of [['import sys\nsys.exit(9)\n', 'COLLECTOR_EXIT'],
    ["import sys\nsys.stdout.write('x' * 8000001)\nsys.stdout.flush()\n", 'COLLECTOR_RESPONSE_LIMIT']]) {
    const collector = new PersistentReleaseCapture({ ...options, pythonPath: fakePython(t, code) });
    await assert.rejects(collector.fingerprint('secret-input'), { code: expected });
    const receipt = await collector.close();
    assert.equal(receipt.complete, false); assert.equal(receipt.unacknowledged, 1); assert.equal(receipt.submitted, 1);
    assert.equal(receipt.failure.code, expected);
    assert.ok(!JSON.stringify(receipt).includes('secret-input'));
  }
});

test('shutdown drains a full queue and enforces a separate deadline', async t => {
  const collector = new PersistentReleaseCapture({ ...options, maxPending: 1 });
  const first = collector.fingerprint('queued');
  const closing = collector.close();
  await first;
  assert.equal((await closing).complete, true);
  await assert.rejects(collector.fingerprint('late'), { code: 'COLLECTOR_UNAVAILABLE' });
  assert.equal(collector.receipt.dropped, 1);
  const stalled = new PersistentReleaseCapture({ ...options, pythonPath: fakePython(t, 'import time\ntime.sleep(20)\n'), shutdownTimeout: 150 });
  const outcome = stalled.fingerprint('queued').catch(error => error);
  const receipt = await stalled.close();
  assert.equal((await outcome).code, 'COLLECTOR_SHUTDOWN_TIMEOUT');
  assert.equal(receipt.complete, false); assert.equal(receipt.unacknowledged, 1);
});

test('persistent child receives no provider, GitHub or proxy environment credentials', async t => {
  const keys = ['LANGFUSE_SECRET_KEY', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'HTTPS_PROXY'];
  const saved = keys.map(key => [key, process.env[key]]);
  const pythonPath = fakePython(t, `import json,os,sys\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({'id':r['id'],'submitted_at_utc':r['submitted_at_utc'],'processed_at_utc':'2026-11-11T00:00:00Z','ok':True,'result':{key:os.environ.get(key) for key in ${JSON.stringify(keys)}}}),flush=True)\n`);
  try {
    keys.forEach(key => { process.env[key] = 'synthetic-only'; });
    const collector = new PersistentReleaseCapture({ ...options, pythonPath });
    try { assert.deepEqual(await collector.fingerprint({}), Object.fromEntries(keys.map(key => [key, null]))); }
    finally { assert.equal((await collector.close()).complete, true); }
  } finally {
    saved.forEach(([key, value]) => { if (value === undefined) delete process.env[key]; else process.env[key] = value; });
  }
});

test('constructor limits and invalid SDK objects cannot silently drop samples', async () => {
  for (const extra of [{ maxPending: 0 }, { maxQueuedBytes: 16_000_001 }, { maxMessageBytes: 8_000_001 }, { timeout: 60001 }, { shutdownTimeout: 0 }]) {
    assert.throws(() => new PersistentReleaseCapture({ ...options, ...extra }), TypeError);
  }
  const collector = new PersistentReleaseCapture(options);
  try {
    await assert.rejects(collector.observation({}, metadata), /official SDK/);
    assert.equal(collector.receipt.dropped, 1);
  } finally { assert.equal((await collector.close()).complete, false); }
});
