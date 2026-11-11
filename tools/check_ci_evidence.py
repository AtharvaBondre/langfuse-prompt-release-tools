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
from langfuse_release_common import fingerprint
from langfuse_release_rollout import build_rollout_report, check_rollout_report
from make_ci_evidence import trusted_ci_policy, synthetic_rollout_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    artifact, attestation = directory / "synthetic-release.zip", directory / "attestation.json"
    policy = trusted_ci_policy()
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
    # Reconstruct reviewed expectations from this checkout and CI context. The
    # transferred policy/window files are archival copies, not trust inputs.
    fixture, samples = synthetic_rollout_fixture(policy)
    rollout_artifact = directory / "synthetic-rollout-observations.json"
    rollout_attestation = directory / "rollout-attestation.json"
    provenance_request = {"artifact_path": rollout_artifact, "trust_policy_path": policy_path,
                          "attestation_path": rollout_attestation, "trusted_root_path": trusted_root}
    try:
        rollout_report = build_rollout_report(samples, fixture.window, fixture.policy, fixture.promotion, fixture.lock,
                                             now=fixture.finish, provenance=provenance_request)
        check_rollout_report(rollout_report, fixture.window, fixture.policy, fixture.promotion, fixture.lock,
                             now=fixture.finish, provenance=provenance_request)
        assert rollout_report["status"] == "complete"
        assert rollout_report["provenance"]["classification"] == "verified_ci"
        changed_rollout = directory / "changed-rollout-observations.json"
        # Same parsed samples with different original bytes: only real signature
        # verification can distinguish this substitution from the attested file.
        changed_rollout.write_bytes(rollout_artifact.read_bytes() + b"\n")
        try:
            build_rollout_report(samples, fixture.window, fixture.policy, fixture.promotion, fixture.lock,
                                 now=fixture.finish, provenance={**provenance_request, "artifact_path": changed_rollout})
        except ReleaseError:
            rejected.append("tampered-rollout-artifact")
        else:
            raise AssertionError("Changed rollout artifact unexpectedly authenticated")
        wrong_window = fixture.pin(window_id=fixture.window["window_id"] + "-other")
        try:
            build_rollout_report(samples, wrong_window, fixture.policy, fixture.promotion, fixture.lock,
                                 now=fixture.finish, provenance=provenance_request)
        except ReleaseError:
            rejected.append("wrong-rollout-window")
        else:
            raise AssertionError("Another rollout window accepted the signed observations")
        try:
            build_rollout_report(samples, fixture.window, fixture.policy, fixture.promotion, fixture.lock,
                                 now=fixture.finish, provenance={**provenance_request, "trust_policy_path": directory / "wrong-source-commit.json"})
        except ReleaseError:
            rejected.append("rollout-trust-policy-substitution")
        else:
            raise AssertionError("Rollout accepted a substituted independent trust policy")
        rollout_summary = {"status": rollout_report["status"], "provenance": rollout_report["provenance"],
                           "counts": rollout_report["counts"], "window_sha256": fingerprint(fixture.window),
                           "source": "synthetic normalized SDK observation fixtures; not live application traffic",
                           "verification_time": fixture.finish.isoformat(),
                           "time_semantics": "fixed historical fixture time; not current rollout freshness"}
    finally:
        fixture.doCleanups()
    print(json.dumps({"portable": offline, "provenance": signed, "rollout": rollout_summary, "rejections": rejected}, indent=2))


if __name__ == "__main__":
    main()
