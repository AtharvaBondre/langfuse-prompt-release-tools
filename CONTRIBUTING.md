# Contributing

Start with a reproducible release problem: the target Langfuse version, a synthetic prompt, expected label/version state, and observed behavior. Do not attach project keys, private prompts or raw tenant receipts.

For a first attempt, use the [demo or setup feedback form](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/issues/new?template=demo-feedback.yml). Name the toolkit version, exact step, expected result and redacted output. An unclear instruction is useful feedback even when the command succeeds. Distinguish trying the demo from integrating it into an application.

Keep the core runtime dependency-free. Use official Langfuse SDKs or tools for optional integrations rather than rebuilding their APIs. The [roadmap](docs/roadmap.md) lists prioritized work and acceptance criteria.

Use the [customer workflows](docs/customer-workflows.md) to connect a proposal to a release decision. The [community contribution guide](docs/community-contributions.md) describes small pieces that could help upstream Langfuse, including reproduction requirements and the distinction between an observed integration issue and an upstream defect.

## Local checks

```bash
python3 -m unittest discover -v -p 'test_*.py'
```

Tests must exercise observable outcomes: label state, writes made or avoided, mismatched evidence, or recovery after uncertainty. Network requests are mocked. Keep fixtures synthetic.

Follow [PACKAGING.md](PACKAGING.md) to build and test an installed wheel. Preserve existing public source attribution and the Compose notice. Update CLI examples and migration notes when defaults or receipt formats change.

## Live validation

Live tests use a fresh disposable project with explicitly selected credentials and scope. Record the Langfuse version/image digest, runtime, exact assertions and any model calls. Never convert an offline or synthetic pass into a claim about production adoption or answer quality. The provided [local demo](LOCAL-DEMO.md) is optional and uses a separately selected local Docker context.

## Pull requests

Explain the failure or user need, the changed behavior and the checks run. Include limitations, especially for concurrent writes, dependency resolution, client caches and partial batch completion. A broad API wrapper or new MCP server needs a concrete unmet use case before implementation.
