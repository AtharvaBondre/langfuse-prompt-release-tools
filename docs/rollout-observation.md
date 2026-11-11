# Observe rollout samples over a reviewed window

A single application access proves only that access. The rollout observer checks
repeated SDK accesses over a window declared before collection. It requires each
reviewed consumer/deployment to supply enough distinct requests across enough
time buckets for every promoted prompt. It reports the supplied samples; it does
not infer coverage of all traffic, all processes, regions or customers.

The core performs no Langfuse API reads or writes. Optional provenance checking
invokes the separate official CI-attestation verifier. Recovery output is a
read-only proposal, never an automatic label change or retry.

## Review the policy separately from results

Store the expected policy outside the observation artifact and review it before
pinning the window. Do not let the observed application choose its own thresholds,
deployment identity or trusted CI issuer. A five-minute policy can look like:

```json
{
  "format_version": 1,
  "id": "support-rollout/v1",
  "required_consumers": {
    "support-api": {
      "deployment_id": "support-api@<commit>",
      "sdk": {"name": "langfuse-python", "version": "5.2.0"}
    },
    "support-worker": {
      "deployment_id": "support-worker@<commit>",
      "sdk": {"name": "langfuse-js", "version": "6.3.0"}
    }
  },
  "window_seconds": 300,
  "bucket_seconds": 60,
  "min_unique_requests_per_target": 10,
  "min_requests_per_bucket": 1,
  "min_covered_buckets": 5,
  "max_observation_age_seconds": 600,
  "max_latest_observation_age_seconds": 60,
  "max_capture_delay_seconds": 10,
  "max_fallback_rate": 0,
  "max_mismatch_rate": 0,
  "provenance": {"required": false, "trust_policy_sha256": null}
}
```

Those numbers are illustrative, not recommended production thresholds. Start
with observed application traffic and choose coverage that is operationally
meaningful. Exact SDK/deployment strings must match the actual application.
Both minimum request count and time-bucket coverage apply independently to each
`(consumer, promoted prompt)` pair. Coverage counts only fresh, non-fallback,
correct-version/content/identity samples.

Any non-fallback version, resolved-content, prompt-type, deployment, SDK or label
mismatch blocks completion. `max_mismatch_rate` must be zero; mixed old/new
versions cannot become a complete rollout by choosing a more permissive number.
Fallbacks have their own rate limit and never satisfy clean sample counts. A
recent fallback cannot make an old clean access appear fresh.

## Pin before collecting

The promotion must be applied and verified with a timestamp and the exact
pre-promotion dependency-lock hash. Unknown or incomplete promotions are rejected.

```sh
langfuse-release-rollout \
  --policy rollout-policy.json --promotion reports/promote.json \
  --dependency-lock reports/dependencies.json \
  --window rollout-window.json --pin-window --window-id support-rollout-unique-id
```

The equivalent Python function is:

```python
window = pin_rollout_window(
    policy, promotion, dependency_lock,
    window_id="support-rollout-unique-id",
)
```

The pin includes the complete policy digest, release/lock digests, target versions
and resolved hashes, creation timestamp and fixed start/end times. Start defaults
to now; `start_at_utc`/`--start-at` can set a future aware-UTC timestamp. Backdating
is rejected. Preserve the window file. Extending or weakening the window requires
a newly reviewed policy/window and fresh accesses, not editing prior evidence.

## Bind each actual SDK access promptly

Use the actual SDK capture helper at the consuming application's prompt-access
boundary, then bind its normalized observation immediately in the collector:

```python
sample = bind_rollout_observation(observation, window)
```

`observation` uses the existing adoption v1 contract: consumer/deployment/request
identity, exact SDK version, requested label, request-start/observation timestamps,
cache settings, actual fallback state and returned name/version/type/resolved
hash. Explicitly supported method pairs are:

| SDK name | Capture method |
|---|---|
| `langfuse-python` | `langfuse_release_adoption.capture_sdk_observation/v1` |
| `langfuse-js` | `langfuse_release_adoption.capture_js_sdk_observation/v1` |

The binding adds a collector timestamp and the exact window/policy/release/lock
hashes. Record real request IDs; do not generate a new ID for a duplicate delivery.
Events are unique by `(consumer_id, prompt_name, request_id)`. Repeated IDs fail
the window and do not increase counts, even if their timestamps or payloads differ.
The same request may legitimately access two different prompt names.

