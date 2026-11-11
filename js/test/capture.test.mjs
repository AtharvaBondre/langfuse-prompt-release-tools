import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

// A worktree can review the parent's uncommitted bridge without copying source.
// Shipped tests default to the adjacent module and installed local SDK.
const captureUrl = process.env.RELEASE_CAPTURE_MODULE
  ? (process.env.RELEASE_CAPTURE_MODULE.startsWith('file:') ? process.env.RELEASE_CAPTURE_MODULE : pathToFileURL(resolve(process.env.RELEASE_CAPTURE_MODULE)).href)
  : new URL('../capture.mjs', import.meta.url).href;
const { ReleaseCapture, snapshotPrompt, sdkVersion } = await import(captureUrl);
const { TextPromptClient, ChatPromptClient } = await import(new URL('./node_modules/@langfuse/client/dist/index.mjs', captureUrl));
const python = process.env.RELEASE_PYTHON || 'python3';
const pythonPath = process.env.RELEASE_PYTHON_PATH || resolve(dirname(fileURLToPath(captureUrl)), '..');
const options = { python, pythonPath };
const bridge = () => new ReleaseCapture(options);
const metadata = {
  consumerId: 'test-api', deploymentId: 'test-api@abc', requestId: 'request-1', requestedLabel: 'production',
  requestStartedAtUtc: '2026-11-11T12:00:00+00:00', observedAtUtc: '2026-11-11T12:00:01+00:00', cacheTtlSeconds: 0,
};
function textPrompt({ version = 2, config = {}, content = 'Hello {{name}} 🌍', fallback = false } = {}) {
  return new TextPromptClient({ name: 'root', version, prompt: content, config, labels: [], tags: [] }, fallback);
}
function chatPrompt() {
  return new ChatPromptClient({ name: 'root', version: 2, config: { temperature: 0.2 }, labels: [], tags: [], prompt: [
    { role: 'system', content: 'Keep café and 漢字 unchanged.' },
    { type: 'placeholder', name: 'history' },
    { role: 'user', content: '{{question}}' },
  ] });
}
function runningCapture() {
  return { completed_at_utc: null, completion: { local: 'running' }, targets: [{ name: 'root', version: 2, case_ids: ['case-1'] }], items: [] };
}
function pythonHash(value, prompt = false) {
  const code = `import json,sys\nfrom langfuse_release_common import fingerprint\nfrom langfuse_release_dependencies import prompt_sha256\nvalue=json.loads(sys.stdin.read())\nprint(${prompt ? 'prompt_sha256' : 'fingerprint'}(value))`;
  const env = { ...process.env, PYTHONPATH: pythonPath };
  for (const key of Object.keys(env)) if (key.startsWith('LANGFUSE_') || ['GH_TOKEN', 'GITHUB_TOKEN'].includes(key)) delete env[key];
  return execFileSync(python, ['-c', code], { input: JSON.stringify(value), env, encoding: 'utf8' }).trim();
}

test('accepts exact official text/chat objects and records installed SDK identity', () => {
  assert.match(sdkVersion, /^6\./);
  assert.equal(snapshotPrompt(textPrompt()).type, 'text');
  assert.equal(snapshotPrompt(chatPrompt()).type, 'chat');
  const observed = bridge().observation(textPrompt(), metadata);
  assert.equal(observed.capture_method, 'langfuse_release_adoption.capture_js_sdk_observation/v1');
  assert.deepEqual(observed.sdk, { name: 'langfuse-js', version: sdkVersion });
  assert.equal(observed.returned.version, 2);
});

test('refuses plain mappings, null and subclasses instead of duck typing', () => {
  class Derived extends TextPromptClient {}
  for (const candidate of [null, {}, snapshotPrompt(textPrompt()), new Derived({ name: 'root', version: 2, prompt: 'x', config: {}, labels: [], tags: [] })]) {
    assert.throws(() => snapshotPrompt(candidate), /official SDK|actual/);
  }
});

