import { execFileSync, spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { types } from 'node:util';
import { ChatPromptClient, TextPromptClient } from '@langfuse/client';

export const sdkVersion = JSON.parse(readFileSync(
  new URL('../package.json', import.meta.resolve('@langfuse/client')), 'utf8')).version;

const MAX_BYTES = 8_000_000;
function childEnvironment(pythonPath) {
  // The bridge has no network duties. Do not forward provider credentials,
  // proxy settings, Python startup hooks or SDK exporter configuration.
  const env = {};
  for (const key of ['PATH', 'SystemRoot', 'WINDIR', 'PATHEXT', 'HOME', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'LC_CTYPE']) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  env.PYTHONNOUSERSITE = '1';
  if (pythonPath !== undefined) {
    if (typeof pythonPath !== 'string' || !pythonPath) throw new TypeError('pythonPath must be a trusted nonempty path');
    env.PYTHONPATH = pythonPath;
  }
  return env;
}

function boundedInteger(value, min, max, name) {
  if (!Number.isSafeInteger(value) || value < min || value > max) throw new TypeError(`${name} is outside its bound`);
}

function jsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
      throw new TypeError('Capture requires finite numbers and safe integer identities');
    }
    return;
  }
  if (typeof value !== 'object' || types.isProxy(value) || seen.has(value)) throw new TypeError('Capture requires acyclic plain JSON data');
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) {
    throw new TypeError('Capture requires plain JSON objects');
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.length > 100000 || (Array.isArray(value) && value.length > 100000)) throw new TypeError('Capture collection exceeds its size bound');
  if (keys.some(key => typeof key !== 'string')) throw new TypeError('Capture refuses symbol keys');
  if (Array.isArray(value)) {
    if (Object.getPrototypeOf(value) !== Array.prototype || keys.length !== value.length + 1 ||
        keys.some(key => key !== 'length' && !/^(0|[1-9][0-9]*)$/.test(key)) ||
        Array.from({ length: value.length }, (_, index) => String(index)).some(key => !(key in descriptors))) {
      throw new TypeError('Capture refuses sparse arrays or extra array properties');
    }
  }
  seen.add(value);
  for (const key of keys) {
    if (key === 'length' && Array.isArray(value)) continue;
    const descriptor = descriptors[key];
    if (!('value' in descriptor) || !descriptor.enumerable) throw new TypeError('Capture refuses getters or hidden properties');
    jsonValue(descriptor.value, seen);
  }
  seen.delete(value);
}

export function snapshotPrompt(prompt) {
  if (types.isProxy(prompt) || ![TextPromptClient.prototype, ChatPromptClient.prototype].includes(Object.getPrototypeOf(prompt ?? {}))) {
    throw new TypeError('Pass an actual official SDK prompt client, not a plain object');
  }
  const value = {};
  for (const [key, source] of Object.entries({ name: 'name', version: 'version', type: 'type',
    prompt: 'prompt', config: 'config', is_fallback: 'isFallback' })) {
    const descriptor = Object.getOwnPropertyDescriptor(prompt, source);
    if (!descriptor || !('value' in descriptor)) throw new TypeError('SDK snapshot refuses field getters');
    value[key] = descriptor.value;
  }
  jsonValue(value);
  return JSON.parse(JSON.stringify(value));
}

function selectedFields(value, fields) {
  if (!value || typeof value !== 'object' || types.isProxy(value)) throw new TypeError('SDK result fields require an object');
  const output = {};
  for (const key of fields) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor) continue;
    if (!('value' in descriptor)) throw new TypeError('SDK result projection refuses field getters');
    if (descriptor.value !== undefined) output[key] = descriptor.value;
  }
  return output;
}

