# Security policy

## Reporting a vulnerability

Please report security issues privately through the repository host's security
advisory feature. Do not include credentials, session cookies, personal data, or
proof-of-concept data from third parties in a public issue.

## Keeping sensitive data out of the repository

- Store API keys, passwords, session cookies, and access tokens in environment
  variables or an external secret manager. Never commit them, even for tests.
- Use synthetic values in fixtures and documentation.
- Keep local media, generated output, browser profiles, databases, logs, and
  private-key files untracked. The root `.gitignore` covers the common cases.
- Review staged changes for secrets and personal data before every push.

If a secret is committed, removing it in a later commit is not sufficient:
revoke or rotate it immediately, then rewrite the affected Git history before
publishing or mirroring the repository again.
