#!/usr/bin/env python3
"""Verify downloaded synthetic CI evidence against independent workflow inputs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langfuse_release_bundle import verify_bundle
from langfuse_release_common import ReleaseError
from langfuse_release_provenance import verify_provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    artifact, attestation = directory / "synthetic-release.zip", directory / "attestation.json"
    policy = {"format_version": 1, "repository": os.environ["EXPECTED_REPOSITORY"],
              "workflow": ".github/workflows/evidence.yml", "source_commit": os.environ["EXPECTED_SOURCE_COMMIT"],
              "source_ref": os.environ["EXPECTED_SOURCE_REF"], "oidc_issuer": "https://token.actions.githubusercontent.com",
              "runner": "github-hosted", "allow_unsigned": False}
    policy_path = directory / "independent-trust-policy.json"
    policy_path.write_text(json.dumps(policy))
    # Fetch the trust root independently with gh's TUF validation, never from
    # the artifact under verification. Subsequent signature checks use it.
    trusted_root = directory / "trusted-root.jsonl"
    trusted_root.write_bytes(subprocess.check_output(["gh", "attestation", "trusted-root"], timeout=60))
    offline = verify_bundle(artifact, expected_release_id=os.environ["EXPECTED_RELEASE_ID"])
    signed = verify_provenance(artifact, policy_path, attestation_path=attestation, trusted_root_path=trusted_root)
    assert signed["classification"] == "verified_ci"
    rejected = []
    for label, change in (("wrong-source-commit", {"source_commit": "0" * 40}),
                          ("wrong-workflow", {"workflow": ".github/workflows/not-this-workflow.yml"})):
        wrong = directory / (label + ".json")
        wrong.write_text(json.dumps({**policy, **change}))
        try:
            verify_provenance(artifact, wrong, attestation_path=attestation, trusted_root_path=trusted_root)
        except ReleaseError:
            rejected.append(label)
        else:
            raise AssertionError(label + " unexpectedly verified")
    changed = directory / "changed-artifact.zip"
    changed.write_bytes(artifact.read_bytes() + b"tampered")
    try:
        verify_provenance(changed, policy_path, attestation_path=attestation, trusted_root_path=trusted_root)
    except ReleaseError:
        rejected.append("tampered-artifact")
    else:
        raise AssertionError("Changed artifact unexpectedly verified")
    try:
        verify_bundle(artifact, expected_release_id="0" * 64)
    except ReleaseError:
        rejected.append("wrong-release")
    else:
        raise AssertionError("Cross-release replay unexpectedly verified")
    print(json.dumps({"portable": offline, "provenance": signed, "rejections": rejected}, indent=2))


if __name__ == "__main__":
    main()