function sdkRows(value) {
  if (types.isProxy(value) || !Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) {
    throw new TypeError('SDK result collections must be plain arrays');
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const length = descriptors.length.value;
  if (length > 100000 || Reflect.ownKeys(descriptors).length !== length + 1) throw new TypeError('SDK result array exceeds its bound or contains extra fields');
  return Array.from({ length }, (_, index) => {
    const descriptor = descriptors[String(index)];
    if (!descriptor || !('value' in descriptor) || !descriptor.enumerable) throw new TypeError('SDK result array refuses getters or sparse entries');
    return descriptor.value;
  });
}

function resultArguments(result, { scriptPath, durationMs, status = 'passed', error = null, metadata = {} }) {
  const projected = selectedFields(result, ['experimentId', 'experimentUrl', 'runName', 'datasetRunId', 'itemResults', 'runEvaluations']);
  const scores = values => {
    return sdkRows(values).map(row => selectedFields(row, ['name', 'value', 'comment', 'metadata', 'dataType', 'configId']));
  };
  projected.itemResults = sdkRows(projected.itemResults).map(row => {
    const item = selectedFields(row, ['item', 'input', 'expectedOutput', 'output', 'traceId', 'datasetRunId', 'evaluations']);
    item.evaluations = scores(item.evaluations);
    return item;
  });
  projected.runEvaluations = scores(projected.runEvaluations);
  const args = { result: projected, script_path: scriptPath, duration_ms: durationMs,
    status, error, metadata: { sdk_version: sdkVersion, ...metadata } };
  jsonValue(args);
  return args;
}

/** Project the actual SDK result; Python validates the same Action v1 contract. */
export function projectExperimentResult(result, options, bridge = new ReleaseCapture()) {
  return bridge.projectResult(result, options);
}

/** Synchronous sidecar for experiments/low-volume probes, not a hot traffic hook.
 * Python remains the single implementation of canonical hashes and policy
 * checks. Configure the interpreter independently; evidence cannot choose it.
 */
export class ReleaseCapture {
  #state;
  constructor({ python = 'python3', pythonPath, timeout = 30000 } = {}) {
    if (typeof python !== 'string' || !python || !Number.isFinite(timeout) || timeout <= 0 || timeout > 60000) {
      throw new TypeError('Use an explicit Python executable and a bounded timeout');
    }
    this.python = python;
    this.pythonPath = pythonPath;
    this.timeout = timeout;
  }

  call(operation, args) {
    jsonValue(args);
    const env = childEnvironment(this.pythonPath);
    const input = JSON.stringify({ operation, arguments: args });
    if (Buffer.byteLength(input) > MAX_BYTES) throw new RangeError('Capture message exceeds the 8 MB bound');
    try {
      return JSON.parse(execFileSync(this.python, ['-m', 'langfuse_release_capture'], {
        input, encoding: 'utf8',
        env, timeout: this.timeout, maxBuffer: 8_000_000, stdio: ['pipe', 'pipe', 'pipe'],
      }));
    } catch (error) {
      // Do not expose child command arguments or captured prompt content.
      const detail = String(error.stderr ?? '').trim();
      throw new Error(detail.startsWith('Capture rejected:') ? detail : 'Python capture bridge failed');
    }
  }

  fingerprint(value) { return this.call('hash', { value }); }

  projectResult(result, options) { return this.call('project-result', resultArguments(result, options)); }

  start({ manifestPath, stageReportPath, dependencyLockPath, dataset, execution }) {
    if (this.#state) throw new Error('Capture already started');
    const args = { manifest_path: manifestPath, stage_report_path: stageReportPath, dataset, execution };
    if (dependencyLockPath !== undefined) args.dependency_lock_path = dependencyLockPath;
    this.#state = this.call('start', args);
    return this.state;
  }

  capturePrompt(itemId, prompt, { name, version }) {
    if (!this.#state) throw new Error('Start capture before the task');
    const actual = snapshotPrompt(prompt);
    if (actual.is_fallback) throw new Error('An experiment cannot use a fallback prompt');
    this.#state = this.call('prompt', { capture: this.#state, item_id: itemId,
      requested_name: name, requested_version: version, prompt: actual });
    return this.state;
  }

  finish(resultPath, { remoteExport = 'unconfirmed' } = {}) {
    if (!this.#state) throw new Error('Capture was not started');
    this.#state = this.call('finish', { capture: this.#state, result_path: resultPath, remote_export: remoteExport });
    return this.state;
  }

  observation(prompt, { consumerId, deploymentId, requestId, requestedLabel,
    requestStartedAtUtc, cacheTtlSeconds, observedAtUtc = new Date().toISOString() }) {
    return this.call('adoption', { prompt_snapshot: snapshotPrompt(prompt), sdk_version: sdkVersion,
      consumer_id: consumerId, deployment_id: deploymentId, request_id: requestId,
      requested_label: requestedLabel, request_started_at_utc: requestStartedAtUtc,
      cache_ttl_seconds: cacheTtlSeconds, observed_at_utc: observedAtUtc });
  }

  get state() { return this.#state ? structuredClone(this.#state) : undefined; }
}

export class CaptureCollectorError extends Error {
  constructor(code, message) { super(message); this.name = 'CaptureCollectorError'; this.code = code; }
}

/** Opt-in persistent Python process. Await every operation's validation ack.
 * No retries, dropping queue, background uploads or automatic restarts occur.
 * Receipts count rejected/unacknowledged requests; they are not durable proof.
 */
export class PersistentReleaseCapture {
  #child; #pending = []; #active; #bytes = 0; #output = Buffer.alloc(0); #stderrBytes = 0;
  #nextId = 1; #state; #closing = false; #closed = false; #failure; #closePromise; #exitPromise;
  #idlePromise; #resolveIdle;
  #stats = { submitted: 0, acknowledged: 0, rejected: 0, dropped: 0, unacknowledged: 0,
    backpressureEvents: 0, highWaterPending: 0, highWaterBytes: 0, exitCode: null, signal: null };

  constructor({ python = 'python3', pythonPath, timeout = 30000, shutdownTimeout = 5000,
    maxPending = 32, maxQueuedBytes = MAX_BYTES, maxMessageBytes = MAX_BYTES } = {}) {
    if (typeof python !== 'string' || !python) throw new TypeError('Use an explicit trusted Python executable');
    boundedInteger(timeout, 1, 60000, 'timeout');
    boundedInteger(shutdownTimeout, 1, 60000, 'shutdownTimeout');
    boundedInteger(maxPending, 1, 1024, 'maxPending');
    boundedInteger(maxMessageBytes, 256, MAX_BYTES, 'maxMessageBytes');
    boundedInteger(maxQueuedBytes, 256, 16_000_000, 'maxQueuedBytes');
    this.timeout = timeout; this.shutdownTimeout = shutdownTimeout; this.maxPending = maxPending;
    this.maxQueuedBytes = maxQueuedBytes; this.maxMessageBytes = maxMessageBytes;
    this.#child = spawn(python, ['-u', '-m', 'langfuse_release_capture', '--serve'], {
      env: childEnvironment(pythonPath), stdio: ['pipe', 'pipe', 'pipe'], shell: false,
    });
    this.#exitPromise = new Promise(resolve => {
      this.#child.once('close', (code, signal) => {
        this.#stats.exitCode = code; this.#stats.signal = signal; this.#closed = true;
        if (!this.#closing || this.#active || this.#pending.length || code !== 0) this.#fail('COLLECTOR_EXIT', 'Capture collector exited before clean shutdown');
        resolve();
      });
    });
    this.#child.once('error', () => this.#fail('COLLECTOR_START', 'Capture collector failed to start'));
    this.#child.stdin.on('error', () => this.#fail('COLLECTOR_PIPE', 'Capture collector input closed unexpectedly'));
    this.#child.stdout.on('data', chunk => this.#receive(chunk));
    this.#child.stderr.on('data', chunk => {
      this.#stderrBytes += chunk.length;
      if (this.#stderrBytes > 4096) this.#fail('COLLECTOR_STDERR', 'Capture collector exceeded its diagnostic bound');
    });
  }

  #error(code, message) { return new CaptureCollectorError(code, message); }

  #drop(code, message) {
    this.#stats.dropped += 1;
    return Promise.reject(this.#error(code, message));
  }

  #fail(code, message) {
    this.#failure ??= { code, message };
    const entries = [...(this.#active ? [this.#active] : []), ...this.#pending];
    this.#active = undefined; this.#pending = []; this.#bytes = 0; this.#output = Buffer.alloc(0);
    for (const entry of entries) {
      clearTimeout(entry.timer); this.#stats.unacknowledged += 1;
      entry.reject(this.#error(code, message));
    }
    this.#settleIdle();
    if (!this.#closed) this.#child.kill('SIGKILL');
  }

  #receive(chunk) {
    if (this.#failure) return;
    if (this.#output.length + chunk.length > MAX_BYTES) return this.#fail('COLLECTOR_RESPONSE_LIMIT', 'Capture collector response exceeded its byte bound');
    this.#output = Buffer.concat([this.#output, chunk]);
    const newline = this.#output.indexOf(10);
    if (newline < 0) return;
    // Exactly one response can arrive: there is only one in-flight request.
    if (newline !== this.#output.length - 1 || !this.#active) return this.#fail('COLLECTOR_PROTOCOL', 'Unexpected capture collector response');
    let ack;
    try { ack = JSON.parse(this.#output.subarray(0, newline).toString('utf8')); }
    catch { return this.#fail('COLLECTOR_PROTOCOL', 'Invalid capture collector response'); }
    const entry = this.#active;
    if (ack.id !== entry.id || ack.submitted_at_utc !== entry.submittedAtUtc || typeof ack.ok !== 'boolean' ||
        typeof ack.processed_at_utc !== 'string' || (ack.ok ? !Object.hasOwn(ack, 'result') : ack.error?.code !== 'CAPTURE_REJECTED')) {
      return this.#fail('COLLECTOR_PROTOCOL', 'Capture collector acknowledgment did not match the request');
    }
    this.#output = Buffer.alloc(0); this.#active = undefined; this.#bytes -= entry.bytes; clearTimeout(entry.timer);
    this.#stats.acknowledged += 1;
    if (ack.ok) entry.resolve({ result: ack.result, acknowledgment: { id: ack.id,
      submittedAtUtc: ack.submitted_at_utc, processedAtUtc: ack.processed_at_utc } });
    else { this.#stats.rejected += 1; entry.reject(this.#error('CAPTURE_REJECTED', 'Capture operation rejected by the shared validator')); }
    this.#pump();
    this.#settleIdle();
  }

  #settleIdle() {
    if (!this.#active && !this.#pending.length) {
      this.#resolveIdle?.(); this.#idlePromise = undefined; this.#resolveIdle = undefined;
    }
  }

  #pump() {
    if (this.#active || this.#failure || !this.#pending.length) return;
    this.#active = this.#pending.shift();
    if (!this.#child.stdin.write(this.#active.raw)) this.#stats.backpressureEvents += 1;
  }

  #submit(operation, args, { submittedAtUtc = new Date().toISOString() } = {}) {
    if (this.#closing || this.#closed || this.#failure) return this.#drop('COLLECTOR_UNAVAILABLE', 'Capture collector is closing, closed or failed');
    if (operation === 'finish' && (this.#stats.dropped || this.#stats.rejected || this.#stats.unacknowledged)) {
      return this.#drop('CAPTURE_INCOMPLETE', 'Dropped, rejected or unacknowledged collector requests prevent finalization');
    }
    let raw;
    try {
      jsonValue(args);
      if (typeof submittedAtUtc !== 'string') throw new TypeError('Capture source time must be a string');
      raw = JSON.stringify({ id: this.#nextId, submitted_at_utc: submittedAtUtc, operation, arguments: args }) + '\n';
    } catch { return this.#drop('CAPTURE_INVALID', 'Capture request is not supported JSON data'); }
    const bytes = Buffer.byteLength(raw);
    const count = this.#pending.length + (this.#active ? 1 : 0);
    if (bytes > this.maxMessageBytes || bytes > this.maxQueuedBytes) return this.#drop('CAPTURE_TOO_LARGE', 'Capture request exceeds its byte bound');
    if (count >= this.maxPending || this.#bytes + bytes > this.maxQueuedBytes) {
      this.#stats.backpressureEvents += 1;
      return this.#drop('CAPTURE_BACKPRESSURE', 'Capture queue is full; request was not accepted');
    }
    this.#stats.submitted += 1; this.#bytes += bytes;
    this.#stats.highWaterPending = Math.max(this.#stats.highWaterPending, count + 1);
    this.#stats.highWaterBytes = Math.max(this.#stats.highWaterBytes, this.#bytes);
    this.#idlePromise ??= new Promise(resolve => { this.#resolveIdle = resolve; });
    return new Promise((resolve, reject) => {
      const entry = { id: this.#nextId++, raw, bytes, submittedAtUtc, resolve, reject };
      entry.timer = setTimeout(() => this.#fail('COLLECTOR_TIMEOUT', 'Capture acknowledgment deadline expired; queued and in-flight outcomes are unknown'), this.timeout);
      this.#pending.push(entry); this.#pump();
    });
  }

  /** Low-level operation returning both result and source-time acknowledgment. */
  request(operation, args, options) { return this.#submit(operation, args, options); }

  async fingerprint(value) { return (await this.#submit('hash', { value })).result; }
  async projectResult(result, options) {
    let args;
    try { args = resultArguments(result, options); }
    catch (error) { this.#stats.dropped += 1; throw error; }
    return (await this.#submit('project-result', args)).result;
  }
  async start({ manifestPath, stageReportPath, dependencyLockPath, dataset, execution }) {
    const args = { manifest_path: manifestPath, stage_report_path: stageReportPath, dataset, execution };
    if (dependencyLockPath !== undefined) args.dependency_lock_path = dependencyLockPath;
    this.#state = (await this.#submit('start', args)).result;
    return this.state;
  }
  async capturePrompt(itemId, prompt, { name, version, capturedAtUtc = new Date().toISOString() }) {
    let snapshot;
    try { snapshot = snapshotPrompt(prompt); }
    catch (error) { this.#stats.dropped += 1; throw error; }
    const ack = await this.#submit('prompt', { item_id: itemId, requested_name: name, requested_version: version,
      prompt: snapshot }, { submittedAtUtc: capturedAtUtc });
    this.#state = ack.result;
    return ack.acknowledgment;
  }
  async observation(prompt, { consumerId, deploymentId, requestId, requestedLabel,
    requestStartedAtUtc, cacheTtlSeconds, observedAtUtc = new Date().toISOString() }) {
    let snapshot;
    try { snapshot = snapshotPrompt(prompt); }
    catch (error) { this.#stats.dropped += 1; throw error; }
    return (await this.#submit('adoption', { prompt_snapshot: snapshot, sdk_version: sdkVersion,
      consumer_id: consumerId, deployment_id: deploymentId, request_id: requestId,
      requested_label: requestedLabel, request_started_at_utc: requestStartedAtUtc,
      cache_ttl_seconds: cacheTtlSeconds, observed_at_utc: observedAtUtc }, { submittedAtUtc: observedAtUtc })).result;
  }
  async finish(resultPath, { remoteExport = 'unconfirmed' } = {}) {
    await this.drain();
    if (!this.receipt.complete) throw this.#error('CAPTURE_INCOMPLETE', 'Dropped, rejected or unacknowledged collector requests prevent finalization');
    const value = (await this.#submit('finish', { result_path: resultPath, remote_export: remoteExport })).result;
    if (!this.receipt.complete) throw this.#error('CAPTURE_INCOMPLETE', 'Capture coverage changed during finalization');
    this.#state = value;
    return this.state;
  }
  async drain() {
    await this.#idlePromise;
    return this.receipt;
  }
  close() {
    if (this.#closePromise) return this.#closePromise;
    this.#closing = true;
    this.#closePromise = (async () => {
      const deadline = setTimeout(() => this.#fail('COLLECTOR_SHUTDOWN_TIMEOUT', 'Capture collector did not drain and exit before its shutdown deadline'), this.shutdownTimeout);
      try {
        if (!this.#failure && !this.#closed) {
          await this.#idlePromise;
          if (!this.#failure) this.#child.stdin.end();
        }
        await this.#exitPromise;
      } finally { clearTimeout(deadline); }
      return this.receipt;
    })();
    return this.#closePromise;
  }
  get state() { return this.#state ? structuredClone(this.#state) : undefined; }
  get receipt() {
    return { ...this.#stats, pending: this.#pending.length + (this.#active ? 1 : 0), queuedBytes: this.#bytes,
      complete: !this.#active && !this.#pending.length && !this.#failure && this.#stats.dropped === 0 && this.#stats.rejected === 0 && this.#stats.unacknowledged === 0,
      closed: this.#closed, failure: this.#failure ? { ...this.#failure } : null };
  }
}
