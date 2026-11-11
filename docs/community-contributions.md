# Contribute useful release checks to the Langfuse community

A useful contribution lets a Langfuse user reproduce a release problem, understand the result and reuse the smallest part that helps them. Contributors should be able to adopt an example or test without adopting this entire toolkit.

The [customer workflows](customer-workflows.md) describe the problems we target. Our current evidence covers synthetic tasks, real SDK/server behavior and CI artifact verification. We have not yet completed an existing-application pilot or received an upstream endorsement.

## Pieces maintainers could reuse

These are contribution candidates. Check the current upstream implementation, tests and contribution instructions before opening a proposal. Start with an example or reproduction; propose a platform change only after establishing the missing behavior and agreeing on its scope.

### 1. Typed-score persistence across Python and JavaScript

**User problem:** A local boolean verdict and a stored BOOLEAN score have different serialization requirements. Task completion or `flush()` alone leaves required-score coverage open.

**Small contribution:** A paired Python/JavaScript example that runs the same passing and failing deterministic cases, then reads back the score's type and value. Document the supported representation: Python's explicit BOOLEAN type and JavaScript's typed numeric `0`/`1` values for the checked SDK versions.

**Evidence to include:** The exact SDK/server versions; evaluator code; original source results; a redacted stored score; and the missing-export outcome. The [remote persistence guide](remote-persistence.md) records the contracts and observed cases. The [verified example](../examples/verified_release_demo.py) demonstrates persisted failing JavaScript scores that still block promotion.

**Acceptance:** Both supported representations retain their intended type and value. A false verdict remains false after storage. A missing score remains incomplete. Distinguish documented input constraints from a confirmed SDK defect before filing a bug.

Primary references: [official experiment guide](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk), [Action result schema](https://github.com/langfuse/experiment-action/blob/v1.0.12/schemas/result-json.v1.schema.json) and [released API schema](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/public/generated/api/openapi.yml).

### 2. SDK-result compatibility fixtures

**User problem:** A CI integration can lose case identity, trace identity or score types while translating SDK results into an artifact format.

**Small contribution:** Offer the two preserved synthetic SDK result files and a minimal projection example as a test corpus. Keep them separate from the release engine. Preserve upstream source and license notices.

**Evidence to include:** [Original fixtures and provenance](../tests/fixtures/sdk-results/README.md), their unchanged bytes, the official schema version and [schema/semantic tests](../test_sdk_result_fixtures.py).

**Acceptance:** Python and JavaScript projections validate against the declared official schema; IDs and typed scores survive; a completed script with a false evaluator does not become a passing evaluation; runtime and producer identities agree.

This is a candidate for SDK/Action documentation or contract tests. Review existing upstream fixture coverage first. Our next supported projection helpers should use the same fixtures, so users and maintainers can inspect one contract.

### 3. Dependency changes between evaluation and promotion

**User problem:** A parent prompt's version stays unchanged while a nested label moves. The next resolved prompt can differ from the one the task evaluated.

**Small contribution:** A CI recipe that retains the evaluated dependency graph and resolved-content hashes, then compares them before moving a deployment label. Reuse Langfuse's resolver.

**Reproduction:** Create parent → child → leaf. Evaluate the parent, move the leaf's label and attempt promotion. Our [live example](../examples/verified_release_demo.py) demonstrates refusal of the earlier evidence, followed by an actual rerun with a fresh lock on the two recorded server versions.

**Acceptance:** The recipe identifies the changed dependency, preserves the earlier results and requires reevaluation. Explain the concurrent-write race that remains between a check and a write; do not claim an atomic transaction.

Primary references: [composition documentation](https://langfuse.com/docs/prompt-management/features/composability), [released resolver](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/packages/shared/src/server/services/PromptService/index.ts) and [reviewed main resolver](https://github.com/langfuse/langfuse/blob/3781307833fc079426f6330fe6be5054cabbf3a3/packages/shared/src/server/services/PromptService/index.ts). Main has newer resolver changes, so rerun a proposed upstream example against the maintainer's target revision.

### 4. Recovery after an uncertain label update

**User problem:** The client can lose a response after the server commits a label change. An operator needs to inspect the outcome before choosing another write.

**Small contribution:** A troubleshooting recipe that records the intended mutation, simulates response loss and reads the exact version/label state. Keep the recovery step read-only.

**Evidence to include:** The [Python baseline](sdk-release-example.md#older-05-python-baseline), saved attempt and reconciliation outcome. It injects client-side response loss after a successful PATCH. That demonstrates recovery; it is not evidence of an upstream outage.

**Acceptance:** A committed label update can be identified without repeating the PATCH. Conflicting state remains explicit. An uncertain creation cannot become confirmed merely because a matching version exists. Any proposed server fix needs a separate reproduction of the actual transport or post-commit failure.

Primary reference: the [released label-update action](https://github.com/langfuse/langfuse/blob/138beceb728b998003059981892ad09e04f154b1/web/src/features/prompts/server/actions/updatePrompts.ts).

## Prepare a proposal a maintainer can assess

Include these details in the issue or pull request:

1. The user's task and the exact failure or confusing outcome.
2. Current upstream behavior, source revision and existing documentation.
3. A small reproduction with synthetic inputs and exact versions.
4. Expected and observed output, including failed or incomplete outcomes.
5. The smallest proposed change and its acceptance test.
6. Remaining limits, migration implications and preserved source attribution.

Avoid claiming that Langfuse lacks versions, labels, rollback, composition, caching, CI regression gates, reviewed-baseline guidance, CLI or MCP support. The [comparison](langfuse-comparison.md) documents that overlap. Cite this toolkit as an implementation/example where useful, rather than requiring maintainers to adopt the whole repository.

## Earn adoption through a complete first task

For this project, prioritize project checks before writes, a policy draft helper and a readable release status. Supported result-projection helpers should precede performance-oriented collector expansion. These changes reduce the amount of integration code a new user must understand before their first useful release check.

Then resume the deferred application pilot when an app and test environment are available. Collect setup failures, report clarity and capture overhead. Use that evidence to choose the next integration and to support any claims about time saved or incidents avoided.

Follow the target repository's current contribution process and the maintainer's chosen scope when submitting a proposal.
