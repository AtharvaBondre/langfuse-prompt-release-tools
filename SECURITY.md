# Security and private data

This is alpha release tooling. Test it in a disposable Langfuse project before using it for deployment-label changes.

Credentials are read from environment variables. The HTTP client rejects redirects, requires HTTPS except for loopback tests and does not print response bodies in errors. Exported prompts and reports can still contain sensitive content, hostnames and local paths. Keep them outside public repositories; review attachments before sharing.

On POSIX, new exports use 0700 directories and 0600 files. These bits do not establish Windows ACL privacy. Use a private working directory and appropriate filesystem permissions on your operating system. Existing result files supplied by your application remain that application's responsibility.

Receipts are unsigned local attestations. They detect changed content but cannot authenticate a runner or prove that tests ran. The workflow is not an authorization boundary and does not prevent a different authorized API client from moving a label. It does not provide a multi-process lock or atomic remote batch.

For a vulnerability, use the repository's private vulnerability reporting option when available. If it is unavailable, open an issue requesting a private contact without including an exploit, credentials, tenant data or sensitive logs. Public issues are suitable for non-sensitive correctness bugs using synthetic data.
