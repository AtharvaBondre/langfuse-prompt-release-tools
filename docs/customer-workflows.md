# Put the release checks to work

Start with a decision your team needs to make. The toolkit helps you connect that decision to the prompt, tests and app observations behind it.

| Your question | Start here | Evidence you get |
|---|---|---|
| Can we promote this prompt change? | Required checks, task capture and a dependency lock. | The reviewed requirements, fetched versions and evaluation outcome. |
| Did our app receive the release? | SDK observations and a rollout window. | Versions and fallback behavior for the consumers and requests you instrumented. |
| Can someone else review or recover this release? | Portable evidence, signer verification and reconciliation. | Original test artifacts, CI identity checks and the recorded write attempt. |

The application situations below are examples of where to integrate the toolkit. The linked validation comes from synthetic SDK/server runs, not customer deployments.

## Release a shared prompt used by a support assistant

Your assistant has an answer prompt that includes a shared policy prompt. You change the answer format and evaluate the candidate. Another contributor then edits the shared policy before you release.

With Langfuse, you can version both prompts and inspect their labels. Add this toolkit to keep the evaluated dependency graph with your release and check it before promotion.

1. Export the answer prompt from its deployment label. Review the edit and choose required cases, such as preserving an account identifier or returning the expected response fields.
2. Pin the test policy, stage the candidate and record its dependency lock.
3. Run your actual task through the official SDK. Capture the returned prompt before compiling it and retain the real evaluator outputs.
4. Verify required remote cases/scores if the policy requires persistence. Import the results.
5. Preview promotion. If the shared prompt changed, review that change and rerun the task against a fresh lock before continuing.

**The useful outcome:** the reviewer can trace the candidate to the dependency graph and results that support it. They have a specific mismatch to investigate when the check fails.

**Demonstrated:** [`verified_release_demo.py`](../examples/verified_release_demo.py) changes a nested prompt label after a passing JavaScript experiment. The test checks that promotion refuses the earlier evidence and leaves the deployment label unchanged. It then runs the task against a fresh lock and verifies the new results. See [the recorded live matrix](validation.md).

**You supply:** the real support task, dataset and judgments about answer quality. The current demo uses two deterministic JSON cases to test release behavior. Passing those cases says nothing about the quality of a support model.

## Check a rollout across services and caches

Your API and worker both fetch a prompt by the `production` label. You move that label after evaluation, but a worker can keep receiving an older version from its SDK cache.

Capture the SDK prompt object at the app access point. You can compare its returned version, resolved content and fallback state with the release. For several consumers, declare the consumer/deployment identities and observation window before collecting the samples.

| Observation | Decision it supports |
|---|---|
| A cached access returns the previous version. | Inspect that consumer's cache behavior before claiming it received the release. |
| A fresh access matches the released prompt and dependencies. | Record that access as matching evidence. |
| A required consumer has no samples. | Keep the rollout incomplete and investigate the missing coverage. |
| A prompt fetch uses a fallback. | Inspect the missing-prompt or failure path; keep fallback separate from normal adoption. |
| Someone submits the same request observation twice. | Reject the replay as additional coverage. |

**The useful outcome:** you can distinguish a successful label move from what your instrumented consumers received. The report includes the versions and coverage behind that conclusion.

**Demonstrated:** the JavaScript cache probe returns the old version from a warmed cache and the released version when bypassing that cache. The same live demo captures four accesses across Python and JavaScript consumers in two time buckets and rejects replayed coverage. Inspect `cache-adoption.json`, `rollout-report.json` and `replayed-rollout.json` in a local demo's JavaScript output directory. Public CI retains sanitized summaries and logs.

**You supply:** actual capture points, consumer identities and thresholds appropriate to your deployment. The demo's eight-second window is a test setting. We have deferred an existing-application pilot and capture-overhead measurements until an app/test environment is available.

## Hand off a release to another reviewer or CI job

The person who ran the experiment may not be the person who deploys the prompt. A copied evaluation receipt can refer to local files the next person does not have. A passing status alone also leaves the producer's identity open.

Create a bundle containing the original snapshot, stage, policy, dependency and experiment evidence. The next job can inventory and verify it without the first machine's paths. For CI handoffs, select the expected repository, workflow, source revision and signer policy outside the bundle.

**The useful outcome:** a reviewer can inspect the original test evidence and check the expected build identity before deciding to use it. Keep promotion and adoption reports alongside the bundle as the release continues.

**Demonstrated:** the [signed evidence workflow](https://github.com/AtharvaBondre/langfuse-prompt-release-tools/actions/workflows/evidence.yml) builds and signs synthetic artifacts on Linux. A separate macOS job verifies them after the original paths are gone and rejects wrong identity, tampered bytes and release replay. These signatures authenticate the synthetic CI artifacts; they do not establish customer execution.

**You supply:** CI permissions, trusted identity expectations and a policy for the deployment decision. Version 0.6 verifies signatures as a separate step. Requiring an accepted signer inside the promotion command is a [planned improvement](product-improvements.md#4-require-a-trusted-signer-during-promotion-when-policy-asks-for-it).

## Inspect a release after a lost response

A deployment command can lose its connection after the server changes a label. Rerunning a write without checking can make the release harder to explain.

Use [reconciliation](reconciliation.md) to compare the saved attempt with current version and label state. The read-only command classifies the observed outcome. An uncertain version-creation request can remain unknown even if you find matching content; a matching version does not prove which request created it.

**Demonstrated:** the [Python baseline](sdk-release-example.md#older-05-python-baseline) injects a client exception after a real successful label change, then reconciles by GET and avoids repeating the PATCH. This tests recovery from a lost response, not a discovered Langfuse outage.

## Start with one task

Run the [guided example](getting-started.md#1-run-the-working-example), then adapt one prompt/task using [the application integration steps](getting-started.md#3-run-a-release-with-your-own-app). Keep the resulting reports and note the first step that required help.

Useful feedback includes a redacted failing report, the expected outcome and the server/SDK versions. That gives contributors a problem they can reproduce. It also tells us whether the next improvement should simplify setup, strengthen a check or improve how the result reads. See [the product plan](product-improvements.md) for those priorities.
