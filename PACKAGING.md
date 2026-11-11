# Install the release commands

Use Python 3.10 or later. Keep the source files in one directory, including `pyproject.toml`. The fifteen release commands use Python's standard library; the package has no required runtime dependencies.

## Build and install a wheel

Run these commands from the source directory. The build-tool installation needs a package index or a prepared local wheel cache.

```bash
python3 -m venv .venv-build
.venv-build/bin/python -m pip install setuptools==80.9.0 wheel==0.47.0
.venv-build/bin/python -m pip wheel . --no-index --no-deps --no-build-isolation --wheel-dir dist

python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps \
  dist/langfuse_prompt_release_tools-0.6.0a1-py3-none-any.whl
```

Once the build tools are installed, both the wheel build and its installation run without contacting a package index. Windows virtual environments use `Scripts` in place of `bin`.

| Installed command | Source module | Purpose |
|---|---|---|
| `langfuse-release-pull` | `pull_langfuse_prompts.py` | Export an explicit prompt baseline |
| `langfuse-release-publish` | `publish_langfuse_prompts.py` | Preview or create reviewed versions |
| `langfuse-release-verify` | `verify_langfuse_prompts.py` | Check saved versions against the release report |
| `langfuse-release-evaluate` | `evaluate_langfuse_release.py` | Validate the supplied evaluation evidence |
| `langfuse-release-promote` | `promote_langfuse_prompts.py` | Preview or apply an explicit label promotion |
| `langfuse-release-policy` | `langfuse_release_policy.py` | Pin reviewed cases and thresholds before staging |
| `langfuse-release-import-experiment` | `import_langfuse_experiment.py` | Import typed official experiment results and execution capture |
| `langfuse-release-dependencies` | `langfuse_release_dependencies.py` | Capture/check the server-resolved dependency closure |
| `langfuse-release-reconcile` | `reconcile_langfuse_release.py` | Inspect interrupted outcomes without writes |
| `langfuse-release-adoption` | `langfuse_release_adoption.py` | Validate actual SDK consumer observations |
| `langfuse-release-bundle` | `langfuse_release_bundle.py` | Package or verify portable original evidence |
| `langfuse-release-provenance` | `langfuse_release_provenance.py` | Verify independently pinned CI signer identity |
| `langfuse-release-remote` | `langfuse_release_remote.py` | Verify persisted experiments and typed scores with GETs |
| `langfuse-release-rollout` | `langfuse_release_rollout.py` | Pin, aggregate and check rollout observation windows |
| `langfuse-release-capture` | `langfuse_release_capture.py` | Allowlisted JSON sidecar used by the JavaScript helper |

Inspect the arguments before a run:

```bash
.venv/bin/langfuse-release-pull --help
.venv/bin/langfuse-release-publish --help
.venv/bin/langfuse-release-verify --help
.venv/bin/langfuse-release-evaluate --help
.venv/bin/langfuse-release-promote --help
```

The installed commands call the same `main()` functions as the source scripts. The packaging step does not change request semantics, credentials, approval boundaries or release behavior. Follow `README.md` for the full workflow and evidence requirements.

## Source archive

To create a source distribution with the same prepared build environment:

```bash
.venv-build/bin/python -c 'from setuptools.build_meta import build_sdist; build_sdist("dist")'
```

`MANIFEST.in` includes the workflow documents, portable tests, schema-review fixtures and disposable-demo files. These files are available when you unpack the source archive. The wheel installs only the sixteen runtime modules, fifteen commands and package metadata, including license notices. It does not install the demo, tests or schema checker. The release tools are MIT-licensed; `THIRD-PARTY-NOTICES.md` preserves the notice for the derived Compose configuration.

## Run the source tests

Run the portable tests from the source directory:

```bash
python3 -m unittest discover -v -p 'test_*.py'
```

The test fixtures simulate HTTP in memory and block real network access. They require neither Langfuse credentials nor the optional schema-check packages. These tests do not establish compatibility with a running Langfuse deployment; record local integration results separately.

## Optional API-schema review

The schema checker remains a source-tree development tool. Keep `validate_api_examples.py` and `api-examples.json` together and install the optional dependencies:

```bash
.venv/bin/python -m pip install -r requirements-review.txt
.venv/bin/python validate_api_examples.py --report api-check.json
```

You can also install the same dependency set with `pip install '.[review]'` from the source directory. The wheel's `[review]` extra provides dependencies; it does not bundle the checker or its JSON fixture file.

For an offline check, add `--schema openapi.yml` using the pinned official artifact. The checker verifies its SHA-256 before validating examples. `API-NOTES.md` describes the schema/runtime differences and the meaning of the negative controls.

## Check the installed artifact

After installing the wheel into the test environment, run:

```bash
.venv/bin/python tools/smoke_installed.py
```

This copies the portable tests to a temporary directory, verifies that imports come from the installed package, runs those tests and checks all fifteen installed entrypoints. It removes inherited Langfuse environment settings from child processes and makes no tenant calls. The source archive includes the skill and guides; the wheel remains limited to runtime modules and metadata.