Windows are half-open `[start, end)`. Both request start and observation must be
inside; the sample must reach the collector within `max_capture_delay_seconds`.
Future, reversed, late and stale timestamps are failures, not silently discarded.
This catches accidental replay and delayed batches. It does not cryptographically
prove application clocks or prevent a malicious producer from inventing new IDs.
A trusted collector should own the window assignment and request-ID source.

Persist the exact bound samples in this portable artifact shape:

```json
{
  "format_version": 1,
  "window_sha256": "<canonical fingerprint of rollout-window.json>",
  "observations": ["<bound sample objects>"]
}
```

## Report coverage and freshness

```python
report = write_rollout_report(
    "rollout-report.json", samples, window, policy, promotion, dependency_lock,
)
check_rollout_report(report, window, policy, promotion, dependency_lock)
```

Or use the CLI:

```sh
langfuse-release-rollout \
  --policy rollout-policy.json --promotion reports/promote.json \
  --dependency-lock reports/dependencies.json --window rollout-window.json \
  --observations rollout-observations.json --report rollout-report.json
```

Use `--check rollout-report.json` to revalidate an existing report. New reports
are exclusive files; earlier evidence is never overwritten. Outcomes are:

- `collecting`: the window is still open and no hard failure has been seen.
  Missing counts/coverage are listed as pending requirements. Early good samples
  cannot close the window.
- `failed`: a hard failure occurred, or a closed window lacks required coverage
  or freshness. The artifact retains counts and individual failure reasons.
- `complete`: the window has ended and all reviewed requirements are satisfied.

Only `complete` exits successfully. The checker recomputes results and rechecks
freshness at the current time. An old complete report does not remain current
forever. A later evidence file cannot substitute another policy, release, window
or sample body without failing the bindings.

Per-consumer/prompt output shows distinct request and clean-request counts,
version counts, fallback/mismatch rates, possible SDK-cache observations, bucket
counts/coverage, and latest observed/latest clean timestamps. Rates have a null
denominator when no observations exist. Invalid or unexpected samples remain
failures; they are never omitted to improve the reported rate. Cache metadata
says `sdk_cache_possible`, not a measured hit/miss classification.

## Optional authenticated provenance

Without verification the report says `unsigned_local`. To require authentication,
set `policy.provenance.required=true` and pin the canonical JSON fingerprint of
an independently stored provenance trust policy in `trust_policy_sha256`.

Pass a provenance **verification request**, not a claimed verdict:

```json
{
  "artifact_path": "rollout-observations.json",
  "trust_policy_path": "trusted-ci-policy.json",
  "attestation_path": "observations-attestation.jsonl"
}
```

Supply it through `--provenance-request` or `provenance=...` in the Python API.
The observer invokes `langfuse_release_provenance.verify_provenance` itself,
checks the artifact's exact observations/window, verifies the externally pinned
trust-policy digest, and binds the verifier's artifact-byte digest. Changes
during verification fail. `{ "verified": true }` or a copied verifier result
cannot authenticate anything. Required authentication cannot pass on an unsigned
verifier outcome. Rechecking authenticated reports reruns verification.

CI authentication identifies the artifact producer under the trusted workflow
policy. It still does not prove every application request was captured or that
a customer fleet adopted a release.

## Recovery proposals and validation limits

A failed mixed-version/content window suggests reviewing affected consumers and
current label/version state. When a promotion records a previous version, the
report can list it as a **candidate** for a separately reviewed rollback. It
explicitly marks current remote state unverified. Missing/stale/fallback evidence
suggests investigating the collector/application and collecting a new window
after correction. No path executes rollback, remote writes or an unknown POST.

Offline tests use synthetic normalized Python/JS captures and a mocked provenance
verifier; they are not SDK execution or cryptographic integration tests. They
cover passing/collecting/failed windows, request and bucket minima, multiple
consumers, mixed versions, caches/fallbacks, stale/latest timing, duplicate/replayed
samples, policy/window/release substitutions, tampering and read-only proposals.
The root application's live pilot and actual provenance checks are separate
validation levels; a local synthetic application is not a customer rollout.
