# Use this project alongside Langfuse

Langfuse provides the prompt platform and experiment tools. This repository gives you a repeatable release workflow that connects reviewed requirements, task-time prompt capture, test results, label changes and app observations.

Choose it when you want those connections without writing and maintaining the integration from scratch. Keep using official Langfuse tools for editing prompts, running experiments and inspecting traces.

## Native tools and the workflow we add

We checked the official sources below on **11 November 2026**. This is a comparison with the documented tools and examples, not a claim that nobody has built a similar workflow.

| Need | Official Langfuse support | This repository's additional workflow |
|---|---|---|
| Version and deploy prompts | [Versions, labels, diffs, rollback and protected labels](https://langfuse.com/docs/prompt-management/features/prompt-version-control). | Export a reviewable snapshot, stage candidates, then validate the bound test evidence before moving a selected label. Read the resulting version back. |
| Gate a release on evaluations | [Experiment Action](https://github.com/langfuse/experiment-action/tree/v1.0.12), [CI regression gates and approved baselines](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd#approved-baseline), plus [release-policy guidance](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd#release-policy). | Pin required cases and typed checks before staging. Bind their actual results to the staged prompt objects and dependencies your task fetched. Revalidate the preserved evidence at promotion. |
| Use nested prompts | [Prompt composition](https://langfuse.com/docs/prompt-management/features/composability) and SDK resolution. | Record the transitive dependency graph and raw/resolved content hashes used in evaluation. Refuse promotion when later label changes invalidate that evidence. |
| Store experiments and scores | Official experiment execution, datasets, trace export and score APIs. | Read required cases and typed scores back from the selected project. Keep local completion, remote persistence and evaluation success as separate outcomes. Require a fresh receipt when the reviewed policy calls for it. |
| Connect GitHub and deployment | [GitHub integration](https://langfuse.com/docs/prompt-management/features/github-integration), repository dispatch and custom CI/API deployment. | Supply release commands, original-byte evidence bundles and optional verification against a separately selected GitHub signer policy. |
| Inspect an interrupted operation | Prompt/label APIs and version history let you inspect current state. | Compare that state with the saved attempt. Classify completed, pending, conflicting or unknown outcomes without guessing that a lost response means the write failed. |
| Understand app use after promotion | [SDK caching](https://langfuse.com/docs/prompt-management/features/caching), prompt-linked traces and observability. | Compare captured app accesses against released versions and dependencies. Check fallback, mixed versions, named consumers and time-window coverage. |
| Work from a terminal or agent | The [official CLI](https://github.com/langfuse/langfuse-cli), [MCP server](https://langfuse.com/docs/api-and-data-platform/features/mcp-server) and [skills](https://github.com/langfuse/skills). | Offer fifteen commands for this release workflow and a scoped agent skill. Continue using official tools for general platform work. |

Langfuse's current CI documentation includes per-case checks, reviewed baselines, evaluator/dataset identity and missing/duplicate-case handling. Those ideas are not unique to this repository. Our contribution is the connected implementation, its evidence formats and the tested handling of failures between steps.

## A concrete reason to adopt it

Suppose your evaluation passes for prompt version 18. That prompt refers to `shared/safety` through a label. Before promotion, a teammate moves that nested label.

You can investigate using Langfuse's versions and APIs. With this toolkit's dependency lock and captured experiment, you also have the exact graph you tested and a promotion check that rejects the changed graph. Review the dependency change, rerun the experiment and keep both attempts as evidence.

After promotion, capture an access from each app consumer you want to check. If one returns version 17 from its cache, the adoption report shows that mismatch even though the deployment label points to version 18. The observed requests define the scope of that conclusion.

## Choose the amount of workflow you need

| Your situation | A useful starting point |
|---|---|
| You need prompt editing, versions and a manual label update. | Start with native Langfuse. |
| You already have CI evaluation scripts and need release evidence tied to the fetched prompts. | Add policy pinning, capture, dependency locking and import. |
| Several people or jobs review and deploy prompts. | Add portable evidence and a signer policy for CI handoffs. |
| Several services, caches or deployments consume a prompt. | Add SDK adoption observations and reviewed rollout windows. |
| You need a turnkey UI or framework plugin that requires no application wiring. | Version 0.6 does not provide that experience; see the [product plan](product-improvements.md). |

The package supplies the release checks. You still choose useful datasets, quality thresholds, model configuration and the application capture points. The [working example](getting-started.md#1-run-the-working-example) shows the integration before you commit to it.

## Boundaries that matter in a release

- A passing gate confirms that the supplied evidence meets the selected policy. Execution claims still depend on the capture producer; this does not establish general model quality.
- Observation reports cover the requests you instrument. They do not discover your whole fleet or prove that unsampled requests used the candidate.
- CI provenance authenticates the expected builder/artifact relationship. Signer-required enforcement inside promotion remains planned.
- The remote verifier checks project identity. Earlier export/staging/promotion commands still need a dedicated project key set; binding project ID before all writes is planned.
- Local reconciliation preserves uncertain outcomes. It does not turn several API writes into a server transaction or perform automatic rollback.

## Research and test references

The source review used Langfuse server **4.78.0**, Experiment Action **1.0.12**, and the official documentation at [revision `3c7670a`](https://github.com/langfuse/langfuse-docs/tree/3c7670a175810c67d530c89be2c0fdae17ca2761). Official `main` can contain changes that a released server does not yet expose.

Our [validation record](validation.md) identifies the exact tested server/SDK pairs. The [platform contract audit](official-platform-contracts.md) and [agent-tools audit](official-agent-tools.md) contain the API, CLI, MCP and skill details behind this comparison. Use those dated records when checking a later Langfuse release.
