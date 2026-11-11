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
from test_release_rollout import RolloutTests
from langfuse_release_common import fingerprint
from langfuse_release_provenance import validate_trust_policy


def trusted_ci_policy():
    """Expectations come from the GitHub workflow context, never an artifact."""
    return validate_trust_policy({
        "format_version": 1, "repository": os.environ["EXPECTED_REPOSITORY"],
        "workflow": ".github/workflows/evidence.yml", "source_commit": os.environ["EXPECTED_SOURCE_COMMIT"],
        "source_ref": os.environ["EXPECTED_SOURCE_REF"], "oidc_issuer": "https://token.actions.githubusercontent.com",
        "runner": "github-hosted", "allow_unsigned": False,
    })


def synthetic_rollout_fixture(trust):
    """Reproducible historical fixture, not application traffic or current adoption."""
    run_id, attempt = os.environ["EXPECTED_RUN_ID"], os.environ["EXPECTED_RUN_ATTEMPT"]
    if not run_id.isdecimal() or not attempt.isdecimal() or int(attempt) < 1:
        raise ValueError("Expected a numeric GitHub run ID and positive attempt from workflow context.")
    fixture = RolloutTests()
    fixture.setUp()
    fixture.policy["provenance"] = {"required": True, "trust_policy_sha256": fingerprint(trust)}
    fixture.window = fixture.pin(window_id=f"synthetic-ci-rollout/{run_id}/{attempt}")
    samples = fixture.samples()
    ids = {(sample["observation"]["consumer_id"], sample["observation"]["returned"]["name"],
            sample["observation"]["request_id"]) for sample in samples}
    if len(ids) != len(samples):
        raise AssertionError("Synthetic CI source unexpectedly duplicated a request event.")
    return fixture, samples


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
    # The rollout trust policy is generated from CI context after the isolated
    # HTTP fixture restores the real environment. The verifier recreates these
    # expectations independently on the second runner.
    trust = trusted_ci_policy()
    rollout_fixture, samples = synthetic_rollout_fixture(trust)
    try:
        artifacts = {
            "observations": {"format_version": 1, "window_sha256": fingerprint(rollout_fixture.window), "observations": samples},
            "policy": rollout_fixture.policy, "window": rollout_fixture.window,
            "promotion": rollout_fixture.promotion, "dependency-lock": rollout_fixture.lock,
            "description": {"source": "synthetic normalized SDK observation fixtures; no application requests executed",
                            "verification_time": rollout_fixture.finish.isoformat(),
                            "time_semantics": "fixed historical fixture time; not current rollout freshness",
                            "expected_request_events": len(samples), "window_sha256": fingerprint(rollout_fixture.window)},
        }
        for name, value in artifacts.items():
            (output / f"synthetic-rollout-{name}.json").write_text(json.dumps(value, indent=2) + "\n")
    finally:
        rollout_fixture.doCleanups()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("release_id=" + result["release_id"] + "\n")
            stream.write("build_attempt=" + os.environ["EXPECTED_RUN_ATTEMPT"] + "\n")
    print("Synthetic release and historical rollout fixtures saved; portable verification passed; no live/model/application traffic claimed.")


if __name__ == "__main__":
    main()
