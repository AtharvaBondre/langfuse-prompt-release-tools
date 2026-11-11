#!/usr/bin/env python3
"""Create only synthetic public CI evidence; accepts no user artifact inputs."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse_release_bundle import create_bundle, verify_bundle
from test_experiment_policy import ExperimentPolicyTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--synthetic-public-demo", required=True, action="store_true")
    args = parser.parse_args()
    output = Path(args.out_dir).resolve()
    output.mkdir(mode=0o700, exist_ok=False)
    fixture = ExperimentPolicyTests()
    fixture.setUp()
    try:
        values = fixture.imported()
        result = create_bundle(values[2], values[3], values[-1], output / "synthetic-release.zip",
                               dependency_lock_path=values[9])
    finally:
        fixture.doCleanups()
    # The original absolute paths are now gone. Verification must use the
    # preserved bundle bytes only and retain its unsigned trust classification.
    verified = verify_bundle(output / "synthetic-release.zip", expected_release_id=result["release_id"])
    (output / "inventory.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "identity.json").write_text(json.dumps({"release_id": result["release_id"],
        "source": "synthetic offline HTTP fixture; no live experiment or model executed",
        "unsigned_verification": verified}, indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("release_id=" + result["release_id"] + "\n")
    print("Synthetic evidence inventory saved; original source directory removed; portable verification passed.")


if __name__ == "__main__":
    main()
