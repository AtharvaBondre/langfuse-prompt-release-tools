# Upgrade to 0.7

Version **0.7.0a1** adds two local commands. Existing policy, capture and release report formats remain unchanged. Upgrading does not pin a policy, move a label or migrate any saved evidence.

## Build a policy without copying hashes

Use `langfuse-release-policy-draft` with selected case data and an explicit requirements spec. It computes the same fingerprints that the existing producer examples use. Review the generated JSON before running the existing `langfuse-release-policy` pin command.

The [policy setup guide](policy-setup.md) includes example inputs. Choose evaluator names, score types, thresholds and source boundaries yourself; the helper does not select them from candidate results. Existing hand-built policies remain supported.

For hosted dataset verification, select format 2 and an actual timezone-aware dataset version. If pinning a hosted schema, supply both `inputSchema` and `expectedOutputSchema` in the documented wrapper. A local case-array fingerprint is not a hosted dataset version.

## Read a release folder

```bash
langfuse-release-status --workspace ./release-review
langfuse-release-status --workspace ./release-review --format json
```

The inspector reads local reports and their references. Keep the related evidence inside that release folder. If several attempts exist, select the intended reports explicitly rather than relying on modification times. See [release status](release-status.md) for each selector and the validation scope.

Exit `0` means inspection completed; failed or missing release checks can still appear in the report. Exit `1` means an invalid, ambiguous or unreadable input, and `2` means invalid CLI arguments. This command is not a promotion gate or a substitute for the existing verification commands.

The inspector never contacts Langfuse, invokes a signer verifier, or modifies release files. Historical promotion is not a fresh check of a label, and recorded observations do not establish that an entire application fleet adopted the release.

## Packages and compatibility

The Python package now installs seventeen commands. Both new commands use Python's standard library and support the existing Python 3.10+ minimum. The JavaScript capture behavior and pinned Langfuse SDK dependencies are unchanged; its package version follows this release as `0.7.0-alpha.1`.

The [validation record](validation.md) gives dated test and compatibility results. The existing-application pilot remains deferred until a suitable repository and test environment are available.
