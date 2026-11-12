#!/usr/bin/env python3
"""Run portable tests and CLI help outside the checkout against an installed wheel."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("test_prompt_release", "test_staged_release", "test_evaluation_gate",
           "test_experiment_policy", "test_reconciliation", "test_release_dependencies",
           "test_release_adoption", "test_release_integration", "test_release_bundle", "test_release_provenance",
           "test_release_remote", "test_release_rollout", "test_release_capture", "test_remote_policy_integration", "test_sdk_result_fixtures",
           "test_policy_draft", "test_release_status")
COMMANDS = ("pull", "publish", "verify", "evaluate", "promote", "policy",
            "import-experiment", "reconcile", "dependencies", "adoption", "bundle", "provenance", "remote", "rollout", "capture",
            "policy-draft", "status")


def main():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    for key in ("RELEASE_PYTHON_PATH", "RELEASE_CAPTURE_MODULE", "RELEASE_PYTHON"):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("LANGFUSE_"):
            env.pop(key)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="langfuse-installed-test-") as directory:
        destination = Path(directory)
        for module in MODULES:
            shutil.copyfile(ROOT / (module + ".py"), destination / (module + ".py"))
        shutil.copytree(ROOT / "tests/fixtures", destination / "tests/fixtures")
        (destination / "examples").mkdir()
        for name in ("__init__.py", "sdk_experiment.py"):
            shutil.copyfile(ROOT / "examples" / name, destination / "examples" / name)
        shutil.copytree(ROOT / "examples/policy-inputs", destination / "examples/policy-inputs")
        probe = subprocess.run(
            [sys.executable, "-c", "import langfuse_release_common as m; print(m.__file__)"],
            cwd=destination, env=env, check=True, capture_output=True, text=True)
        imported = Path(probe.stdout.strip()).resolve()
        if imported == (ROOT / "langfuse_release_common.py").resolve():
            raise RuntimeError("Smoke test imported the checkout instead of the installed wheel")
        print("Installed module:", imported, flush=True)
        subprocess.run([sys.executable, "-m", "unittest", "-v", *MODULES],
                       cwd=destination, env=env, check=True)
        suffix = ".exe" if os.name == "nt" else ""
        for command in COMMANDS:
            executable = Path(sysconfig.get_path("scripts")) / ("langfuse-release-" + command + suffix)
            result = subprocess.run([str(executable), "--help"], cwd=destination,
                                    env=env, check=True, capture_output=True, text=True)
            if "usage:" not in result.stdout.lower():
                raise RuntimeError("Missing help output: " + command)
        print(f"Installed wheel: portable tests and all {len(COMMANDS)} CLI entrypoints passed.")


if __name__ == "__main__":
    main()
