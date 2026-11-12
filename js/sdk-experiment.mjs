/** Real official SDK experiment and consumer probe. Synthetic cases; no LLM. */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { LangfuseClient, RunnerContext } from '@langfuse/client';
import { LangfuseSpanProcessor } from '@langfuse/otel';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { ReleaseCapture, PersistentReleaseCapture, projectExperimentResult, sdkVersion } from './capture.mjs';

const bridge = new ReleaseCapture({ python: process.env.RELEASE_PYTHON ?? 'python3',
  pythonPath: process.env.RELEASE_PYTHON_PATH });
const CASES = [
  { id: 'one', input: { answer: 'A pinned prompt version is immutable.' }, expectedOutput: { answer: 'A pinned prompt version is immutable.' } },
  { id: 'two', input: { answer: 'A label can move between versions.' }, expectedOutput: { answer: 'A label can move between versions.' } },
];
const canonicalCases = CASES.map(row => ({ id: row.id, input: row.input, expected_output: row.expectedOutput }));

export function identities() {
  const revision = bridge.fingerprint(readFileSync(fileURLToPath(import.meta.url), 'utf8'));
  return { dataset: { id: 'local-js-release-demo', name: 'local-js-release-demo', version: bridge.fingerprint(canonicalCases) },
    execution: { task_revision: revision, evaluator_revision: revision,
      model: { provider: 'local', name: 'deterministic-json-renderer', config_sha256: bridge.fingerprint({}) } },
    cases: canonicalCases, sdk_version: sdkVersion };
}

function save(path, value) {
  writeFileSync(path, JSON.stringify(value, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
}

export async function experiment(config) {
  const capture = config.collector === 'persistent'
    ? new PersistentReleaseCapture({ python: process.env.RELEASE_PYTHON ?? 'python3', pythonPath: process.env.RELEASE_PYTHON_PATH })
    : bridge;
  let provider;
  try {
    const identity = identities();
    const state = await capture.start({ manifestPath: config.manifest, stageReportPath: config.stage,
      dependencyLockPath: config.lock, dataset: identity.dataset, execution: identity.execution });
    if (state.targets.length !== 1) throw new Error('Example expects one target');
    const target = state.targets[0];
    const auth = { publicKey: process.env.LANGFUSE_PUBLIC_KEY, secretKey: process.env.LANGFUSE_SECRET_KEY,
      baseUrl: config.host };
    provider = new NodeTracerProvider({ spanProcessors: [new LangfuseSpanProcessor(auth)] });
    provider.register();
    const client = new LangfuseClient(auth);
    const started = performance.now();
    const result = await new RunnerContext({ client }).runExperiment({ name: 'js-release-contract-demo',
      data: CASES, maxConcurrency: 1,
      task: async item => {
        const prompt = await client.prompt.get(target.name, { version: target.version, cacheTtlSeconds: 0, type: 'text' });
        await capture.capturePrompt(item.id, prompt, { name: target.name, version: target.version });
        return JSON.parse(prompt.compile(item.input));
      },
      evaluators: [async ({ output, expectedOutput }) => ({ name: 'answer-exact',
        value: JSON.stringify(output) === JSON.stringify(expectedOutput) ? 1 : 0, dataType: 'BOOLEAN' })],
    });
    const artifact = await projectExperimentResult(result, { scriptPath: 'js/sdk-experiment.mjs',
      durationMs: Math.round(performance.now() - started) }, capture);
    save(config.results, artifact);
    await provider.forceFlush();
    save(config.capture, await capture.finish(config.results, { remoteExport: 'unconfirmed' }));
    return { experiment_id: result.experimentId, cases: result.itemResults.length, sdk: sdkVersion };
  } finally {
    try { await provider?.shutdown(); }
    finally {
      if (capture instanceof PersistentReleaseCapture) {
        const receipt = await capture.close();
        if (config.collector_receipt) save(config.collector_receipt, receipt);
        if (!receipt.complete) throw new Error('Capture collector closed with incomplete evidence');
      }
    }
  }
}

export async function observe(config) {
  const client = new LangfuseClient({ publicKey: process.env.LANGFUSE_PUBLIC_KEY,
    secretKey: process.env.LANGFUSE_SECRET_KEY, baseUrl: config.host });
  const start = new Date().toISOString();
  const prompt = await client.prompt.get(config.name, { label: config.label, type: config.type ?? 'text',
    cacheTtlSeconds: config.cache_ttl_seconds ?? 0,
    ...(config.fallback !== undefined ? { fallback: config.fallback } : {}) });
  const observation = bridge.observation(prompt, { consumerId: config.consumer_id, deploymentId: config.deployment_id,
    requestId: randomUUID(), requestedLabel: config.label, requestStartedAtUtc: start,
    cacheTtlSeconds: config.cache_ttl_seconds ?? 0 });
  save(config.output, observation);
  return { returned_version: prompt.version, is_fallback: prompt.isFallback };
}

export async function cacheProbe(config) {
  const client = new LangfuseClient({ publicKey: process.env.LANGFUSE_PUBLIC_KEY,
    secretKey: process.env.LANGFUSE_SECRET_KEY, baseUrl: config.host });
  const get = async (ttl, name = config.name, fallback) => {
    const started = new Date().toISOString();
    const prompt = await client.prompt.get(name, { label: 'production', type: 'text', cacheTtlSeconds: ttl,
      ...(fallback !== undefined ? { fallback } : {}) });
    return bridge.observation(prompt, { consumerId: 'js-demo', deploymentId: 'local-v06',
      requestId: randomUUID(), requestedLabel: 'production', requestStartedAtUtc: started, cacheTtlSeconds: ttl });
  };
  const baseline = await get(600);
  save(config.ready, baseline);
  const deadline = Date.now() + 60000;
  while (!existsSync(config.go)) {
    if (Date.now() > deadline) throw new Error('Cache probe timed out waiting for promotion');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const cached = await get(600), fresh = await get(0);
  const fallback = await get(0, config.name + '-missing', 'synthetic fallback');
  save(config.output, { baseline, cached, fresh, fallback });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const [operation, path] = process.argv.slice(2);
  if (operation === 'identity') save(path, identities());
  else if (operation === 'experiment') console.log(JSON.stringify(await experiment(JSON.parse(readFileSync(path, 'utf8')))));
  else if (operation === 'observe') console.log(JSON.stringify(await observe(JSON.parse(readFileSync(path, 'utf8')))));
  else if (operation === 'cache-probe') await cacheProbe(JSON.parse(readFileSync(path, 'utf8')));
  else throw new Error('Choose identity, experiment or observe with a new output/config path');
}
