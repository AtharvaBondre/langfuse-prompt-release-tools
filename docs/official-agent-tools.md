# Official Langfuse agent tools

Reviewed and refreshed **11 November 2026** against official documentation, pinned source and public metadata. The integration notes describe shipped **0.6.0a1**. Langfuse supplies the general API, agent access, experiment execution and documented CI gate recipes; this toolkit packages release-specific bindings and checks around those capabilities. See the [comparison](langfuse-comparison.md).

## Choose the upstream surface

| Need | Official tool | Boundary |
|---|---|---|
| Find documentation | [Docs MCP](https://langfuse.com/docs/docs-mcp), `https://langfuse.com/api/mcp` | Unauthenticated Streamable HTTP. Three documentation reads plus an outbound feedback tool. |
| Work with project data from an MCP client | [Native Langfuse MCP](https://langfuse.com/docs/api-and-data-platform/features/mcp-server), `<host>/api/public/mcp` | Project-scoped API keys with Basic authorization; read and write tools enabled by default. Supports Cloud regions and self-hosting. |
| Use Langfuse from a terminal or CI | [Official CLI](https://github.com/langfuse/langfuse-cli/tree/46d3bce82e695cdf1dc05c89f9f9822e4260d065), `@langfuse/cli` | General API reads/writes, JSON/schema discovery, body files, pagination and API-version snapshots. Can be combined with a CI workflow's checks and approval steps. |
| Teach a coding agent general Langfuse workflows | [Official Agent Skills](https://github.com/langfuse/skills/tree/f0bcb13088b490c04b3f4870bace17f6d57b199c) | Instructions/references for tracing, prompts, datasets, evaluation and CLI use. A skill is not an authorization boundary. |
| Run application experiments | [Langfuse SDK experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk) and [experiment-action](https://github.com/langfuse/experiment-action) | Execute the application's task/evaluators. May make model calls and write experiment/score data. The Action can fail CI on regressions and emit `result_json`. |
| Work inside the Langfuse UI | [Halo](https://langfuse.com/docs/api-and-data-platform/features/halo) | Managed in-app assistant using project MCP tools, docs search and sandboxed code. Adds UI context, background runs and action approvals. |
| Trace the coding agent itself | Official observability plugins below | Upload agent telemetry, often including prompts, tool inputs/outputs and images. Separate from prompt management and release validation. |
| Version and distribute a skill | [Skill Management](https://langfuse.com/docs/skill-management/overview) | Hosted immutable skill files/labels; official CLI already supports install/update/push. Publishing does not automatically trace usage. |

[Upstream recommends](https://langfuse.com/docs/prompt-management/features/agentic-access) Agent Skill + CLI for agents that can run shell commands, MCP for clients that cannot, and CLI/public API for scripts or CI. These are composition choices; this project's Python release commands do not require a new MCP service.

## MCP inventory and writes

The [canonical tool catalogue](https://mcp.reference.langfuse.com/generated/mcp-reference.json), fetched on 11 November with `build.generatedAt` **2026-11-11T12:33:51.955Z**, lists **102 project tools and four documentation tools** (106 total). The earlier **01:29:07.248Z** snapshot had 99 project tools. The three added tools get, set and delete the project's default evaluation model. This catalogue refreshes independently; its inventory is not a promise that every self-hosted release has those tools.

Latest released Langfuse remains [4.78.0](https://github.com/langfuse/langfuse/releases/tag/v4.78.0). Refreshed [main `3781307`](https://github.com/langfuse/langfuse/commit/3781307833fc079426f6330fe6be5054cabbf3a3) includes the new [default-model tool source](https://github.com/langfuse/langfuse/blob/3781307833fc079426f6330fe6be5054cabbf3a3/web/src/features/mcp/server/evals/tools/defaultEvaluationModel.ts); do not attribute that main/catalogue addition to the released server without checking availability.

The four documentation tools are `searchLangfuseDocs`, `getLangfuseDocsPage`, `getLangfuseOverview`, and **`submitFeedback`**. The first three read documentation; `submitFeedback` sends information to Langfuse and requires explicit approval. The documentation server as a whole is therefore not read-only. Its WebMCP integration exposes only the three reads.

The six project prompt tools are:

- `getPrompt`: fetch by label or version with dependencies resolved; defaults to `latest`.
- `getPromptUnresolved`: fetch raw content with dependency tags intact.
- `listPrompts`: list/filter metadata with pagination.
- `createTextPrompt` and `createChatPrompt`: create immutable versions, advancing `latest`.
- `updatePromptLabels`: move labels to a specified version.

The remaining project tools cover hosted skills, observations/filter schemas, annotation queues, comments, datasets/items/runs, scores/configs, metrics, model definitions, LLM-connection metadata, media, evaluators/rules, dashboards/widgets/placements, experiments, alerts, migration readiness, external media storage and feedback.

The inventory includes reads, state-changing operations, evaluator execution, feedback and signed-URL generation; tool counts are not an executable allowlist. `testEvaluator` and `runBatchEvaluation` execute evaluators, write traces/scores and may incur model or code-execution costs. `setDefaultEvaluationModel` also validates the model through a provider test call before saving it and can resume paused evaluators.

Native MCP [runtime validation](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/web/src/features/mcp/server/prompts/validation.ts) prevents assigning `production` during prompt creation. Promotion uses `updatePromptLabels`. **Do not assume this restriction applies to generic REST/CLI creation**, or that native MCP checks this project's local evaluation receipts. Other supplied deployment labels can still move during creation, and `latest` advances in either case.

Use client allowlists for restricted MCP access. Halo's in-app approval UI is separate from the behavior of an external MCP client. The initial 11 November catalogue reported native MCP server version `0.3.0-unstable`; that historical server identifier is not a Langfuse platform version or a compatibility guarantee.

## CLI and skill composition

Official CLI **1.3.1** is published as `@langfuse/cli`; the older `langfuse-cli` name is still published but upstream prefers the scoped package. Node 20+ or Bun is required. Upstream supports npx/bunx invocation or npm/Bun installation.

Supply project keys through environment/secrets and set the target host explicitly. The CLI also accepts `.env` files and credential flags; avoid putting secret values in command history. `--curl` previews a request without executing and redacts authorization by default. API write commands otherwise perform their requested mutation; `--curl` is not an evaluation gate.

The pinned CLI's checked-in v4.71.0 conformance golden lists **116 operations across 29 resources**. It is a versioned snapshot, not proof of coverage for every later platform version. `--api-version` supports version selection; upstream documents v3 snapshots for self-hosted deployments only.

Official skills can be installed using the skills CLI (`npx skills add langfuse/skills --skill langfuse`, optionally selecting an agent) or a manual skill-directory symlink. The repository also contains Claude, Codex and Cursor plugin manifests at version **1.12.0**; Codex/Cursor point to `./skills/`.

The toolkit ships a focused [langfuse-prompt-release skill](../skills/langfuse-prompt-release/SKILL.md), separate from upstream's broader `langfuse` skill. It guides use of the existing commands. A plugin wrapper remains a distribution option requiring client testing; it does not add release checks by itself.

## Reuse experiment execution

The official [`experiment-action` v1.0.12](https://github.com/langfuse/experiment-action/tree/c7c4efe432e37d4e3693a018de469233c0018de4) runs Python/JS/TS experiments, supports dataset-version selection, and exposes `result_json`. Regression and script-error gates default on. PR commenting defaults on but requires a supplied GitHub token and permission; it can be disabled.

The [current official CI guide](https://github.com/langfuse/langfuse-docs/blob/3c7670a175810c67d530c89be2c0fdae17ca2761/content/docs/evaluation/experiments/experiments-ci-cd.mdx) also shows reviewed baselines, per-case regression detection, dataset/evaluator version checks and required-case policies. These can be composed with the official [GitHub integration](https://langfuse.com/docs/prompt-management/features/github-integration) and prompt API.

The toolkit's shipped [importer and capture contract](experiment-evidence.md) retain the original Action v1 results, required check IDs and the **exact staged prompt versions** returned during the task. Its Python and [JavaScript producers](../js/README.md) run through official SDKs. They bind the result to a policy pinned before staging and the evaluated transitive dependency lock. A normalized SDK result does not claim the GitHub Action itself ran.

Shipped [remote verification](remote-persistence.md) separately checks stored cases and typed scores; [portable bundles and provenance](portable-evidence.md) preserve and authenticate selected artifacts; [adoption and rollout checks](rollout-observation.md) assess supplied consumer observations after promotion. See [validation](validation.md) for actual SDK/server and signed-CI evidence. These checks package a release process users could also build from official APIs, SDKs and their own CI.

## Official telemetry plugins are separate

The following table preserves the **initial 11 November source/package snapshot**; plugin package versions were not re-audited in the later prompt-release refresh. All listed repositories had MIT licenses in that inspected source. Versions distinguish published artifacts from repository state.

| Integration | Official repository | Captured version and limitation |
|---|---|---|
| Claude Code | [claude-observability-plugin](https://github.com/langfuse/claude-observability-plugin/tree/b1619f994240ad49de4a97caa01d7d7853944fc0) | Source manifest 1.2.2; latest returned GitHub release 1.2.1. Marketplace plugin traces CLI/desktop Code mode, not desktop Chat. Uses uv or Python 3.10+ and SDK dependencies. |
| Codex | [codex-observability-plugin](https://github.com/langfuse/codex-observability-plugin/tree/e1d02be28e6bacbfe350a1bc1ccf861e54edee17) | npm 0.4.3; GitHub release list still 0.4.2. Requires Codex 0.143+, Node 22+, npm, explicit tracing opt-in and trusted hooks. |
| OpenCode | [opencode-observability-plugin](https://github.com/langfuse/opencode-observability-plugin/tree/e7596bd8b15596eb1ce8c443059fbc0920abdbd4) | npm/GitHub 0.7.0. Supports OpenCode 1/2; Node 22+, self-hosted Langfuse 4.59.0+ or Cloud. |
| Pi | [pi-observability-plugin](https://github.com/langfuse/pi-observability-plugin/tree/237e61c38f066c1aeebd2817f051b2a5baa1741d) | npm/GitHub 0.1.2, marked experimental. Supports one-run extension loading or installation. |
| Cursor | [cursor-observability-plugin](https://github.com/langfuse/cursor-observability-plugin/tree/498607b0b37305364dada4d66154f54a924d0322) | Source package 0.1.0, experimental; no GitHub release returned and npm latest lookup returned 404. README documents marketplace/source installation; installation was not tested. |
| OpenClaw | [openclaw-x-langfuse-plugin](https://github.com/langfuse/openclaw-x-langfuse-plugin/tree/4a9e74b35899bfb39347133420183b726dc0d44b) | Source package 1.5.0, npm latest 1.5.0-beta1. Diagnostics bridge; input/output recovery is best-effort and direct `openclaw agent` runs lack the required events. |

Do not enable telemetry merely to install a release skill. Choose the destination and data handling separately. [Claude Gateway](https://langfuse.com/integrations/developer-tools/claude-code-gateway) and [Codex Gateway](https://langfuse.com/integrations/developer-tools/codex-gateway) are additional, distinct integrations: they change inference routing and use gateway keys rather than project API-key pairs.

## Older and community implementations

[`langfuse/mcp-server-langfuse`](https://github.com/langfuse/mcp-server-langfuse/tree/a534b5a995d50c21ba45f14419246ef64d3ca6f4) is an **official older repository**, not the native project MCP. Its inspected source offers MCP prompt list/get and tools `get-prompts`/`get-prompt`, reads only production-labeled prompts, compiles variables, and requires a locally built stdio server. In the initial 11 November snapshot, GitHub listed stable v0.0.1 and newer prerelease v0.0.2-rc.0; npm latest lookup returned 404. The repository was not marked archived and no formal deprecation was found in those inspected sources. Prefer current official native-MCP guidance for new integrations.

The official docs also mention community client bridges such as `mcp-remote` and [`pi-mcp-adapter`](https://github.com/nicobailon/pi-mcp-adapter). Those are not official Langfuse data servers. Other third-party Langfuse servers are outside this audit.

## Verification limits

The refreshed source audit checked the latest release metadata, CLI/Action documentation, core main changes, documentation revision `3c7670a` and the dated public MCP catalogue. It was not an MCP/plugin installation or tenant execution test. Separate [release validation](validation.md) covers disposable synthetic SDK/server workflows. Package metadata, source manifests and GitHub releases sometimes differ; pins above preserve that distinction. Generated MCP examples can omit runtime constraints: for example, prompt label/version are mutually exclusive even if a generated example shows both. The label-update description also contains inconsistent empty-list wording: the [pinned shared runtime](https://github.com/langfuse/langfuse/blob/47a70342f1736561dfa157a654d011aac29db79d/web/src/features/prompts/server/actions/updatePrompts.ts) adds labels and preserves existing ones, so an empty list is a no-op. See the [platform contracts](official-platform-contracts.md) for the detailed source check.

The focused integration is reviewed snapshot → exact stage → task-bound evaluation/dependencies → promotion/readback → sampled adoption. Official tools supply the underlying operations, and users can assemble their own release workflow around them. A release-only MCP interface remains conditional on a consumer need the existing commands cannot meet; the [roadmap](roadmap.md) distinguishes shipped checks from future integrations.
