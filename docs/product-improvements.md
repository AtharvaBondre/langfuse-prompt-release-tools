# Product improvements after the 0.6 review

Reviewed **11 November 2026** against the code, working examples, package checks and current official Langfuse sources.

## Assessment

The project covers more than the old README made clear. You can pin release requirements, capture the exact prompts used by a task, check nested dependencies, verify remote results, transfer evidence, inspect interrupted writes and observe application adoption. Python and JavaScript examples exercise those steps against real Langfuse servers.

The first-use experience still asks too much of a new user. You assemble a policy, insert capture hooks, project SDK results into a common format and connect several commands. You then read several JSON reports to understand one release. Better documentation helps you do that work, but it does not remove it.

The strongest direction is to make this release workflow easier to adopt and harder to misconfigure. Langfuse already supplies experiments, prompt composition, CI guidance and general agent tools. We can add more value by improving the connections between those tools than by duplicating them.

The [customer workflows](customer-workflows.md) connect these capabilities to concrete release decisions. The [community contribution guide](community-contributions.md) identifies pieces that Langfuse maintainers could reuse. Community adoption and upstream inclusion are outcomes to earn through reproducible utility and feedback, not current project achievements.

## Documentation improvements in this update

| Previous problem | Change |
|---|---|
| The README led with implementation terms and restrictions. | Start with concrete release problems, user outcomes and a workflow visual. |
| The comparison understated official Langfuse's CI capabilities. | Name the native overlap and describe the connected implementation this project provides. |
| New users had to discover the current example. | Lead with the complete 0.6 demo, expected outcomes and a GitHub Actions route without existing credentials. |
| Installation left the environment activation implicit. | Include activation before the CLI commands. |
| Manual policy and capture work was easy to miss. | Explain the inputs, code insertion points and original result files in a guided release. |
| Report names and paths were inconsistent across examples. | Show a file map, filename-versus-path behavior and distinct preview/apply outputs. |
| Older guides described shipped features as future work. | Refresh the official-tools and experiment guides and label legacy behavior. |

## Next implementation priorities

These items are **planned**, not features available in 0.6. Acceptance criteria describe the evidence needed before marking them complete.

### 1. Bind the selected project before writes

**User benefit:** Catch a changed project key set before staging or promoting in the wrong project on the same Langfuse host.

The snapshot currently binds the host and prompt baseline. The remote verifier also checks project identity, but the earlier mutation path does not require that binding. Two projects with matching prompt baselines could evade a host-only distinction if the operator changes credentials. This is a code-review finding, not a report of a wrong-project incident.

Record the independently selected project ID in the export and check the project associated with the current key before mutations. Define migration behavior for older snapshots.

**Acceptance:** a wrong project produces zero prompt/label writes; the expected project comes from reviewed configuration; preview and apply enforce the same identity; errors explain which configuration to check without exposing keys.

### 2. Generate and validate a policy draft

**User benefit:** Start from real cases and source identities without writing hashes and JSON structure by hand.

Add a helper that reads selected cases, computes fingerprints, records task/evaluator/model configuration and produces a reviewable policy draft. Ask users to choose score names, types and thresholds; do not derive an easier passing policy from candidate results. Offer validation before snapshot pinning.

**Acceptance:** a new user can produce a valid policy from the supported example; missing/duplicate cases and malformed check types get field-specific errors; generation performs no remote writes; the user reviews the draft before pinning.

### 3. Explain release status in one place

**User benefit:** Answer “Did testing pass, did the label move, and did my app receive it?” without opening several files.

Add a read-only `status`/`inspect` command with a short terminal view and stable JSON output. Show the selected project/label, stage, evaluation, remote receipt age, dependency state, promotion and adoption coverage. Link each conclusion to its original report and give the next safe action.

**Acceptance:** stale or absent evidence stays visible; a changed label cannot appear as confirmed application adoption; inspection sends no writes; readers can distinguish historical evidence from any fresh server check. Print explicit output paths across the CLI.

### 4. Require a trusted signer during promotion when policy asks for it

**User benefit:** Carry a reviewed CI trust decision into the command that moves the label.

Bundle/signature verification works today as a separate step. Connect it to a signer-required promotion policy and a validated release workspace. Select the expected signer, release and destination outside the producer's bundle.

**Acceptance:** unsigned, wrong-signer or replayed evidence cannot satisfy a signer-required release; fresh dependency/remote checks still apply; preview and apply use the same rule; preserve an explicit unsigned-local workflow.

### 5. Improve concurrent and interrupted operation handling

**User benefit:** Keep enough evidence to decide what happened after a crash and prevent two local writers from corrupting the same release folder.

Add a folder operation lock, durable journal flushes and process-death tests. Generate resume proposals from reconciliation with a fresh preflight before any new mutation.

**Acceptance:** concurrent local writers cannot corrupt evidence; crash tests retain the attempt state; uncertain creation does not trigger a blind retry. A local lock does not claim distributed exclusion across independent release folders.

### 6. Reduce application integration work

**User benefit:** Add capture to a task with less format-conversion code and predictable request overhead.

Promote the Python/JavaScript result projections from examples into supported helpers. Add one small application recipe with real inputs, evaluators and capture points. Prototype a bounded asynchronous collector for JavaScript, which currently starts a Python sidecar for each capture.

**Acceptance:** helpers preserve the actual case IDs, trace IDs, typed scores and execution identity; packaged examples use the public APIs; queue limits, dropped samples and shutdown outcomes remain visible. Measure latency before making request-path performance claims. Framework-specific packages should follow a demonstrated integration need.

### 7. Expand live coverage where users will depend on it

**User benefit:** Know which dataset and version combinations have been exercised against actual servers.

Add a disposable hosted-dataset scenario with dataset/schema changes, run-level scores and pagination. Extend compatibility one recorded SDK/server pair at a time. Keep fixtures from official contracts and publish exact versions with outcomes.

**Acceptance:** wrong dataset/run identities and missing exports cannot pass; the release records all selected cases and required scores; a skipped endpoint remains a compatibility gap. Do not infer support for untested versions between matrix entries.

## Later, once an application is available

The maintainer deferred the existing-application rollout pilot on 11 November 2026. Resume it when a repository and test environment exist. Measure capture overhead, choose consumer coverage from actual deployment behavior and collect first-use feedback.

Use that evidence to decide whether users need a compact HTML report, a framework adapter or a release-specific MCP wrapper. Those are candidate directions, not committed capabilities. The official general CLI/MCP, compiler and evaluator engine remain the right starting points for their existing jobs.

## Signals that the improvements helped

| Question | Measure |
|---|---|
| Can a new user complete the example? | Successful first run and the exact step where help was needed. |
| Can they integrate one real task? | Setup steps, manual edits and errors before the first valid capture/import. |
| Can a reviewer explain a release? | Whether they can identify tested version, promotion result and adoption coverage from the reports. |
| Did the checks catch meaningful drift? | Reproductions of changed dependencies, missing scores, wrong project selection or stale observations. |
| Is capture suitable for the app? | Measured latency, queue behavior and sampling/drop coverage in the deferred pilot. |

No adoption or performance baseline exists yet. Collect those measurements before claiming time savings or production reliability. The [implementation roadmap](roadmap.md) tracks the next work; the [Langfuse comparison](langfuse-comparison.md) explains the product's scope.