test('snapshot is detached from mutable SDK content and config', () => {
  const source = chatPrompt();
  source.config.nested = { value: 1 };
  const captured = snapshotPrompt(source);
  source.config.nested.value = 2;
  source.prompt[0].content = 'changed after capture';
  assert.equal(captured.config.nested.value, 1);
  assert.equal(captured.prompt[0].content, 'Keep café and 漢字 unchanged.');
  captured.config.nested.value = 3;
  assert.equal(source.config.nested.value, 2);
});

test('state access and returned start snapshots cannot mutate internal state', () => {
  const capture = bridge();
  capture.call = () => runningCapture();
  const first = capture.start({ manifestPath: 'unused', stageReportPath: 'unused', dataset: {}, execution: {} });
  first.targets[0].version = 999;
  assert.equal(capture.state.targets[0].version, 2);
  const second = capture.state;
  second.items.push({ id: 'injected' });
  assert.deepEqual(capture.state.items, []);
  assert.throws(() => capture.start({}), /already started/);
});

test('experiment capture rejects fallback before touching the sidecar', () => {
  const capture = bridge();
  let calls = 0;
  capture.call = () => { calls += 1; return runningCapture(); };
  capture.start({ manifestPath: 'unused', stageReportPath: 'unused', dataset: {}, execution: {} });
  assert.throws(() => capture.capturePrompt('case-1', textPrompt({ fallback: true }), { name: 'root', version: 2 }), /fallback/);
  assert.equal(calls, 1);
});

test('fallback adoption stays explicit and unknown fallback values fail', () => {
  const observed = bridge().observation(textPrompt({ version: 0, fallback: true }), metadata);
  assert.equal(observed.is_fallback, true);
  assert.equal(observed.returned.version, 0);
  for (const value of [undefined, null, 'false', 0]) {
    const prompt = textPrompt(); prompt.isFallback = value;
    assert.throws(() => bridge().observation(prompt, metadata), /Capture|fallback|JSON/);
  }
});

test('rejects non-finite numbers, unsafe integers and non-JSON values', () => {
  const cycle = {}; cycle.self = cycle;
  for (const value of [NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER + 1, 1n, undefined, () => 1, Symbol('x'), new Date(), new Map(), cycle]) {
    assert.throws(() => bridge().fingerprint(value), /Capture|finite|JSON/);
  }
  assert.equal(bridge().fingerprint(Number.MAX_SAFE_INTEGER), pythonHash(Number.MAX_SAFE_INTEGER));
});

test('rejects sparse arrays, extra array keys and symbol or hidden properties', () => {
  const symbolic = { normal: 1, [Symbol('dropped')]: 2 };
  const hidden = {}; Object.defineProperty(hidden, 'secret', { value: 1, enumerable: false });
  for (const value of [Array(1), Object.assign([1], { extra: 2 }), symbolic, hidden]) {
    assert.throws(() => bridge().fingerprint(value), /Capture|JSON|array|property/i);
  }
});

test('refuses getters without executing them, including top-level SDK fields', () => {
  let called = 0;
  const config = {}; Object.defineProperty(config, 'value', { enumerable: true, get() { called += 1; return 1; } });
  assert.throws(() => snapshotPrompt(textPrompt({ config })), /getter|accessor/i);
  assert.equal(called, 0);
  const source = textPrompt();
  Object.defineProperty(source, 'config', { get() { called += 1; return {}; } });
  assert.throws(() => snapshotPrompt(source), /getter|accessor/i);
  assert.equal(called, 0);
});

test('rejects proxies without invoking their traps', () => {
  let traps = 0;
  const handler = { get() { traps += 1; return 1; }, ownKeys() { traps += 1; return []; }, getPrototypeOf() { traps += 1; return Object.prototype; } };
  assert.throws(() => bridge().fingerprint(new Proxy({}, handler)), /Capture|JSON/);
  assert.throws(() => snapshotPrompt(new Proxy(textPrompt(), handler)), /actual|official SDK/);
  assert.equal(traps, 0);
});

test('validation errors do not echo captured prompt content', () => {
  const marker = 'PRIVATE-PROMPT-CONTENT-test-only';
  const source = textPrompt({ content: marker }); source.version = 'invalid';
  assert.throws(() => bridge().observation(source, metadata), error => {
    assert.equal(error.message.includes(marker), false);
    return /Capture rejected/.test(error.message);
  });
});

