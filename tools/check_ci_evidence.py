#!/usr/bin/env python3
"""Verify downloaded synthetic CI evidence against independent workflow inputs."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langfuse_release_bundle import verify_bundle
from langfuse_release_common import Client, ReleaseError
from langfuse_release_provenance import verify_provenance
from langfuse_release_common import fingerprint
from langfuse_release_rollout import build_rollout_report, check_rollout_report
from langfuse_release_trust import trusted_promotion
from promote_langfuse_prompts import _load_stage, promote
from publish_langfuse_prompts import load_snapshot
from test_staged_release import PromotionHTTP
from tools.make_ci_evidence import trusted_ci_policy, synthetic_rollout_fixture

# Independently selected by this checked-out CI harness, never by bundle data.
SYNTHETIC_DESTINATION = {"host": "https://example.com", "project_id": "synthetic-project", "label": "production"}


def synthetic_promotion_client(evidence):
    """Reconstruct the synthetic leaf service from authenticated data only.

    This uses trusted fixture code in the checkout. Nothing from the bundle is
    imported, executed, used as a network destination, or used as credentials.
    """
    _, snapshot = load_snapshot(evidence.manifest_path)
    _, plans = _load_stage(snapshot, evidence.stage_report_path, SYNTHETIC_DESTINATION["label"])
    lock = json.loads(evidence.dependency_lock_path.read_text())
    if any(root["graph"]["dependencies"] or len(root["nodes"]) != 1 for root in lock["roots"]):
        raise AssertionError("This synthetic CI service only implements the reviewed leaf fixture.")
    previous = {key: os.environ.get(key) for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")}
    try:
        os.environ.update(LANGFUSE_PUBLIC_KEY="public-test", LANGFUSE_SECRET_KEY="secret-test")
        client = Client(SYNTHETIC_DESTINATION["host"], timeout=5, project_id=SYNTHETIC_DESTINATION["project_id"])
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    backend = PromotionHTTP([copy.deepcopy(entry["baseline"]) for entry in snapshot["prompts"]])
    for plan in plans:
        original = backend.versions[plan["name"]][plan["baseline"]["version"]]
        original["labels"] = [label for label in original["labels"] if label != "latest"]
        backend.versions[plan["name"]][plan["target_version"]] = {
            **copy.deepcopy(plan["payload"]), "version": plan["target_version"], "labels": ["latest"], "isUserManaged": True}
    # Every transport operation stays inside the strict example.com fixture;
    # gh's separate subprocess still performs genuine signature verification.
    client._opener = backend
    client.get_resolved = lambda name, *, version: {**client.get(name, version=version), "resolutionGraph": None}
    return client, backend, len(plans)


def check_signed_promotion(directory, artifact, attestation, policy_path, trusted_root, *, expected_release_id, wrong_policy_path):
    """Exercise the production promotion gate with real gh verification in CI."""
    directory = Path(directory)
    options = {"expected_host": SYNTHETIC_DESTINATION["host"],
               "expected_project_id": SYNTHETIC_DESTINATION["project_id"],
               "expected_label": SYNTHETIC_DESTINATION["label"], "expected_release_id": expected_release_id,
               "trust_policy_path": policy_path, "attestation_path": attestation, "trusted_root_path": trusted_root}

    def attempt(client, evidence, *, apply):
        return promote(client, evidence.manifest_path, evidence.stage_report_path,
                       label=SYNTHETIC_DESTINATION["label"], project_id=SYNTHETIC_DESTINATION["project_id"],
                       evaluation_report=evidence.evaluation_report_path, dependency_lock=evidence.dependency_lock_path,
                       trusted_evidence=evidence, apply=apply, report_name="promotion.json")

    with trusted_promotion(artifact, **options, materialize_dir=directory / "promotion-signed-preview") as evidence:
        client, backend, expected_patches = synthetic_promotion_client(evidence)
        preview = attempt(client, evidence, apply=False)
        assert preview["status"] == "dry_run_complete"
        assert not backend.patches
    rejected = []
    for label, changes in (("unsigned-promotion", {"attestation_path": None}),
                           ("wrong-signer-promotion", {"trust_policy_path": wrong_policy_path})):
        try:
            with trusted_promotion(artifact, **{**options, **changes}, materialize_dir=directory / label) as evidence:
                attempt(client, evidence, apply=True)
        except ReleaseError:
            if backend.patches:
                raise AssertionError(label + " changed a label before rejecting trust")
            rejected.append(label)
        else:
            raise AssertionError(label + " unexpectedly authorized promotion")
    with trusted_promotion(artifact, **options, materialize_dir=directory / "promotion-signed-apply") as evidence:
        applied = attempt(client, evidence, apply=True)
        assert applied["status"] == "verified"
        assert applied["trust"]["trust"]["classification"] == "verified_ci"
        assert len(backend.patches) == expected_patches
        assert sum(result["status"] == "verified" for result in applied["results"]) == expected_patches
    # An already-promoted retry still needs a new genuine verifier invocation.
    with trusted_promotion(artifact, **options, materialize_dir=directory / "promotion-signed-retry") as evidence:
        retried = attempt(client, evidence, apply=True)
        assert retried["status"] == "verified"
        assert all(result["status"] == "already_promoted" for result in retried["results"])
        assert len(backend.patches) == expected_patches
    return {"preview_status": preview["status"], "apply_status": applied["status"], "retry_status": retried["status"],
            "verified_prompts": expected_patches, "patch_count": len(backend.patches),
            "trust": applied["trust"], "destination": copy.deepcopy(SYNTHETIC_DESTINATION), "rejections": rejected,
            "source": "actual promotion engine against a synthetic HTTP fixture; genuine CI signature verification; no live tenant writes",
            "evaluation_time_semantics": "current promotion evaluation checks; no historical freshness override"}


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
    promotion_summary = check_signed_promotion(directory, artifact, attestation, policy_path, trusted_root,
                                               expected_release_id=os.environ["EXPECTED_RELEASE_ID"],
                                               wrong_policy_path=directory / "wrong-source-commit.json")
    rejected.extend(promotion_summary["rejections"])
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
    print(json.dumps({"portable": offline, "provenance": signed, "promotion": promotion_summary,
                      "rollout": rollout_summary, "rejections": rejected}, indent=2))


if __name__ == "__main__":
    main()
