# Disposable local integration

This demo runs a fresh Langfuse instance and synthetic prompt checks on your computer. It uses no model provider and needs no existing Langfuse account. Keep it separate from any company or personal Langfuse deployment.

The Compose template derives from the [official configuration at `e3a2bee2`](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/docker-compose.yml). `compose.demo.json` pins Langfuse web/worker 4.78.0 plus the four infrastructure image digests. They are **Linux arm64** images; other architectures and emulation have not been verified. The memory limits total approximately 3.5 GiB, excluding Docker overhead. Allow time for downloads and first-start migrations.

Only the web service publishes a port: `127.0.0.1:13830`. PostgreSQL, Redis, ClickHouse and MinIO remain on the Compose network. Telemetry and the in-app agent are disabled. This small setup covers prompt release behavior; media uploads and general production operations are outside its scope.

## Prepare files

Keep `prepare_local_demo.py` beside `compose.demo.json`, then run:

```bash
python3 prepare_local_demo.py --out-dir ./local-langfuse-demo
```

Preparation writes files only. It refuses an existing output path and generates fresh IDs, passwords and API keys. `runtime.env` is mode `0600`, inside a mode `0700` directory. Never commit it or paste its contents into a report. The project uses fresh named volumes; do not attach another deployment's volumes or replace the generated keys with existing credentials.

## Start explicitly

Start Docker yourself. Set `DEMO_CONTEXT` to a context you have verified points to your **local** engine, and retain the generated project name. These commands remove inherited cloud, proxy and Compose environment variables. They retain `HOME` and `PATH` for Docker's local configuration and executable.

```bash
DEMO_CONTEXT=desktop-linux  # replace with your verified local context
DEMO_DIR="$(pwd)/local-langfuse-demo"
DEMO_PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_name"])' "$DEMO_DIR/demo.json")"

env -i HOME="$HOME" PATH="$PATH" docker --context "$DEMO_CONTEXT" context inspect "$DEMO_CONTEXT"
env -i HOME="$HOME" PATH="$PATH" docker --context "$DEMO_CONTEXT" compose \
  --project-name "$DEMO_PROJECT" --env-file "$DEMO_DIR/runtime.env" \
  -f "$DEMO_DIR/compose.json" up -d
```

Inspect the context endpoint before running `up`; use a local Unix socket or local named pipe, never a remote daemon. If port 13830 is already occupied, stop and investigate rather than attaching to whichever service is there. Do not print `docker compose config`: its expanded environment contains generated secrets.

## Check readiness and run

Migrations and project initialization can take several minutes. This read-only check lists prompt names using only the generated local credentials. Run it from the source directory; retry manually if initialization is still in progress.

```bash
env -i HOME="$HOME" PATH="$PATH" sh -c '
  set -a
  . "$1/runtime.env"
  set +a
  python3 -c '\''import os; from langfuse_release_common import Client, ensure_project; client = Client("http://127.0.0.1:13830", project_id=os.environ["LANGFUSE_INIT_PROJECT_ID"]); ensure_project(client); print(client.list_names("production"))'\''
' sh "$DEMO_DIR"
```

Then run the write test against this disposable project:

```bash
env -i HOME="$HOME" PATH="$PATH" sh -c '
  set -a
  . "$1/runtime.env"
  set +a
  python3 integration_langfuse_release.py \
    --host http://127.0.0.1:13830 --project-id "$LANGFUSE_INIT_PROJECT_ID" \
    --out-dir "$1/integration-output" \
    --confirm-disposable-project
' sh "$DEMO_DIR"
```

The test creates synthetic text/chat prompts, stages versions, validates deterministic caller checks, blocks a failing evaluation, promotes passing candidates and verifies label targets. Its code explicitly selects unsigned local compatibility for this disposable test; production promotion defaults to the [signed evidence gate](docs/trusted-promotion.md). It also injects a client exception **after a successful real PATCH** to test reconciliation; this is not an observed Langfuse server failure. No LLM is called. A passing result establishes this tested workflow on the recorded local images, not model quality or universal version compatibility.

## Stop or remove manually

`stop` retains the local data for inspection:

```bash
env -i HOME="$HOME" PATH="$PATH" docker --context "$DEMO_CONTEXT" compose \
  --project-name "$DEMO_PROJECT" --env-file "$DEMO_DIR/runtime.env" \
  -f "$DEMO_DIR/compose.json" stop
```

To discard this demo's containers and named volumes, use the same command with `down --volumes` instead of `stop`, after checking the generated project name. That deletes the demo data. The preparer and integration test never start Docker, select a context or perform cleanup automatically.