test('uses Python canonical hashes for fractional/exponent values and Unicode', () => {
  const value = { floats: [0.2, 1e-7, 1e-12, 123456.789, -0.25, Number.MIN_VALUE], unicode: 'café 漢字 🌍 e\u0301', nested: { 'é': false, null: null } };
  assert.equal(bridge().fingerprint(value), pythonHash(value));
  assert.notEqual(bridge().fingerprint('é'), bridge().fingerprint('e\u0301'));
});

test('chat placeholders and optional chatmessage discriminator match Python prompt hashing', () => {
  const source = chatPrompt();
  const snapshot = snapshotPrompt(source);
  const observed = bridge().observation(source, metadata);
  assert.equal(observed.returned.resolved_sha256, pythonHash(snapshot, true));
  const normalized = structuredClone(snapshot);
  normalized.prompt.forEach(item => { if (item.type === 'chatmessage') delete item.type; });
  assert.equal(pythonHash(normalized, true), observed.returned.resolved_sha256);
  normalized.prompt[1].name = 'other_history';
  assert.notEqual(pythonHash(normalized, true), observed.returned.resolved_sha256);
});

test('wrong requested or returned versions fail through the real Python dispatcher', () => {
  const capture = bridge();
  for (const [requested, returned] of [[1, 2], [2, 1], [true, 2], [2, true]]) {
    assert.throws(() => capture.call('prompt', { capture: runningCapture(), item_id: 'case-1', requested_name: 'root', requested_version: requested,
      prompt: snapshotPrompt(textPrompt({ version: returned })) }), /Capture rejected/);
  }
});

test('direct bridge prompt operation cannot bypass fallback enforcement', () => {
  for (const fallback of [true, 'false', null]) {
    const snapshot = snapshotPrompt(textPrompt()); snapshot.is_fallback = fallback;
    assert.throws(() => bridge().call('prompt', { capture: runningCapture(), item_id: 'case-1', requested_name: 'root', requested_version: 2, prompt: snapshot }), /Capture rejected/);
  }
});

test('operation names and Python executable strings never execute shell syntax', () => {
  const root = mkdtempSync(join(tmpdir(), 'langfuse-capture-shell-test-'));
  const marker = join(root, 'must-not-exist');
  try {
    for (const operation of [`touch ${marker}`, `$(touch ${marker})`, '__import__("os").system("id")']) {
      assert.throws(() => bridge().call(operation, {}), /Capture rejected|bridge failed/);
    }
    assert.throws(() => new ReleaseCapture({ ...options, python: `${python}; touch ${marker}` }).fingerprint({}), /bridge failed/);
    assert.equal(existsSync(marker), false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('sidecar environment strips Langfuse credentials and GitHub tokens', () => {
  const root = mkdtempSync(join(tmpdir(), 'langfuse-capture-env-test-'));
  const keys = ['LANGFUSE_SECRET_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_HOST', 'GH_TOKEN', 'GITHUB_TOKEN'];
  const saved = new Map(keys.map(key => [key, process.env[key]]));
  try {
    writeFileSync(join(root, 'langfuse_release_capture.py'), `import json,os\nprint(json.dumps({key:os.environ.get(key) for key in ${JSON.stringify(keys)}}))\n`);
    keys.forEach(key => { process.env[key] = 'synthetic-test-only'; });
    const observed = new ReleaseCapture({ python, pythonPath: root }).fingerprint({});
    assert.deepEqual(observed, Object.fromEntries(keys.map(key => [key, null])));
  } finally {
    for (const [key, value] of saved) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    rmSync(root, { recursive: true, force: true });
  }
});

test('constructor and lifecycle reject unbounded timeout and use before start', () => {
  for (const timeout of [0, -1, NaN, Infinity, 60001]) assert.throws(() => new ReleaseCapture({ ...options, timeout }), TypeError);
  assert.throws(() => bridge().capturePrompt('case-1', textPrompt(), { name: 'root', version: 2 }), /Start capture/);
  assert.throws(() => bridge().finish('missing.json'), /not started/);
});
