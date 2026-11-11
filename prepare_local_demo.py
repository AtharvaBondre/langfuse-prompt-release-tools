#!/usr/bin/env python3
"""Write a fresh disposable demo configuration. Does not run Docker or make requests."""
import argparse
import json
import os
from pathlib import Path
import secrets
import uuid


CLICKHOUSE_SETTINGS = """<clickhouse>
  <background_schedule_pool_size>16</background_schedule_pool_size>
  <background_buffer_flush_schedule_pool_size>4</background_buffer_flush_schedule_pool_size>
  <background_message_broker_schedule_pool_size>4</background_message_broker_schedule_pool_size>
  <background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size>
  <max_server_memory_usage_to_ram_ratio>0.8</max_server_memory_usage_to_ram_ratio>
</clickhouse>
"""


def write_new(path, content, mode=0o600):
    """Exclusive creation; never truncate a file from an earlier run."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(out_dir):
    target = Path(out_dir).expanduser()
    if target.is_symlink() or target.exists():
        raise ValueError("Choose a new output directory; existing paths are not replaced.")
    target = target.absolute()
    if not target.parent.is_dir():
        raise ValueError("The parent directory must already exist.")
    template_path = Path(__file__).resolve().with_name("compose.demo.json")
    template = json.loads(template_path.read_text(encoding="utf-8"))
    suffix = uuid.uuid4().hex
    public_key, secret_key = "pk-lf-" + str(uuid.uuid4()), "sk-lf-" + secrets.token_hex(32)
    values = {key: secrets.token_hex(32) for key in (
        "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH", "MINIO_ROOT_PASSWORD",
        "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET", "LANGFUSE_INIT_USER_PASSWORD",
    )}
    values.update({
        "MINIO_ROOT_USER": "demo" + suffix[:16],
        "LANGFUSE_INIT_ORG_ID": "demo-org-" + suffix,
        "LANGFUSE_INIT_PROJECT_ID": "demo-project-" + suffix,
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": public_key,
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": secret_key,
        "LANGFUSE_INIT_USER_EMAIL": "local-demo-" + suffix + "@example.invalid",
        "LANGFUSE_PUBLIC_KEY": public_key,
        "LANGFUSE_SECRET_KEY": secret_key,
    })
    # Values are generated locally and contain no shell metacharacters or whitespace.
    environment = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    target.mkdir(mode=0o700)
    write_new(target / "runtime.env", environment)
    write_new(target / "compose.json", json.dumps(template, indent=2) + "\n")
    # Contains no secrets; the unprivileged ClickHouse container must be able to read it.
    write_new(target / "clickhouse-demo.xml", CLICKHOUSE_SETTINGS, mode=0o644)
    metadata = {
        "format_version": 1,
        "project_name": "langfuse-release-demo-" + suffix[:12],
        "host": "http://127.0.0.1:13830", "platform": "linux/arm64",
        "docker_context": None,
        "scope": "Fresh disposable local Langfuse; no company or cloud credentials.",
        "images": {name: row["image"] for name, row in template["services"].items()},
    }
    write_new(target / "demo.json", json.dumps(metadata, indent=2) + "\n")
    write_new(target / ".gitignore", "*\n")
    return target, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, help="New directory under an existing parent.")
    args = parser.parse_args()
    try:
        target, metadata = prepare(args.out_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Preparation stopped: {exc}\n")
    print(f"Created: {target}")
    print(f"Compose project: {metadata['project_name']}")
    print("Select an explicit local Docker context before starting; see LOCAL-DEMO.md.")
    print("No containers were started. Credentials were written only to runtime.env (0600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
