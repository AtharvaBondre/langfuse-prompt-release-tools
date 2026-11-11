import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { types } from 'node:util';
import { ChatPromptClient, TextPromptClient } from '@langfuse/client';

export const sdkVersion = JSON.parse(readFileSync(
  new URL('../package.json', import.meta.resolve('@langfuse/client')), 'utf8')).version;

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
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      if (key.startsWith('LANGFUSE_') || ['GH_TOKEN', 'GITHUB_TOKEN'].includes(key)) delete env[key];
    }
    if (this.pythonPath !== undefined) env.PYTHONPATH = this.pythonPath;
    try {
      return JSON.parse(execFileSync(this.python, ['-m', 'langfuse_release_capture'], {
        input: JSON.stringify({ operation, arguments: args }), encoding: 'utf8',
        env, timeout: this.timeout, maxBuffer: 8_000_000, stdio: ['pipe', 'pipe', 'pipe'],
      }));
    } catch (error) {
      // Do not expose child command arguments or captured prompt content.
      const detail = String(error.stderr ?? '').trim();
      throw new Error(detail.startsWith('Capture rejected:') ? detail : 'Python capture bridge failed');
    }
  }

  fingerprint(value) { return this.call('hash', { value }); }

  start({ manifestPath, stageReportPath, dependencyLockPath, dataset, execution }) {
    if (this.#state) throw new Error('Capture already started');
    this.#state = this.call('start', { manifest_path: manifestPath, stage_report_path: stageReportPath,
      dependency_lock_path: dependencyLockPath, dataset, execution });
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
