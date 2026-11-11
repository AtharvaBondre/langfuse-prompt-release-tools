#!/usr/bin/env python3
"""Run synthetic SDK compatibility checks on an isolated GitHub Linux runner.

Only summary.json and sanitized.log are public artifacts. Generated credentials,
Compose input and raw experiment evidence stay in the private runner directory.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_local_demo import prepare, write_new

# Official registry manifest lists inspected 2026-11-11. Pin their linux/amd64
# child manifests, not an implicit host-platform choice or a mutable tag.
LANGFUSE_IMAGES = {
    "4.78.0": {
        "langfuse-web": "docker.langfuse.com/langfuse/langfuse@sha256:129cb76d4cdf6b2be2beaffe5c4b3b8e56af91cdb3e95d47cef0ef73583e7e1d",
        "langfuse-worker": "docker.langfuse.com/langfuse/langfuse-worker@sha256:74c8020966a9556b25c21e368b64c9cfe24e1df1c4f76569aee6d230c9443de5",
    },
    "4.59.0": {
        "langfuse-web": "docker.langfuse.com/langfuse/langfuse@sha256:cee4fce72336daca27d224f35bc2431e302b045f5ab7e1b0c6c876a47e340b36",
        "langfuse-worker": "docker.langfuse.com/langfuse/langfuse-worker@sha256:1bb9b0176ad7917202aab4cb6ae5452f783c32bbfbe4bf5489390bbdda546fc1",
    },
}
INFRA_IMAGES = {
    "clickhouse": "clickhouse/clickhouse-server@sha256:8ecb92fa564142a2bad5fef7b39f89af7265ddbfd782693b15f002ca0f70be14",
    "minio": "cgr.dev/chainguard/minio@sha256:b51b70a1be6a3ff9d32b5474b178fddcca84e71a1c5b9028a237f4401943d309",
    "redis": "redis@sha256:cd745595f143052dd6a743bc5651d3ce4b03979fe5c99c7fcfab73461f6f217b",
    "postgres": "postgres@sha256:3cec7eb015ba8adb28139fa5c83b8489cdf0e666e53dfdf20f598ae0cc8739e3",
}
MEMORY_LIMITS = {"langfuse-web": "2048m", "langfuse-worker": "1536m", "clickhouse": "1536m",
                 "minio": "384m", "postgres": "512m", "redis": "256m"}
HOST = "http://127.0.0.1:13830"


def clean_environment():
    # Never inherit credentials, tracing exporters, remote Docker endpoints or
    # proxy settings from a shell. Only runtime/tool location variables survive.
    allowed = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV", "LD_LIBRARY_PATH",
               "DYLD_LIBRARY_PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {**{key: value for key, value in os.environ.items() if key in allowed},
            "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "NO_PROXY": "localhost,127.0.0.1,::1"}


def read_values(private):
    return dict(line.split("=", 1) for line in (private / "runtime.env").read_text().splitlines() if line)


def redact(value, values):
    result = str(value)
    secret_values = set(values.values())
    public, secret = values.get("LANGFUSE_PUBLIC_KEY"), values.get("LANGFUSE_SECRET_KEY")
    if public and secret:
        secret_values.add(base64.b64encode((public + ":" + secret).encode()).decode())
    for secret_value in sorted(secret_values, key=len, reverse=True):
        result = result.replace(secret_value, "[redacted]")
        result = result.replace(urllib.parse.quote(secret_value, safe=""), "[redacted]")
    result = re.sub(r"(?i)(?:postgres(?:ql)?|redis|rediss)://[^\s\"']+", "[redacted-connection]", result)
    result = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:basic|bearer)\s+[^\s\"']+", r"\1[redacted]", result)
    result = re.sub(r"(?:sk|pk)-lf-[A-Za-z0-9-]+", "[redacted-key]", result)
    return result


def compose_command(private):
    metadata = json.loads((private / "demo.json").read_text())
    project = metadata["project_name"]
    if not re.fullmatch(r"langfuse-release-demo-[a-f0-9]{12}", project):
        raise ValueError("Refusing unexpected Compose project identity")
    return ["docker", "--context", "default", "compose", "--project-name", project,
            "--env-file", str(private / "runtime.env"), "--file", str(private / "compose.json")]


def configure(private, version):
    private, metadata = prepare(private)
    config = json.loads((private / "compose.json").read_text())
    images = {**INFRA_IMAGES, **LANGFUSE_IMAGES[version]}
    for name, service in config["services"].items():
        service.update(image=images[name], platform="linux/amd64", mem_limit=MEMORY_LIMITS[name])
    config["services"]["langfuse-web"]["environment"]["NODE_OPTIONS"] = "--max-old-space-size=1536"
    config["services"]["langfuse-worker"]["environment"]["NODE_OPTIONS"] = "--max-old-space-size=1024"
    (private / "compose.json").write_text(json.dumps(config, indent=2) + "\n")
    metadata.update(platform="linux/amd64", docker_context="default", images=images, server_version=version)
    (private / "demo.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return read_values(private), images


def request_json(path, values=None):
    request = urllib.request.Request(HOST + path)
    if values is not None:
        raw = values["LANGFUSE_PUBLIC_KEY"] + ":" + values["LANGFUSE_SECRET_KEY"]
        request.add_header("Authorization", "Basic " + base64.b64encode(raw.encode()).decode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Error bodies are not needed for the route-presence compatibility probe.
        return exc.code, None


def cleanup(private, public):
    if not (private / "demo.json").exists():
        return 0
    values = read_values(private)
    process = subprocess.run(compose_command(private) + ["down", "--volumes", "--remove-orphans", "--timeout", "15"],
                             env=clean_environment(), capture_output=True, text=True, timeout=90)
    if public.is_dir():
        with (public / "sanitized.log").open("a") as stream:
            stream.write("Cleanup return code: " + str(process.returncode) + "\n")
            stream.write(redact(process.stdout + process.stderr, values))
    return process.returncode


def run(version, private, public):
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.environ.get("RUNNER_ARCH") != "X64" or platform.machine() not in ("x86_64", "AMD64")):
        raise ValueError("Live execution requires an isolated GitHub Linux X64 runner")
    public.mkdir(mode=0o700, exist_ok=False)
    values, images = configure(private, version)
    env = clean_environment()
    summary = {"status": "failed", "scope": "Synthetic local SDK/server compatibility; no model or cloud calls",
               "server_version": version, "platform": "linux/amd64", "images": images,
               "memory_limits": MEMORY_LIMITS, "docker_context": "default", "host": HOST,
               "source_commit": os.environ.get("GITHUB_SHA"), "checks": [], "model_calls": 0}
    log = public / "sanitized.log"

    def execute(label, argv, *, timeout, environment=env):
        started = time.monotonic()
        try:
            process = subprocess.run(argv, env=environment, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with log.open("a") as stream:
                stream.write(label + ": timed out; never classified as compatibility success\n")
                for raw in (exc.stdout, exc.stderr):
                    if raw:
                        stream.write(redact(raw.decode(errors="replace") if isinstance(raw, bytes) else raw, values))
            raise RuntimeError(label + " timed out") from None
        with log.open("a") as stream:
            stream.write(f"{label}: returncode={process.returncode}, duration_seconds={time.monotonic()-started:.1f}\n")
            stream.write(redact(process.stdout + process.stderr, values))
        if process.returncode:
            raise RuntimeError(label + " failed; see sanitized.log")
        return process.stdout

    try:
        endpoint = execute("Check Docker endpoint", ["docker", "--context", "default", "context", "inspect", "default",
                           "--format", "{{.Endpoints.docker.Host}}"], timeout=10).strip()
        if endpoint != "unix:///var/run/docker.sock":
            raise ValueError("Refusing a Docker endpoint outside the isolated runner")
        command = compose_command(private)
        execute("Pull pinned AMD64 images", command + ["pull", "--quiet"], timeout=300)
        for name, ref in images.items():
            architecture = execute("Verify image platform " + name,
                ["docker", "--context", "default", "image", "inspect", ref, "--format", "{{.Os}}/{{.Architecture}}"], timeout=10).strip()
            if architecture != "linux/amd64":
                raise ValueError("Pulled image platform mismatch: " + name)
        execute("Start isolated stack", command + ["up", "--detach"], timeout=180)
        deadline = time.monotonic() + 300
        while True:
            try:
                status, body = request_json("/api/public/health")
                project_status, project = request_json("/api/public/projects", values)
                if status == 200 and project_status == 200:
                    if body.get("version") != version:
                        raise ValueError("Health response differs from the pinned server version")
                    if [item.get("id") for item in project.get("data", [])] != [values["LANGFUSE_INIT_PROJECT_ID"]]:
                        raise ValueError("API key is not bound to the newly generated synthetic project")
                    summary["health"] = {"status": status, "version": body["version"], "project_identity_verified": True}
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Server health/project initialization exceeded 300 seconds")
            time.sleep(2)
        probe_paths = ["/api/public/experiments?limit=1&fields=core",
                       "/api/public/experiment-items?experimentId=synthetic-compatibility-probe&limit=1&fields=core,dataset,io",
                       "/api/public/v3/scores?limit=1&fields=subject"]
        probes = []
        for path in probe_paths:
            code, _ = request_json(path, values)
            probes.append({"path": path, "http_status": code})
        summary["api_probes"] = probes
        if any(row["http_status"] != 200 for row in probes):
            if version == "4.59.0" and all(row["http_status"] in (200, 404) for row in probes) and any(row["http_status"] == 404 for row in probes):
                summary.update(status="unsupported_missing_apis", missing_apis=[row["path"] for row in probes if row["http_status"] == 404],
                               support_claim="Rejected: required remote-evidence APIs absent on healthy authenticated server")
                return 0
            raise RuntimeError("Required API compatibility probe failed (not treated as missing-API success)")
        demo_env = {**env, "LANGFUSE_PUBLIC_KEY": values["LANGFUSE_PUBLIC_KEY"],
                    "LANGFUSE_SECRET_KEY": values["LANGFUSE_SECRET_KEY"], "LANGFUSE_BASE_URL": HOST,
                    "LANGFUSE_HOST": HOST, "LANGFUSE_TRACING_ENABLED": "true"}
        demos = [("verified_release_demo", ["--project-id", values["LANGFUSE_INIT_PROJECT_ID"]])]
        if version == "4.78.0":
            demos.append(("sdk_release_demo", []))
        for name, extra in demos:
            destination = private / name
            execute(name, [sys.executable, str(ROOT / "examples" / (name + ".py")), "--host", HOST,
                          "--out-dir", str(destination), "--disposable-local", *extra], timeout=360, environment=demo_env)
            result = json.loads((destination / "summary.json").read_text())
            if result.get("status") != "passed" or result.get("model_calls") != 0:
                raise RuntimeError(name + " did not report a successful synthetic-only run")
            summary["checks"].extend({"demo": name, "check": row} for row in result["checks"])
        summary.update(status="passed", support_claim="Full requested synthetic SDK/server workflow passed")
        return 0
    except Exception as exc:
        summary["error"] = redact(str(exc), values)
        return 1
    finally:
        try:
            execute("Final service state", compose_command(private) + ["ps", "--format", "json"], timeout=20)
            execute("Final service logs", compose_command(private) + ["logs", "--no-color", "--tail", "100"], timeout=30)
        except Exception as exc:
            summary["diagnostic_error"] = redact(str(exc), values)
        try:
            code = cleanup(private, public)
            summary["cleanup_status"] = "complete" if code == 0 else "failed"
        except Exception as exc:
            summary["cleanup_status"] = "failed"
            summary["cleanup_error"] = redact(str(exc), values)
        summary["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_new(public / "summary.json", json.dumps(summary, indent=2) + "\n")
        print(json.dumps({"status": summary["status"], "server_version": version,
                          "checks_passed": len(summary["checks"]), "cleanup_status": summary["cleanup_status"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", choices=sorted(LANGFUSE_IMAGES))
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--public-dir", required=True, type=Path)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.cleanup:
        return cleanup(args.private_dir.absolute(), args.public_dir.absolute())
    if not args.version:
        parser.error("--version is required for execution")
    return run(args.version, args.private_dir.absolute(), args.public_dir.absolute())


if __name__ == "__main__":
    raise SystemExit(main())
