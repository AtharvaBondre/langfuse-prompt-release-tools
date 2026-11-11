# Langfuse prompt API contract

This helper uses four prompt operations from [Langfuse e3a2bee2](https://github.com/langfuse/langfuse/commit/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3), committed 16 October 2026. The review indexed the [generated OpenAPI](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/web/public/generated/api/openapi.yml): 66 paths and 112 operations. Detailed checks cover the four operations below. Contract review used public source and synthetic fixtures. A later disposable local v4 run exercised these operations; see `DEMO-RESULTS.md` for exact scope and image identities.

## Operations

| Method and path | Request / response |
|---|---|
| GET `/api/public/v2/prompts` | Paginated metadata: `{data: [...], meta: {page, limit, totalItems, totalPages}}` |
| GET `/api/public/v2/prompts/{name}` | Select version or label; returns one text/chat Prompt |
| POST `/api/public/v2/prompts` | Create a version; returns its Prompt object and version number |
| PATCH `/api/public/v2/prompts/{name}/versions/{version}` | Add/move labels to an explicit version; returns its Prompt object |

URL-encode the full name, including folder separators. List pagination starts at 1, defaults to 10 items and permits 100 per page. Filtered metadata can contain only matching versions. Fetch a full baseline before relying on its version/label history.

Named GET defaults to `production`. Specify either `version` or `label`; supplying both returns 400. Default `resolve=true` expands dependencies. For release verification, fetch the returned version with `resolve=false` to read stored content and bypass the prompt cache. Check the deployment label through a separate GET, then confirm its version matches the staged version.

## Payloads and schema differences

`api-examples.json` contains synthetic text, chat, placeholder, list and promotion requests/responses. Text creation requires name and string prompt. Chat requires `type:"chat"` and a message array. Both return name, version, type, prompt, config, labels and tags, with optional metadata.

| Detail | Pinned implementation |
|---|---|
| POST status | 201; generated schema advertises 200 |
| Chat placeholder | Requires `type:"placeholder"` and a name matching `^[A-Za-z][A-Za-z0-9_]*$`; generated schema makes type optional |
| Normal chat message | String role/content; runtime strips extra properties |
| Create labels | Array or omitted; runtime rejects null despite generated nullable schema |
| Config | Arbitrary JSON; omitted/root-null config becomes `{}` |
| Label / commitMessage length | Create labels: 1–36 lowercase letters, digits, `_`, `-`, `.`; commitMessage: at most 500 characters |

The [request schemas](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/packages/shared/src/features/prompts/types.ts#L31) and [placeholder validation](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/packages/shared/src/server/llm/types.ts#L196) define these runtime constraints. Config shape checks do not evaluate model output quality.

## Staging and promotion

Staging sends `labels:[]`. Langfuse still advances **latest**, so applications consuming latest can see the candidate. Explicit tags apply to **all versions**; omitted/null tags inherit prior tags and `[]` clears them. [Creation rules](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/web/src/features/prompts/server/actions/createPrompt.ts#L136).

Promote one chosen existing label with this synthetic PATCH body:

```json
{"newLabels":["production"]}
```

PATCH preserves other target labels and moves each supplied label from its previous version. An empty array leaves labels unchanged. Runtime rejects latest. [Additive update](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/web/src/features/prompts/server/actions/updatePrompts.ts#L52).

Bind the staged report to the reviewed snapshot. Before promotion, verify all selected candidates and current label targets. If a label already points to the verified candidate, record completion without another write. If it points to a version other than the baseline or candidate, stop for review. The API provides no compare-and-swap condition; preflight cannot eliminate the final race or make a batch atomic.

## Auth and uncertain outcomes

Use HTTP Basic with the public key as username and secret key as password. Project permissions and protected-label permissions apply. Error responses include validation/auth failures, 429 rate limits and server failures; body shapes vary.

**PATCH can return 409 after moving the label.** Its [transaction commits before event preparation](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/web/src/features/prompts/server/actions/updatePrompts.ts#L134), where [dependency resolution can throw a conflict](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/packages/shared/src/server/services/PromptService/index.ts#L386). Reconcile uncertain writes through exact-version and label reads before considering another mutation. Retain partial results; do not retry writes or roll back a batch without review. Successful server read-back does not establish immediate application adoption: SDK caches can retain earlier versions.

## Reproduce the schema check

The optional checker verifies the schema's pinned SHA-256 before validation. It uses native OAS 3.0 semantics and resolves references against the full document.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-review.txt
.venv/bin/python validate_api_examples.py --report api-check.json
```

Omit `--report` for console output. For offline use, add `--schema openapi.yml`; the checksum requirement stays the same. The helpers use Python's standard library.

All **18 expectations** match: nine valid examples, six malformed controls and three schema/runtime discrepancy demonstrations. A negative control passes when the validator rejects it. The `oas_accepts_*` cases show payloads that pass the generated schema but fail runtime checks; use them as tests, not requests. This checker does not exercise authorization, label movement, concurrency, caching or model behavior.

Langfuse’s [versioning workflow](https://langfuse.com/docs/prompt-management/data-model) covers staged validation and label deployment. Its [official skill](https://github.com/langfuse/skills/blob/127253d0e26dfa77fb6d732d1b70c36625581eb4/skills/langfuse/SKILL.md) provides API access guidance. Use this helper to record reviewed snapshots, stage versions and promote one label with per-prompt reconciliation.
