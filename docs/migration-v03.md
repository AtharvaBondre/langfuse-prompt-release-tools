# Migrating from the 0.3 Gist

Version 0.4.0a1 changes two defaults to make the staged, evaluated path the ordinary path.

| Operation | 0.3 default | 0.4.0a1 default |
|---|---|---|
| Publish | Direct creation with baseline deployment labels | Stage with no deployment labels; `latest` still advances |
| Promote | Structural checks without evaluation | Require a passing bound evaluation receipt, even for preview |

## CLI

Existing commands that specify `--stage` and `--require-evaluation` continue to work. Both flags are now redundant but supported.

Direct release requires `--allow-direct-release`; this creates a version carrying all baseline non-`latest` labels. It bypasses the separate promotion/evaluation workflow. Do not add this flag merely to make an old command succeed.

Structural-only promotion requires `--allow-structural-only`. A supplied evaluation report is still validated, including failures. `--stage` conflicts with `--allow-direct-release`; `--require-evaluation` conflicts with `--allow-structural-only`.

Both opt-outs are recorded in their reports. Choose them only when that behavior is intended for the selected target and release.

## Python API

```python
# Ordinary path
release(client, manifest, apply=True)  # stage=True
promote(client, manifest, stage_report, label="production",
        apply=True, evaluation_report=receipt)  # require_evaluation=True

# Explicit compatibility paths
release(client, manifest, apply=True,
        stage=False, allow_direct_release=True)
promote(client, manifest, stage_report, label="production", apply=True,
        require_evaluation=False, allow_structural_only=True)
```

Both halves of each Python opt-out are required; inconsistent flags and string/integer substitutes for booleans are rejected. Low-level `Client` methods remain API transport methods, not guarded release workflows.

Stage reports remain format 2 and evaluation receipts remain format 1. Existing evidence still needs its original unchanged snapshot, stage report and source results. New policy fields are additive. An older report does not acquire a new validation history because this version can read it.

New exports and receipts have private POSIX permissions. No migration changes permissions on existing source result files or provides Windows ACL enforcement.
