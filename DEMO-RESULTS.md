# Disposable integration results

Test completed 2026-10-17T06:14:29.039198+00:00 using a fresh local Langfuse project and synthetic prompts.

I repeated the same 15 assertions in a second disposable project using `prepare_local_demo.py` and the supplied Compose template. Both runs passed. During setup, I fixed a readiness race by making PostgreSQL's health check use TCP; its temporary initialization socket had reported ready too early. Docker also stopped during the reproduction and needed a restart. These are two runs of the same checks, not 30 distinct tests.

## Results

| Check | Observed result |
|---|---|
| Failed application output | The empty-answer fixture failed its required check; promotion stopped before PATCH. The production label stayed on version 1. |
| Stage text and chat | Both created version 2. Production stayed on version 1 and latest advanced to version 2. |
| Lost promotion response | The test client raised an exception after the server completed the first PATCH. Promotion stopped with an unknown outcome. |
| Reconcile | The next run recognized the first completed promotion and patched the remaining prompt. Both exact-version and label reads matched. |
| Repeat completed promotion | Both items reported already_promoted; no PATCH occurred. |

The runner passed **15 assertions** using **six POSTs and two PATCHes**. It also exercised paginated listing and exact-version/label GETs. The tests called no model. The application-output fixture and response-loss injection are deliberate test inputs; these results do not demonstrate model quality or an observed Langfuse server bug.

## Run it

Follow `LOCAL-DEMO.md` to prepare a fresh local instance, then run:

```bash
python3 integration_langfuse_release.py --host http://127.0.0.1:13830 \
  --out-dir ./integration-results --confirm-disposable-project
```

The runner creates three uniquely named prompts and leaves them for inspection. It requires a loopback URL, environment credentials and an empty output directory. Results include per-check evidence and operation counts without credentials. The output directory contains prompt snapshots and local paths; keep it private.

## Tested images

These are the image identities from the ARM64 Docker run. The source review used Langfuse commit `e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3`; the runtime evidence identifies pulled images independently. Other deployment versions and CPU architectures remain untested.

- `clickhouse/clickhouse-server@sha256:8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6` (arm64)
- `docker.langfuse.com/langfuse/langfuse@sha256:67f48d12a190e4ad585249c75ae1ce172b449de3dbbe7486b8179cd28e21408e` (arm64)
- `docker.langfuse.com/langfuse/langfuse-worker@sha256:ca32a8fb2a214c7b5f42dd9c540c8072e41db659cb621f317911ce695ad6bca2` (arm64)
- `cgr.dev/chainguard/minio@sha256:c93745c56fccdaf0e5dcb88999a96a65a136428beb045ef5d546255a80d747a7` (arm64)
- `postgres@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3` (arm64)
- `redis@sha256:4fa24486b8bcca8eec45ee0eb166edc674795e53a2b53d1a9ef263eecebaac85` (arm64)

## Other validation

- 97 offline tests cover the release helpers and evaluation gate.
- A clean wheel installation exposes five CLI commands without runtime dependencies.
- 18 expectations check synthetic requests/responses against the pinned OpenAPI schema, including malformed controls.

The evaluation gate validates trusted application evidence; hashes do not authenticate its author. The release API offers no transaction across prompts or compare-and-swap guarantee. Staging still advances latest.
