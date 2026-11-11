#!/usr/bin/env python3
"""Verify optional CI provenance with the official GitHub CLI and external policy."""
import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from langfuse_release_common import ReleaseError, fingerprint
from langfuse_release_policy import object_fields, read_object, timestamp

_PREDICATE = "https://slsa.dev/provenance/v1"


def validate_trust_policy(value):
    object_fields(value, ("format_version", "repository", "workflow", "source_commit", "source_ref", "oidc_issuer", "runner", "allow_unsigned"), where="provenance trust policy")
    if type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ReleaseError("Unsupported provenance trust-policy version.")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ReleaseError("Trust policy must pin one GitHub owner/repository.")
    if (not isinstance(value["workflow"], str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", value["workflow"])):
        raise ReleaseError("Trust policy must pin one workflow path.")
    if not isinstance(value["source_commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", value["source_commit"]):
        raise ReleaseError("Trust policy must pin a full lowercase source commit SHA.")
    ref = value["source_ref"]
    if (not isinstance(ref, str) or not re.fullmatch(r"refs/(heads|tags|pull)/[A-Za-z0-9_./-]+", ref) or
            ".." in ref or "//" in ref or ref.endswith(("/", "."))):
        raise ReleaseError("Trust policy must pin an explicit source ref.")
    if value["oidc_issuer"] != "https://token.actions.githubusercontent.com" or value["runner"] != "github-hosted":
        raise ReleaseError("Policy v1 supports the GitHub Actions issuer and GitHub-hosted runners only.")
    if type(value["allow_unsigned"]) is not bool:
        raise ReleaseError("allow_unsigned must be a boolean selected outside the artifact.")
    return copy.deepcopy(value)


def _read_bytes(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file():
        raise ReleaseError("Provenance inputs must be regular files without symlink ancestors.")
    return path.read_bytes()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _parse_output(raw):
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ReleaseError("Verifier output contains duplicate JSON keys.")
            value[key] = child
        return value
    try:
        parsed = json.loads(raw, object_pairs_hook=pairs)
    except ValueError as exc:
        raise ReleaseError("Official verifier did not return valid JSON.") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ReleaseError("Official verifier returned no verified attestations.")
    fingerprint(parsed)
    return parsed


def _verified_identity(results, policy, artifact_hash):
    """Use signed certificate claims, never workflow-controlled predicate fields."""
    repo_url = "https://github.com/" + policy["repository"]
    san = f"{repo_url}/{policy['workflow']}@{policy['source_ref']}"
    expected = {"subjectAlternativeName": san, "issuer": policy["oidc_issuer"],
                "sourceRepositoryURI": repo_url, "sourceRepositoryDigest": policy["source_commit"],
                "sourceRepositoryRef": policy["source_ref"], "runnerEnvironment": "github-hosted",
                "buildSignerURI": san, "buildSignerDigest": policy["source_commit"]}
    # gh verifies every returned attestation against the same pinned flags.
    # Refuse mixed/ambiguous output rather than selecting convenient claims.
    timestamps = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("verificationResult"), dict):
            raise ReleaseError("Official verifier result lacks cryptographic verification details.")
        result = item["verificationResult"]
        signature = result.get("signature")
        certificate = signature.get("certificate") if isinstance(signature, dict) else None
        if not isinstance(certificate, dict):
            raise ReleaseError("Verified provenance lacks signed certificate identity.")
        for field, value in expected.items():
            if certificate.get(field) != value:
                raise ReleaseError(f"Verified certificate {field} does not match independent trust policy.")
        statement = result.get("statement")
        if (not isinstance(statement, dict) or statement.get("predicateType") != _PREDICATE or
                statement.get("_type") != "https://in-toto.io/Statement/v1"):
            raise ReleaseError("Verified statement has an unsupported predicate type.")
        subjects = statement.get("subject")
        if not isinstance(subjects, list) or not any(
                isinstance(subject, dict) and isinstance(subject.get("digest"), dict) and subject["digest"].get("sha256") == artifact_hash for subject in subjects):
            raise ReleaseError("Verified statement does not bind this artifact digest.")
        observed = result.get("verifiedTimestamps")
        if not isinstance(observed, list) or not observed:
            raise ReleaseError("Verified provenance lacks a trusted timestamp.")
        for observed_time in observed:
            if not isinstance(observed_time, dict) or not all(isinstance(observed_time.get(field), str) and observed_time[field] for field in ("type", "uri", "timestamp")):
                raise ReleaseError("Verified timestamp is malformed.")
            timestamp(observed_time["timestamp"], "verified signing timestamp")
        timestamps.extend(copy.deepcopy(observed))
    return {"repository": policy["repository"], "workflow": policy["workflow"], "source_commit": policy["source_commit"],
            "source_ref": policy["source_ref"], "oidc_issuer": policy["oidc_issuer"], "runner": policy["runner"],
            "certificate_subject": san, "verified_timestamps": timestamps}


def verify_provenance(artifact_path, trust_policy_path, *, attestation_path=None, trusted_root_path=None, timeout=60):
    """Run gh verification; saved output JSON is never accepted as authentication.

    An absent attestation is unsigned, not an instruction to query a public
    endpoint. A present invalid attestation never falls back to unsigned mode.
    """
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ReleaseError("Verifier timeout must be an integer from 1 to 60 seconds.")
    policy_path, policy_data = read_object(trust_policy_path, "provenance trust policy")
    policy = validate_trust_policy(policy_data)
    artifact_path = Path(artifact_path)
    artifact = _read_bytes(artifact_path)
    artifact_path = artifact_path.resolve()
    artifact_hash = _sha(artifact)
    if artifact_path.samefile(policy_path):
        raise ReleaseError("Artifact cannot choose its own trust policy.")
    base = {"format_version": 1, "mode": "provenance_verification", "artifact_sha256": artifact_hash,
            "trust_policy_sha256": fingerprint(policy), "identity": None}
    if attestation_path is None:
        if trusted_root_path is not None:
            raise ReleaseError("A trusted root alone is not an attestation.")
        if not policy["allow_unsigned"]:
            raise ReleaseError("Independent trust policy requires signed CI provenance.")
        return {**base, "classification": "unsigned_local", "verifier": None}
    attestation_path = Path(attestation_path)
    attestation = _read_bytes(attestation_path)
    attestation_path = attestation_path.resolve()
    if any(attestation_path.samefile(path) for path in (artifact_path, policy_path)):
        raise ReleaseError("Artifact, trust policy and attestation must be distinct files.")
    executable = shutil.which("gh")
    if executable is None:
        raise ReleaseError("Install the official GitHub CLI to verify CI provenance.")
    repo_url = "https://github.com/" + policy["repository"]
    san = f"{repo_url}/{policy['workflow']}@{policy['source_ref']}"
    # gh makes --cert-identity and --signer-workflow mutually exclusive. The
    # exact certificate SAN below already pins the repository/workflow/ref.
    command = [executable, "attestation", "verify", str(artifact_path), "--bundle", str(attestation_path),
               "--repo", policy["repository"],
               "--cert-identity", san, "--cert-oidc-issuer", policy["oidc_issuer"],
               "--source-digest", policy["source_commit"], "--source-ref", policy["source_ref"],
               "--signer-digest", policy["source_commit"], "--deny-self-hosted-runners",
               "--predicate-type", _PREDICATE, "--format", "json"]
    trusted_root = None
    if trusted_root_path is not None:
        trusted_root_path = Path(trusted_root_path)
        trusted_root = _read_bytes(trusted_root_path)
        trusted_root_path = trusted_root_path.resolve()
        if any(trusted_root_path.samefile(path) for path in (artifact_path, policy_path, attestation_path)):
            raise ReleaseError("Trusted root must be supplied as a separate administrator-controlled file.")
        command.extend(["--custom-trusted-root", str(trusted_root_path)])
    try:
        completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError("Official provenance verification timed out; no trust classification was granted.") from exc
    except OSError as exc:
        raise ReleaseError("Could not execute the official GitHub verifier.") from exc
    if completed.returncode != 0:
        # Do not echo subprocess output: it may include private artifact metadata.
        raise ReleaseError("Official GitHub attestation verification rejected this artifact or signer.")
    identity = _verified_identity(_parse_output(completed.stdout), policy, artifact_hash)
    _, current_policy = read_object(policy_path, "provenance trust policy")
    if (_read_bytes(artifact_path) != artifact or _read_bytes(attestation_path) != attestation or
            fingerprint(current_policy) != fingerprint(policy) or
            (trusted_root_path is not None and _read_bytes(trusted_root_path) != trusted_root)):
        raise ReleaseError("Provenance inputs changed during verification.")
    return {**base, "classification": "verified_ci", "identity": identity, "verifier": "gh attestation verify",
            "attestation_sha256": _sha(attestation), "trusted_root_sha256": _sha(trusted_root) if trusted_root is not None else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--trust-policy", required=True)
    parser.add_argument("--attestation")
    parser.add_argument("--trusted-root")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_provenance(args.artifact, args.trust_policy, attestation_path=args.attestation,
                                          trusted_root_path=args.trusted_root, timeout=args.timeout), indent=2))
    except (ReleaseError, OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Provenance stopped: {exc}\n")


if __name__ == "__main__":
    main()
