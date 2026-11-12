# Agent skill moved

The maintained skill is [skills/langfuse-prompt-release/SKILL.md](skills/langfuse-prompt-release/SKILL.md). Install that folder when using a client that supports Agent Skills. The root file remains a pointer for readers of the original Gist.

## 0.8 release boundary

Select the destination project independently and pass `--project-id` to pull, publish, promote and reconcile. Format 1 snapshots require a new export before remote operations. Promotion defaults to trusted CI: use the original signed bundle and external host/project/label/release ID/signer expectations. Only select `--allow-unsigned-local` for an explicitly chosen local workflow; it does not bypass evaluation or dependency checks. Read [migration](docs/migration-v08.md) and [trusted promotion](docs/trusted-promotion.md). Keep `.release.lock` in place and use read-only reconciliation after an uncertain write.
